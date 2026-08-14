"""Checks for async_setup_entry / async_unload_entry with the transports mocked.

Covers the wiring an account-based entry depends on: one coordinator per door,
a single shared API session, and a clean teardown driven by the on-unload
callback the coordinators register with the entry.
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

from homeassistant.config_entries import ConfigEntryState

import custom_components.flinx_garage as flinx
from custom_components.flinx_garage.const import (
    CONF_BLE_NAME,
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DEVICES,
    CONF_DOOR_ALIAS,
    DOMAIN,
)
from custom_components.flinx_garage.coordinator import FlinxGarageCoordinator
from custom_components.flinx_garage.mqtt_client import FlinxMqttClient

# As /device/deviceInfo/<code> reports it: position, cycles, LED on.
DEVICE_INFO = {
    "attributes": [
        {"attributeCode": 10012, "attributeValue": 42},
        {"attributeCode": 10006, "attributeValue": 1234},
        {"attributeCode": 10013, "attributeValue": 0xF0},
    ],
    "firmwareVersion": "1.2.3",
    "onlineState": 1,
}


async def main() -> None:
    print("\n== setup and unload ==")
    async with test_hass() as hass:
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
                        CONF_BLE_NAME: "Noru_9C9E6E09CAFC",
                    },
                    {
                        CONF_DEVICE_CODE: CODE_B,
                        CONF_DEV_KEY: "cd" * 16,
                        CONF_DOOR_ALIAS: "Carport",
                    },
                ],
            },
            entry_id="entry_setup",
            title="user@example.com",
            unique_id="user@example.com",
        )
        # async_config_entry_first_refresh insists on being called during setup.
        object.__setattr__(entry, "state", ConfigEntryState.SETUP_IN_PROGRESS)

        with (
            patch.object(FlinxMqttClient, "connect", new=AsyncMock()),
            patch.object(FlinxMqttClient, "disconnect", new=AsyncMock()) as disconnect,
            patch.object(
                FlinxGarageCoordinator,
                "_async_fetch_device_info",
                new=AsyncMock(return_value=DEVICE_INFO),
            ),
            patch.object(
                hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
            ) as forward,
        ):
            check("setup returns True", await flinx.async_setup_entry(hass, entry))
            await hass.async_block_till_done()

            data = hass.data[DOMAIN][entry.entry_id]
            coordinators = data["coordinators"]
            check(
                "one coordinator per configured door",
                set(coordinators) == {CODE_A, CODE_B},
                str(sorted(coordinators)),
            )
            check(
                "the API session is shared",
                all(
                    c._account is data["account"]  # noqa: SLF001
                    for c in coordinators.values()
                ),
            )
            check(
                "reported opener name passed through",
                coordinators[CODE_A]._ble_name == "Noru_9C9E6E09CAFC"  # noqa: SLF001
                and coordinators[CODE_B]._ble_name is None,  # noqa: SLF001
            )
            check(
                "BLE autodetect off with two doors",
                not any(
                    c._ble_autodetect for c in coordinators.values()  # noqa: SLF001
                ),
            )
            check(
                "first refresh applied the cloud state",
                coordinators[CODE_A].door_position == 42
                and coordinators[CODE_A].operated_cycles == 1234
                and coordinators[CODE_A].led_state is True,
                f"pos={coordinators[CODE_A].door_position}",
            )
            check("platforms forwarded once", forward.call_count == 1)
            check(
                "coordinators registered themselves for unload",
                len(entry._on_unload or []) >= 3,  # noqa: SLF001
                f"callbacks={len(entry._on_unload or [])}",  # noqa: SLF001
            )

            object.__setattr__(entry, "state", ConfigEntryState.LOADED)
            with patch.object(
                hass.config_entries,
                "async_unload_platforms",
                new=AsyncMock(return_value=True),
            ):
                check(
                    "unload returns True", await flinx.async_unload_entry(hass, entry)
                )
            check("hass.data entry dropped", entry.entry_id not in hass.data[DOMAIN])

            # HA runs the on-unload callbacks right after async_unload_entry.
            await entry._async_process_on_unload(hass)  # noqa: SLF001
            check(
                "every coordinator disconnected MQTT",
                disconnect.await_count == 2,
                f"disconnects={disconnect.await_count}",
            )
            check(
                "coordinators marked as closing",
                all(c._closing for c in coordinators.values()),  # noqa: SLF001
            )


if __name__ == "__main__":
    from .harness import summary

    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())
