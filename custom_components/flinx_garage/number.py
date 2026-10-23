"""Number platform for F-LINX Garage Door: the light's auto-off delay."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LED_OFF_DELAY_MAX, LED_OFF_DELAY_MIN
from .coordinator import FlinxGarageCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door numbers."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    async_add_entities(FlinxLedOffDelayNumber(c) for c in coordinators.values())


class FlinxLedOffDelayNumber(CoordinatorEntity[FlinxGarageCoordinator], NumberEntity):
    """How long the light stays on after the door stops (1-9 minutes, as in the app)."""

    _attr_has_entity_name = True
    _attr_name = "Light off delay"
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = LED_OFF_DELAY_MIN
    _attr_native_max_value = LED_OFF_DELAY_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_led_off_delay"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    @property
    def native_value(self) -> int | None:
        return self.coordinator.led_off_delay

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(value)
        if not await self.coordinator.async_set_led_off_delay(minutes):
            raise self.coordinator.command_error(f"set the light off delay to {minutes} min")
