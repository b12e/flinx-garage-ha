"""Checks for async_migrate_entry — the pre-3.0 → account-based conversion.

The point of these is that upgrading must not cost anyone their entity ids, so
they assert against real entity/device registries rather than mocks.
"""

from __future__ import annotations

from .harness import (  # noqa: F401 — .harness installs the import stubs
    ACCOUNT,
    CODE_A,
    CODE_B,
    add_entry,
    check,
    test_hass,
)

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

import custom_components.flinx_garage as flinx
from custom_components.flinx_garage.const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DEVICES,
    CONF_DOOR_ALIAS,
    DOMAIN,
)

DEV_KEY = "ab" * 16

# The three entities a pre-3.0 entry owned, as (platform domain, unique id suffix).
PRE3_ENTITIES = (
    ("cover", "cover", "garage_door"),
    ("light", "light", "light"),
    ("sensor", "operation_count", "operation_count"),
)


def add_pre3_entry(hass, *, username, device_code, alias, entry_id):
    """A version 2 entry: one door, keyed on the device code."""
    return add_entry(
        hass,
        version=2,
        data={
            "username": username,
            "password": "hunter2",
            "device_code": device_code,
            "dev_key": DEV_KEY,
            "door_alias": alias,
        },
        entry_id=entry_id,
        title=alias,
        unique_id=device_code,
    )


def add_account_entry(hass, *, username, devices, entry_id):
    """An already-migrated version 3 entry."""
    return add_entry(
        hass,
        version=3,
        data={"username": username, "password": "hunter2", CONF_DEVICES: devices},
        entry_id=entry_id,
        title=username,
        unique_id=username.strip().lower(),
    )


def seed_pre3_registries(hass, entry, alias, object_id_prefix):
    """Create the device and entities a pre-3.0 entry would have registered."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=alias,
        manufacturer="F-LINX",
        model="BIT-DOOR",
    )
    entity_registry = er.async_get(hass)
    entity_ids = {}
    for platform_domain, suffix, object_id in PRE3_ENTITIES:
        reg_entry = entity_registry.async_get_or_create(
            platform_domain,
            DOMAIN,
            f"{entry.entry_id}_{suffix}",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"{object_id_prefix}_{object_id}",
        )
        entity_ids[suffix] = reg_entry.entity_id
    return device, entity_ids


def check_entities(label, hass, entity_ids, *, device_code, config_entry_id):
    """The entity ids must survive, re-keyed onto the device code."""
    entity_registry = er.async_get(hass)
    for suffix, entity_id in entity_ids.items():
        reg_entry = entity_registry.async_get(entity_id)
        if reg_entry is None:
            check(f"{label}: {entity_id} still exists", False, "entity is gone")
            continue
        check(
            f"{label}: {entity_id} re-keyed",
            reg_entry.unique_id == f"{device_code}_{suffix}",
            f"unique_id={reg_entry.unique_id}",
        )
        check(
            f"{label}: {entity_id} owned by the right entry",
            reg_entry.config_entry_id == config_entry_id,
            f"config_entry_id={reg_entry.config_entry_id}",
        )


async def scenario_single(hass: HomeAssistant) -> None:
    print("\n== migration: single pre-3.0 entry ==")
    entry = add_pre3_entry(
        hass, username=ACCOUNT, device_code=CODE_A, alias="Garage", entry_id="entry_one"
    )
    device, entity_ids = seed_pre3_registries(hass, entry, "Garage", "garage")

    check("migrate returns True", await flinx.async_migrate_entry(hass, entry) is True)
    check("entry version bumped", entry.version == 3, f"version={entry.version}")
    check(
        "devices list written",
        entry.data[CONF_DEVICES]
        == [
            {
                CONF_DEVICE_CODE: CODE_A,
                CONF_DEV_KEY: DEV_KEY,
                CONF_DOOR_ALIAS: "Garage",
            }
        ],
        str(entry.data[CONF_DEVICES]),
    )
    check(
        "unique_id is the normalised account",
        entry.unique_id == ACCOUNT.lower(),
        f"unique_id={entry.unique_id}",
    )
    check("title left alone", entry.title == "Garage", f"title={entry.title}")
    check_entities(
        "single", hass, entity_ids, device_code=CODE_A, config_entry_id=entry.entry_id
    )

    device_registry = dr.async_get(hass)
    moved = device_registry.async_get_device(identifiers={(DOMAIN, CODE_A)})
    check(
        "device re-keyed to the device code",
        moved is not None and moved.id == device.id,
        f"device={moved.id if moved else None}",
    )
    check(
        "old device identifier gone",
        device_registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)}) is None,
    )
    check(
        "device keeps its name",
        moved is not None and moved.name == "Garage",
        f"name={moved.name if moved else None}",
    )

    before = dict(entry.data)
    check("second migrate returns True", await flinx.async_migrate_entry(hass, entry))
    check("second migrate changed nothing", dict(entry.data) == before)


async def scenario_two_pre3_entries(hass: HomeAssistant) -> None:
    print("\n== migration: two pre-3.0 entries for one account ==")
    first = add_pre3_entry(
        hass, username=ACCOUNT, device_code=CODE_A, alias="Garage", entry_id="entry_aaa"
    )
    second = add_pre3_entry(
        hass,
        username=ACCOUNT.lower(),
        device_code=CODE_B,
        alias="Carport",
        entry_id="entry_bbb",
    )
    _, ids_first = seed_pre3_registries(hass, first, "Garage", "garage")
    device_second, ids_second = seed_pre3_registries(hass, second, "Carport", "carport")

    check("first migrate returns True", await flinx.async_migrate_entry(hass, first))
    check(
        "second migrate returns False (absorbed)",
        await flinx.async_migrate_entry(hass, second) is False,
    )
    await hass.async_block_till_done()

    codes = [d[CONF_DEVICE_CODE] for d in first.data[CONF_DEVICES]]
    check("both doors on the surviving entry", codes == [CODE_A, CODE_B], str(codes))
    check(
        "absorbed entry removed",
        hass.config_entries.async_get_entry(second.entry_id) is None,
    )
    check_entities(
        "merged-first", hass, ids_first, device_code=CODE_A, config_entry_id=first.entry_id
    )
    check_entities(
        "merged-second",
        hass,
        ids_second,
        device_code=CODE_B,
        config_entry_id=first.entry_id,
    )

    moved = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, CODE_B)})
    check(
        "absorbed device survives removal of its old entry",
        moved is not None and moved.id == device_second.id,
        f"device={moved.id if moved else None}",
    )
    check(
        "absorbed device now belongs to the surviving entry",
        moved is not None and moved.config_entries == {first.entry_id},
        f"config_entries={moved.config_entries if moved else None}",
    )
    entries = hass.config_entries.async_entries(DOMAIN)
    check(
        "no duplicate unique ids",
        len({e.unique_id for e in entries}) == len(entries),
    )


async def scenario_pre3_plus_account(hass: HomeAssistant) -> None:
    print("\n== migration: pre-3.0 entry alongside a 3.0 entry ==")
    account_entry = add_account_entry(
        hass,
        username=ACCOUNT,
        devices=[
            {
                CONF_DEVICE_CODE: CODE_A,
                CONF_DEV_KEY: DEV_KEY,
                CONF_DOOR_ALIAS: "Garage",
                CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            }
        ],
        entry_id="entry_new",
    )
    old = add_pre3_entry(
        hass, username=ACCOUNT, device_code=CODE_B, alias="Carport", entry_id="entry_old"
    )
    _, ids_old = seed_pre3_registries(hass, old, "Carport", "carport")

    check(
        "migrate returns False (absorbed)",
        await flinx.async_migrate_entry(hass, old) is False,
    )
    await hass.async_block_till_done()

    codes = [d[CONF_DEVICE_CODE] for d in account_entry.data[CONF_DEVICES]]
    check("door appended to the 3.0 entry", codes == [CODE_A, CODE_B], str(codes))
    check(
        "existing BLE binding kept",
        account_entry.data[CONF_DEVICES][0].get(CONF_BLE_ADDRESS)
        == "AA:BB:CC:DD:EE:FF",
    )
    check(
        "old entry removed", hass.config_entries.async_get_entry(old.entry_id) is None
    )
    check_entities(
        "absorbed",
        hass,
        ids_old,
        device_code=CODE_B,
        config_entry_id=account_entry.entry_id,
    )


async def scenario_version_1(hass: HomeAssistant) -> None:
    print("\n== migration: version 1 entry with no device key ==")
    entry = add_entry(
        hass,
        version=1,
        data={"username": ACCOUNT, "password": "hunter2"},
        entry_id="entry_v1",
        title="F-LINX",
        unique_id=None,
    )
    check(
        "migrate returns False", await flinx.async_migrate_entry(hass, entry) is False
    )
    check("entry left untouched", entry.version == 1)


async def scenario_future_version(hass: HomeAssistant) -> None:
    print("\n== migration: entry from a newer version ==")
    entry = add_account_entry(
        hass, username=ACCOUNT, devices=[], entry_id="entry_future"
    )
    object.__setattr__(entry, "version", 4)
    check(
        "migrate refuses a downgrade",
        await flinx.async_migrate_entry(hass, entry) is False,
    )


async def main() -> None:
    for scenario in (
        scenario_single,
        scenario_two_pre3_entries,
        scenario_pre3_plus_account,
        scenario_version_1,
        scenario_future_version,
    ):
        # A fresh instance per scenario: migration reads every entry in the domain.
        async with test_hass() as hass:
            await scenario(hass)


if __name__ == "__main__":
    from .harness import summary

    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())
