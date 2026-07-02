"""Sensoren für Addon Update Checker."""
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
    """Richtet Sensoren ein."""
    coordinator: AddonUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for key, dep in coordinator.data.items():
        entities.append(AddonInstalledVersionSensor(coordinator, key))
        entities.append(AddonLatestVersionSensor(coordinator, key))
        _LOGGER.debug("[AUC] Sensoren angelegt für: %s", key)

    async_add_entities(entities)

    # Neue Sensoren bei späteren Updates dynamisch hinzufügen
    def _handle_coordinator_update() -> None:
        new_keys = set(coordinator.data.keys()) - {e._key for e in entities if hasattr(e, '_key')}
        if new_keys:
            new_entities = []
            for key in new_keys:
                new_entities.append(AddonInstalledVersionSensor(coordinator, key))
                new_entities.append(AddonLatestVersionSensor(coordinator, key))
                _LOGGER.debug("[AUC] Neue Sensoren für neu erkanntes Dockerfile: %s", key)
            async_add_entities(new_entities)

    coordinator.async_add_listener(_handle_coordinator_update)


class AddonBaseSensor(CoordinatorEntity, SensorEntity):
    """Basis-Sensor."""

    def __init__(self, coordinator: AddonUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._dep = coordinator.data.get(key, {})

    @property
    def _current_dep(self) -> dict:
        return self.coordinator.data.get(self._key, {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dep = self._current_dep
        return {
            "addon_repo": dep.get("addon_repo"),
            "dockerfile_path": dep.get("dockerfile_path"),
            "upstream_owner": dep.get("upstream_owner"),
            "upstream_repo": dep.get("upstream_repo"),
            "status": dep.get("status"),
            "dynamic": dep.get("dynamic"),
            "update_available": dep.get("update_available"),
        }


class AddonInstalledVersionSensor(AddonBaseSensor):
    """Sensor für die aktuell im Dockerfile referenzierte Version."""

    @property
    def unique_id(self) -> str:
        return f"auc_{self._key}_installed"

    @property
    def name(self) -> str:
        dep = self._current_dep
        return f"AUC {dep.get('addon_repo', '')} {dep.get('upstream_repo', '')} Installed"

    @property
    def native_value(self) -> str | None:
        dep = self._current_dep
        if dep.get("dynamic"):
            return "dynamic (always latest)"
        return dep.get("installed_version")

    @property
    def icon(self) -> str:
        return "mdi:package-down"


class AddonLatestVersionSensor(AddonBaseSensor):
    """Sensor für die neueste verfügbare upstream Version."""

    @property
    def unique_id(self) -> str:
        return f"auc_{self._key}_latest"

    @property
    def name(self) -> str:
        dep = self._current_dep
        return f"AUC {dep.get('addon_repo', '')} {dep.get('upstream_repo', '')} Latest"

    @property
    def native_value(self) -> str | None:
        return self._current_dep.get("upstream_latest")

    @property
    def icon(self) -> str:
        dep = self._current_dep
        if dep.get("update_available"):
            return "mdi:package-up"
        return "mdi:package-check"
