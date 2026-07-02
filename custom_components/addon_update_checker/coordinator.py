"""DataUpdateCoordinator fuer Addon Update Checker."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

import aiohttp
import yaml
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
    CONF_GITHUB_TOKEN,
    CONF_GITHUB_USERNAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    GITHUB_API_BASE,
    GITHUB_RAW_BASE,
    PYPI_API_BASE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

PATTERN_FIXED = re.compile(
    r'https://github\.com/([\w.-]+)/([\w.-]+)/releases/download/v?([\d][\d\.]*)/'
)
PATTERN_DYNAMIC_API = re.compile(
    r'https://api\.github\.com/repos/([\w.-]+)/([\w.-]+)/releases/latest'
)
PATTERN_DYNAMIC_VAR = re.compile(
    r'https://github\.com/([\w.-]+)/([\w.-]+)/releases/download/\$\{?\w+\}?/'
)
# Erkennt: pip install --no-cache-dir paketname oder pip install paketname==1.2.3
PATTERN_PYPI = re.compile(
    r'pip(?:3)? install\s+((?:--[\w-]+\s+)*)([^\s&|\\]+)'
)
PYPI_IGNORE = {
    # pip Flags und Metapakete
    "pip", "setuptools", "wheel", "no-cache-dir", "break-system-packages",
    "upgrade", "r", "q", "quiet", "user",
    # Standard HTTP/Netzwerk Libs - updaten sich zu haeufig, nicht Kernfunktion
    "aiohttp", "requests", "urllib3", "httpx",
    # Sonstige Standard-Hilfsbibliotheken
    "certifi", "charset-normalizer", "idna", "pyyaml",
}


class AddonUpdateCoordinator(DataUpdateCoordinator):
    """Koordiniert alle GitHub Scans und Versionsvergleiche."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.github_username = entry.data[CONF_GITHUB_USERNAME]
        self.github_token = entry.data.get(CONF_GITHUB_TOKEN, "").strip()
        scan_minutes = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stored: dict[str, dict] = {}
        self._store_loaded = False
        self.session = async_get_clientsession(hass)
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(minutes=scan_minutes),
        )
        auth_info = "mit Token" if self.github_token else "OHNE Token (Rate Limit: 60/h)"
        _LOGGER.debug("[AUC] Coordinator init: user=%s, intervall=%d min, auth=%s",
                      self.github_username, scan_minutes, auth_info)

    async def _load_store(self) -> None:
        data = await self._store.async_load()
        if data:
            self._stored = data.get("versions", {})
            _LOGGER.debug("[AUC] Storage geladen: %d Eintraege", len(self._stored))
        else:
            _LOGGER.debug("[AUC] Kein Storage vorhanden, starte frisch")
        self._store_loaded = True

    async def _save_store(self) -> None:
        await self._store.async_save({"versions": self._stored})
        _LOGGER.debug("[AUC] Storage gespeichert: %d Eintraege", len(self._stored))

    def _github_headers(self) -> dict:
        headers = {"User-Agent": "HA-AddonUpdateChecker/1.0",
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

    async def _gh_text(self, url: str) -> str | None:
        try:
            async with self.session.get(
                url, headers={"User-Agent": "HA-AddonUpdateChecker/1.0"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
                _LOGGER.debug("[AUC] HTTP %d bei RAW %s", resp.status, url)
        except Exception as e:
            _LOGGER.warning("[AUC] Fehler bei %s: %s", url, e)
        return None

    async def _pypi_json(self, url: str) -> Any:
        try:
            async with self.session.get(
                url, headers={"User-Agent": "HA-AddonUpdateChecker/1.0"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                _LOGGER.debug("[AUC] PyPI HTTP %d bei %s", resp.status, url)
        except Exception as e:
            _LOGGER.warning("[AUC] PyPI Fehler bei %s: %s", url, e)
        return None

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

    def _parse_dockerfile(self, content: str, repo: str, path: str) -> list[dict]:
        """Externe Abhaengigkeiten aus Dockerfile extrahieren (GitHub + PyPI)."""
        results = []
        seen = set()

        for pattern in [PATTERN_FIXED, PATTERN_DYNAMIC_API, PATTERN_DYNAMIC_VAR]:
            for m in pattern.finditer(content):
                gh_owner, gh_repo = m.group(1), m.group(2)
                k = f"gh:{gh_owner}/{gh_repo}"
                if k not in seen:
                    seen.add(k)
                    _LOGGER.debug("[AUC] GitHub erkannt in %s/%s: %s/%s", repo, path, gh_owner, gh_repo)
                    results.append({"type": "github", "upstream_owner": gh_owner, "upstream_repo": gh_repo})

        for m in PATTERN_PYPI.finditer(content):
            pkg = m.group(2).strip().lower().split('==')[0]
            if pkg in PYPI_IGNORE or len(pkg) < 2 or pkg.startswith('-'):
                continue
            k = f"py:{pkg}"
            if k not in seen:
                seen.add(k)
                _LOGGER.debug("[AUC] PyPI erkannt in %s/%s: %s", repo, path, pkg)
                results.append({"type": "pypi", "package": pkg})

        return results

    async def _read_config_yaml(self, repo: str, branch: str, dockerfile_path: str) -> dict:
        folder = dockerfile_path.rsplit("/", 1)[0] if "/" in dockerfile_path else ""
        config_path = f"{folder}/config.yaml" if folder else "config.yaml"
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
        data = await self._pypi_json(url)
        if data:
            version = data.get("info", {}).get("version", "")
            _LOGGER.debug("[AUC] PyPI %s latest: %s", package, version)
            return version
        return None

    def _notify(self, notif_id: str, title: str, message: str) -> None:
        pn_create(self.hass, message=message, title=title, notification_id=notif_id)

    def _dismiss(self, notif_id: str) -> None:
        pn_dismiss(self.hass, notification_id=notif_id)

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

        elif upstream_changed:
            _LOGGER.warning("[AUC] UPDATE VERFUEGBAR: %s | %s: %s -> %s (addon bleibt %s)",
                            addon_name, source_label, last_upstream, upstream_latest, addon_version)
            self._notify(
                notif_id,
                f"\U0001f527 Add-on Update: {addon_name}",
                (f"**{addon_name}** (`{slug}`) verwendet\n"
                 f"`{source_label}` in Version **{last_upstream}**,\n"
                 f"aber **{upstream_latest}** ist verfuegbar.\n\n"
                 f"Bitte Dockerfile anpassen und Add-on neu aufbauen.\n"
                 f"Diese Meldung verschwindet automatisch nach dem Update."),
            )
            return "update_available", True

        else:
            _LOGGER.debug("[AUC] OK: %s | addon=%s upstream=%s", addon_name, addon_version, upstream_latest)
            self._dismiss(notif_id)
            return "up_to_date", False

    async def _async_update_data(self) -> dict:
        _LOGGER.debug("[AUC] ===== Scan Start =====")
        if not self._store_loaded:
            await self._load_store()

        repos = await self._get_repos()
        if not repos:
            raise UpdateFailed("Konnte keine Repos abrufen")

        result: dict[str, dict] = {}
        found_keys: set[str] = set()

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

        removed = [k for k in list(self._stored.keys()) if k not in found_keys]
        for k in removed:
            _LOGGER.info("[AUC] Eintrag entfernt (Dockerfile weg): %s", k)
            del self._stored[k]

        await self._save_store()
        _LOGGER.debug("[AUC] ===== Scan Ende: %d Eintraege =====", len(result))
        return result
