"""Constants for F-LINX Garage Door integration."""

DOMAIN = "flinx_garage"

# HTTP REST API
API_BASE_URL = "https://api.bit-door.com"
API_VERSION = "2.0.0"

# MQTT broker (cloud, shared app credentials)
MQTT_BROKER = "conn.bit-door.com"
MQTT_PORT = 1883
MQTT_USERNAME = "bd-app"
MQTT_PASSWORD = "GwZEJ9R8RNi3yP4c"
MQTT_KEEPALIVE = 60

# MQTT topic template — {device_code} is the 16-hex-char deviceCode
# Subscribes: attr/up (state reports), service/up (heartbeats),
#             service/down (commands from cloud — read-only for us).
# Publishes to service/down are ACL-blocked by the broker.
MQTT_TOPIC_ATTR_UP = "/thing/dongle/{device_code}/attr/up"
MQTT_TOPIC_SERVICE_UP = "/thing/dongle/{device_code}/service/up"
MQTT_TOPIC_SERVICE_DOWN = "/thing/dongle/{device_code}/service/down"
MQTT_TOPIC_WILDCARD = "/thing/dongle/{device_code}/#"

# BLE configuration
BLE_NAME_PREFIX = "Noru_"  # Match any Noru_* device (discovered via HA Bluetooth)
BLE_WRITE_CHAR = "02362a10-cf3a-11e1-efdc-000215d5c51b"
BLE_NOTIFY_CHAR = "02362a11-cf3a-11e1-efdc-000215d5c51b"
BLE_NOTIFY_CHAR2 = "02367a11-cf3a-11e1-efdc-000215d5c51b"

# BLE command generation is in crypto.py — commands are built dynamically from
# the per-device devKey using AES-128-ECB encryption. No hardcoded blobs needed.

# Attribute codes observed in MQTT attr/up reports (0x27XX big-endian)
ATTR_DOOR_CONTROL = 10001       # Door control state
ATTR_LED_TIMER = 10002
ATTR_AUTO_CLOSE_DELAY = 10003
ATTR_AUTO_CLOSE_ENABLED = 10004
ATTR_LED_STATE = 10005          # LED state (0=off, 1=on) — real-time via MQTT
ATTR_OPERATED_CYCLES = 10006    # 2-byte cumulative counter
ATTR_MOTOR_BASELINE = 10010     # 2-byte motor force baseline
ATTR_DOOR_POSITION = 10012      # 0-100% door position — PRIMARY state
ATTR_DEVICE_ID = 10014          # 8-byte device ID (informational)

# Known 2-byte attrs (vs default 1-byte)
ATTR_SIZE_2B = {ATTR_OPERATED_CYCLES, ATTR_MOTOR_BASELINE}
ATTR_SIZE_8B = {ATTR_DEVICE_ID}

# Door states
DOOR_STATE_CLOSED = 0
DOOR_STATE_OPEN = 100

# Config entry keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_CODE = "device_code"
CONF_DEV_KEY = "dev_key"
CONF_DOOR_ALIAS = "door_alias"

# Cloud command controlIdent values
# (Smi-decoded from Dart ARM64 assembly: raw 0x200N >> 1)
CLOUD_CMD_OPEN = 4097
CLOUD_CMD_CLOSE = 4098
CLOUD_CMD_STOP = 4099
CLOUD_CMD_PARTIAL = 4100    # Pedestrian/partial open (~20cm)
CLOUD_CMD_LED_ON = 4101
CLOUD_CMD_LED_OFF = 4102

# Cloud command gateway URL (different from api.bit-door.com)
CLOUD_GATEWAY_URL = "https://conn.bit-door.com"

# Fallback polling interval when MQTT is not connected
DEFAULT_FALLBACK_SCAN_INTERVAL = 60  # seconds

# How long to assume MQTT is healthy after last message before falling back to polling
MQTT_STALE_THRESHOLD = 30  # seconds
