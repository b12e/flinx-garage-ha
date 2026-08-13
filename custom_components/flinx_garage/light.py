"""Light platform for F-LINX Garage Door."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import LightEntity, ColorMode
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
    """Set up F-LINX Garage Door lights."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    async_add_entities(FlinxGarageLight(c) for c in coordinators.values())


class FlinxGarageLight(CoordinatorEntity[FlinxGarageCoordinator], LightEntity):
    """Representation of F-LINX Garage Door LED light."""

    _attr_has_entity_name = True
    _attr_name = "Light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_light"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    @property
    def is_on(self) -> bool | None:
        """Return True if the light is on."""
        return self.coordinator.led_state

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        await self.coordinator.async_led_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.async_led_off()
