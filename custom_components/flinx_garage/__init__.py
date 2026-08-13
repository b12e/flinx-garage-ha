"""F-LINX Garage Door integration — hybrid MQTT (state) + BLE (commands)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .account import FlinxAccount
from .const import (
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DEVICES,
    CONF_DOOR_ALIAS,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import FlinxGarageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER, Platform.LIGHT, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up F-LINX Garage Door from a config entry (one entry per account)."""
    account = FlinxAccount(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    coordinators: dict[str, FlinxGarageCoordinator] = {}
    for device in entry.data[CONF_DEVICES]:
        coordinator = FlinxGarageCoordinator(
            hass,
            account=account,
            device_code=device[CONF_DEVICE_CODE],
            dev_key=device[CONF_DEV_KEY],
            door_alias=device[CONF_DOOR_ALIAS],
            poll_interval=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        coordinators[device[CONF_DEVICE_CODE]] = coordinator

    # Start MQTT before first refresh so the initial state can come from it.
    # If setup fails (e.g. the first refresh raises permanently), shut down
    # every started coordinator so no MQTT connection or poll timer leaks.
    try:
        for coordinator in coordinators.values():
            await coordinator.async_start()

        # First refresh: falls back to REST API if MQTT hasn't delivered yet.
        await asyncio.gather(
            *(c.async_config_entry_first_refresh() for c in coordinators.values())
        )
    except Exception:
        await asyncio.gather(
            *(c.async_shutdown() for c in coordinators.values()),
            return_exceptions=True,
        )
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "account": account,
        "coordinators": coordinators,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _shutdown_coordinators() -> None:
        await asyncio.gather(*(c.async_shutdown() for c in coordinators.values()))

    entry.async_on_unload(_shutdown_coordinators)
    # Reload when options (e.g. the periodic poll interval) change.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await asyncio.gather(
            *(c.async_shutdown() for c in data["coordinators"].values()),
            return_exceptions=True,
        )

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old per-device entries by forcing re-config."""
    _LOGGER.warning(
        "F-LINX entry (version %s) is in the old per-device format; please "
        "remove and re-add the integration (one login adds all devices)",
        entry.version,
    )
    return False
