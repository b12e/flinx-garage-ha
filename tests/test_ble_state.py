"""Checks that BLE command replies are used as a state source.

Every frame here is verbatim from a real debug log, decrypted with the real
devKey for that door — the reply to a command carries the same attribute TLVs
MQTT delivers, so BLE reports the position locally and immediately instead of
waiting on the controller's WiFi.
"""

from __future__ import annotations

from .harness import (  # noqa: F401 — .harness installs the import stubs
    CODE_A,
    add_entry,
    check,
    test_hass,
)

import asyncio
import logging

from custom_components.flinx_garage.account import FlinxAccount
from custom_components.flinx_garage.const import (
    ATTR_DOOR_POSITION,
    ATTR_LED_ACTUAL,
    ATTR_OPERATED_CYCLES,
    CONF_DEVICES,
)
from custom_components.flinx_garage.coordinator import FlinxGarageCoordinator
from custom_components.flinx_garage.crypto import unwrap_ble_frame
from custom_components.flinx_garage.mqtt_client import parse_attr_report

# The door this log came from, and its key.
DEV_KEY = "5cfbef5b9463ed8c49d7fa72c4441bc1"
DEVICE_CODE = "2012fdbc00528e7d"

# 15:25:03.803 "BLE command acked" — a full attribute report, position 17%.
STATE_FRAME = bytes.fromhex(
    "55550048016280c60f5fd0425af1452f84fde8402d6773705995d808a47d9fd2"
    "a08bdfc0b6a0c7017979889c662d9737dcee26de419c5e9455c46112ec80ec53"
    "4f826c72e723aaaa"
)
# 15:25:00.127 — a short ack with no attributes.
ACK_FRAME = bytes.fromhex("555500180194fece4612b1d139b3d897ead694736d8caaaa")
# 15:25:17.777 — the reply to the auth frame.
AUTH_FRAME = bytes.fromhex("555500180144bb42e37087f337152cd53ee9598798bdaaaa")


def make_coordinator(hass, entry):
    return FlinxGarageCoordinator(
        hass,
        entry,
        account=FlinxAccount("user@example.com", "pw"),
        device_code=DEVICE_CODE,
        dev_key=DEV_KEY,
        door_alias="garage",
        poll_interval=0,
    )


async def main() -> None:
    print("\n== BLE replies as a state source ==")
    key = bytes.fromhex(DEV_KEY)

    plaintext = unwrap_ble_frame(STATE_FRAME, key)
    check("the state frame decrypts", plaintext is not None)
    attrs = parse_attr_report(plaintext) if plaintext else None
    check("it parses as an attribute report", bool(attrs))
    check(
        "position 17% is recovered",
        attrs and attrs.get(ATTR_DOOR_POSITION) == 17,
        str(attrs.get(ATTR_DOOR_POSITION) if attrs else None),
    )
    check(
        "so are the cycle counter and LED",
        attrs
        and attrs.get(ATTR_OPERATED_CYCLES) == 356
        and attrs.get(ATTR_LED_ACTUAL) == 0xF0,
        str(attrs),
    )

    for label, frame in (("short ack", ACK_FRAME), ("auth reply", AUTH_FRAME)):
        plain = unwrap_ble_frame(frame, key)
        check(f"the {label} decrypts", plain is not None)
        check(
            f"the {label} carries no attributes",
            parse_attr_report(plain) is None if plain else False,
        )

    # Frames that must be rejected rather than half-parsed.
    check("a truncated frame is rejected", unwrap_ble_frame(STATE_FRAME[:20], key) is None)
    check(
        "a frame with a bad footer is rejected",
        unwrap_ble_frame(STATE_FRAME[:-2] + b"\x00\x00", key) is None,
    )
    check("an empty frame is rejected", unwrap_ble_frame(b"", key) is None)
    check(
        "a frame for another door decrypts to nothing usable",
        parse_attr_report(unwrap_ble_frame(STATE_FRAME, bytes(16)) or b"") is None,
    )

    async with test_hass() as hass:
        entry = add_entry(
            hass,
            version=3,
            data={"username": "u", "password": "p", CONF_DEVICES: []},
            entry_id="entry_ble_state",
            title="u",
            unique_id="u",
        )
        coordinator = make_coordinator(hass, entry)

        # The notify callback is what the door's reply actually lands in.
        coordinator._ble_notification(0, STATE_FRAME)  # noqa: SLF001
        check(
            "a notification updates the door position",
            coordinator.door_position == 17,
            str(coordinator.door_position),
        )
        check("and records BLE as the source", coordinator.position_source == "BLE")
        check("and is treated as a live local reading", coordinator.position_is_local())
        check("the operation count follows too", coordinator.operated_cycles == 356)
        check("and the LED state", coordinator.led_state is True)

        # An ack with no attributes must not disturb the state.
        coordinator._ble_notification(0, ACK_FRAME)  # noqa: SLF001
        check("a plain ack leaves the position alone", coordinator.door_position == 17)
        check(
            "every notification still counts as an ack",
            coordinator._last_notification == ACK_FRAME,  # noqa: SLF001
        )

        # A reply split across MTU-sized chunks is reassembled.
        coordinator.door_position = 0
        coordinator._ble_frame.clear()  # noqa: SLF001
        for start in range(0, len(STATE_FRAME), 20):
            coordinator._ble_notification(0, STATE_FRAME[start : start + 20])  # noqa: SLF001
        check(
            "a chunked notification is reassembled",
            coordinator.door_position == 17,
            str(coordinator.door_position),
        )

        # MQTT reports must still be attributed to MQTT.
        await coordinator._on_mqtt_attrs({ATTR_DOOR_POSITION: 42})  # noqa: SLF001
        check("an MQTT report updates the position", coordinator.door_position == 42)
        check("and records MQTT as the source", coordinator.position_source == "MQTT")
        check(
            "an MQTT reading is not treated as local",
            not coordinator.position_is_local(),
        )


if __name__ == "__main__":
    from .harness import summary

    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())
