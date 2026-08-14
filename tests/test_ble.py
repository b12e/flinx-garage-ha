"""Checks for the coordinator's BLE peripheral resolution.

An advertisement carries nothing that identifies a device code, so with several
doors configured the coordinator must never guess: sending a command built from
one door's key to another door's opener is the failure this guards against.
"""

from __future__ import annotations

from .harness import (  # noqa: F401 — .harness installs the import stubs
    CODE_A,
    CODE_B,
    add_entry,
    advert,
    check,
    test_hass,
)

import asyncio
import logging

from custom_components.flinx_garage import coordinator as coordinator_module
from custom_components.flinx_garage.account import FlinxAccount
from custom_components.flinx_garage.const import CONF_DEVICES


def make_coordinator(hass, entry, device_code, *, ble_address=None, autodetect=True):
    return coordinator_module.FlinxGarageCoordinator(
        hass,
        entry,
        account=FlinxAccount("user@example.com", "pw"),
        device_code=device_code,
        dev_key="ab" * 16,
        door_alias=f"Door {device_code[-4:]}",
        ble_address=ble_address,
        ble_autodetect=autodetect,
        poll_interval=0,
    )


async def main() -> None:
    print("\n== BLE resolution ==")
    async with test_hass() as hass:
        entry = add_entry(
            hass,
            version=3,
            data={
                "username": "user@example.com",
                "password": "pw",
                CONF_DEVICES: [],
            },
            entry_id="entry_ble",
            title="user@example.com",
            unique_id="user@example.com",
        )

        # One configured door: any F-LINX opener in range can only be that door.
        solo = make_coordinator(hass, entry, CODE_A, autodetect=True)
        found = solo._match_ble_device([advert("opener_zzzz", "AA:01")])  # noqa: SLF001
        check(
            "single door falls back to the name prefix",
            found is not None and found.address == "AA:01",
            f"found={found.address if found else None}",
        )
        check(
            "a non-F-LINX advert is never used",
            solo._match_ble_device([advert("Kettle", "AA:02")]) is None,  # noqa: SLF001
        )

        # Two doors, neither identifiable: no BLE beats the wrong door.
        multi_a = make_coordinator(hass, entry, CODE_A, autodetect=False)
        multi_b = make_coordinator(hass, entry, CODE_B, autodetect=False)
        anonymous = [advert("opener_zzzz", "AA:01"), advert("Noru_yyyy", "AA:02")]
        check(
            "unidentifiable door A gets no peripheral",
            multi_a._match_ble_device(anonymous) is None,  # noqa: SLF001
        )
        check(
            "unidentifiable door B gets no peripheral",
            multi_b._match_ble_device(anonymous) is None,  # noqa: SLF001
        )

        # A name carrying the device code identifies a door on its own.
        identified = [advert("opener_6677", "AA:01"), advert("Noru_eeff", "AA:02")]
        a_match = multi_a._match_ble_device(identified)  # noqa: SLF001
        b_match = multi_b._match_ble_device(identified)  # noqa: SLF001
        check(
            "door A matches the advert ending in its code",
            a_match is not None and a_match.address == "AA:01",
            f"found={a_match.address if a_match else None}",
        )
        check(
            "door B matches the advert ending in its code",
            b_match is not None and b_match.address == "AA:02",
            f"found={b_match.address if b_match else None}",
        )
        check(
            "the two doors never resolve to the same peripheral",
            a_match is not None
            and b_match is not None
            and a_match.address != b_match.address,
        )
        check(
            "a suffix too short to be unambiguous is ignored",
            multi_a._match_ble_device([advert("opener_77", "AA:03")]) is None,  # noqa: SLF001
        )
        check(
            "a non-F-LINX name is not matched on its suffix",
            multi_a._match_ble_device([advert("kettle_6677", "AA:04")]) is None,  # noqa: SLF001
        )

        # A bound address wins, and is the only thing considered.
        bound = make_coordinator(hass, entry, CODE_A, ble_address="AA:02")
        bound_match = bound._match_ble_device(identified)  # noqa: SLF001
        check(
            "a bound address is used even when another advert names this door",
            bound_match is not None and bound_match.address == "AA:02",
            f"found={bound_match.address if bound_match else None}",
        )
        lowercase = make_coordinator(hass, entry, CODE_A, ble_address="aa:02")
        lower_match = lowercase._match_ble_device(identified)  # noqa: SLF001
        check(
            "address matching is case-insensitive",
            lower_match is not None and lower_match.address == "AA:02",
        )
        check(
            "a bound address that isn't advertising falls back to nothing",
            bound._match_ble_device([advert("opener_6677", "AA:01")]) is None,  # noqa: SLF001
        )

        # _find_ble_device wires the above to the bluetooth helper, and logs.
        discovered: list = []

        def fake_discovered(_hass, connectable=True):
            return list(discovered)

        original = coordinator_module.bluetooth.async_discovered_service_info
        coordinator_module.bluetooth.async_discovered_service_info = fake_discovered
        try:
            discovered = [advert("opener_6677", "AA:01")]
            found = multi_a._find_ble_device()  # noqa: SLF001
            check(
                "_find_ble_device returns the identified peripheral",
                found is not None and found.address == "AA:01",
            )
            discovered = [advert("opener_zzzz", "AA:09")]
            check(
                "_find_ble_device returns None when nothing identifies the door",
                multi_a._find_ble_device() is None,  # noqa: SLF001
            )
            check(
                "the unusable-BLE warning only fires once per episode",
                multi_a._ble_not_connectable_warned is True,  # noqa: SLF001
            )
            discovered = []
            check(
                "no advertisements at all is handled",
                multi_a._find_ble_device() is None,  # noqa: SLF001
            )
        finally:
            coordinator_module.bluetooth.async_discovered_service_info = original


if __name__ == "__main__":
    from .harness import summary

    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())
