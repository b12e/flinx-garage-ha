"""Select platform for F-LINX Garage Door: the partial open height."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import DistanceConverter
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .const import DOMAIN, PARTIAL_LEVELS_CM
from .coordinator import FlinxGarageCoordinator


def height_options(imperial: bool) -> list[str]:
    """Labels for the heights the F-LINX app offers, in the opener's level order.

    The app only shows cm, so imperial labels keep the cm value alongside the
    inches to match what the app shows. All 13 stay distinct at whole inches.
    """
    if not imperial:
        return [f"{cm} cm" for cm in PARTIAL_LEVELS_CM]
    return [
        f"{round(DistanceConverter.convert(cm, UnitOfLength.CENTIMETERS, UnitOfLength.INCHES))} in ({cm} cm)"
        for cm in PARTIAL_LEVELS_CM
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door selects."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    # Labels follow the unit system at setup; changing it applies on the next reload.
    options = height_options(hass.config.units is US_CUSTOMARY_SYSTEM)
    async_add_entities(
        FlinxPartialHeightSelect(c, options) for c in coordinators.values()
    )


class FlinxPartialHeightSelect(CoordinatorEntity[FlinxGarageCoordinator], SelectEntity):
    """The height the door opens to on a partial (pedestrian) open.

    Stored in the opener, so it is shared with the F-LINX app and with a
    remote's partial button. Changing it does not move the door.
    """

    _attr_has_entity_name = True
    _attr_name = "Partial open height"
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: FlinxGarageCoordinator, options: list[str]
    ) -> None:
        super().__init__(coordinator)
        self._attr_options = options
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
        return None if level is None else self.options[level]

    async def async_select_option(self, option: str) -> None:
        if not await self.coordinator.async_set_partial_level(self.options.index(option)):
            raise self.coordinator.command_error(f"set the partial open height to {option}")
