"""Crypto helpers for F-LINX MQTT and BLE messages.

MQTT messages are AES-128-ECB encrypted with a per-device key, PKCS7 padded.
The last 4 bytes of the plaintext (before padding) are a zlib.adler32 checksum.

BLE commands follow a frame format:
  [55 55] [total_len:2 BE] [01] [AES-ECB encrypted payload] [checksum] [aa aa]
Plaintext for regular commands: [03 06 cmd 00 05 10 cmd] (7 bytes, PKCS7 padded)
Plaintext for auth: [03 06 07 0e 03 timestamp:4BE 0b MD5(devKey):16] (25 bytes)
"""

from __future__ import annotations

import hashlib
import struct
import time
import zlib

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

BLOCK_SIZE = 16

# BLE command IDs (plaintext byte values)
BLE_CMD_OPEN = 0x01
BLE_CMD_CLOSE = 0x02
BLE_CMD_STOP = 0x03
BLE_CMD_LED_ON = 0x05
BLE_CMD_LED_OFF = 0x06


def build_ble_command(cmd_id: int, dev_key: bytes) -> bytes:
    """Build an encrypted BLE command frame for a given command.

    Mirrors the app's ``buildData(positional=[0,5], data=[0x10,cmd], sep=2*cmd)``:
    plaintext = ``03 06 <sep> 00 05 10 <cmd>`` where ``sep`` is ``2 * cmd``
    (open 1→2, close 2→4, stop 3→6, LED-on 5→10, LED-off 6→12). The earlier
    implementation put ``cmd`` in the ``sep`` slot, which the device rejected.
    """
    sep = (cmd_id * 2) & 0xFF
    plaintext = bytes([0x03, 0x06, sep, 0x00, 0x05, 0x10, cmd_id])
    return _ble_frame(plaintext, dev_key)


def build_ble_auth(dev_key: bytes) -> bytes:
    """Build the BLE auth frame (sent once per connection).

    Mirrors the app's ``getAuthNew`` → ``buildData(positional=[0x0e,0x03],
    auth=true, sep=7)``: plaintext = ``03 06 07 0E 03`` + timestamp (4-byte BE
    epoch seconds) + ``MD5(devKey)`` (16 bytes). There is no separator byte
    between the timestamp and the MD5 (the previous ``0x0B`` was spurious).
    """
    ts = int(time.time())
    md5 = hashlib.md5(dev_key).digest()
    plaintext = (
        bytes([0x03, 0x06, 0x07, 0x0E, 0x03])
        + struct.pack(">I", ts)
        + md5
    )
    return _ble_frame(plaintext, dev_key)


def _ble_frame(plaintext: bytes, dev_key: bytes) -> bytes:
    """Encrypt plaintext and wrap in BLE frame with checksum."""
    encrypted = encrypt(plaintext, dev_key)
    payload = b'\x01' + encrypted
    total_len = 2 + 2 + len(payload) + 1 + 2  # header + len + payload + checksum + footer
    frame = b'\x55\x55' + struct.pack(">H", total_len) + payload
    checksum = sum(frame) & 0xFF
    return frame + bytes([checksum]) + b'\xAA\xAA'


def sign(body: bytes) -> bytes:
    """Return 4-byte big-endian Adler32 of ``body``."""
    return struct.pack(">I", zlib.adler32(body) & 0xFFFFFFFF)


def verify_sign(plaintext: bytes) -> bool:
    """Check that ``plaintext[-4:]`` is the Adler32 of ``plaintext[:-4]``."""
    if len(plaintext) < 4:
        return False
    return plaintext[-4:] == sign(plaintext[:-4])


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding."""
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(pad(plaintext, BLOCK_SIZE))


def decrypt(ciphertext: bytes, key: bytes) -> bytes | None:
    """AES-128-ECB decrypt and strip PKCS7 padding. Returns None on error."""
    if len(ciphertext) == 0 or len(ciphertext) % BLOCK_SIZE != 0:
        return None
    try:
        cipher = AES.new(key, AES.MODE_ECB)
        dec = cipher.decrypt(ciphertext)
    except ValueError:
        return None
    # Strip PKCS7 padding if valid, otherwise return as-is (some messages
    # are exactly block-aligned with a full block of padding byte n).
    try:
        return unpad(dec, BLOCK_SIZE)
    except ValueError:
        return dec
