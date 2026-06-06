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
from datetime import timedelta
from typing import Any

import aiohttp
from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_BASE_URL,
    API_VERSION,
    ATTR_DOOR_POSITION,
    ATTR_LED_ACTUAL,
    ATTR_OPERATED_CYCLES,
    BLE_ACK_TIMEOUT,
    BLE_CONNECT_TIMEOUT,
    BLE_NAME_PREFIX,
    BLE_NOTIFY_CHAR,
    BLE_NOTIFY_CHAR2,
    BLE_WRITE_CHAR,
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
)
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


class FlinxGarageCoordinator(DataUpdateCoordinator):
    """Hybrid MQTT (state) + BLE (commands) coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        device_code: str,
        dev_key: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Light polling cadence — MQTT push is primary; polling is a fallback
            # in case MQTT disconnects or we miss messages.
            update_interval=timedelta(seconds=DEFAULT_FALLBACK_SCAN_INTERVAL),
        )
        self._username = username
        self._password = password
        self._device_code = device_code
        self._dev_key = dev_key

        self._token: str | None = None
        self._ble_client: BleakClient | None = None
        self._ble_connecting = False
        self._command_lock = asyncio.Lock()
        self._last_notification: bytes | None = None
        self._post_command_refresh: asyncio.Task[None] | None = None

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
        """Find the Noru_* device among discovered BLE advertisements.

        Logs why no connection is attempted so we can distinguish "device not
        seen at all", "seen but not connectable" (a passive-only proxy), and
        "seen and connectable".
        """
        connectable = list(
            bluetooth.async_discovered_service_info(self.hass, connectable=True)
        )
        for si in connectable:
            if si.name and si.name.startswith(BLE_NAME_PREFIX):
                _LOGGER.debug(
                    "BLE: found connectable %s (%s) rssi=%s via %s",
                    si.name, si.address, si.rssi, si.source,
                )
                return si.device

        all_si = list(
            bluetooth.async_discovered_service_info(self.hass, connectable=False)
        )
        matches = [s for s in all_si if s.name and s.name.startswith(BLE_NAME_PREFIX)]
        if matches:
            _LOGGER.warning(
                "BLE: %s* seen but not connectable (proxy may be passive-only): %s",
                BLE_NAME_PREFIX,
                ", ".join(
                    f"{s.name}/{s.address} rssi={s.rssi} src={s.source}" for s in matches
                ),
            )
        else:
            _LOGGER.debug(
                "BLE: no %s* device discovered (%d connectable, %d total adverts)",
                BLE_NAME_PREFIX, len(connectable), len(all_si),
            )
        return None

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
        _LOGGER.debug("BLE disconnected — will reconnect in 10s")
        self.is_ble_connected = False
        self._ble_client = None
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
                await self._ble_client.write_gatt_char(BLE_WRITE_CHAR, cmd_frame)
            except (BleakError, AttributeError) as err:
                _LOGGER.debug("BLE command failed: %s", err)
                self.is_ble_connected = False
                self._ble_client = None
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
        return await self._send_command(
            BLE_CMD_OPEN, CLOUD_CMD_OPEN, target_position=DOOR_STATE_OPEN
        )

    async def async_door_close(self) -> bool:
        return await self._send_command(
            BLE_CMD_CLOSE, CLOUD_CMD_CLOSE, target_position=DOOR_STATE_CLOSED
        )

    async def async_door_stop(self) -> bool:
        return await self._send_command(BLE_CMD_STOP, CLOUD_CMD_STOP, target_position=None)

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
        async with aiohttp.ClientSession() as session:
            url = f"{CLOUD_GATEWAY_URL}/device/control/{self._device_code}"
            params = {
                "timestamp": int(time.time()),
                "controlIdent": control_ident,
            }
            headers = {
                "Accept-Language": "en",
                "Api-Version": API_VERSION,
                "Authorization": f"Bearer {self._token}",
                "client-id": "f-linx",
            }
            for attempt in range(2):
                if not self._token and not await self._api_login(session):
                    _LOGGER.error("Cloud command failed: unable to authenticate")
                    return False
                headers["Authorization"] = f"Bearer {self._token}"
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
                                self._token = None
                                continue
                            _LOGGER.warning("Cloud command rejected: %s", msg)
                            return False
                        elif resp.status == 401:
                            self._token = None
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

    async def _api_login(self, session: aiohttp.ClientSession) -> bool:
        url = f"{API_BASE_URL}/app/user/login"
        headers = {"api-version": API_VERSION, "Content-Type": "application/json"}
        payload = {"username": self._username, "password": self._password}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200:
                        self._token = data["data"]["token"]
                        return True
                _LOGGER.debug("API login failed: status=%s", resp.status)
                return False
        except aiohttp.ClientError as err:
            _LOGGER.debug("API login error: %s", err)
            return False

    async def _api_get_device_info(
        self, session: aiohttp.ClientSession
    ) -> dict[str, Any] | None:
        if not self._token and not await self._api_login(session):
            return None

        url = f"{API_BASE_URL}/device/deviceInfo/{self._device_code}"
        headers = {
            "api-version": API_VERSION,
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            async with session.post(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200:
                        return data.get("data", {})
                elif resp.status == 401:
                    self._token = None
                    if await self._api_login(session):
                        return await self._api_get_device_info(session)
                return None
        except aiohttp.ClientError as err:
            _LOGGER.debug("API get device info error: %s", err)
            return None

    def _apply_device_info(self, info: dict[str, Any], push_update: bool) -> None:
        changed = False

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
        async with aiohttp.ClientSession() as session:
            return await self._api_get_device_info(session)

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

    async def async_shutdown(self) -> None:
        """Disconnect MQTT and BLE cleanly."""
        if self._post_command_refresh is not None:
            self._post_command_refresh.cancel()
        await self.mqtt.disconnect()
        if self._ble_client and self._ble_client.is_connected:
            try:
                await self._ble_client.stop_notify(BLE_NOTIFY_CHAR)
                await self._ble_client.stop_notify(BLE_NOTIFY_CHAR2)
                await self._ble_client.disconnect()
            except BleakError:
                pass
