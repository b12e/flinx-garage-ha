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
    ATTR_LED_STATE,
    ATTR_OPERATED_CYCLES,
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

        led = attrs.get(ATTR_LED_STATE)
        if led is not None:
            new_led = bool(led)
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
            for _ in range(50):
                await asyncio.sleep(0.1)
                if self.is_ble_connected:
                    return True
            return False

        self._ble_connecting = True
        try:
            ble_device = None
            for service_info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            ):
                if service_info.name and service_info.name.startswith(BLE_NAME_PREFIX):
                    ble_device = service_info.device
                    _LOGGER.debug(
                        "Found BLE device %s (%s)",
                        service_info.name,
                        ble_device.address,
                    )
                    break

            if ble_device is None:
                _LOGGER.debug("No %s* BLE device in range", BLE_NAME_PREFIX)
                return False

            self._ble_client = await establish_connection(
                BleakClient,
                ble_device,
                ble_device.name or "flinx",
                disconnected_callback=self._on_ble_disconnect,
            )
            # Ensure GATT services are resolved before subscribing
            if not self._ble_client.services:
                await self._ble_client.get_services()
            await self._ble_client.start_notify(BLE_NOTIFY_CHAR, self._ble_notification)
            await self._ble_client.start_notify(BLE_NOTIFY_CHAR2, self._ble_notification)

            self.is_ble_connected = True
            _LOGGER.debug("BLE connected")
            return True

        except BleakError as err:
            _LOGGER.warning("BLE connection failed: %s", err)
            return False
        finally:
            self._ble_connecting = False

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        _LOGGER.debug("BLE disconnected")
        self.is_ble_connected = False
        self._ble_client = None

    async def _send_ble_command(self, ble_cmd_id: int) -> bool:
        dev_key = bytes.fromhex(self._dev_key)
        async with self._command_lock:
            if not await self._ensure_ble_connected():
                _LOGGER.debug("BLE not available")
                return False
            if self._ble_client is None:
                return False
            try:
                auth_frame = build_ble_auth(dev_key)
                cmd_frame = build_ble_command(ble_cmd_id, dev_key)
                await self._ble_client.write_gatt_char(BLE_WRITE_CHAR, auth_frame)
                await asyncio.sleep(0.05)
                await self._ble_client.write_gatt_char(BLE_WRITE_CHAR, cmd_frame)
                return True
            except (BleakError, AttributeError) as err:
                _LOGGER.warning("BLE command failed: %s", err)
                self.is_ble_connected = False
                self._ble_client = None
                return False

    async def async_door_open(self) -> bool:
        return await self._send_command(BLE_CMD_OPEN, CLOUD_CMD_OPEN)

    async def async_door_close(self) -> bool:
        return await self._send_command(BLE_CMD_CLOSE, CLOUD_CMD_CLOSE)

    async def async_door_stop(self) -> bool:
        return await self._send_command(BLE_CMD_STOP, CLOUD_CMD_STOP)

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
        self, ble_cmd_id: int, cloud_control_ident: int
    ) -> bool:
        """Send a command via BLE first; fall back to cloud if BLE unavailable."""
        if await self._send_ble_command(ble_cmd_id):
            return True
        _LOGGER.debug("BLE unavailable, falling back to cloud command")
        return await self._send_cloud_command(cloud_control_ident)

    # -----------------------------------------------------------------
    # Cloud command path
    # -----------------------------------------------------------------

    async def _send_cloud_command(self, control_ident: int) -> bool:
        """Send a command via the cloud HTTP gateway."""
        async with aiohttp.ClientSession() as session:
            if not self._token and not await self._api_login(session):
                _LOGGER.error("Cloud command failed: unable to authenticate")
                return False

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
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 200:
                            _LOGGER.debug("Cloud command OK (controlIdent=%s)", control_ident)
                            return True
                        msg = data.get("msg", "unknown error")
                        _LOGGER.warning("Cloud command rejected: %s", msg)
                        # Re-auth on token expiry
                        if "token" in msg.lower() or "auth" in msg.lower():
                            self._token = None
                        return False
                    elif resp.status == 401:
                        self._token = None
                        _LOGGER.debug("Cloud command 401 — re-authenticating")
                        if await self._api_login(session):
                            return await self._send_cloud_command(control_ident)
                        return False
                    else:
                        _LOGGER.warning("Cloud command HTTP %s", resp.status)
                        return False
            except aiohttp.ClientError as err:
                _LOGGER.warning("Cloud command error: %s", err)
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
        async with aiohttp.ClientSession() as session:
            info = await self._api_get_device_info(session)

        if info is None:
            # Don't flap entities if we just can't reach the API —
            # UpdateFailed will mark them unavailable after several failures.
            raise UpdateFailed("MQTT and API both unreachable")

        for attr in info.get("attributes", []):
            code = attr.get("attributeCode")
            value = attr.get("attributeValue")
            if code == ATTR_DOOR_POSITION:
                self.door_position = value
            elif code == ATTR_OPERATED_CYCLES:
                self.operated_cycles = value
            elif code == ATTR_LED_STATE:
                # Only update from API if BLE hasn't set it optimistically — the
                # LED attribute in the API response sometimes lags BLE writes.
                if self.led_state is None:
                    self.led_state = bool(value)
        self.firmware_version = info.get("firmwareVersion")
        self.is_online = info.get("onlineState") == 1

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
        await self.mqtt.disconnect()
        if self._ble_client and self._ble_client.is_connected:
            try:
                await self._ble_client.stop_notify(BLE_NOTIFY_CHAR)
                await self._ble_client.stop_notify(BLE_NOTIFY_CHAR2)
                await self._ble_client.disconnect()
            except BleakError:
                pass
