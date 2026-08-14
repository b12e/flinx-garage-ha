# F-LinX Garage Door

Home Assistant (HACS) integration for F-LinX / Force-Door garage door controllers.

This integration works with [this](https://www.myforcedoor.com/prodetails/49/546.html) USB dongle.

## Features

- **Real-time state** via MQTT — door position, LED status, operation count
- **Local commands** via Bluetooth — works without internet
- **Remote commands** via cloud API — fallback when Bluetooth is not available
- Garage door cover entity (open / close / stop / set position)
- LED light entity (on / off)
- Operation count sensor

Commands are always sent via Bluetooth first. If BLE is unavailable, the integration falls back to the cloud API automatically.

One integration entry covers one F-LinX account, with all of its doors. Add or remove doors later under **Configure → Manage devices**.

### Bluetooth with more than one door

A Bluetooth advertisement doesn't say which door it belongs to. With a single door that's fine — any `Noru_*` / `opener_*` opener in range must be it. With several, the integration only uses Bluetooth for a door it can identify: either the opener's name ends in that door's device code, or you point the door at its opener under **Configure → Bluetooth**. Unidentified doors use the cloud, rather than risk sending a command to the wrong door.

### Set position (partial open)

You can drive the door to a specific percentage with the cover position slider or the `cover.set_cover_position` action.

> **Note:** This is **best-effort, software-based positioning.** The controller has no native "open to X%" command, so the integration sends a normal open/close and issues a stop once the live position reaches the target. It therefore lands *close* to the requested percentage (typically within a few percent) rather than exactly on it, and the door visibly moves and then stops. `0%` and `100%` use the dedicated close/open commands and reach the hard limits precisely.

## Installation

1. Install via [HACS](https://hacs.xyz/) — add this repo as a custom repository
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration → F-LinX Garage Door**
4. Enter your F-LinX account credentials
5. Select which doors to add (all devices on your account are pre-selected)

## Requirements

- Home Assistant 2025.2.0+
- Bluetooth adapter on your HA host (for local BLE commands)
- [F-LinX / Force-Door garage door controller](https://www.myforcedoor.com/prodetails/49/546.html) connected to WiFi
