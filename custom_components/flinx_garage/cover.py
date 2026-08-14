"""Cover platform for F-LINX Garage Door."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FlinxGarageCoordinator

_LOGGER = logging.getLogger(__name__)

# Position must change within this window (seconds) for us to call the
# door "opening" or "closing". Otherwise we assume the door is idle.
MOVING_WINDOW_SEC = 3.0

# The controller's own readings jitter, and over BLE it reports several times a
# second: a real close was logged as 45, 42, 45, 40. Taking direction from the
# latest delta flips the door to "opening" on every blip, so it is taken from
# movement away from the furthest point reached — the door has to travel this
# far back before it counts as having reversed. Observed jitter is 3%.
DIRECTION_HYSTERESIS = 4  # %

# Two readings further apart than this say nothing about which way the door
# travelled between them. A position that arrives after a gap is a resync — the
# door was at 51% while the state still held 9% from a previous session, and
# adopting that as +42% of travel showed a phantom "opening" the door never did.
DIRECTION_MAX_GAP = 5.0  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door covers."""
    coordinators = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    async_add_entities(FlinxGarageCover(c) for c in coordinators.values())

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "refresh_state", None, "async_refresh_state"
    )


class FlinxGarageCover(CoordinatorEntity[FlinxGarageCoordinator], CoverEntity):
    """Representation of the F-LINX Garage Door."""

    _attr_device_class = CoverDeviceClass.GARAGE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )
    _attr_has_entity_name = True
    _attr_name = "Garage Door"

    def __init__(self, coordinator: FlinxGarageCoordinator) -> None:
        super().__init__(coordinator)
        device_code = coordinator.device_code
        self._attr_unique_id = f"{device_code}_cover"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_code)},
            "name": coordinator.door_alias,
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

        # Direction tracking from accumulated movement, not single deltas.
        self._last_position: int | None = None
        self._last_position_ts: float = 0.0
        self._direction: int = 0  # -1 closing, 0 idle, +1 opening
        self._direction_reset: asyncio.TimerHandle | None = None
        # Furthest position reached in the current direction; movement is judged
        # against this rather than against the previous sample.
        self._extreme: int | None = None
        # When the previous position reading arrived, to tell movement from a
        # resync after a gap.
        self._last_report_ts: float = 0.0

    @callback
    def _cancel_direction_reset(self) -> None:
        if self._direction_reset is not None:
            self._direction_reset.cancel()
            self._direction_reset = None

    @callback
    def _clear_stale_direction(self) -> None:
        self._direction_reset = None
        if self._direction == 0:
            return

        _LOGGER.debug(
            "Clearing stale cover direction after %.1fs without movement",
            MOVING_WINDOW_SEC,
        )
        self._direction = 0
        self.async_write_ha_state()

    @callback
    def _schedule_direction_reset(self) -> None:
        self._cancel_direction_reset()
        if self.hass is not None:
            self._direction_reset = self.hass.loop.call_later(
                MOVING_WINDOW_SEC, self._clear_stale_direction
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        pos = self.coordinator.door_position
        now = time.monotonic()

        if pos is not None:
            if self._extreme is None or now - self._last_report_ts > DIRECTION_MAX_GAP:
                # First reading, or the first after a gap: adopt the position
                # without reading travel into it. Any direction a command set
                # stands — its own timer decides when to give up on it.
                self._extreme = pos
                self._last_report_ts = now
            elif self._direction and (pos - self._extreme) * self._direction > 0:
                # Still travelling the way we thought: extend the reference.
                self._extreme = pos
                self._last_position_ts = now
            elif abs(pos - self._extreme) >= DIRECTION_HYSTERESIS:
                # Far enough back from the furthest point to be a real reversal
                # (or, from a standstill, a real departure).
                self._direction = 1 if pos > self._extreme else -1
                self._extreme = pos
                self._last_position_ts = now

        self._last_position = pos

        # Clear direction if position hasn't changed for a while or we hit a limit.
        if self._direction != 0:
            if now - self._last_position_ts > MOVING_WINDOW_SEC:
                self._direction = 0
            elif self._direction == 1 and pos == 100:
                self._direction = 0
            elif self._direction == -1 and pos == 0:
                self._direction = 0

        if self._direction != 0:
            self._schedule_direction_reset()
        else:
            self._cancel_direction_reset()

        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_direction_reset()
        await super().async_will_remove_from_hass()

    @property
    def current_cover_position(self) -> int | None:
        return self.coordinator.door_position

    @property
    def is_closed(self) -> bool | None:
        if self.coordinator.door_position is None:
            return None
        return self.coordinator.door_position == 0

    @property
    def is_opening(self) -> bool:
        return self._direction == 1

    @property
    def is_closing(self) -> bool:
        return self._direction == -1

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ble_connected": self.coordinator.is_ble_connected,
            "mqtt_connected": self.coordinator.mqtt.is_connected,
        }

    @callback
    def _set_commanded_direction(self, direction: int) -> None:
        """Show the direction we just asked for, until readings say otherwise.

        Re-anchors the movement reference so the first report after the command
        extends the travel rather than reading as a reversal.
        """
        self._direction = direction
        self._extreme = self.coordinator.door_position
        self._last_position_ts = time.monotonic()
        if direction:
            self._schedule_direction_reset()
        else:
            self._cancel_direction_reset()
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        if not await self.coordinator.async_door_open():
            raise self.coordinator.command_error("open the door")
        self._set_commanded_direction(1)

    async def async_close_cover(self, **kwargs: Any) -> None:
        if not await self.coordinator.async_door_close():
            raise self.coordinator.command_error("close the door")
        self._set_commanded_direction(-1)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        if not await self.coordinator.async_door_stop():
            raise self.coordinator.command_error("stop the door")
        self._set_commanded_direction(0)

    async def async_refresh_state(self) -> None:
        """Force a cloud API state refresh (flinx_garage.refresh_state action)."""
        await self.coordinator.async_force_refresh()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = kwargs[ATTR_POSITION]
        current = self.coordinator.door_position
        # Optimistic direction for snappy UI; coordinator position deltas
        # refine it as the door moves.
        if current is not None and position != current:
            self._set_commanded_direction(1 if position > current else -1)
        if not await self.coordinator.async_door_set_position(position):
            raise self.coordinator.command_error(f"move the door to {position}%")
