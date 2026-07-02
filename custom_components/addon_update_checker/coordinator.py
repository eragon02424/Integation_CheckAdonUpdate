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
    CONF_GITHUB_USERNAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    GITHUB_API_BASE,
    GITHUB_RAW_BASE,
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


class AddonUpdateCoordinator(DataUpdateCoordinator):
    """Koordiniert alle GitHub Scans und Versionsvergleiche."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.github_username = entry.data[CONF_GITHUB_USERNAME]
        scan_minutes = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stored: dict[str, dict] = {}
        self._store_loaded = False
        self.session = async_get_clientsession(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_minutes),
        )
        _LOGGER.debug(
            "[AUC] Coordinator init: user=%s, intervall=%d min",
            self.github_username, scan_minutes
        )

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

    async def _gh_json(self, url: str) -> Any:
        headers = {
            "User-Agent": "HA-AddonUpdateChecker/1.0",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with self.session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 403:
                    _LOGGER.warning("[AUC] GitHub Rate Limit bei %s", url)
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
        headers = {"User-Agent": "HA-AddonUpdateChecker/1.0"}
        try:
            async with self.session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
                _LOGGER.debug("[AUC] HTTP %d bei RAW %s", resp.status, url)
        except Exception as e:
            _LOGGER.warning("[AUC] Fehler bei %s: %s", url, e)
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
            _LOGGER.debug("[AUC] Seite %d: %d Repos geladen", page, len(data))
            if len(data) < 100:
                break
            page += 1
        _LOGGER.debug("[AUC] Gesamt %d Repos gefunden", len(repos))
        return repos

    async def _find_dockerfiles(self, repo: str, branch: str) -> list[str]:
        url = f"{GITHUB_API_BASE}/repos/{self.github_username}/{repo}/git/trees/{branch}?recursive=1"
        data = await self._gh_json(url)
        if not data:
            return []
        paths = [
            item["path"]
            for item in data.get("tree", [])
            if item.get("type") == "blob" and item["path"].endswith("Dockerfile")
        ]
        if paths:
            _LOGGER.debug("[AUC] Dockerfiles in %s: %s", repo, paths)
        return paths

    async def _read_raw(self, repo: str, branch: str, path: str) -> str | None:
        url = f"{GITHUB_RAW_BASE}/{self.github_username}/{repo}/{branch}/{path}"
        return await self._gh_text(url)

    def _parse_dockerfile(self, content: str, repo: str, path: str) -> list[dict]:
        results = []
        for m in PATTERN_FIXED.finditer(content):
            gh_owner, gh_repo = m.group(1), m.group(2)
            _LOGGER.debug("[AUC] Feste Version in %s/%s: %s/%s", repo, path, gh_owner, gh_repo)
            results.append({"upstream_owner": gh_owner, "upstream_repo": gh_repo, "dynamic": False})
        for m in PATTERN_DYNAMIC_API.finditer(content):
            gh_owner, gh_repo = m.group(1), m.group(2)
            _LOGGER.debug("[AUC] Dynamischer API-Link in %s/%s: %s/%s", repo, path, gh_owner, gh_repo)
            results.append({"upstream_owner": gh_owner, "upstream_repo": gh_repo, "dynamic": True})
        for m in PATTERN_DYNAMIC_VAR.finditer(content):
            gh_owner, gh_repo = m.group(1), m.group(2)
            if not any(r["upstream_owner"] == gh_owner and r["upstream_repo"] == gh_repo for r in results):
                _LOGGER.debug("[AUC] Dynamische Variable in %s/%s: %s/%s", repo, path, gh_owner, gh_repo)
                results.append({"upstream_owner": gh_owner, "upstream_repo": gh_repo, "dynamic": True})
        return results

    async def _read_config_yaml(self, repo: str, branch: str, dockerfile_path: str) -> dict:
        folder = dockerfile_path.rsplit("/", 1)[0] if "/" in dockerfile_path else ""
        config_path = f"{folder}/config.yaml" if folder else "config.yaml"
        _LOGGER.debug("[AUC] Lese config.yaml: %s/%s", repo, config_path)
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

    async def _get_upstream_latest(self, owner: str, repo: str) -> str | None:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"
        data = await self._gh_json(url)
        if data:
            tag = data.get("tag_name", "").lstrip("v")
            _LOGGER.debug("[AUC] Upstream %s/%s latest: %s", owner, repo, tag)
            return tag
        return None

    def _notify(self, notif_id: str, title: str, message: str) -> None:
        """Persistente HA Benachrichtigung erstellen."""
        pn_create(self.hass, message=message, title=title, notification_id=notif_id)

    def _dismiss(self, notif_id: str) -> None:
        """Persistente HA Benachrichtigung entfernen."""
        pn_dismiss(self.hass, notification_id=notif_id)

    async def _async_update_data(self) -> dict:
        """Wird vom Coordinator regelmaessig aufgerufen."""
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
                    upstream_owner = dep["upstream_owner"]
                    upstream_repo = dep["upstream_repo"]
                    is_dynamic = dep["dynamic"]

                    key = f"{repo}__{df_path.replace('/', '_')}__{upstream_owner}__{upstream_repo}"
                    found_keys.add(key)

                    upstream_latest = await self._get_upstream_latest(upstream_owner, upstream_repo)
                    notif_id = f"auc_{key}"
                    stored = self._stored.get(key)

                    if stored is None:
                        _LOGGER.info(
                            "[AUC] ERSTER FUND (Baseline): %s/%s -> %s/%s | addon=%s upstream=%s",
                            repo, df_path, upstream_owner, upstream_repo, addon_version, upstream_latest
                        )
                        self._stored[key] = {
                            "addon_version": addon_version,
                            "upstream_version": upstream_latest,
                            "dynamic": is_dynamic,
                        }
                        status = "baseline"
                        update_available = False

                    else:
                        last_upstream = stored.get("upstream_version", "")
                        last_addon = stored.get("addon_version", "")
                        addon_changed = addon_version != last_addon
                        upstream_changed = upstream_latest and upstream_latest != last_upstream

                        if addon_changed:
                            _LOGGER.info(
                                "[AUC] ADD-ON AKTUALISIERT: %s | addon %s -> %s | upstream %s",
                                addon_name, last_addon, addon_version, upstream_latest
                            )
                            self._stored[key] = {
                                "addon_version": addon_version,
                                "upstream_version": upstream_latest,
                                "dynamic": is_dynamic,
                            }
                            self._dismiss(notif_id)
                            status = "up_to_date"
                            update_available = False

                        elif upstream_changed and not is_dynamic:
                            _LOGGER.warning(
                                "[AUC] UPDATE VERFUEGBAR: %s | upstream %s -> %s (addon bleibt %s)",
                                addon_name, last_upstream, upstream_latest, addon_version
                            )
                            self._stored[key]["upstream_version"] = upstream_latest
                            self._notify(
                                notif_id,
                                f"\U0001f527 Add-on Update: {addon_name}",
                                (
                                    f"**{addon_name}** (`{slug}`) verwendet\n"
                                    f"`{upstream_owner}/{upstream_repo}` in Version **{last_upstream}**,\n"
                                    f"aber **{upstream_latest}** ist verfuegbar.\n\n"
                                    f"Bitte Dockerfile anpassen und Add-on neu aufbauen.\n"
                                    f"Diese Meldung verschwindet automatisch nach dem Update."
                                ),
                            )
                            status = "update_available"
                            update_available = True

                        elif is_dynamic and upstream_changed:
                            _LOGGER.info(
                                "[AUC] Dynamisch, upstream geaendert: %s/%s upstream %s -> %s",
                                repo, df_path, last_upstream, upstream_latest
                            )
                            self._stored[key]["upstream_version"] = upstream_latest
                            status = "dynamic"
                            update_available = False

                        else:
                            _LOGGER.debug(
                                "[AUC] OK: %s | addon=%s upstream=%s",
                                addon_name, addon_version, upstream_latest
                            )
                            self._dismiss(notif_id)
                            status = "up_to_date"
                            update_available = False

                    result[key] = {
                        "key": key,
                        "addon_repo": repo,
                        "addon_name": addon_name,
                        "slug": slug,
                        "dockerfile_path": df_path,
                        "upstream_owner": upstream_owner,
                        "upstream_repo": upstream_repo,
                        "addon_version": addon_version,
                        "upstream_latest": upstream_latest,
                        "dynamic": is_dynamic,
                        "status": status,
                        "update_available": update_available,
                    }

        removed = [k for k in list(self._stored.keys()) if k not in found_keys]
        for k in removed:
            _LOGGER.info("[AUC] Eintrag entfernt (Dockerfile weg): %s", k)
            del self._stored[k]

        await self._save_store()
        _LOGGER.debug("[AUC] ===== Scan Ende: %d Eintraege =====", len(result))
        return result
