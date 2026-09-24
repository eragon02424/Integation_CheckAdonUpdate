"""DataUpdateCoordinator fuer Addon Update Checker."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from datetime import timedelta
from typing import Any

import aiohttp
import yaml
from awesomeversion import AwesomeVersion
from homeassistant.components.persistent_notification import (
    async_create as pn_create,
    async_dismiss as pn_dismiss,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AUTO_BUMP,
    CONF_GITHUB_TOKEN,
    CONF_GITHUB_USERNAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_AUTO_BUMP,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    GITHUB_API_BASE,
    GITHUB_RAW_BASE,
    PYPI_API_BASE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# GitHub Release Links
PATTERN_FIXED = re.compile(
    r'https://github\.com/([\w.-]+)/([\w.-]+)/releases/download/v?([\d][\d\.]*)/'
)
PATTERN_DYNAMIC_API = re.compile(
    r'https://api\.github\.com/repos/([\w.-]+)/([\w.-]+)/releases/latest'
)
PATTERN_DYNAMIC_VAR = re.compile(
    r'https://github\.com/([\w.-]+)/([\w.-]+)/releases/download/\$\{?\w+\}?/'
)

# PyPI: pip install --no-cache-dir paketname oder pip install paketname==1.2.3
PATTERN_PYPI = re.compile(
    r'pip(?:3)? install\s+((?:--[\w-]+\s+)*)([^\s&|\\]+)'
)
PYPI_IGNORE = {
    "pip", "setuptools", "wheel", "no-cache-dir", "break-system-packages",
    "upgrade", "r", "q", "quiet", "user",
    "aiohttp", "requests", "urllib3", "httpx",
    "certifi", "charset-normalizer", "idna", "pyyaml",
}

# Docker Hub FROM image:tag
# Erkennt: FROM owner/image:tag oder FROM registry/owner/image:tag
# Ignoriert: FROM $BUILD_FROM, FROM alpine:3.x, FROM python:x.x-alpine (Standard-Basis-Images)
PATTERN_DOCKER_FROM = re.compile(
    r'^FROM\s+(?!\$)([\w.-]+(?:/[\w.-]+)+)(?::([\w.-]+))?\s*$',
    re.MULTILINE
)
# Standard Basis-Images die wir ignorieren
DOCKER_BASE_IGNORE = {
    "alpine", "python", "node", "ubuntu", "debian", "golang", "rust",
    "nginx", "redis", "postgres", "mysql", "mongo", "scratch", "busybox",
}

# Auto-Bump: version-Zeile in config.yaml (nur x.y.z)
PATTERN_CFG_VERSION = re.compile(
    r'^(version:\s*["\']?)(\d+)\.(\d+)\.(\d+)(["\']?\s*)$', re.MULTILINE
)
# Cache-Buster: Supervisor uebergibt beim Build --build-arg BUILD_VERSION=<version>.
# Ist ARG BUILD_VERSION im Dockerfile deklariert, invalidiert eine neue Version den
# Docker-Build-Cache aller folgenden RUN-Schritte (curl/pip holen dann wirklich neu).
CACHE_BUSTER = "ARG BUILD_VERSION"


class AddonUpdateCoordinator(DataUpdateCoordinator):
    """Koordiniert alle GitHub Scans und Versionsvergleiche."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.github_username = entry.data[CONF_GITHUB_USERNAME]
        self.github_token = str(entry.options.get(
            CONF_GITHUB_TOKEN, entry.data.get(CONF_GITHUB_TOKEN, "")
        )).strip()
        scan_minutes = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self.auto_bump = bool(entry.options.get(CONF_AUTO_BUMP, DEFAULT_AUTO_BUMP))
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stored: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}  # slug -> {"target": ver, "name": ...}
        self._store_loaded = False
        self.session = async_get_clientsession(hass)
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(minutes=scan_minutes),
        )
        auth_info = "mit Token" if self.github_token else "OHNE Token (Rate Limit: 60/h)"
        _LOGGER.debug("[AUC] Coordinator init: user=%s, intervall=%d min, auth=%s, auto_bump=%s",
                      self.github_username, scan_minutes, auth_info, self.auto_bump)

    # ------------------------------------------------------------------ Storage
    async def _load_store(self) -> None:
        data = await self._store.async_load()
        if data:
            self._stored = data.get("versions", {})
            self._pending = data.get("pending_installs", {})
            _LOGGER.debug("[AUC] Storage geladen: %d Eintraege, %d offene Installationen",
                          len(self._stored), len(self._pending))
        else:
            _LOGGER.debug("[AUC] Kein Storage vorhanden, starte frisch")
        self._store_loaded = True

    async def _save_store(self) -> None:
        await self._store.async_save(
            {"versions": self._stored, "pending_installs": self._pending}
        )
        _LOGGER.debug("[AUC] Storage gespeichert: %d Eintraege", len(self._stored))

    # ------------------------------------------------------------------ HTTP
    def _github_headers(self) -> dict:
        headers = {"User-Agent": "HA-AddonUpdateChecker/1.1",
                   "Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    async def _gh_json(self, url: str) -> Any:
        try:
            async with self.session.get(
                url, headers=self._github_headers(), timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 403:
                    _LOGGER.warning("[AUC] GitHub Rate Limit bei %s", url)
                elif resp.status == 401:
                    _LOGGER.error("[AUC] GitHub Token ungueltig!")
                elif resp.status == 404:
                    _LOGGER.debug("[AUC] 404: %s", url)
                else:
                    _LOGGER.warning("[AUC] HTTP %d bei %s", resp.status, url)
        except asyncio.TimeoutError:
            _LOGGER.warning("[AUC] Timeout: %s", url)
        except aiohttp.ClientError as e:
            _LOGGER.warning("[AUC] Verbindungsfehler %s: %s", url, e)
        return None

    async def _gh_put(self, url: str, payload: dict) -> tuple[int, Any]:
        try:
            async with self.session.put(
                url, headers=self._github_headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    body = await resp.text()
                return resp.status, body
        except Exception as e:
            return 0, str(e)

    async def _gh_text(self, url: str) -> str | None:
        try:
            async with self.session.get(
                url, headers={"User-Agent": "HA-AddonUpdateChecker/1.1"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
                _LOGGER.debug("[AUC] HTTP %d bei RAW %s", resp.status, url)
        except Exception as e:
            _LOGGER.warning("[AUC] Fehler bei %s: %s", url, e)
        return None

    async def _api_json(self, url: str) -> Any:
        """Generischer JSON GET ohne Auth."""
        try:
            async with self.session.get(
                url, headers={"User-Agent": "HA-AddonUpdateChecker/1.1"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                _LOGGER.debug("[AUC] HTTP %d bei %s", resp.status, url)
        except Exception as e:
            _LOGGER.warning("[AUC] Fehler bei %s: %s", url, e)
        return None

    # ------------------------------------------------------------------ Supervisor
    def _supervisor_base(self) -> tuple[str, dict] | None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        host = os.environ.get("SUPERVISOR")
        if not token or not host:
            return None
        return f"http://{host}", {"Authorization": f"Bearer {token}"}

    async def _get_installed_addons(self) -> dict[str, dict] | None:
        """Liefert {config-slug: {"full_slug", "version"}} der installierten Add-ons."""
        base = self._supervisor_base()
        if not base:
            return None
        url, headers = base
        try:
            async with self.session.get(
                f"{url}/addons", headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("[AUC] Supervisor /addons HTTP %d", resp.status)
                    return None
                data = await resp.json()
        except Exception as e:
            _LOGGER.warning("[AUC] Supervisor nicht erreichbar: %s", e)
            return None
        result: dict[str, dict] = {}
        for addon in data.get("data", {}).get("addons", []):
            full = addon.get("slug", "")
            short = full.split("_", 1)[1] if "_" in full else full
            result[short] = {"full_slug": full, "version": str(addon.get("version", ""))}
        return result

    async def _supervisor_store_reload(self) -> None:
        base = self._supervisor_base()
        if not base:
            return
        url, headers = base
        try:
            async with self.session.post(
                f"{url}/store/reload", headers=headers,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                _LOGGER.info("[AUC] Supervisor store/reload -> HTTP %d", resp.status)
        except Exception as e:
            _LOGGER.warning("[AUC] store/reload fehlgeschlagen: %s", e)

    # ------------------------------------------------------------------ GitHub Scan
    async def _get_repos(self) -> list[dict]:
        _LOGGER.debug("[AUC] Lade Repos von: %s", self.github_username)
        repos, page = [], 1
        while True:
            url = f"{GITHUB_API_BASE}/users/{self.github_username}/repos?per_page=100&page={page}"
            data = await self._gh_json(url)
            if not data:
                break
            repos.extend(data)
            _LOGGER.debug("[AUC] Seite %d: %d Repos", page, len(data))
            if len(data) < 100:
                break
            page += 1
        _LOGGER.debug("[AUC] Gesamt %d Repos", len(repos))
        return repos

    async def _find_dockerfiles(self, repo: str, branch: str) -> list[str]:
        url = f"{GITHUB_API_BASE}/repos/{self.github_username}/{repo}/git/trees/{branch}?recursive=1"
        data = await self._gh_json(url)
        if not data:
            return []
        paths = [
            item["path"] for item in data.get("tree", [])
            if item.get("type") == "blob" and item["path"].endswith("Dockerfile")
        ]
        if paths:
            _LOGGER.debug("[AUC] Dockerfiles in %s: %s", repo, paths)
        return paths

    async def _read_raw(self, repo: str, branch: str, path: str) -> str | None:
        url = f"{GITHUB_RAW_BASE}/{self.github_username}/{repo}/{branch}/{path}"
        return await self._gh_text(url)

    async def _get_contents(self, repo: str, branch: str, path: str) -> tuple[str, str] | None:
        """Datei ueber Contents-API (ohne CDN-Cache). Liefert (text, sha)."""
        url = f"{GITHUB_API_BASE}/repos/{self.github_username}/{repo}/contents/{path}?ref={branch}"
        data = await self._gh_json(url)
        if not data or "content" not in data:
            return None
        text = base64.b64decode(data["content"]).decode("utf-8")
        return text, data["sha"]

    def _parse_dockerfile(self, content: str, repo: str, path: str) -> list[dict]:
        """Externe Abhaengigkeiten aus Dockerfile extrahieren (GitHub + PyPI + Docker Hub)."""
        results = []
        seen = set()

        # GitHub Release Links
        for pattern in [PATTERN_FIXED, PATTERN_DYNAMIC_API, PATTERN_DYNAMIC_VAR]:
            for m in pattern.finditer(content):
                gh_owner, gh_repo = m.group(1), m.group(2)
                k = f"gh:{gh_owner}/{gh_repo}"
                if k not in seen:
                    seen.add(k)
                    _LOGGER.debug("[AUC] GitHub erkannt in %s/%s: %s/%s", repo, path, gh_owner, gh_repo)
                    results.append({"type": "github", "upstream_owner": gh_owner, "upstream_repo": gh_repo})

        # PyPI pip install
        for m in PATTERN_PYPI.finditer(content):
            pkg = m.group(2).strip().lower().split('==')[0]
            if pkg in PYPI_IGNORE or len(pkg) < 2 or pkg.startswith('-'):
                continue
            k = f"py:{pkg}"
            if k not in seen:
                seen.add(k)
                _LOGGER.debug("[AUC] PyPI erkannt in %s/%s: %s", repo, path, pkg)
                results.append({"type": "pypi", "package": pkg})

        # Docker Hub FROM image
        for m in PATTERN_DOCKER_FROM.finditer(content):
            image_full = m.group(1).strip()
            tag = m.group(2) or "latest"
            parts = image_full.split("/")

            if "." in parts[0] or ":" in parts[0]:
                registry = parts[0]
                dh_owner = parts[1] if len(parts) > 2 else None
                dh_image = parts[2] if len(parts) > 2 else parts[1]
            else:
                registry = "hub.docker.com"
                dh_owner = parts[0] if len(parts) > 1 else "library"
                dh_image = parts[1] if len(parts) > 1 else parts[0]

            base_name = dh_image.split(":")[0].lower()
            if base_name in DOCKER_BASE_IGNORE or dh_owner in DOCKER_BASE_IGNORE:
                _LOGGER.debug("[AUC] Docker Base-Image ignoriert: %s", image_full)
                continue

            k = f"dh:{image_full}"
            if k not in seen:
                seen.add(k)
                _LOGGER.debug("[AUC] Docker Hub erkannt in %s/%s: %s:%s", repo, path, image_full, tag)
                results.append({
                    "type": "dockerhub",
                    "image_full": image_full,
                    "dh_owner": dh_owner,
                    "dh_image": dh_image,
                    "registry": registry,
                    "tag": tag,
                })

        return results

    @staticmethod
    def _config_path(dockerfile_path: str) -> str:
        folder = dockerfile_path.rsplit("/", 1)[0] if "/" in dockerfile_path else ""
        return f"{folder}/config.yaml" if folder else "config.yaml"

    async def _read_config_yaml(self, repo: str, branch: str, dockerfile_path: str) -> dict:
        config_path = self._config_path(dockerfile_path)
        content = None
        if self.github_token:
            got = await self._get_contents(repo, branch, config_path)
            content = got[0] if got else None
        if content is None:
            content = await self._read_raw(repo, branch, config_path)
        if not content:
            return {}
        try:
            data = yaml.safe_load(content)
            return {
                "slug": data.get("slug", ""),
                "addon_version": str(data.get("version", "")),
                "addon_name": data.get("name", data.get("slug", repo)),
            }
        except Exception as e:
            _LOGGER.warning("[AUC] Fehler beim Parsen von config.yaml: %s", e)
            return {}

    async def _get_github_latest(self, owner: str, repo: str) -> str | None:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"
        data = await self._gh_json(url)
        if data:
            tag = data.get("tag_name", "").lstrip("v")
            _LOGGER.debug("[AUC] GitHub %s/%s latest: %s", owner, repo, tag)
            return tag
        return None

    async def _get_pypi_latest(self, package: str) -> str | None:
        url = f"{PYPI_API_BASE}/{package}/json"
        data = await self._api_json(url)
        if data:
            version = data.get("info", {}).get("version", "")
            _LOGGER.debug("[AUC] PyPI %s latest: %s", package, version)
            return version
        return None

    async def _get_dockerhub_digest(self, dh_owner: str, dh_image: str, tag: str, registry: str) -> str | None:
        """Holt last_updated eines Docker Image Tags via Docker Hub API."""
        url = f"https://hub.docker.com/v2/repositories/{dh_owner}/{dh_image}/tags/{tag}"
        data = await self._api_json(url)
        if data:
            images = data.get("images", [])
            if images:
                last_updated = data.get("last_updated", "")
                _LOGGER.debug("[AUC] Docker Hub %s/%s:%s updated=%s",
                              dh_owner, dh_image, tag, last_updated)
                return last_updated
        return None

    # ------------------------------------------------------------------ Notifications
    def _notify(self, notif_id: str, title: str, message: str) -> None:
        pn_create(self.hass, message=message, title=title, notification_id=notif_id)

    def _dismiss(self, notif_id: str) -> None:
        pn_dismiss(self.hass, notification_id=notif_id)

    def _notify_manual(self, notif_id: str, addon_name: str, slug: str, source_label: str,
                       last_upstream: str | None, upstream_latest: str | None,
                       extra: str = "") -> None:
        self._notify(
            notif_id,
            f"\U0001f527 Add-on Update: {addon_name}",
            (f"**{addon_name}** (`{slug}`) verwendet\n"
             f"`{source_label}` in Version **{last_upstream[:16] if last_upstream else '?'}**,\n"
             f"aber eine neuere Version ist verfuegbar (Stand: **{upstream_latest[:16] if upstream_latest else '?'}**).\n\n"
             f"Bitte Dockerfile pruefen und Add-on neu aufbauen.\n"
             f"Diese Meldung verschwindet automatisch nach dem Update.{extra}"),
        )

    # ------------------------------------------------------------------ Vergleich
    def _process_dep(
        self, key: str, notif_id: str, addon_name: str, slug: str,
        source_label: str, addon_version: str, upstream_latest: str | None
    ) -> tuple[str, bool]:
        stored = self._stored.get(key)
        if stored is None:
            _LOGGER.info("[AUC] ERSTER FUND (Baseline): %s | addon=%s upstream=%s",
                         key, addon_version, upstream_latest)
            self._stored[key] = {"addon_version": addon_version, "upstream_version": upstream_latest}
            return "baseline", False

        last_upstream = stored.get("upstream_version", "")
        last_addon = stored.get("addon_version", "")
        addon_changed = addon_version != last_addon
        upstream_changed = upstream_latest and upstream_latest != last_upstream

        if addon_changed:
            _LOGGER.info("[AUC] ADD-ON AKTUALISIERT: %s | addon %s -> %s | upstream %s",
                         addon_name, last_addon, addon_version, upstream_latest)
            self._stored[key] = {"addon_version": addon_version, "upstream_version": upstream_latest}
            self._dismiss(notif_id)
            return "up_to_date", False

        if upstream_changed:
            _LOGGER.warning("[AUC] UPDATE VERFUEGBAR: %s | %s: %s -> %s (addon bleibt %s)",
                            addon_name, source_label, last_upstream, upstream_latest, addon_version)
            if not self.auto_bump:
                self._notify_manual(notif_id, addon_name, slug, source_label,
                                    last_upstream, upstream_latest)
            return "update_available", True

        _LOGGER.debug("[AUC] OK: %s | addon=%s upstream=%s",
                      addon_name, addon_version,
                      upstream_latest[:16] if upstream_latest else None)
        self._dismiss(notif_id)
        return "up_to_date", False

    # ------------------------------------------------------------------ Auto-Bump
    @staticmethod
    def _add_cache_buster(dockerfile: str) -> str:
        """Fuegt nach jeder FROM-Zeile 'ARG BUILD_VERSION' ein, falls noch nicht vorhanden."""
        lines = dockerfile.split("\n")
        out: list[str] = []
        for i, line in enumerate(lines):
            out.append(line)
            if re.match(r"^\s*FROM\s", line, re.IGNORECASE):
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt != CACHE_BUSTER:
                    out.append(CACHE_BUSTER)
        return "\n".join(out)

    async def _bump_addon(self, repo: str, branch: str, df_path: str, slug: str,
                          changes: list[str]) -> tuple[str | None, str]:
        """Erhoeht Patch-Version in config.yaml (+ Cache-Buster im Dockerfile).

        Rueckgabe: (neue_version | None, fehlertext)
        """
        if not self.github_token:
            return None, "kein GitHub Token konfiguriert"

        cfg_path = self._config_path(df_path)
        api = f"{GITHUB_API_BASE}/repos/{self.github_username}/{repo}/contents"

        # 1) Dockerfile: Cache-Buster sicherstellen (eigener Commit, vor dem Bump)
        df = await self._get_contents(repo, branch, df_path)
        if df is None:
            return None, f"{df_path} nicht lesbar"
        df_text, df_sha = df
        new_df = self._add_cache_buster(df_text)
        if new_df != df_text:
            status, body = await self._gh_put(f"{api}/{df_path}", {
                "message": f"{slug}: ARG BUILD_VERSION als Docker-Cache-Buster ergaenzt",
                "content": base64.b64encode(new_df.encode("utf-8")).decode("ascii"),
                "sha": df_sha, "branch": branch,
            })
            if status not in (200, 201):
                return None, f"Dockerfile-Commit HTTP {status}: {str(body)[:200]}"
            _LOGGER.info("[AUC] Cache-Buster in %s/%s ergaenzt", repo, df_path)

        # 2) config.yaml: Patch-Version +1
        cfg = await self._get_contents(repo, branch, cfg_path)
        if cfg is None:
            return None, f"{cfg_path} nicht lesbar"
        cfg_text, cfg_sha = cfg
        m = PATTERN_CFG_VERSION.search(cfg_text)
        if not m:
            return None, "keine version im Format x.y.z in config.yaml"
        old_v = f"{m.group(2)}.{m.group(3)}.{m.group(4)}"
        new_v = f"{m.group(2)}.{m.group(3)}.{int(m.group(4)) + 1}"
        new_cfg = (cfg_text[:m.start()]
                   + f"{m.group(1)}{new_v}{m.group(5)}"
                   + cfg_text[m.end():])
        msg = f"{slug}: Auto-Bump {old_v} -> {new_v}\n\nUpstream-Aenderungen:\n" + \
              "\n".join(f"- {c}" for c in changes)
        status, body = await self._gh_put(f"{api}/{cfg_path}", {
            "message": msg,
            "content": base64.b64encode(new_cfg.encode("utf-8")).decode("ascii"),
            "sha": cfg_sha, "branch": branch,
        })
        if status not in (200, 201):
            return None, f"config.yaml-Commit HTTP {status}: {str(body)[:200]}"
        _LOGGER.warning("[AUC] AUTO-BUMP: %s %s -> %s", slug, old_v, new_v)
        return new_v, ""

    def _check_pending(self, installed: dict[str, dict] | None) -> None:
        """Bump-Meldung entfernen, sobald die Zielversion installiert ist."""
        if installed is None:
            return
        for slug in list(self._pending):
            target = self._pending[slug].get("target", "")
            inst = installed.get(slug)
            if inst is None:
                done = True  # deinstalliert
            else:
                try:
                    done = AwesomeVersion(inst["version"]) >= AwesomeVersion(target)
                except Exception:
                    done = inst["version"] == target
            if done:
                _LOGGER.info("[AUC] Installation erkannt: %s -> %s", slug, target)
                self._dismiss(f"auc_bump_{slug}")
                del self._pending[slug]

    # ------------------------------------------------------------------ Haupt-Scan
    async def _async_update_data(self) -> dict:
        _LOGGER.debug("[AUC] ===== Scan Start =====")
        if not self._store_loaded:
            await self._load_store()

        repos = await self._get_repos()
        if not repos:
            raise UpdateFailed("Konnte keine Repos abrufen")

        installed = await self._get_installed_addons()
        self._check_pending(installed)

        result: dict[str, dict] = {}
        found_keys: set[str] = set()
        bump_candidates: dict[tuple[str, str, str], dict] = {}

        for repo_data in repos:
            repo = repo_data["name"]
            branch = repo_data.get("default_branch", "main")
            dockerfile_paths = await self._find_dockerfiles(repo, branch)
            if not dockerfile_paths:
                continue

            for df_path in dockerfile_paths:
                dockerfile_content = await self._read_raw(repo, branch, df_path)
                if not dockerfile_content:
                    continue
                deps = self._parse_dockerfile(dockerfile_content, repo, df_path)
                if not deps:
                    _LOGGER.debug("[AUC] Keine externen Links in %s/%s", repo, df_path)
                    continue

                cfg = await self._read_config_yaml(repo, branch, df_path)
                addon_version = cfg.get("addon_version", "")
                addon_name = cfg.get("addon_name", repo)
                slug = cfg.get("slug", repo)

                for dep in deps:
                    dep_type = dep["type"]

                    if dep_type == "github":
                        upstream_owner = dep["upstream_owner"]
                        upstream_repo = dep["upstream_repo"]
                        key = f"{repo}__{df_path.replace('/', '_')}__gh__{upstream_owner}__{upstream_repo}"
                        source_label = f"{upstream_owner}/{upstream_repo}"
                        upstream_latest = await self._get_github_latest(upstream_owner, upstream_repo)

                    elif dep_type == "pypi":
                        package = dep["package"]
                        key = f"{repo}__{df_path.replace('/', '_')}__py__{package}"
                        source_label = f"pypi:{package}"
                        upstream_latest = await self._get_pypi_latest(package)

                    elif dep_type == "dockerhub":
                        image_full = dep["image_full"]
                        key = f"{repo}__{df_path.replace('/', '_')}__dh__{image_full.replace('/', '_').replace('.', '_')}"
                        source_label = f"docker:{image_full}"
                        upstream_latest = await self._get_dockerhub_digest(
                            dep["dh_owner"], dep["dh_image"], dep["tag"], dep["registry"]
                        )

                    else:
                        continue

                    found_keys.add(key)
                    notif_id = f"auc_{key}"
                    status, update_available = self._process_dep(
                        key, notif_id, addon_name, slug,
                        source_label, addon_version, upstream_latest
                    )
                    result[key] = {
                        "key": key, "type": dep_type,
                        "addon_repo": repo, "addon_name": addon_name,
                        "slug": slug, "dockerfile_path": df_path,
                        "source_label": source_label,
                        "addon_version": addon_version,
                        "upstream_latest": upstream_latest,
                        "status": status, "update_available": update_available,
                    }
                    if update_available and self.auto_bump:
                        cand = bump_candidates.setdefault((repo, branch, df_path), {
                            "addon_name": addon_name, "slug": slug,
                            "addon_version": addon_version, "deps": [],
                        })
                        cand["deps"].append({
                            "key": key, "notif_id": notif_id, "label": source_label,
                            "old": self._stored.get(key, {}).get("upstream_version"),
                            "new": upstream_latest,
                        })

        # --- Auto-Bump ausfuehren
        bumped_any = False
        for (repo, branch, df_path), cand in bump_candidates.items():
            slug, name = cand["slug"], cand["addon_name"]
            changes = [f"{d['label']}: {(d['old'] or '?')[:19]} -> {(d['new'] or '?')[:19]}"
                       for d in cand["deps"]]

            if installed is not None and slug not in installed:
                # Nicht installiert: nur neue Baseline merken, nichts bauen
                _LOGGER.info("[AUC] %s nicht installiert - kein Auto-Bump, Baseline aktualisiert", slug)
                for d in cand["deps"]:
                    self._stored[d["key"]] = {"addon_version": cand["addon_version"],
                                              "upstream_version": d["new"]}
                    self._dismiss(d["notif_id"])
                    result[d["key"]].update(status="not_installed", update_available=False)
                continue

            new_v, err = await self._bump_addon(repo, branch, df_path, slug, changes)
            if new_v is None:
                _LOGGER.error("[AUC] Auto-Bump %s fehlgeschlagen: %s", slug, err)
                for d in cand["deps"]:
                    self._notify_manual(d["notif_id"], name, slug, d["label"], d["old"], d["new"],
                                        extra=f"\n\n⚠️ Automatischer Versions-Bump fehlgeschlagen: {err}")
                continue

            bumped_any = True
            for d in cand["deps"]:
                self._stored[d["key"]] = {"addon_version": new_v, "upstream_version": d["new"]}
                self._dismiss(d["notif_id"])
                result[d["key"]].update(status="bumped", addon_version=new_v)
            self._pending[slug] = {"target": new_v, "name": name}
            self._notify(
                f"auc_bump_{slug}",
                f"⬆️ Add-on Update bereit: {name}",
                (f"**{name}** (`{slug}`) – neue Upstream-Version erkannt:\n"
                 + "\n".join(f"- `{c}`" for c in changes)
                 + f"\n\nDie Add-on-Version wurde automatisch von **{cand['addon_version']}** "
                   f"auf **{new_v}** erhoeht (Commit in `{repo}`).\n"
                   f"Unter **Einstellungen → Updates** erscheint das Update – dort *Aktualisieren* "
                   f"klicken, dann wird das Add-on neu gebaut und zieht die aktuellen Versionen.\n\n"
                   f"Diese Meldung verschwindet automatisch nach der Installation."),
            )

        if bumped_any:
            await self._supervisor_store_reload()

        removed = [k for k in list(self._stored.keys()) if k not in found_keys]
        for k in removed:
            _LOGGER.info("[AUC] Eintrag entfernt (Dockerfile weg): %s", k)
            del self._stored[k]

        await self._save_store()
        _LOGGER.debug("[AUC] ===== Scan Ende: %d Eintraege =====", len(result))
        return result
