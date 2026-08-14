"""Shared bootstrap for the standalone checks.

There is no pytest-homeassistant-custom-component here: the venv's real
Home Assistant is booted just far enough to hold config entries and the
entity/device registries, which is what the migration has to be checked
against. Anything that would reach the network is mocked by the callers.
"""

from __future__ import annotations

from . import stubs  # noqa: F401  — must precede any homeassistant.components import

import contextlib
import socket
import tempfile
from collections.abc import AsyncIterator
from types import MappingProxyType, SimpleNamespace
from typing import Any

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    aiohttp_client,
    area_registry as ar,
    category_registry as cr,
    device_registry as dr,
    entity,
    entity_registry as er,
    floor_registry as fr,
    frame,
    issue_registry as ir,
    label_registry as lr,
)
from homeassistant.util import ssl as ssl_util

from custom_components.flinx_garage.const import DOMAIN

# Two 16-hex-char device codes, as the cloud API hands them out.
CODE_A = "0011223344556677"
CODE_B = "8899aabbccddeeff"
ACCOUNT = "Bram@Example.com"

_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record and print one assertion."""
    passed = bool(condition)
    _results.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name}{f' — {detail}' if detail else ''}")


def summary() -> int:
    """Print the tally and return a process exit code."""
    failed = [name for name, passed, _ in _results if not passed]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


@contextlib.asynccontextmanager
async def test_hass() -> AsyncIterator[HomeAssistant]:
    """A HomeAssistant with config entries and registries, in a temp config dir."""
    with tempfile.TemporaryDirectory() as config_dir:
        hass = HomeAssistant(config_dir)
        # Pre-seed the shared client session: creating a real one pulls in the
        # zeroconf/network stack, and every API call in these tests is mocked.
        hass.data[aiohttp_client.DATA_CLIENTSESSION] = {
            aiohttp_client._make_key(  # noqa: SLF001
                True, socket.AF_UNSPEC, ssl_util.SSLCipherList.PYTHON_DEFAULT
            ): object()
        }
        loader.async_setup(hass)
        entity.async_setup(hass)
        frame.async_setup(hass)
        dr.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        for load in (
            ar.async_load,
            cr.async_load,
            dr.async_load,
            er.async_load,
            fr.async_load,
            ir.async_load,
            lr.async_load,
        ):
            await load(hass)
        try:
            yield hass
        finally:
            await hass.async_stop()


def add_entry(
    hass: HomeAssistant,
    *,
    version: int,
    data: dict[str, Any],
    entry_id: str,
    title: str,
    unique_id: str | None,
) -> ConfigEntry:
    """Register a config entry without setting the integration up.

    ConfigEntries.async_add() would try to load and set up the integration for
    real, so the entry goes straight into the (indexed) container instead.
    """
    entry = ConfigEntry(
        version=version,
        minor_version=1,
        domain=DOMAIN,
        title=title,
        data=data,
        options={},
        source="user",
        unique_id=unique_id,
        entry_id=entry_id,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry  # noqa: SLF001
    return entry


def advert(name: str, address: str) -> SimpleNamespace:
    """Minimal stand-in for a BluetoothServiceInfoBleak."""
    return SimpleNamespace(
        name=name,
        address=address,
        rssi=-60,
        source="proxy",
        device=SimpleNamespace(name=name, address=address),
    )
