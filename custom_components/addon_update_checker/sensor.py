"""Sensoren fuer Addon Update Checker."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AddonUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AddonUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_keys: set[str] = set()
    entities: list = []

    def _add_for_keys(keys: set[str]) -> None:
        new = []
        for key in keys:
            new.append(AddonInstalledVersionSensor(coordinator, key))
            new.append(AddonLatestVersionSensor(coordinator, key))
            new.append(AddonLastKnownAddonSensor(coordinator, key))
            new.append(AddonLastKnownDockerSensor(coordinator, key))
            _LOGGER.debug("[AUC] Sensoren angelegt fuer: %s", key)
        async_add_entities(new)
        entities.extend(new)
        known_keys.update(keys)

    _add_for_keys(set(coordinator.data.keys()))

    def _on_update() -> None:
        new_keys = set(coordinator.data.keys()) - known_keys
        if new_keys:
            _add_for_keys(new_keys)

    coordinator.async_add_listener(_on_update)


class AddonBaseSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: AddonUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key

    @property
    def _dep(self) -> dict:
        return self.coordinator.data.get(self._key, {})

    @property
    def _stored(self) -> dict:
        return self.coordinator._stored.get(self._key, {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._dep
        return {
            "addon_repo": d.get("addon_repo"),
            "addon_name": d.get("addon_name"),
            "slug": d.get("slug"),
            "dockerfile": d.get("dockerfile_path"),
            "source": d.get("source_label"),
            "type": d.get("type"),
            "status": d.get("status"),
            "update_available": d.get("update_available"),
        }


class AddonInstalledVersionSensor(AddonBaseSensor):
    @property
    def unique_id(self) -> str:
        return f"auc_{self._key}_installed"

    @property
    def name(self) -> str:
        d = self._dep
        return f"AUC {d.get('addon_name', d.get('addon_repo', ''))} Addon Version"

    @property
    def native_value(self) -> str | None:
        v = self._dep.get("addon_version")
        return v if v else None

    @property
    def icon(self) -> str:
        return "mdi:package-down"


class AddonLatestVersionSensor(AddonBaseSensor):
    @property
    def unique_id(self) -> str:
        return f"auc_{self._key}_latest"

    @property
    def name(self) -> str:
        d = self._dep
        src = d.get('source_label', '')
        return f"AUC {d.get('addon_name', d.get('addon_repo', ''))} Docker Version ({src})"

    @property
    def native_value(self) -> str | None:
        return self._dep.get("upstream_latest")

    @property
    def icon(self) -> str:
        return "mdi:package-up" if self._dep.get("update_available") else "mdi:package-check"


class AddonLastKnownAddonSensor(AddonBaseSensor):
    @property
    def unique_id(self) -> str:
        return f"auc_{self._key}_last_addon"

    @property
    def name(self) -> str:
        d = self._dep
        return f"AUC {d.get('addon_name', d.get('addon_repo', ''))} Last Addon Version"

    @property
    def native_value(self) -> str | None:
        v = self._stored.get("addon_version")
        return v if v else None

    @property
    def icon(self) -> str:
        return "mdi:history"


class AddonLastKnownDockerSensor(AddonBaseSensor):
    @property
    def unique_id(self) -> str:
        return f"auc_{self._key}_last_docker"

    @property
    def name(self) -> str:
        d = self._dep
        return f"AUC {d.get('addon_name', d.get('addon_repo', ''))} Last Docker Version"

    @property
    def native_value(self) -> str | None:
        v = self._stored.get("upstream_version")
        return v if v else None

    @property
    def icon(self) -> str:
        return "mdi:history"
