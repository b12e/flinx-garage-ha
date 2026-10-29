"""Sensor platform for F-LINX Garage Door."""

from __future__ import annotations

import logging

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TRANSPORT_BLE, TRANSPORT_CLOUD, TRANSPORTS
from .coordinator import FlinxGarageCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door sensors."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    async_add_entities(
        entity
        for c in coordinators.values()
        for entity in (FlinxOperationCountSensor(c), FlinxConnectionSensor(c))
    )


class FlinxOperationCountSensor(CoordinatorEntity[FlinxGarageCoordinator], SensorEntity):
    """Sensor for garage door operation count."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name = True
    _attr_name = "Operation Count"
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_operation_count"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    @property
    def native_value(self) -> int | None:
        """Return the operation count."""
        return self.coordinator.operated_cycles


class FlinxConnectionSensor(CoordinatorEntity[FlinxGarageCoordinator], SensorEntity):
    """Which connection the next command goes out over: Bluetooth or the cloud.

    Disabled by default: it answers "is Bluetooth actually being used?" while
    setting the integration up, not something to keep on a dashboard.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "connection"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = TRANSPORTS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_connection"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    @property
    def available(self) -> bool:
        # Worked out locally, so it stays readable through a cloud outage —
        # which is exactly when it is worth looking at.
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.command_transport

    @property
    def icon(self) -> str:
        transport = self.coordinator.command_transport
        if transport == TRANSPORT_BLE:
            return "mdi:bluetooth-connect"
        if transport == TRANSPORT_CLOUD:
            return "mdi:cloud-outline"
        return "mdi:bluetooth-off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "connection_mode": self.coordinator.connection_mode,
            "last_command_via": self.coordinator.last_command_via,
        }
