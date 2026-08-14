"""F-LINX Garage Door integration — hybrid MQTT (state) + BLE (commands)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .account import FlinxAccount
from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DEVICES,
    CONF_DOOR_ALIAS,
    CONF_POLL_INTERVAL,
    DEFAULT_DOOR_ALIAS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    ENTRY_VERSION,
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

    devices = entry.data[CONF_DEVICES]
    poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    coordinators: dict[str, FlinxGarageCoordinator] = {
        device[CONF_DEVICE_CODE]: FlinxGarageCoordinator(
            hass,
            entry,
            account=account,
            device_code=device[CONF_DEVICE_CODE],
            dev_key=device[CONF_DEV_KEY],
            door_alias=device.get(CONF_DOOR_ALIAS) or DEFAULT_DOOR_ALIAS,
            ble_address=device.get(CONF_BLE_ADDRESS),
            # With one door configured, an unbound Noru_*/opener_* advert can
            # only be that door. With more, guessing risks commanding another.
            ble_autodetect=len(devices) == 1,
            poll_interval=poll_interval,
        )
        for device in devices
    }

    # Start MQTT before the first refresh so the initial state can come from it.
    # Each coordinator registered its own async_shutdown with the entry when it
    # was constructed, and HA runs those on-unload callbacks even when setup
    # fails, so a partial setup can't leak an MQTT connection or a poll timer.
    for coordinator in coordinators.values():
        await coordinator.async_start()

    # First refresh: falls back to the REST API if MQTT hasn't delivered yet.
    await asyncio.gather(
        *(c.async_config_entry_first_refresh() for c in coordinators.values())
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "account": account,
        "coordinators": coordinators,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload when the options or the device list change.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options or devices change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    The coordinators shut themselves down through the on-unload callback they
    registered with the entry, which HA runs right after this returns True.
    """
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Convert a pre-3.0 per-device entry to the account-based format.

    Pre-3.0 entries carried a single door (device_code/dev_key/door_alias) at the
    top level of entry.data and were keyed on the device code; 3.0 keys the entry
    on the account and holds a list of doors under CONF_DEVICES. Entities and the
    device registry moved from the entry id to the device code at the same time,
    so they are re-keyed here too — that is what keeps entity ids, and with them
    automations, dashboards and history, intact.
    """
    if entry.version > ENTRY_VERSION:
        _LOGGER.error(
            "F-LINX entry '%s' was written by a newer version of the integration "
            "(entry version %s); downgrading is not supported",
            entry.title,
            entry.version,
        )
        return False
    if entry.version == ENTRY_VERSION:
        return True

    username = entry.data.get(CONF_USERNAME)
    device_code = entry.data.get(CONF_DEVICE_CODE)
    dev_key = entry.data.get(CONF_DEV_KEY)
    if not (username and entry.data.get(CONF_PASSWORD) and device_code and dev_key):
        # Version 1 entries predate the devKey fetch — there is no key to carry
        # over, so the door has to be re-added by hand.
        _LOGGER.error(
            "F-LINX entry '%s' (version %s) has no stored device key; please "
            "remove and re-add the integration",
            entry.title,
            entry.version,
        )
        return False

    device = {
        CONF_DEVICE_CODE: device_code,
        CONF_DEV_KEY: dev_key,
        CONF_DOOR_ALIAS: entry.data.get(CONF_DOOR_ALIAS) or DEFAULT_DOOR_ALIAS,
    }

    # The helpers below are deliberately synchronous: HA migrates the entries of
    # a domain concurrently, and never awaiting keeps each migration atomic, so
    # two pre-3.0 entries for one account can't both claim it.
    if account_entry := _async_find_account_entry(hass, entry, username):
        _async_absorb_into_account_entry(hass, entry, account_entry, device)
        # The door now lives in account_entry and this entry is on its way out,
        # so it must not be set up.
        return False

    _async_convert_to_account_entry(hass, entry, device, username)
    return True


def _account_key(username: str | None) -> str:
    """Return the normalised account key used as the entry unique id."""
    return (username or "").strip().lower()


@callback
def _async_find_account_entry(
    hass: HomeAssistant, entry: ConfigEntry, username: str
) -> ConfigEntry | None:
    """Return an already-migrated entry for the same account, if there is one."""
    account = _account_key(username)
    for other in hass.config_entries.async_entries(DOMAIN):
        if (
            other.entry_id != entry.entry_id
            and other.version >= ENTRY_VERSION
            and other.disabled_by is None
            and _account_key(other.data.get(CONF_USERNAME)) == account
        ):
            return other
    return None


@callback
def _async_convert_to_account_entry(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any], username: str
) -> None:
    """Rewrite this entry in place as the account entry for its door."""
    _async_rekey_registries(hass, entry, device[CONF_DEVICE_CODE])

    # Claim the account-level unique id, unless another entry already holds it —
    # HA raises a repair issue when two entries share a unique id.
    unique_id = _account_key(username)
    if any(
        other.entry_id != entry.entry_id and other.unique_id == unique_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        unique_id = entry.unique_id

    hass.config_entries.async_update_entry(
        entry,
        data={
            CONF_USERNAME: username,
            CONF_PASSWORD: entry.data[CONF_PASSWORD],
            CONF_DEVICES: [device],
        },
        unique_id=unique_id,
        version=ENTRY_VERSION,
    )
    _LOGGER.info(
        "Migrated F-LINX entry '%s' (door %s) to the account-based format",
        entry.title,
        device[CONF_DOOR_ALIAS],
    )


@callback
def _async_absorb_into_account_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    account_entry: ConfigEntry,
    device: dict[str, Any],
) -> None:
    """Move this entry's door into an existing account entry, then drop it."""
    _async_rekey_registries(hass, entry, device[CONF_DEVICE_CODE], account_entry)

    devices = list(account_entry.data.get(CONF_DEVICES) or [])
    if all(d.get(CONF_DEVICE_CODE) != device[CONF_DEVICE_CODE] for d in devices):
        devices.append(device)
        # Fires the account entry's update listener, which reloads it so the
        # door gets a coordinator (a no-op while it is still being set up).
        hass.config_entries.async_update_entry(
            account_entry, data={**account_entry.data, CONF_DEVICES: devices}
        )

    _LOGGER.info(
        "Merged F-LINX door %s into the '%s' account entry and removed the old "
        "per-device entry",
        device[CONF_DOOR_ALIAS],
        account_entry.title,
    )
    # We can't remove ourselves while our own setup lock is held.
    hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))


@callback
def _async_rekey_registries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_code: str,
    new_entry: ConfigEntry | None = None,
) -> None:
    """Re-key the entities and device from the entry id to the device code.

    Pre-3.0 entities had unique id ``<entry_id>_<suffix>`` and hung off a device
    identified by the entry id; 3.0 keys both on the device code. Renaming them
    in the registries — instead of letting the platforms register fresh ids — is
    what preserves the existing entity ids, names, areas and history. When
    new_entry is given they are handed over to that entry as well.
    """
    entity_registry = er.async_get(hass)
    old_prefix = f"{entry.entry_id}_"
    # Copy: updating config_entry_id mutates the index we're iterating.
    for reg_entry in list(
        er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    ):
        updates: dict[str, Any] = {}
        if reg_entry.unique_id.startswith(old_prefix):
            suffix = reg_entry.unique_id.removeprefix(old_prefix)
            new_unique_id = f"{device_code}_{suffix}"
            if existing := entity_registry.async_get_entity_id(
                reg_entry.domain, DOMAIN, new_unique_id
            ):
                _LOGGER.warning(
                    "Not re-keying %s: %s already uses unique id %s",
                    reg_entry.entity_id,
                    existing,
                    new_unique_id,
                )
            else:
                updates["new_unique_id"] = new_unique_id
        if new_entry is not None:
            updates["config_entry_id"] = new_entry.entry_id
        if updates:
            entity_registry.async_update_entity(reg_entry.entity_id, **updates)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    if device_entry is None:
        return

    device_updates: dict[str, Any] = {}
    if device_registry.async_get_device(identifiers={(DOMAIN, device_code)}) is None:
        device_updates["new_identifiers"] = {(DOMAIN, device_code)}
    if new_entry is not None:
        # The add is applied before the remove, so the device always keeps an
        # owning entry and survives the removal of this one.
        device_updates["add_config_entry_id"] = new_entry.entry_id
        device_updates["remove_config_entry_id"] = entry.entry_id
    if device_updates:
        device_registry.async_update_device(device_entry.id, **device_updates)
