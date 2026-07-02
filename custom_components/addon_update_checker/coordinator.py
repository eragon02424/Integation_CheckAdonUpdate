"""DataUpdateCoordinator für Addon Update Checker."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

import aiohttp
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

# Regex Patterns
PATTERN_FIXED_VERSION = re.compile(
    r'https://github\.com/([\w.-]+)/([\w.-]+)/releases/download/v?([\d][\d\.]*\d)/'
)
PATTERN_DYNAMIC_LATEST = re.compile(
    r'https://api\.github\.com/repos/([\w.-]+)/([\w.-]+)/releases/latest'
)
PATTERN_DYNAMIC_VAR = re.compile(
    r'https://github\.com/([\w.-]+)/([\w.-]+)/releases/download/\$\{?\w+\}?/'
)


class AddonUpdateCoordinator(DataUpdateCoordinator):
    """Koordiniert alle GitHub Scans und Versionsvergleiche."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialisierung."""
        self.github_username = entry.data[CONF_GITHUB_USERNAME]
        scan_minutes = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._known_versions: dict[str, dict] = {}
        self.session = async_get_clientsession(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_minutes),
        )
        _LOGGER.debug(
            "[AUC] Coordinator initialisiert: user=%s, intervall=%d min",
            self.github_username, scan_minutes
        )

    async def _async_load_stored_versions(self) -> None:
        """Lädt gespeicherte Versionen aus HA Storage (überlebt Neustart)."""
        stored = await self._store.async_load()
        if stored:
            self._known_versions = stored.get("versions", {})
            _LOGGER.debug(
                "[AUC] %d bekannte Einträge aus Storage geladen",
                len(self._known_versions)
            )
        else:
            _LOGGER.debug("[AUC] Kein Storage gefunden, starte frisch")

    async def _async_save_versions(self) -> None:
        """Speichert Versionen persistent."""
        await self._store.async_save({"versions": self._known_versions})

    async def _github_get(self, url: str) -> Any:
        """GitHub API GET mit Fehlerbehandlung."""
        headers = {"User-Agent": "HA-AddonUpdateChecker/1.0",
                   "Accept": "application/vnd.github+json"}
        try:
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 403:
                    _LOGGER.warning("[AUC] GitHub Rate Limit erreicht bei %s", url)
                elif resp.status == 404:
                    _LOGGER.debug("[AUC] 404 - nicht gefunden: %s", url)
                else:
                    _LOGGER.warning("[AUC] HTTP %d bei %s", resp.status, url)
        except asyncio.TimeoutError:
            _LOGGER.warning("[AUC] Timeout bei %s", url)
        except aiohttp.ClientError as err:
            _LOGGER.warning("[AUC] Verbindungsfehler bei %s: %s", url, err)
        return None

    async def _github_get_text(self, url: str) -> str | None:
        """GitHub RAW GET für Textdateien."""
        headers = {"User-Agent": "HA-AddonUpdateChecker/1.0"}
        try:
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.text()
                _LOGGER.debug("[AUC] HTTP %d bei RAW %s", resp.status, url)
        except Exception as err:
            _LOGGER.warning("[AUC] Fehler beim Lesen von %s: %s", url, err)
        return None

    async def _get_all_repos(self) -> list[dict]:
        """Holt alle Repos des GitHub Users."""
        _LOGGER.debug("[AUC] Scanne Repos von GitHub User: %s", self.github_username)
        repos = []
        page = 1
        while True:
            url = f"{GITHUB_API_BASE}/users/{self.github_username}/repos?per_page=100&page={page}"
            data = await self._github_get(url)
            if not data:
                break
            repos.extend(data)
            if len(data) < 100:
                break
            page += 1
        _LOGGER.debug("[AUC] %d Repos gefunden für %s", len(repos), self.github_username)
        return repos

    async def _find_dockerfiles_in_repo(self, owner: str, repo: str, branch: str) -> list[dict]:
        """Sucht alle Dockerfiles in einem Repo rekursiv."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        _LOGGER.debug("[AUC] Suche Dockerfiles in %s/%s (branch: %s)", owner, repo, branch)
        data = await self._github_get(url)
        if not data:
            return []
        found = []
        for item in data.get("tree", []):
            if item.get("type") == "blob" and item.get("path", "").endswith("Dockerfile"):
                found.append({
                    "repo": repo,
                    "path": item["path"],
                    "raw_url": f"{GITHUB_RAW_BASE}/{owner}/{repo}/{branch}/{item['path']}"
                })
        _LOGGER.debug("[AUC] %d Dockerfile(s) in %s/%s gefunden: %s",
                      len(found), owner, repo, [f["path"] for f in found])
        return found

    async def _parse_dockerfile(self, raw_url: str, repo: str, path: str) -> list[dict]:
        """Liest Dockerfile und extrahiert externe GitHub Abhängigkeiten."""
        content = await self._github_get_text(raw_url)
        if not content:
            return []

        _LOGGER.debug("[AUC] Parse Dockerfile: %s/%s", repo, path)
        results = []

        # Pattern 1: Feste Version im Download Link
        for match in PATTERN_FIXED_VERSION.finditer(content):
            gh_owner, gh_repo, version = match.group(1), match.group(2), match.group(3)
            _LOGGER.debug(
                "[AUC] ✓ Feste Version erkannt in %s/%s: %s/%s@%s",
                repo, path, gh_owner, gh_repo, version
            )
            results.append({
                "key": f"{repo}__{path.replace('/', '_')}__{gh_owner}__{gh_repo}",
                "addon_repo": repo,
                "dockerfile_path": path,
                "upstream_owner": gh_owner,
                "upstream_repo": gh_repo,
                "installed_version": version,
                "dynamic": False,
            })

        # Pattern 2: Dynamisch via releases/latest API
        for match in PATTERN_DYNAMIC_LATEST.finditer(content):
            gh_owner, gh_repo = match.group(1), match.group(2)
            _LOGGER.debug(
                "[AUC] ~ Dynamischer Latest-Link erkannt in %s/%s: %s/%s (keine feste Version)",
                repo, path, gh_owner, gh_repo
            )
            results.append({
                "key": f"{repo}__{path.replace('/', '_')}__{gh_owner}__{gh_repo}",
                "addon_repo": repo,
                "dockerfile_path": path,
                "upstream_owner": gh_owner,
                "upstream_repo": gh_repo,
                "installed_version": None,  # Unbekannt - lädt immer latest
                "dynamic": True,
            })

        # Pattern 3: Dynamisch via Variable
        for match in PATTERN_DYNAMIC_VAR.finditer(content):
            gh_owner, gh_repo = match.group(1), match.group(2)
            _LOGGER.debug(
                "[AUC] ~ Variable-Version erkannt in %s/%s: %s/%s",
                repo, path, gh_owner, gh_repo
            )
            results.append({
                "key": f"{repo}__{path.replace('/', '_')}__{gh_owner}__{gh_repo}",
                "addon_repo": repo,
                "dockerfile_path": path,
                "upstream_owner": gh_owner,
                "upstream_repo": gh_repo,
                "installed_version": None,
                "dynamic": True,
            })

        if not results:
            _LOGGER.debug("[AUC] Keine externen GitHub Links in %s/%s", repo, path)

        return results

    async def _get_upstream_latest(self, owner: str, repo: str) -> str | None:
        """Holt die neueste Release Version von upstream GitHub."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"
        data = await self._github_get(url)
        if data:
            tag = data.get("tag_name", "").lstrip("v")
            _LOGGER.debug("[AUC] Upstream %s/%s latest: %s", owner, repo, tag)
            return tag
        return None

    async def _async_update_data(self) -> dict:
        """Hauptfunktion - wird vom Coordinator stündlich aufgerufen."""
        _LOGGER.debug("[AUC] ===== Starte Update-Scan =====")

        # Storage laden beim ersten Mal
        if not self._known_versions:
            await self._async_load_stored_versions()

        repos = await self._get_all_repos()
        if not repos:
            raise UpdateFailed("Konnte keine Repos abrufen")

        # Alle Dockerfiles in allen Repos finden
        all_dependencies: dict[str, dict] = {}
        for repo_data in repos:
            repo_name = repo_data["name"]
            branch = repo_data.get("default_branch", "main")
            dockerfiles = await self._find_dockerfiles_in_repo(
                self.github_username, repo_name, branch
            )
            for df in dockerfiles:
                deps = await self._parse_dockerfile(df["raw_url"], repo_name, df["path"])
                for dep in deps:
                    all_dependencies[dep["key"]] = dep

        _LOGGER.debug("[AUC] Insgesamt %d externe Abhängigkeiten erkannt", len(all_dependencies))

        # Nicht mehr vorhandene Einträge entfernen
        removed = [k for k in self._known_versions if k not in all_dependencies]
        for key in removed:
            _LOGGER.info("[AUC] Eintrag entfernt (Dockerfile nicht mehr gefunden): %s", key)
            del self._known_versions[key]

        # Jede Abhängigkeit prüfen
        result: dict[str, dict] = {}
        for key, dep in all_dependencies.items():
            upstream_latest = await self._get_upstream_latest(
                dep["upstream_owner"], dep["upstream_repo"]
            )
            installed = dep["installed_version"]
            is_dynamic = dep["dynamic"]
            known = self._known_versions.get(key)

            if is_dynamic:
                # Dynamisches Dockerfile - immer latest, kein Versionsvergleich möglich
                status = "dynamic"
                update_available = False
                if not known:
                    _LOGGER.info(
                        "[AUC] NEU (dynamisch) %s/%s → %s/%s lädt immer 'latest' (%s)",
                        dep["addon_repo"], dep["dockerfile_path"],
                        dep["upstream_owner"], dep["upstream_repo"], upstream_latest
                    )
                    self._known_versions[key] = {"latest": upstream_latest, "dynamic": True}
            else:
                # Feste Version - Vergleich möglich
                if not known:
                    # Erster Fund - Baseline, keine Warnung
                    _LOGGER.info(
                        "[AUC] NEU erkannt: %s/%s → %s/%s installiert=%s latest=%s (Baseline gesetzt)",
                        dep["addon_repo"], dep["dockerfile_path"],
                        dep["upstream_owner"], dep["upstream_repo"],
                        installed, upstream_latest
                    )
                    self._known_versions[key] = {
                        "installed": installed,
                        "latest": upstream_latest,
                        "dynamic": False
                    }
                    status = "baseline"
                    update_available = False
                elif upstream_latest and upstream_latest != installed:
                    # Update verfügbar!
                    status = "update_available"
                    update_available = True
                    _LOGGER.warning(
                        "[AUC] ⚠ UPDATE VERFÜGBAR: %s/%s → %s/%s: %s → %s",
                        dep["addon_repo"], dep["dockerfile_path"],
                        dep["upstream_owner"], dep["upstream_repo"],
                        installed, upstream_latest
                    )
                    # HA persistente Benachrichtigung
                    self.hass.components.persistent_notification.async_create(
                        message=(
                            f"**{dep['addon_repo']}/{dep['dockerfile_path']}** verwendet "
                            f"`{dep['upstream_owner']}/{dep['upstream_repo']}` "
                            f"in Version **{installed}**, aber **{upstream_latest}** ist verfügbar.\n\n"
                            f"Bitte Dockerfile anpassen und Add-on neu aufbauen."
                        ),
                        title=f"🔧 Add-on Update: {dep['addon_repo']}",
                        notification_id=f"auc_{key}",
                    )
                    self._known_versions[key]["latest"] = upstream_latest
                else:
                    status = "up_to_date"
                    update_available = False
                    _LOGGER.debug(
                        "[AUC] ✓ Aktuell: %s/%s → %s/%s @ %s",
                        dep["addon_repo"], dep["dockerfile_path"],
                        dep["upstream_owner"], dep["upstream_repo"], installed
                    )
                    # Benachrichtigung wegräumen falls vorhanden
                    self.hass.components.persistent_notification.async_dismiss(
                        notification_id=f"auc_{key}"
                    )

            result[key] = {
                **dep,
                "upstream_latest": upstream_latest,
                "status": status,
                "update_available": update_available,
            }

        await self._async_save_versions()
        _LOGGER.debug("[AUC] ===== Scan abgeschlossen: %d Einträge =====", len(result))
        return result
