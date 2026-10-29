"""Binary sensor platform for F-LINX Garage Door: the Bluetooth link."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    """Set up F-LINX Garage Door binary sensors."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    # Created in Cloud only too, where it simply stays off: dropping it there
    # would leave an orphaned entity behind every time the mode is switched.
    async_add_entities(FlinxBleConnectedSensor(c) for c in coordinators.values())


class FlinxBleConnectedSensor(
    CoordinatorEntity[FlinxGarageCoordinator], BinarySensorEntity
):
    """On while an authenticated Bluetooth link to the opener is up.

    Only an authenticated link counts: one that connects but never gets past
    the handshake can't carry a command, so it reads as off.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "ble_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_ble_connected"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

    @property
    def available(self) -> bool:
        # The link state is known locally, whatever the cloud is doing.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_ble_connected
