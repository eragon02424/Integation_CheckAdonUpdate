"""Config Flow für Addon Update Checker."""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_GITHUB_USERNAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    GITHUB_API_BASE,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


class AddonUpdateCheckerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow Handler."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Erster Schritt: GitHub Username eingeben."""
        errors = {}

        if user_input is not None:
            username = user_input[CONF_GITHUB_USERNAME].strip()
            scan_interval = user_input[CONF_SCAN_INTERVAL]

            # Prüfen ob GitHub User existiert
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    f"{GITHUB_API_BASE}/users/{username}",
                    headers={"User-Agent": "HA-AddonUpdateChecker/1.0"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 404:
                        errors[CONF_GITHUB_USERNAME] = "user_not_found"
                    elif resp.status != 200:
                        errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

            if not errors:
                # Eindeutige ID damit man den gleichen User nicht zweimal hinzufügt
                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_configured()

                _LOGGER.debug(
                    "[AUC] Config Flow abgeschlossen: user=%s, intervall=%d min",
                    username, scan_interval
                )
                return self.async_create_entry(
                    title=f"GitHub: {username}",
                    data={
                        CONF_GITHUB_USERNAME: username,
                        CONF_SCAN_INTERVAL: scan_interval,
                    },
                )

        schema = vol.Schema({
            vol.Required(CONF_GITHUB_USERNAME, default="eragon02424"): str,
            vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_MINUTES): vol.All(
                int,
                vol.Range(min=MIN_SCAN_INTERVAL_MINUTES, max=MAX_SCAN_INTERVAL_MINUTES)
            ),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "min": str(MIN_SCAN_INTERVAL_MINUTES),
                "max": str(MAX_SCAN_INTERVAL_MINUTES),
            }
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Options Flow für nachträgliche Änderungen (z.B. Intervall)."""
        return AddonUpdateCheckerOptionsFlow(config_entry)


class AddonUpdateCheckerOptionsFlow(config_entries.OptionsFlow):
    """Options Flow - Intervall nachträglich ändern."""

    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Intervall ändern."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )

        schema = vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                int,
                vol.Range(min=MIN_SCAN_INTERVAL_MINUTES, max=MAX_SCAN_INTERVAL_MINUTES)
            ),
        })

        return self.async_show_form(step_id="init", data_schema=schema)
