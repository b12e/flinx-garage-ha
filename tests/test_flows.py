"""Checks for the config and options flow steps, with the cloud API mocked.

The flow objects are driven directly rather than through the flow manager: the
steps return plain result dicts, and everything worth asserting on (the entry
data, the registries, which FlinxAccount is used) is a side effect on hass.
"""

from __future__ import annotations

from .harness import (  # noqa: F401 — .harness installs the import stubs
    CODE_A,
    CODE_B,
    add_entry,
    check,
    test_hass,
)

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.flinx_garage import config_flow as flow_module
from custom_components.flinx_garage.account import CannotConnect, FlinxAccount
from custom_components.flinx_garage.const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DEVICES,
    CONF_DOOR_ALIAS,
    DOMAIN,
)

# Devices as /device/queryDevice returns them.
API_DEVICES = [
    {"deviceCode": CODE_A, "devKey": "ab" * 16, "doorAlias": "Garage"},
    {"deviceCode": CODE_B, "devKey": "cd" * 16, "doorAlias": "Carport"},
]


def config_flow(hass):
    flow = flow_module.FlinxGarageConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {}
    return flow


def options_flow(hass, entry):
    flow = flow_module.FlinxGarageOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id  # OptionsFlow.config_entry resolves this
    return flow


def seed_door(hass, entry, code, alias, object_id_prefix):
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, code)},
        name=alias,
    )
    reg_entry = er.async_get(hass).async_get_or_create(
        "cover",
        DOMAIN,
        f"{code}_cover",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=f"{object_id_prefix}_garage_door",
    )
    return device, reg_entry.entity_id


async def config_flow_checks(hass) -> None:
    print("\n== config flow ==")
    flow = config_flow(hass)

    with (
        patch.object(FlinxAccount, "async_login", new=AsyncMock(return_value="tok")),
        patch.object(
            FlinxAccount, "async_query_devices", new=AsyncMock(return_value=API_DEVICES)
        ),
    ):
        result = await flow.async_step_user(
            {"username": " User@Example.com ", "password": "pw"}
        )
    check(
        "credentials step advances to the door picker",
        result["step_id"] == "devices",
        str(result.get("step_id")),
    )
    check(
        "unique id claimed and normalised",
        flow.context["unique_id"] == "user@example.com",
        flow.context.get("unique_id"),
    )
    check(
        "username trimmed", flow._username == "User@Example.com", flow._username  # noqa: SLF001
    )

    created = await flow.async_step_devices({CONF_DEVICES: [CODE_B]})
    check(
        "only the selected door is stored",
        [d[CONF_DEVICE_CODE] for d in created["data"][CONF_DEVICES]] == [CODE_B],
        str(created["data"][CONF_DEVICES]),
    )
    check(
        "entry titled with the account",
        created["title"] == "User@Example.com",
        created["title"],
    )

    empty = await flow.async_step_devices({CONF_DEVICES: []})
    check(
        "selecting nothing re-shows the form with an error",
        empty["type"] == "form" and empty["errors"] == {"base": "no_devices_selected"},
        str(empty.get("errors")),
    )

    # Rejected credentials, an unreachable API and an empty account must all be
    # distinguishable to the user.
    with (
        patch.object(FlinxAccount, "async_login", new=AsyncMock(return_value=None)),
        patch.object(
            FlinxAccount, "async_query_devices", new=AsyncMock(return_value=None)
        ),
    ):
        bad = await flow.async_step_user({"username": "u", "password": "bad"})
    check("bad credentials -> invalid_auth", bad["errors"] == {"base": "invalid_auth"})

    with patch.object(
        FlinxAccount, "async_login", new=AsyncMock(side_effect=CannotConnect)
    ):
        down = await flow.async_step_user({"username": "u", "password": "pw"})
    check("api unreachable -> cannot_connect", down["errors"] == {"base": "cannot_connect"})

    with (
        patch.object(FlinxAccount, "async_login", new=AsyncMock(return_value="tok")),
        patch.object(FlinxAccount, "async_query_devices", new=AsyncMock(return_value=[])),
    ):
        none = await flow.async_step_user({"username": "u", "password": "pw"})
    check("account with no doors -> no_devices", none["errors"] == {"base": "no_devices"})


async def options_devices_checks(hass, entry) -> None:
    print("\n== options: manage devices ==")
    device_a, entity_a = seed_door(hass, entry, CODE_A, "Garage", "garage")
    _, entity_b = seed_door(hass, entry, CODE_B, "Carport", "carport")

    flow = options_flow(hass, entry)
    menu = await flow.async_step_init()
    check(
        "menu offers all three steps",
        menu["menu_options"] == ["poll_interval", "devices", "bluetooth"],
        str(menu["menu_options"]),
    )

    # The loaded entry's account must be reused — a separate login would revoke
    # the session the coordinators are holding.
    live = FlinxAccount("user@example.com", "pw")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "account": live,
        "coordinators": {},
    }
    with (
        patch.object(live, "async_login", new=AsyncMock(return_value="tok")) as login,
        patch.object(
            live, "async_query_devices", new=AsyncMock(return_value=API_DEVICES)
        ),
        patch.object(FlinxAccount, "async_login", new=AsyncMock(return_value="other")),
    ):
        form = await flow.async_step_devices()
        check("devices form shown", form["type"] == "form")
        check("the live account provided the token", login.await_count == 1)

    # Drop the Carport door.
    result = flow._async_save_devices([API_DEVICES[0]])  # noqa: SLF001
    check("step finishes", result["type"] == "create_entry")
    codes = [d[CONF_DEVICE_CODE] for d in entry.data[CONF_DEVICES]]
    check("entry keeps only the selected door", codes == [CODE_A], str(codes))
    check(
        "kept door keeps its BLE binding",
        entry.data[CONF_DEVICES][0].get(CONF_BLE_ADDRESS) == "AA:BB:CC:DD:EE:FF",
        str(entry.data[CONF_DEVICES][0]),
    )
    check(
        "dropped door's device removed",
        dr.async_get(hass).async_get_device(identifiers={(DOMAIN, CODE_B)}) is None,
    )
    check(
        "dropped door's entity removed", er.async_get(hass).async_get(entity_b) is None
    )
    check(
        "kept door's entity untouched",
        er.async_get(hass).async_get(entity_a) is not None,
    )
    kept_device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, CODE_A)})
    check(
        "kept door's device untouched",
        kept_device is not None and kept_device.id == device_a.id,
    )

    # A failure must abort the step, never rewrite the door list.
    with patch.object(live, "async_login", new=AsyncMock(side_effect=CannotConnect)):
        aborted = await flow.async_step_devices()
    check(
        "unreachable API aborts the step",
        aborted["type"] == "abort" and aborted["reason"] == "cannot_connect",
        str(aborted.get("reason")),
    )
    with (
        patch.object(live, "async_login", new=AsyncMock(return_value="tok")),
        patch.object(live, "async_query_devices", new=AsyncMock(return_value=None)),
    ):
        rejected = await flow.async_step_devices()
    check(
        "rejected session aborts as invalid_auth",
        rejected["type"] == "abort" and rejected["reason"] == "invalid_auth",
        str(rejected.get("reason")),
    )


async def options_bluetooth_checks(hass, entry) -> None:
    print("\n== options: bluetooth ==")
    flow = options_flow(hass, entry)
    form = await flow.async_step_bluetooth()
    fields = list(form["data_schema"].schema)
    check(
        "one field per configured door",
        [str(field) for field in fields] == [f"Garage ({CODE_A})"],
        str([str(field) for field in fields]),
    )
    values = [
        option["value"]
        for option in form["data_schema"].schema[fields[0]].config["options"]
    ]
    check(
        "cloud-only plus the bound address are offered",
        values == [flow_module.BLE_ADDRESS_NONE, "AA:BB:CC:DD:EE:FF"],
        str(values),
    )

    cleared = await flow.async_step_bluetooth(
        {f"Garage ({CODE_A})": flow_module.BLE_ADDRESS_NONE}
    )
    check("step finishes", cleared["type"] == "create_entry")
    check(
        "binding cleared",
        CONF_BLE_ADDRESS not in entry.data[CONF_DEVICES][0],
        str(entry.data[CONF_DEVICES][0]),
    )

    await flow.async_step_bluetooth({f"Garage ({CODE_A})": "11:22:33:44:55:66"})
    check(
        "binding stored",
        entry.data[CONF_DEVICES][0].get(CONF_BLE_ADDRESS) == "11:22:33:44:55:66",
        str(entry.data[CONF_DEVICES][0]),
    )


async def main() -> None:
    async with test_hass() as hass:
        await config_flow_checks(hass)
        entry = add_entry(
            hass,
            version=3,
            data={
                "username": "user@example.com",
                "password": "pw",
                CONF_DEVICES: [
                    {
                        CONF_DEVICE_CODE: CODE_A,
                        CONF_DEV_KEY: "ab" * 16,
                        CONF_DOOR_ALIAS: "Garage",
                        CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
                    },
                    {
                        CONF_DEVICE_CODE: CODE_B,
                        CONF_DEV_KEY: "cd" * 16,
                        CONF_DOOR_ALIAS: "Carport",
                    },
                ],
            },
            entry_id="entry_flows",
            title="user@example.com",
            unique_id="user@example.com",
        )
        await options_devices_checks(hass, entry)
        await options_bluetooth_checks(hass, entry)


if __name__ == "__main__":
    from .harness import summary

    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())
