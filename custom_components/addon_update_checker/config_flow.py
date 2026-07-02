"""Config Flow fuer Addon Update Checker."""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_GITHUB_TOKEN,
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
        errors = {}

        if user_input is not None:
            username = user_input[CONF_GITHUB_USERNAME].strip()
            token = user_input.get(CONF_GITHUB_TOKEN, "").strip()
            scan_interval = user_input[CONF_SCAN_INTERVAL]

            session = async_get_clientsession(self.hass)
            headers = {"User-Agent": "HA-AddonUpdateChecker/1.0"}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            try:
                async with session.get(
                    f"{GITHUB_API_BASE}/users/{username}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 404:
                        errors[CONF_GITHUB_USERNAME] = "user_not_found"
                    elif resp.status == 401:
                        errors[CONF_GITHUB_TOKEN] = "token_invalid"
                    elif resp.status != 200:
                        errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

            if not errors:
                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_configured()
                _LOGGER.debug("[AUC] Config Flow OK: user=%s, token=%s, intervall=%d",
                              username, "ja" if token else "nein", scan_interval)
                return self.async_create_entry(
                    title=f"GitHub: {username}",
                    data={
                        CONF_GITHUB_USERNAME: username,
                        CONF_GITHUB_TOKEN: token,
                        CONF_SCAN_INTERVAL: scan_interval,
                    },
                )

        schema = vol.Schema({
            vol.Required(CONF_GITHUB_USERNAME, default="eragon02424"): str,
            vol.Optional(CONF_GITHUB_TOKEN, default=""): str,
            vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_MINUTES): vol.All(
                int, vol.Range(min=MIN_SCAN_INTERVAL_MINUTES, max=MAX_SCAN_INTERVAL_MINUTES)
            ),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return AddonUpdateCheckerOptionsFlow()


class AddonUpdateCheckerOptionsFlow(config_entries.OptionsFlow):
    """Options Flow - Token und Intervall nachtraeglich aendern."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        current_token = self.config_entry.options.get(
            CONF_GITHUB_TOKEN,
            self.config_entry.data.get(CONF_GITHUB_TOKEN, "")
        )

        schema = vol.Schema({
            vol.Optional(CONF_GITHUB_TOKEN, default=current_token): str,
            vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                int, vol.Range(min=MIN_SCAN_INTERVAL_MINUTES, max=MAX_SCAN_INTERVAL_MINUTES)
            ),
        })

        return self.async_show_form(step_id="init", data_schema=schema)
