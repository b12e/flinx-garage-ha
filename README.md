# F-LinX Garage Door

Home Assistant (HACS) integration for F-LinX / Force-Door garage door controllers.

This integration works with [this](https://www.myforcedoor.com/prodetails/49/546.html) USB dongle.

## Features

- **Real-time state** via MQTT — door position, LED status, operation count
- **Local commands** via Bluetooth — works without internet
- **Remote commands** via cloud API — fallback when Bluetooth is not available
- Garage door cover entity (open / close / stop)
- LED light entity (on / off)
- Operation count sensor

Commands are always sent via Bluetooth first. If BLE is unavailable, the integration falls back to the cloud API automatically.

## Installation

1. Install via [HACS](https://hacs.xyz/) — add this repo as a custom repository
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration → F-LinX Garage Door**
4. Enter your F-LinX account credentials
5. Select your device (auto-detected from your account)

## Requirements

- Home Assistant 2024.1.0+
- Bluetooth adapter on your HA host (for local BLE commands)
- [F-LinX / Force-Door garage door controller](https://www.myforcedoor.com/prodetails/49/546.html) connected to WiFi
