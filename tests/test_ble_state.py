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
import time

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

        await source_precedence_checks(hass, coordinator)
        await cover_direction_checks(hass, coordinator)
        await recorded_open_checks(hass, coordinator)


async def source_precedence_checks(hass, coordinator) -> None:
    """BLE outranks MQTT and the cloud while it is streaming, and only then."""
    print("\n== state source precedence ==")

    coordinator._ble_notification(0, STATE_FRAME)  # noqa: SLF001
    check("BLE sets the position", coordinator.door_position == 17)

    # MQTT lags by seconds and the cloud by up to a minute: mid-movement their
    # reports describe the past, so they must not drag the state backwards.
    await coordinator._on_mqtt_attrs({ATTR_DOOR_POSITION: 0})  # noqa: SLF001
    check("a stale MQTT position is ignored while BLE is live", coordinator.door_position == 17)
    check("and BLE stays the source", coordinator.position_source == "BLE")

    coordinator._apply_device_info(  # noqa: SLF001
        {"attributes": [{"attributeCode": ATTR_DOOR_POSITION, "attributeValue": 0}]},
        push_update=False,
    )
    check("a stale cloud position is ignored too", coordinator.door_position == 17)

    # The recording's failure: a 51% from the middle of the movement arriving
    # after the 9% the door finished at, 5s later. Rejected on its own timestamp,
    # however late it turns up.
    await coordinator._on_mqtt_attrs(  # noqa: SLF001
        {ATTR_DOOR_POSITION: 51, "_ts": time.time() - 20}
    )
    check(
        "a position describing an earlier moment never lands late",
        coordinator.door_position == 17,
        str(coordinator.door_position),
    )

    # A genuinely new MQTT report is how an app or remote operation shows up.
    await coordinator._on_mqtt_attrs(  # noqa: SLF001
        {ATTR_DOOR_POSITION: 55, "_ts": time.time()}
    )
    check("a newer MQTT report is applied", coordinator.door_position == 55)
    check("and records MQTT as the source", coordinator.position_source == "MQTT")
    check("an MQTT reading is not treated as local", not coordinator.position_is_local())

    # A controller clock miles off tells us nothing about ordering, so such a
    # report is treated as undated rather than trusted or silently dropped.
    await coordinator._on_mqtt_attrs(  # noqa: SLF001
        {ATTR_DOOR_POSITION: 3, "_ts": 1}
    )
    check(
        "an implausible timestamp does not overwrite a dated reading",
        coordinator.door_position == 55,
        str(coordinator.door_position),
    )

    # The REST snapshot is undated: believed only when nothing dated is recent.
    coordinator._apply_device_info(  # noqa: SLF001
        {"attributes": [{"attributeCode": ATTR_DOOR_POSITION, "attributeValue": 60}]},
        push_update=False,
    )
    check("an undated cloud read is ignored while MQTT is recent", coordinator.door_position == 55)
    coordinator._position_ts -= 120  # noqa: SLF001
    coordinator._apply_device_info(  # noqa: SLF001
        {"attributes": [{"attributeCode": ATTR_DOOR_POSITION, "attributeValue": 60}]},
        push_update=False,
    )
    check("and believed when it is all we have", coordinator.door_position == 60)
    check("recorded as the cloud", coordinator.position_source == "cloud")


class FakeClock:
    """Controllable stand-in for the cover's time module."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


async def cover_direction_checks(hass, coordinator) -> None:
    """The controller's few-percent jitter must not flip opening/closing."""
    print("\n== cover direction vs position jitter ==")
    from unittest.mock import patch

    from custom_components.flinx_garage import cover as cover_module

    clock = FakeClock()
    cover = cover_module.FlinxGarageCover(coordinator)
    cover.hass = hass
    cover.entity_id = "cover.garage_door"

    def report(position: int, after: float = 0.3) -> None:
        clock.now += after
        coordinator.door_position = position
        cover._handle_coordinator_update()  # noqa: SLF001

    with patch.object(cover_module, "time", clock):
        # A real close, with the exact jitter from the log: 42 -> 45 -> 40.
        for position in (70, 69, 68, 67, 65, 63, 61, 59, 56, 54, 52, 49, 47, 45, 42):
            report(position)
        check("a steady close reads as closing", cover.is_closing, f"dir={cover._direction}")  # noqa: SLF001
        report(45)
        check(
            "an upward blip mid-close does not flip to opening",
            cover.is_closing and not cover.is_opening,
            f"dir={cover._direction}",  # noqa: SLF001
        )
        report(40)
        check("and it keeps closing afterwards", cover.is_closing)

        # The door stops around 9-10 and jitters there.
        for position in (11, 10, 9, 10, 9, 10, 9):
            report(position, after=0.5)
        check(
            "jitter at rest is not movement",
            not cover.is_opening,
            f"dir={cover._direction}",  # noqa: SLF001
        )
        check("and the door reads as open at 9%", cover.is_closed is False)

        # A genuine reopen is still picked up.
        for position in (14, 18, 22):
            report(position)
        check("a real reopen reads as opening", cover.is_opening, f"dir={cover._direction}")  # noqa: SLF001

    cover._cancel_direction_reset()  # noqa: SLF001


async def recorded_open_checks(hass, coordinator) -> None:
    """The screen recording: opening 9% -> 51% with a stale 9% landing mid-move.

    What was shown was "Closing 9%", then "Open 9%", then "Opening 51%", then
    "Open 51%" — a door that only ever opened, reported as closing and then
    re-opening, because a report describing the start of the movement arrived
    while the door was already half way through it.
    """
    print("\n== the recorded open, 9% -> 51% ==")
    from unittest.mock import patch

    from custom_components.flinx_garage import cover as cover_module

    clock = FakeClock()
    cover = cover_module.FlinxGarageCover(coordinator)
    cover.hass = hass
    cover.entity_id = "cover.garage_door"

    # Start from a settled 9%, as the previous close left it.
    coordinator._position_ts = time.time() - 120  # noqa: SLF001
    coordinator._apply_attrs({ATTR_DOOR_POSITION: 9}, source="BLE")  # noqa: SLF001

    with patch.object(cover_module, "time", clock):
        cover._handle_coordinator_update()  # noqa: SLF001
        check("starts settled at 9%", cover.current_cover_position == 9)

        # The door opens; BLE streams it.
        for position in (9, 14, 20, 26, 30):
            clock.now += 0.3
            coordinator._apply_attrs({ATTR_DOOR_POSITION: position}, source="BLE")  # noqa: SLF001
            cover._handle_coordinator_update()  # noqa: SLF001
        check("the open reads as opening", cover.is_opening, f"dir={cover._direction}")  # noqa: SLF001

        # Mid-movement, MQTT delivers the position from where it started.
        clock.now += 0.3
        await coordinator._on_mqtt_attrs(  # noqa: SLF001
            {ATTR_DOOR_POSITION: 9, "_ts": time.time() - 15}
        )
        cover._handle_coordinator_update()  # noqa: SLF001
        check(
            "a stale report does not drag the position back",
            cover.current_cover_position == 30,
            str(cover.current_cover_position),
        )
        check(
            "and the door is never shown closing",
            cover.is_opening and not cover.is_closing,
            f"dir={cover._direction}",  # noqa: SLF001
        )

        # It finishes at 51% and is stopped there.
        for position in (40, 47, 51):
            clock.now += 0.3
            coordinator._apply_attrs({ATTR_DOOR_POSITION: position}, source="BLE")  # noqa: SLF001
            cover._handle_coordinator_update()  # noqa: SLF001
        check("it ends at 51%", cover.current_cover_position == 51)

        # A late catch-up arriving after a gap must not read as fresh travel.
        clock.now += 30
        cover._handle_coordinator_update()  # noqa: SLF001
        check(
            "a resync after a gap claims no movement",
            not cover.is_opening and not cover.is_closing,
            f"dir={cover._direction}",  # noqa: SLF001
        )
        check("and the position stands at 51%", cover.current_cover_position == 51)

    cover._cancel_direction_reset()  # noqa: SLF001


if __name__ == "__main__":
    from .harness import summary

    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())
