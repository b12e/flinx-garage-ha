"""Select platform for F-LINX Garage Door: the partial open height."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PARTIAL_LEVELS_CM
from .coordinator import FlinxGarageCoordinator

# The heights the F-LINX app offers, in the order the opener numbers them.
OPTIONS = [f"{cm} cm" for cm in PARTIAL_LEVELS_CM]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door selects."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    async_add_entities(FlinxPartialHeightSelect(c) for c in coordinators.values())


class FlinxPartialHeightSelect(CoordinatorEntity[FlinxGarageCoordinator], SelectEntity):
    """The height the door opens to on a partial (pedestrian) open.

    Stored in the opener, so it is shared with the F-LINX app and with a
    remote's partial button. Changing it does not move the door.
    """

    _attr_has_entity_name = True
    _attr_name = "Partial open height"
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = OPTIONS

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_partial_height"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    @property
    def current_option(self) -> str | None:
        level = self.coordinator.partial_level
        return None if level is None else OPTIONS[level]

    async def async_select_option(self, option: str) -> None:
        if not await self.coordinator.async_set_partial_level(OPTIONS.index(option)):
            raise self.coordinator.command_error(f"set the partial open height to {option}")
