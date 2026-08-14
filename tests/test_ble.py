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


# The cloud reports openers as "<prefix>_<MAC without separators>".
NAME_A = "Noru_9C9E6E09CAFC"
ADDRESS_A = "9C:9E:6E:09:CA:FC"
NAME_B = "opener_AABBCCDDEEFF"
ADDRESS_B = "AA:BB:CC:DD:EE:FF"


def make_coordinator(hass, entry, device_code, *, ble_name=None, autodetect=True):
    return coordinator_module.FlinxGarageCoordinator(
        hass,
        entry,
        account=FlinxAccount("user@example.com", "pw"),
        device_code=device_code,
        dev_key="ab" * 16,
        door_alias=f"Door {device_code[-4:]}",
        ble_name=ble_name,
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

        # The opener name the cloud reports identifies a door exactly.
        named_a = make_coordinator(hass, entry, CODE_A, ble_name=NAME_A, autodetect=False)
        named_b = make_coordinator(hass, entry, CODE_B, ble_name=NAME_B, autodetect=False)
        identified = [advert(NAME_A, ADDRESS_A), advert(NAME_B, ADDRESS_B)]
        a_match = named_a._match_ble_device(identified)  # noqa: SLF001
        b_match = named_b._match_ble_device(identified)  # noqa: SLF001
        check(
            "door A matches its reported opener",
            a_match is not None and a_match.address == ADDRESS_A,
            f"found={a_match.address if a_match else None}",
        )
        check(
            "door B matches its reported opener",
            b_match is not None and b_match.address == ADDRESS_B,
            f"found={b_match.address if b_match else None}",
        )
        check(
            "the two doors never resolve to the same peripheral",
            a_match is not None
            and b_match is not None
            and a_match.address != b_match.address,
        )
        check(
            "name matching is case-insensitive",
            (m := named_a._match_ble_device([advert(NAME_A.lower(), ADDRESS_A)]))  # noqa: SLF001
            is not None
            and m.address == ADDRESS_A,
        )
        check(
            "another door's opener is never used",
            named_a._match_ble_device([advert(NAME_B, ADDRESS_B)]) is None,  # noqa: SLF001
        )

        # A proxy can forward an advertisement with no local name; the address is
        # recoverable from the reported name.
        check(
            "falls back to the address encoded in the reported name",
            (m := named_a._match_ble_device([advert(None, ADDRESS_A)])) is not None  # noqa: SLF001
            and m.address == ADDRESS_A,
        )
        check(
            "address recovered from a reported name",
            coordinator_module.address_from_ble_name(NAME_A) == ADDRESS_A,
            coordinator_module.address_from_ble_name(NAME_A),
        )
        for bad in ("Noru_9C9E", "opener_ZZZZZZZZZZZZ", "Noru", ""):
            check(
                f"no address recovered from {bad!r}",
                coordinator_module.address_from_ble_name(bad) is None,
            )

        # deviceInfo also reports the name, so a migrated entry picks it up.
        learned = make_coordinator(hass, entry, CODE_A, autodetect=False)
        learned._apply_device_info(  # noqa: SLF001
            {"bluetoothName": NAME_A, "onlineState": 1}, push_update=False
        )
        check(
            "opener name learned from a cloud state read",
            (m := learned._match_ble_device(identified)) is not None  # noqa: SLF001
            and m.address == ADDRESS_A,
        )

        check(
            "address matching is case-insensitive",
            (m := named_a._match_ble_device([advert(None, ADDRESS_A.lower())]))  # noqa: SLF001
            is not None
            and m.address == ADDRESS_A.lower(),
        )
        check(
            "a reported opener that isn't advertising resolves to nothing",
            named_a._match_ble_device([advert(NAME_B, ADDRESS_B)]) is None,  # noqa: SLF001
        )
        check(
            "a reported opener is never overridden by the prefix fallback",
            make_coordinator(
                hass, entry, CODE_A, ble_name=NAME_A, autodetect=True
            )._match_ble_device([advert("opener_zzzz", "AA:01")])  # noqa: SLF001
            is None,
        )

        # _find_ble_device wires the above to the bluetooth helper, and logs.
        discovered: list = []

        def fake_discovered(_hass, connectable=True):
            return list(discovered)

        original = coordinator_module.bluetooth.async_discovered_service_info
        coordinator_module.bluetooth.async_discovered_service_info = fake_discovered
        try:
            discovered = [advert(NAME_A, ADDRESS_A)]
            found = named_a._find_ble_device()  # noqa: SLF001
            check(
                "_find_ble_device returns the identified peripheral",
                found is not None and found.address == ADDRESS_A,
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
