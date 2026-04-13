"""F-LINX Garage Door integration — hybrid MQTT (state) + BLE (commands)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_CODE, CONF_DEV_KEY, DOMAIN
from .coordinator import FlinxGarageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER, Platform.LIGHT, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up F-LINX Garage Door from a config entry."""
    coordinator = FlinxGarageCoordinator(
        hass,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        device_code=entry.data[CONF_DEVICE_CODE],
        dev_key=entry.data[CONF_DEV_KEY],
    )

    # Start MQTT before first refresh so the initial state can come from it.
    await coordinator.async_start()

    # First refresh: falls back to REST API if MQTT hasn't delivered yet.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(coordinator.async_shutdown)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: FlinxGarageCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate v1 entries (no deviceCode/devKey) by forcing re-config."""
    if entry.version == 1:
        _LOGGER.warning(
            "F-LINX entry needs to be reconfigured to fetch the device key; "
            "please remove and re-add the integration"
        )
        return False
    return True
