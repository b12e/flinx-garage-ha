"""Cover platform for F-LINX Garage Door."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FlinxGarageCoordinator

_LOGGER = logging.getLogger(__name__)

# Position must change within this window (seconds) for us to call the
# door "opening" or "closing". Otherwise we assume the door is idle.
MOVING_WINDOW_SEC = 3.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up F-LINX Garage Door cover."""
    coordinator: FlinxGarageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FlinxGarageCover(coordinator, entry)])


class FlinxGarageCover(CoordinatorEntity[FlinxGarageCoordinator], CoverEntity):
    """Representation of the F-LINX Garage Door."""

    _attr_device_class = CoverDeviceClass.GARAGE
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    _attr_has_entity_name = True
    _attr_name = "Garage Door"

    def __init__(
        self, coordinator: FlinxGarageCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cover"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.data.get("door_alias") or "F-LINX Garage Door",
            "manufacturer": "F-LINX",
            "model": "BIT-DOOR",
        }

        # Direction tracking from position deltas (MQTT gives us ~5s cadence).
        self._last_position: int | None = None
        self._last_position_ts: float = 0.0
        self._direction: int = 0  # -1 closing, 0 idle, +1 opening
        self._direction_reset: asyncio.TimerHandle | None = None

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

        if pos is not None and self._last_position is not None and pos != self._last_position:
            delta = pos - self._last_position
            if delta > 0:
                self._direction = 1
            elif delta < 0:
                self._direction = -1
            self._last_position_ts = now
        elif pos is not None and self._last_position is None:
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

    async def async_open_cover(self, **kwargs: Any) -> None:
        if await self.coordinator.async_door_open():
            self._direction = 1
            self._last_position_ts = time.monotonic()
            self._schedule_direction_reset()
            self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        if await self.coordinator.async_door_close():
            self._direction = -1
            self._last_position_ts = time.monotonic()
            self._schedule_direction_reset()
            self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        if await self.coordinator.async_door_stop():
            self._direction = 0
            self._cancel_direction_reset()
            self.async_write_ha_state()
