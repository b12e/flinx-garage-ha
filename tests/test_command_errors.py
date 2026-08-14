"""Checks that a command which never reached the door is reported as failed.

Returning False and logging it lets Home Assistant show the action as
successful while the door didn't move — which is what happened when the cloud
gateway answered "Too frequent operation, please try again later".
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
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.flinx_garage.account import FlinxAccount
from custom_components.flinx_garage.const import CONF_DEVICES
from custom_components.flinx_garage.coordinator import FlinxGarageCoordinator
from custom_components.flinx_garage.cover import FlinxGarageCover
from custom_components.flinx_garage.light import FlinxGarageLight


class FakeResponse:
    """Just enough aiohttp response for _send_cloud_command."""

    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_coordinator(hass, entry):
    coordinator = FlinxGarageCoordinator(
        hass,
        entry,
        account=FlinxAccount("user@example.com", "pw"),
        device_code=CODE_A,
        dev_key="ab" * 16,
        door_alias="Garage",
        poll_interval=0,
    )
    coordinator.door_position = 0
    return coordinator


async def main() -> None:
    print("\n== failed commands surface to the user ==")
    async with test_hass() as hass:
        entry = add_entry(
            hass,
            version=3,
            data={"username": "u", "password": "p", CONF_DEVICES: []},
            entry_id="entry_errors",
            title="u",
            unique_id="u",
        )

        # The gateway's rate limit, verbatim from a real log.
        rejection = {"code": 500, "msg": "Too frequent operation, please try again later"}
        coordinator = make_coordinator(hass, entry)
        session = AsyncMock()
        session.get = lambda *args, **kwargs: FakeResponse(rejection)

        with (
            patch.object(
                coordinator._account,  # noqa: SLF001
                "async_get_token",
                new=AsyncMock(return_value="tok"),
            ),
            patch(
                "custom_components.flinx_garage.coordinator.async_get_clientsession",
                return_value=session,
            ),
            patch.object(coordinator, "_send_ble_command", new=AsyncMock(return_value=False)),
            patch.object(coordinator, "_schedule_post_command_refresh"),
        ):
            sent = await coordinator.async_door_open()
            check("a rejected cloud command reports failure", sent is False)

            error = coordinator.command_error("open the door")
            check(
                "the error is a HomeAssistantError",
                isinstance(error, HomeAssistantError),
            )
            check(
                "the gateway's own wording reaches the user",
                "Too frequent operation" in str(error),
                str(error),
            )

            # The entities must raise it rather than report success.
            cover = FlinxGarageCover(coordinator)
            cover.hass = hass
            cover.entity_id = "cover.garage_door"
            raised = None
            try:
                await cover.async_open_cover()
            except HomeAssistantError as err:
                raised = err
            check(
                "cover.open_cover raises when the door didn't move",
                raised is not None and "open the door" in str(raised),
                str(raised),
            )

            light = FlinxGarageLight(coordinator)
            light.hass = hass
            light.entity_id = "light.garage_light"
            raised = None
            try:
                await light.async_turn_on()
            except HomeAssistantError as err:
                raised = err
            check(
                "light.turn_on raises when the command was rejected",
                raised is not None and "turn the light on" in str(raised),
                str(raised),
            )

        # A successful command must not raise, and must clear the last error.
        with (
            patch.object(coordinator, "_send_ble_command", new=AsyncMock(return_value=False)),
            patch.object(coordinator, "_send_cloud_command", new=AsyncMock(return_value=True)),
            patch.object(coordinator, "_schedule_post_command_refresh"),
        ):
            cover = FlinxGarageCover(coordinator)
            cover.hass = hass
            cover.entity_id = "cover.garage_door"
            try:
                await cover.async_close_cover()
                check("a successful command does not raise", True)
            except HomeAssistantError as err:
                check("a successful command does not raise", False, str(err))
            check(
                "the previous failure was cleared",
                coordinator._last_command_error is None,  # noqa: SLF001
            )

        # An unknown position can't be driven to, and says so.
        coordinator.door_position = None
        with patch.object(coordinator, "_schedule_post_command_refresh"):
            check(
                "set_position with an unknown position reports failure",
                await coordinator.async_door_set_position(50) is False,
            )
        check(
            "and explains why",
            "position is unknown" in str(coordinator.command_error("move the door")),
            str(coordinator.command_error("move the door")),
        )


if __name__ == "__main__":
    from .harness import summary

    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    raise SystemExit(summary())
