"""Sensor platform for F-LINX Garage Door."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FlinxGarageCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door sensors."""
    coordinator: FlinxGarageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        FlinxOperationCountSensor(coordinator, entry),
    ])


class FlinxOperationCountSensor(CoordinatorEntity[FlinxGarageCoordinator], SensorEntity):
    """Sensor for garage door operation count."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name = True
    _attr_name = "Operation Count"
    _attr_icon = "mdi:counter"

    def __init__(
        self, coordinator: FlinxGarageCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_operation_count"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "F-LINX Garage Door",
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    @property
    def native_value(self) -> int | None:
        """Return the operation count."""
        return self.coordinator.operated_cycles
