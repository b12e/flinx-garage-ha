"""Coordinator for F-LINX Garage Door integration.

Hybrid architecture:
- MQTT subscribe for real-time state updates (primary, push-based)
- BLE for sending commands (local, works when internet is down)
- REST API for initial auth, device key fetch, and fallback state polling
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_BASE_URL,
    API_KEY_BLE_NAME,
    API_VERSION,
    ATTR_DOOR_POSITION,
    ATTR_LED_ACTUAL,
    ATTR_OPERATED_CYCLES,
    BLE_ACK_TIMEOUT,
    BLE_CONNECT_TIMEOUT,
    BLE_NAME_PREFIXES,
    BLE_NOTIFY_CHAR,
    BLE_NOTIFY_CHAR2,
    BLE_WRITE_CHAR,
    BLE_WRITE_TIMEOUT,
    CLOUD_CMD_CLOSE,
    CLOUD_CMD_LED_OFF,
    CLOUD_CMD_LED_ON,
    CLOUD_CMD_OPEN,
    CLOUD_CMD_STOP,
    CLOUD_GATEWAY_URL,
    DEFAULT_FALLBACK_SCAN_INTERVAL,
    DOOR_STATE_CLOSED,
    DOOR_STATE_OPEN,
    DOMAIN,
    MQTT_STALE_THRESHOLD,
    POSITION_LEAD_TIME,
    POSITION_POLL,
    POSITION_TIMEOUT,
    POSITION_TOLERANCE,
)
from .account import FlinxAccount
from .crypto import (
    BLE_CMD_CLOSE,
    BLE_CMD_LED_OFF,
    BLE_CMD_LED_ON,
    BLE_CMD_OPEN,
    BLE_CMD_STOP,
    build_ble_auth,
    build_ble_command,
)
from .mqtt_client import FlinxMqttClient

_LOGGER = logging.getLogger(__name__)


def _is_flinx_name(name: str | None) -> bool:
    """True when a BLE local name looks like an F-LINX door opener."""
    return bool(name) and name.startswith(BLE_NAME_PREFIXES)


def address_from_ble_name(name: str | None) -> str | None:
    """Recover the peripheral address from an opener's local name.

    The cloud API reports names as ``<prefix>_<MAC without separators>``
    (e.g. ``Noru_9C9E6E09CAFC``), so the address can be read straight out of
    it — useful because a proxy can pass on an advertisement with no local name
    at all, while the address is always there.
    """
    if not name or "_" not in name:
        return None
    tail = name.rsplit("_", 1)[-1]
    if len(tail) != 12 or any(c not in "0123456789abcdefABCDEF" for c in tail):
        return None
    return ":".join(tail[i : i + 2] for i in range(0, 12, 2)).upper()


class FlinxGarageCoordinator(DataUpdateCoordinator):
    """Hybrid MQTT (state) + BLE (commands) coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        account: FlinxAccount,
        device_code: str,
        dev_key: str,
        door_alias: str,
        ble_name: str | None = None,
        ble_autodetect: bool = True,
        poll_interval: int = 0,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            # Light polling cadence — MQTT push is primary; polling is a fallback
            # in case MQTT disconnects or we miss messages.
            update_interval=timedelta(seconds=DEFAULT_FALLBACK_SCAN_INTERVAL),
        )
        self._account = account
        self._device_code = device_code
        self._dev_key = dev_key
        self._door_alias = door_alias
        # The opener's local name as the cloud reports it; refreshed from every
        # deviceInfo read, so entries migrated from pre-3.0 pick it up too.
        self._ble_name = ble_name
        self._ble_autodetect = ble_autodetect
        self._poll_interval = poll_interval
        self._poll_unsub: Callable[[], None] | None = None
        # Warn only once per "BLE unusable" episode to avoid log spam (the BLE
        # scan runs on every reconnect/fallback tick).
        self._ble_not_connectable_warned = False

        self._ble_client: BleakClient | None = None
        self._ble_connecting = False
        # Set on shutdown so a pending reconnect timer can't have a torn-down
        # coordinator competing with its replacement for the proxy's slot.
        self._closing = False
        self._command_lock = asyncio.Lock()
        self._last_notification: bytes | None = None
        self._post_command_refresh: asyncio.Task[None] | None = None
        self._position_task: asyncio.Task[None] | None = None

        # State surface
        self.door_position: int | None = None      # 0–100
        self.led_state: bool | None = None         # True=on, False=off
        self.operated_cycles: int | None = None
        self.is_ble_connected: bool = False
        self.firmware_version: str | None = None
        self.is_online: bool | None = None
        self.last_mqtt_ts: float = 0.0

        # MQTT client (created after construction, started by __init__.py)
        self.mqtt = FlinxMqttClient(
            loop=hass.loop,
            device_code=device_code,
            dev_key_hex=dev_key,
            on_attrs=self._on_mqtt_attrs,
        )

    @property
    def device_code(self) -> str:
        """Cloud device code (16 hex chars) for this coordinator."""
        return self._device_code

    @property
    def door_alias(self) -> str:
        """User-facing alias for this door."""
        return self._door_alias

    # -----------------------------------------------------------------
    # MQTT inbound
    # -----------------------------------------------------------------

    async def _on_mqtt_attrs(self, attrs: dict[int, Any]) -> None:
        """Called from the MQTT client when an attr/up message arrives."""
        changed = False

        pos = attrs.get(ATTR_DOOR_POSITION)
        if pos is not None and pos != self.door_position:
            self.door_position = pos
            changed = True

        led_raw = attrs.get(ATTR_LED_ACTUAL)
        if led_raw is not None:
            # 0xf0 = LED on, 0xf1 = LED off
            new_led = led_raw == 0xF0
            if new_led != self.led_state:
                self.led_state = new_led
                changed = True

        cycles = attrs.get(ATTR_OPERATED_CYCLES)
        if cycles is not None and cycles != self.operated_cycles:
            self.operated_cycles = cycles
            changed = True

        self.last_mqtt_ts = self.mqtt.last_message_ts
        self.is_online = True

        if changed:
            _LOGGER.debug(
                "MQTT state update: pos=%s led=%s cycles=%s",
                self.door_position,
                self.led_state,
                self.operated_cycles,
            )
            # Push the new state to entities
            self.async_set_updated_data(self._build_state())

    def _build_state(self) -> dict[str, Any]:
        return {
            "door_position": self.door_position,
            "led_state": self.led_state,
            "operated_cycles": self.operated_cycles,
            "online": self.is_online,
            "firmware": self.firmware_version,
            "mqtt_connected": self.mqtt.is_connected,
            "ble_connected": self.is_ble_connected,
        }

    # -----------------------------------------------------------------
    # BLE command path
    # -----------------------------------------------------------------

    @callback
    def _ble_notification(self, sender: int, data: bytes) -> None:
        self._last_notification = data

    async def _ensure_ble_connected(self) -> bool:
        if self._closing:
            return False

        if self._ble_client and self._ble_client.is_connected:
            return True

        if self._ble_connecting:
            _LOGGER.debug("BLE: connect already in progress, skipping")
            return False

        self._ble_connecting = True
        try:
            ble_device = self._find_ble_device()
            if ble_device is None:
                return False

            _LOGGER.debug(
                "BLE: connecting to %s (%s)", ble_device.name, ble_device.address
            )
            # Wrap the whole connect + service-discovery + notify setup in a
            # timeout. A BLE-proxy link can establish at the radio level but
            # stall during service discovery/notify subscription, which would
            # otherwise leave us awaiting forever (and _ble_connecting stuck).
            async with asyncio.timeout(BLE_CONNECT_TIMEOUT):
                self._ble_client = await establish_connection(
                    BleakClient,
                    ble_device,
                    ble_device.name or "flinx",
                    disconnected_callback=self._on_ble_disconnect,
                    max_attempts=2,
                )
                if not self._ble_client.services:
                    await self._ble_client.get_services()
                await self._ble_client.start_notify(BLE_NOTIFY_CHAR, self._ble_notification)
                await self._ble_client.start_notify(BLE_NOTIFY_CHAR2, self._ble_notification)

                # Authenticate once per connection. The device validates this
                # frame before it will accept any command; the app sends it
                # once after service discovery (with a short settle delay).
                await asyncio.sleep(0.8)
                self._last_notification = None
                # Bounded separately from the connect budget: a write that hangs
                # on the proxy would otherwise eat the whole of it.
                async with asyncio.timeout(BLE_WRITE_TIMEOUT):
                    await self._ble_client.write_gatt_char(
                        BLE_WRITE_CHAR, build_ble_auth(bytes.fromhex(self._dev_key))
                    )
                await self._wait_for_ble_ack(BLE_ACK_TIMEOUT)

            self.is_ble_connected = True
            _LOGGER.debug(
                "BLE connected and authenticated to %s (auth resp: %s)",
                ble_device.address,
                self._last_notification.hex() if self._last_notification else "none",
            )
            return True

        except TimeoutError:
            _LOGGER.debug("BLE: connect timed out after %ss", BLE_CONNECT_TIMEOUT)
            await self._teardown_ble_client()
            return False
        except (BleakError, Exception) as err:  # noqa: BLE001
            _LOGGER.debug("BLE connection failed: %s", err)
            await self._teardown_ble_client()
            return False
        finally:
            self._ble_connecting = False

    def _find_ble_device(self):
        """Find *this door's* BLE peripheral among the discovered adverts."""
        connectable = list(
            bluetooth.async_discovered_service_info(self.hass, connectable=True)
        )
        device = self._match_ble_device(connectable)
        if device is not None:
            self._ble_not_connectable_warned = False
            return device

        self._log_no_ble_device(connectable)
        return None

    def _match_ble_device(self, service_infos):
        """Return the advertisement that is certainly this door, or None.

        Resolution is deliberately strict: with several doors in range, picking
        the wrong peripheral would mean writing frames built from another door's
        devKey. In order of confidence:

        1. the opener name the cloud reports for this door (``bluetoothName``),
           matched on the name itself or on the address embedded in it;
        2. any Noru_*/opener_* peripheral — but only when this is the one and
           only configured door, where there is nothing to confuse it with.
        """
        if self._ble_name:
            wanted = self._ble_name.casefold()
            for si in service_infos:
                if si.name and si.name.casefold() == wanted:
                    _LOGGER.debug(
                        "BLE: %s (%s) is the opener the cloud reports for %s "
                        "rssi=%s via %s",
                        si.name, si.address, self._door_alias, si.rssi, si.source,
                    )
                    return si.device
            # A proxy can forward an advertisement without its local name, so
            # fall back to the address the name encodes.
            if (address := address_from_ble_name(self._ble_name)) is not None:
                match = self._match_address(service_infos, address)
                if match is not None:
                    _LOGGER.debug(
                        "BLE: %s matches the address in %s's opener name (%s)",
                        address, self._door_alias, self._ble_name,
                    )
                return match
            return None

        if not self._ble_autodetect:
            return None

        for si in service_infos:
            if _is_flinx_name(si.name):
                _LOGGER.debug(
                    "BLE: found connectable %s (%s) rssi=%s via %s",
                    si.name, si.address, si.rssi, si.source,
                )
                return si.device

        return None

    @staticmethod
    def _match_address(service_infos, address: str):
        """Return the advertisement with this address, or None."""
        wanted = address.upper()
        for si in service_infos:
            if si.address.upper() == wanted:
                return si.device
        return None

    def _log_no_ble_device(self, connectable: list) -> None:
        """Explain why no BLE connection is attempted.

        Distinguishes "not seen at all", "seen but not connectable" (a
        passive-only proxy), "the bound address is gone" and "several doors are
        configured and this one isn't identifiable" — the last two are
        actionable, so they warn (once per episode; this runs on every reconnect
        attempt and fallback tick, so warning every time would spam).
        """
        all_si = list(
            bluetooth.async_discovered_service_info(self.hass, connectable=False)
        )
        matches = [s for s in all_si if _is_flinx_name(s.name)]
        detail = ", ".join(
            f"{s.name}/{s.address} rssi={s.rssi} src={s.source}" for s in matches
        )
        prefixes = ", ".join(f"{p}*" for p in BLE_NAME_PREFIXES)

        if self._ble_name:
            reason = f"opener {self._ble_name} is not connectable"
            warn = True
        elif not self._ble_autodetect:
            reason = (
                "the cloud reports no opener for this door, and with more than "
                "one door configured an unidentified opener can't be assumed to "
                "be this one"
            )
            warn = True
        elif matches:
            reason = f"{prefixes} seen but not connectable (proxy may be passive-only)"
            warn = True
        else:
            reason = (
                f"no {prefixes} device discovered "
                f"({len(connectable)} connectable, {len(all_si)} total adverts)"
            )
            warn = False

        if warn and not self._ble_not_connectable_warned:
            self._ble_not_connectable_warned = True
            _LOGGER.warning(
                "BLE unavailable for %s: %s; using cloud for commands%s",
                self._door_alias, reason, f" [{detail}]" if detail else "",
            )
        else:
            _LOGGER.debug(
                "BLE unavailable for %s: %s%s",
                self._door_alias, reason, f" [{detail}]" if detail else "",
            )

    async def _teardown_ble_client(self) -> None:
        """Drop a half-open client so the proxy connection slot is freed."""
        client, self._ble_client = self._ble_client, None
        self.is_ble_connected = False
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        self.is_ble_connected = False
        self._ble_client = None
        if self._closing:
            _LOGGER.debug("BLE disconnected during shutdown")
            return
        _LOGGER.debug("BLE disconnected — will reconnect in 10s")
        # Reconnect after a delay to avoid churn with the BLE proxy
        self.hass.loop.call_later(10, lambda: self.hass.async_create_task(
            self._ensure_ble_connected()
        ))

    async def _send_ble_command(self, ble_cmd_id: int) -> bool:
        """Send a command over BLE if already connected.

        Returns True only if the door acknowledges with a notification — a
        successful GATT write alone does not mean the device accepted the frame
        (the auth/command format is reverse-engineered). Without an ack we
        return False so the caller falls back to the cloud command.
        """
        if not self._ble_client or not self._ble_client.is_connected:
            # Not connected yet: kick off a connect in the background (so the
            # next command can use BLE) but don't block this one — fall back to
            # cloud immediately. Logs the connect lifecycle on button press.
            _LOGGER.debug(
                "BLE not connected (client=%s, connecting=%s); triggering connect, "
                "using cloud for this command",
                self._ble_client is not None,
                self._ble_connecting,
            )
            if not self._ble_connecting:
                self.hass.async_create_task(self._ensure_ble_connected())
            return False
        dev_key = bytes.fromhex(self._dev_key)
        async with self._command_lock:
            try:
                # Clear any stale notification before writing so the ack wait
                # only sees a response triggered by this command. Auth was sent
                # once at connect time, so we only write the command frame here.
                self._last_notification = None
                cmd_frame = build_ble_command(ble_cmd_id, dev_key)
                async with asyncio.timeout(BLE_WRITE_TIMEOUT):
                    await self._ble_client.write_gatt_char(BLE_WRITE_CHAR, cmd_frame)
            # Deliberately broad: BLE is the optimisation, the cloud is the
            # fallback, so nothing on this path may fail the user's action. A
            # proxy raises its own transport errors (e.g. aioesphomeapi's
            # TimeoutAPIError), which are not BleakError.
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "BLE command failed (%s: %s); using cloud",
                    type(err).__name__, err,
                )
                # The link is suspect now — drop it so the slot is freed and the
                # next command starts from a fresh connect.
                await self._teardown_ble_client()
                return False

        # Wait (outside the write lock) for an acknowledgement notification.
        acked = await self._wait_for_ble_ack(BLE_ACK_TIMEOUT)
        if acked:
            _LOGGER.debug("BLE command acked: %s", self._last_notification.hex())
            return True
        _LOGGER.debug("BLE command sent but no ack within %ss", BLE_ACK_TIMEOUT)
        return False

    async def _wait_for_ble_ack(self, timeout: float) -> bool:
        """Poll for a notification arriving after the command write."""
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            if self._last_notification is not None:
                return True
            await asyncio.sleep(0.05)
        return False

    async def async_door_open(self) -> bool:
        self._cancel_position_task()
        return await self._send_command(
            BLE_CMD_OPEN, CLOUD_CMD_OPEN, target_position=DOOR_STATE_OPEN
        )

    async def async_door_close(self) -> bool:
        self._cancel_position_task()
        return await self._send_command(
            BLE_CMD_CLOSE, CLOUD_CMD_CLOSE, target_position=DOOR_STATE_CLOSED
        )

    async def async_door_stop(self) -> bool:
        self._cancel_position_task()
        return await self._send_command(BLE_CMD_STOP, CLOUD_CMD_STOP, target_position=None)

    async def async_door_set_position(self, position: int) -> bool:
        """Drive the door to a target open percentage (0=closed, 100=open).

        The hardware has no native arbitrary-position command, so we send a
        normal open/close (BLE-first, cloud fallback) and STOP once the live
        position reaches the target. Endpoints (0/100) use the dedicated
        open/close commands which drive to the hard limit.
        """
        target = max(DOOR_STATE_CLOSED, min(DOOR_STATE_OPEN, position))
        if target >= DOOR_STATE_OPEN:
            return await self.async_door_open()
        if target <= DOOR_STATE_CLOSED:
            return await self.async_door_close()

        current = self.door_position
        if current is None:
            _LOGGER.warning("Cannot set position: current door position unknown")
            return False
        if abs(current - target) <= POSITION_TOLERANCE:
            _LOGGER.debug("Set position: already at ~%s%% (target %s)", current, target)
            return True

        # Supersede any in-flight positioning, then drive to the new target.
        self._cancel_position_task()
        self._position_task = self.hass.async_create_task(
            self._run_to_position(target, current)
        )
        return True

    async def _run_to_position(self, target: int, start: int) -> None:
        """Open/close toward target and STOP when live position reaches it."""
        opening = target > start
        try:
            ok = await self._send_command(
                BLE_CMD_OPEN if opening else BLE_CMD_CLOSE,
                CLOUD_CMD_OPEN if opening else CLOUD_CMD_CLOSE,
                target_position=None,
            )
            if not ok:
                _LOGGER.warning("Set position: failed to start movement toward %s%%", target)
                return

            # Predictive stop: the door coasts after STOP, so estimate its speed
            # from the live position stream and stop once the *projected* landing
            # spot (pos + speed x POSITION_LEAD_TIME) reaches the target.
            last_pos = start
            last_t = self.hass.loop.time()
            speed = 0.0  # %/s magnitude; smoothed
            deadline = last_t + POSITION_TIMEOUT
            while self.hass.loop.time() < deadline:
                await asyncio.sleep(POSITION_POLL)
                pos = self.door_position
                now = self.hass.loop.time()
                if pos is None:
                    continue
                if pos != last_pos:
                    dt = now - last_t
                    if dt > 0:
                        inst = abs(pos - last_pos) / dt
                        # EMA-smooth; seed on the first observed movement.
                        speed = inst if speed == 0 else 0.6 * speed + 0.4 * inst
                    last_pos = pos
                    last_t = now
                lead = speed * POSITION_LEAD_TIME
                projected = pos + lead if opening else pos - lead
                if (opening and projected >= target) or (
                    not opening and projected <= target
                ):
                    break
            else:
                _LOGGER.warning("Set position: timed out before reaching %s%%", target)

            await self._send_command(
                BLE_CMD_STOP, CLOUD_CMD_STOP, target_position=target
            )
            landing = last_pos + (speed * POSITION_LEAD_TIME if opening else -speed * POSITION_LEAD_TIME)
            _LOGGER.debug(
                "Set position: target %s%%, stop issued at pos=%s speed=%.1f%%/s "
                "(projected ~%s%%, lead %.1fs)",
                target, last_pos, speed, round(landing), POSITION_LEAD_TIME,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if asyncio.current_task() is self._position_task:
                self._position_task = None

    @callback
    def _cancel_position_task(self) -> None:
        """Cancel an in-flight positioning loop (but never cancel ourselves)."""
        task = self._position_task
        self._position_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def async_led_on(self) -> bool:
        ok = await self._send_command(BLE_CMD_LED_ON, CLOUD_CMD_LED_ON)
        if ok:
            self.led_state = True
            self.async_set_updated_data(self._build_state())
        return ok

    async def async_led_off(self) -> bool:
        ok = await self._send_command(BLE_CMD_LED_OFF, CLOUD_CMD_LED_OFF)
        if ok:
            self.led_state = False
            self.async_set_updated_data(self._build_state())
        return ok

    async def _send_command(
        self,
        ble_cmd_id: int,
        cloud_control_ident: int,
        target_position: int | None = None,
    ) -> bool:
        """Send a command via BLE first; fall back to cloud if BLE unavailable."""
        if await self._send_ble_command(ble_cmd_id):
            _LOGGER.debug("BLE command confirmed (ack)")
            self._schedule_post_command_refresh(target_position)
            return True
        _LOGGER.debug("BLE unavailable/unconfirmed, falling back to cloud command")
        ok = await self._send_cloud_command(cloud_control_ident)
        if ok:
            self._schedule_post_command_refresh(target_position)
        return ok

    @callback
    def _schedule_post_command_refresh(self, target_position: int | None) -> None:
        if self._post_command_refresh is not None:
            self._post_command_refresh.cancel()
        self._post_command_refresh = self.hass.async_create_task(
            self._async_post_command_refresh(target_position)
        )

    async def _async_post_command_refresh(self, target_position: int | None) -> None:
        """Poll the cloud API briefly after a command to converge state quickly."""
        try:
            for _ in range(10):
                await asyncio.sleep(2)
                info = await self._async_fetch_device_info()
                if info is None:
                    continue
                self._apply_device_info(info, push_update=True)
                if target_position is None or self.door_position == target_position:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            if asyncio.current_task() is self._post_command_refresh:
                self._post_command_refresh = None

    # -----------------------------------------------------------------
    # Cloud command path
    # -----------------------------------------------------------------

    async def _send_cloud_command(self, control_ident: int) -> bool:
        """Send a command via the cloud HTTP gateway."""
        session = async_get_clientsession(self.hass)
        url = f"{CLOUD_GATEWAY_URL}/device/control/{self._device_code}"
        params = {
            "timestamp": int(time.time()),
            "controlIdent": control_ident,
        }
        headers = {
            "Accept-Language": "en",
            "Api-Version": API_VERSION,
            "client-id": "f-linx",
        }
        for _ in range(2):
            token = await self._account.async_get_token(session)
            if not token:
                _LOGGER.error("Cloud command failed: unable to authenticate")
                return False
            headers["Authorization"] = f"Bearer {token}"
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 200:
                            _LOGGER.debug("Cloud command OK (controlIdent=%s)", control_ident)
                            return True
                        msg = data.get("msg", "unknown error")
                        # Re-auth and retry on token/auth errors
                        if "认证" in msg or "token" in msg.lower() or "auth" in msg.lower():
                            _LOGGER.debug("Cloud token expired, re-authenticating")
                            self._account.async_invalidate_token()
                            continue
                        _LOGGER.warning("Cloud command rejected: %s", msg)
                        return False
                    elif resp.status == 401:
                        self._account.async_invalidate_token()
                        continue
                    else:
                        _LOGGER.warning("Cloud command HTTP %s", resp.status)
                        return False
            except aiohttp.ClientError as err:
                _LOGGER.warning("Cloud command error: %s", err)
                return False
        return False

    # -----------------------------------------------------------------
    # REST fallback (used when MQTT is disconnected or stale)
    # -----------------------------------------------------------------

    async def _api_get_device_info(
        self, session: aiohttp.ClientSession
    ) -> dict[str, Any] | None:
        # Bound the 401 retry: invalidate, re-login once, then give up so a
        # persistently rejected session cannot recurse into a login storm.
        for _ in range(2):
            token = await self._account.async_get_token(session)
            if not token:
                return None

            url = f"{API_BASE_URL}/device/deviceInfo/{self._device_code}"
            headers = {
                "api-version": API_VERSION,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            try:
                async with session.post(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 200:
                            return data.get("data", {})
                    elif resp.status == 401:
                        self._account.async_invalidate_token()
                        continue
                    return None
            except aiohttp.ClientError as err:
                _LOGGER.debug("API get device info error: %s", err)
                return None
        return None

    def _apply_device_info(self, info: dict[str, Any], push_update: bool) -> None:
        changed = False

        # deviceInfo carries the opener's BLE name; picking it up here means an
        # entry migrated from pre-3.0 gets an exact BLE match without the user
        # having to re-run the config flow.
        ble_name = info.get(API_KEY_BLE_NAME)
        if ble_name and ble_name != self._ble_name:
            _LOGGER.debug(
                "Cloud reports opener %s for %s", ble_name, self._door_alias
            )
            self._ble_name = ble_name

        for attr in info.get("attributes", []):
            code = attr.get("attributeCode")
            value = attr.get("attributeValue")
            if code == ATTR_DOOR_POSITION and value != self.door_position:
                self.door_position = value
                changed = True
            elif code == ATTR_OPERATED_CYCLES and value != self.operated_cycles:
                self.operated_cycles = value
                changed = True
            elif code == ATTR_LED_ACTUAL:
                new_led_state = value == 0xF0
                if new_led_state != self.led_state:
                    self.led_state = new_led_state
                    changed = True

        firmware_version = info.get("firmwareVersion")
        if firmware_version != self.firmware_version:
            self.firmware_version = firmware_version
            changed = True

        is_online = info.get("onlineState") == 1
        if is_online != self.is_online:
            self.is_online = is_online
            changed = True

        if changed and push_update:
            self.async_set_updated_data(self._build_state())

    async def _async_fetch_device_info(self) -> dict[str, Any] | None:
        return await self._api_get_device_info(async_get_clientsession(self.hass))

    async def async_force_refresh(self) -> None:
        """Force a cloud REST state read, bypassing the MQTT-freshness check.

        Used by the flinx_garage.refresh_state action and the optional periodic
        poll so state can be re-synced on demand when MQTT is unreliable.
        """
        info = await self._async_fetch_device_info()
        if info is None:
            raise HomeAssistantError(
                "Unable to reach the F-LINX cloud API to refresh state"
            )
        self._apply_device_info(info, push_update=True)

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic tick: mostly a fallback when MQTT is stale."""
        # Opportunistically (re)establish BLE — doesn't fail the update if it can't.
        if not self.is_ble_connected and not self._ble_connecting:
            self.hass.async_create_task(self._ensure_ble_connected())

        mqtt_fresh = (
            self.mqtt.is_connected
            and self.last_mqtt_ts
            and time.time() - self.last_mqtt_ts < MQTT_STALE_THRESHOLD
        )

        if mqtt_fresh:
            # MQTT is delivering — nothing more to do, return current state.
            return self._build_state()

        # MQTT is down/stale. Poll the REST API to keep state current.
        _LOGGER.debug("MQTT stale — polling REST API for state")
        info = await self._async_fetch_device_info()

        if info is None:
            # Don't flap entities if we just can't reach the API —
            # UpdateFailed will mark them unavailable after several failures.
            raise UpdateFailed("MQTT and API both unreachable")

        self._apply_device_info(info, push_update=False)

        return self._build_state()

    # -----------------------------------------------------------------
    # Convenience accessors
    # -----------------------------------------------------------------

    @property
    def is_closed(self) -> bool | None:
        if self.door_position is None:
            return None
        return self.door_position == 0

    @property
    def current_cover_position(self) -> int | None:
        return self.door_position

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def async_start(self) -> None:
        """Start MQTT connection and kick off first update."""
        await self.mqtt.connect()

        # Optional unconditional periodic cloud poll (off by default).
        if self._poll_interval > 0:
            _LOGGER.debug(
                "Scheduling periodic cloud poll every %ss", self._poll_interval
            )
            self._poll_unsub = async_track_time_interval(
                self.hass,
                self._async_periodic_poll,
                timedelta(seconds=self._poll_interval),
            )

    async def _async_periodic_poll(self, now: datetime) -> None:
        """Periodic cloud poll, independent of the MQTT-stale fallback."""
        try:
            await self.async_force_refresh()
        except HomeAssistantError as err:
            # Don't let a transient API hiccup bubble out of the timer.
            _LOGGER.debug("Periodic cloud poll failed: %s", err)

    async def async_shutdown(self) -> None:
        """Cancel scheduled work and disconnect MQTT and BLE cleanly."""
        self._closing = True
        await super().async_shutdown()
        if self._poll_unsub is not None:
            self._poll_unsub()
            self._poll_unsub = None
        if self._post_command_refresh is not None:
            self._post_command_refresh.cancel()
        self._cancel_position_task()
        await self.mqtt.disconnect()
        if self._ble_client and self._ble_client.is_connected:
            try:
                await self._ble_client.stop_notify(BLE_NOTIFY_CHAR)
                await self._ble_client.stop_notify(BLE_NOTIFY_CHAR2)
                await self._ble_client.disconnect()
            except BleakError:
                pass
