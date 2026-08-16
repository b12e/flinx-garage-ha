"""MQTT client for F-LINX Garage Door cloud state updates.

Connects to the F-LINX broker with shared app credentials and subscribes to
the device's attr/up topic for real-time state. Decodes the binary TLV
attribute report and delivers parsed attributes via a callback.

Command publishing is NOT supported — the broker ACL blocks ``bd-app``
publishes to /service/down. Commands are sent via BLE (see coordinator).
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from typing import Any

import paho.mqtt.client as mqtt

from .const import (
    ATTR_SIZE_2B,
    ATTR_SIZE_8B,
    MQTT_BROKER,
    MQTT_KEEPALIVE,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_TOPIC_ATTR_UP,
    MQTT_TOPIC_WILDCARD,
    MQTT_USERNAME,
)
from .crypto import decrypt

_LOGGER = logging.getLogger(__name__)

# Callback signature: called with {attr_code: value, ...} on each decoded attr/up.
AttrCallback = Callable[[dict[int, Any]], Awaitable[None] | None]


# The 02 02 marker that precedes the attribute TLVs. MQTT reports carry
# seq/timestamp/motor ahead of it; the BLE reply to a command carries the same
# TLVs after a bare 3-byte header and none of that.
ATTR_MARKER = b"\x02\x02"
MQTT_MARKER_OFFSET = 9
BLE_MARKER_OFFSET = 3
# Bytes of checksum/trailer after the last TLV, per layout.
TRAILER_LEN = {MQTT_MARKER_OFFSET: 4, BLE_MARKER_OFFSET: 3}


def parse_attr_report(data: bytes) -> dict[int, Any] | None:
    """Parse a decrypted attribute report into an attribute dict.

    Two layouts carry the same TLVs:

    - MQTT attr/up: ``03 TT [seq:1] [ts:4] [motor:2] 02 02 [TLV ...] [adler32:4]``
    - BLE command reply: ``03 TT [seq:1] 02 02 [TLV ...] [trailer:3]``

    Byte 0 is always 0x03; byte 1 (TT) is a message-type/flags byte observed as
    0x00, 0x04 and 0x1f for the same report shape — so the marker's position is
    what identifies the layout, rather than a fixed prefix.

    TLV entries: 2-byte attribute code (big-endian, 0x27XX) followed by
    a variable-length value (1 byte by default; 2 or 8 for known codes).

    Returns a dict mapping attributeCode (int) → value, or None if the
    message doesn't look like an attribute report.
    """
    if len(data) < 12 or data[0] != 0x03:
        return None

    for marker_offset in (MQTT_MARKER_OFFSET, BLE_MARKER_OFFSET):
        if data[marker_offset : marker_offset + 2] == ATTR_MARKER:
            break
    else:
        return None

    if data[1] != 0x00:
        _LOGGER.debug("attr report message-type byte = 0x%02x", data[1])

    result: dict[int, Any] = {"_seq": data[2]}
    if marker_offset == MQTT_MARKER_OFFSET:
        result["_ts"] = struct.unpack(">I", data[3:7])[0]
        result["_motor"] = struct.unpack(">H", data[7:9])[0]

    # Attribute TLVs start after the marker and stop before the trailer.
    end = len(data) - TRAILER_LEN[marker_offset]
    i = marker_offset + 2
    while i + 2 <= end:
        # Peek at 2-byte code
        code = struct.unpack(">H", data[i:i+2])[0]
        if code < 9993 or code > 10020:
            # Not a valid code — either we've walked off the attr region
            # or this is padding/trailer. Stop parsing.
            break

        attr_code = code  # store as 0x27XX decimal equivalent
        i += 2

        if attr_code in ATTR_SIZE_8B:
            size = 8
        elif attr_code in ATTR_SIZE_2B:
            size = 2
        else:
            # Unknown-size attributes default to 1 byte. If the next 2 bytes
            # form another valid code, assume 1 byte; otherwise scan forward.
            if i + 3 <= end:
                next_code = struct.unpack(">H", data[i+1:i+3])[0]
                if 9993 <= next_code <= 10020:
                    size = 1
                else:
                    # Could be 2-byte value
                    next_code_2b = struct.unpack(">H", data[i+2:i+4])[0] if i + 4 <= end else 0
                    size = 2 if 9993 <= next_code_2b <= 10020 else 1
            else:
                size = 1

        if i + size > end:
            break

        raw = data[i:i+size]
        if size == 1:
            value = raw[0]
        elif size == 2:
            value = struct.unpack(">H", raw)[0]
        elif size == 4:
            value = struct.unpack(">I", raw)[0]
        else:
            value = raw.hex()

        result[attr_code] = value
        i += size

    return result


class FlinxMqttClient:
    """Async wrapper around paho-mqtt for F-LINX device state subscription."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        device_code: str,
        dev_key_hex: str,
        on_attrs: AttrCallback,
    ) -> None:
        self._loop = loop
        self._device_code = device_code
        self._dev_key = bytes.fromhex(dev_key_hex)
        self._on_attrs = on_attrs

        self._client = mqtt.Client(
            client_id=f"ha_flinx_{device_code}_{int(time.time())}",
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        self._connected = False
        self._last_message_ts: float = 0.0

    @property
    def is_connected(self) -> bool:
        """Return True if MQTT is connected and receiving messages recently."""
        return self._connected

    @property
    def last_message_ts(self) -> float:
        """Timestamp (epoch seconds) of the most recent attr/up message."""
        return self._last_message_ts

    async def connect(self) -> None:
        """Connect and start the network loop in paho's internal thread."""
        _LOGGER.debug("Connecting to MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
        try:
            await self._loop.run_in_executor(
                None, self._client.connect, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE
            )
        except OSError as err:
            _LOGGER.warning("MQTT connect failed: %s", err)
            return
        self._client.loop_start()

    async def disconnect(self) -> None:
        """Disconnect and stop the network loop."""
        _LOGGER.debug("Disconnecting MQTT")
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        self._connected = False

    # --- paho callbacks (run in paho's thread) ---

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        rc = int(reason_code.value) if hasattr(reason_code, "value") else int(reason_code)
        _LOGGER.debug("MQTT connected (rc=%d)", rc)
        if rc != 0:
            return
        self._connected = True
        topic = MQTT_TOPIC_WILDCARD.format(device_code=self._device_code)
        client.subscribe(topic, qos=1)
        _LOGGER.debug("MQTT subscribed to %s", topic)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        _LOGGER.debug("MQTT disconnected (rc=%s)", reason_code)
        self._connected = False

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        if not msg.topic.endswith("/attr/up"):
            # Only attr/up carries attribute state we care about.
            return

        plaintext = decrypt(msg.payload, self._dev_key)
        if plaintext is None:
            _LOGGER.debug("MQTT: failed to decrypt on %s", msg.topic)
            return

        attrs = parse_attr_report(plaintext)
        if attrs is None:
            _LOGGER.debug("MQTT: unparseable plaintext: %s", plaintext.hex())
            return

        self._last_message_ts = time.time()
        _LOGGER.debug("MQTT attr/up parsed: %s", attrs)

        # Dispatch to HA event loop
        result = self._on_attrs(attrs)
        if asyncio.iscoroutine(result):
            asyncio.run_coroutine_threadsafe(result, self._loop)
