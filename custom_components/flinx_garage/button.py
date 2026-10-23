"""Button platform for F-LINX Garage Door: native partial open."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FlinxGarageCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door buttons."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    async_add_entities(FlinxPartialOpenButton(c) for c in coordinators.values())


class FlinxPartialOpenButton(CoordinatorEntity[FlinxGarageCoordinator], ButtonEntity):
    """Drive the door to its partial open height, where the opener stops by itself.

    From above that height the door closes down to it, so it lands there from
    any starting position.
    """

    _attr_has_entity_name = True
    _attr_name = "Partial open"
    _attr_icon = "mdi:garage-open-variant"

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_partial_open"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    async def async_press(self) -> None:
        if not await self.coordinator.async_door_partial():
            raise self.coordinator.command_error("partially open the door")
