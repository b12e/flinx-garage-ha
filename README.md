# F-LinX Garage Door

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/b12e/flinx-garage-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/b12e/flinx-garage-ha/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Home Assistant (HACS) integration for F-LinX / Force-Door garage door controllers.

This integration works with [this](https://www.myforcedoor.com/prodetails/49/546.html) USB dongle.

## Features

- **Real-time state** via MQTT - door position, LED status, operation count
- **Local commands** via Bluetooth - works without internet
- **Remote commands** via cloud API - fallback when Bluetooth is not available
- Garage door cover entity (open / close / stop / set position)
- LED light entity (on / off)
- Operation count sensor

Commands are always sent via Bluetooth first. If BLE is unavailable, the integration falls back to the cloud API automatically.

One integration entry covers one F-LinX account, with all of its doors. Add or remove doors later under **Configure → Manage devices**.

### Bluetooth with more than one door

Your account reports which opener belongs to each door, so each door only ever talks to its own opener. With a single door configured, any `Noru_*` / `opener_*` opener in range is used. If a door has no opener reported and more than one door is configured, that door uses the cloud rather than risk sending a command to the wrong door.

### Set position (partial open)

You can drive the door to a specific percentage with the cover position slider or the `cover.set_cover_position` action.

> **Note:** This is **best-effort, software-based positioning.** The controller has no native "open to X%" command, so the integration sends a normal open/close and issues a stop once the live position reaches the target. It therefore lands *close* to the requested percentage (typically within a few percent) rather than exactly on it, and the door visibly moves and then stops. `0%` and `100%` use the dedicated close/open commands and reach the hard limits precisely.

## Installation

### HACS (recommended)

This integration is not in the HACS default list yet, so add it as a custom repository:

1. In Home Assistant, go to **HACS → ⋮ → Custom repositories**
2. Add `https://github.com/b12e/flinx-garage-ha` with category **Integration**
3. Search for **F-LinX Garage Door** in HACS and download it
4. Restart Home Assistant

### Manual

1. Copy `custom_components/flinx_garage/` into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

### Configuration

1. Go to **Settings → Devices & Services → Add Integration → F-LinX Garage Door**
2. Enter your F-LinX account credentials
3. Select which doors to add (all doors on your account are pre-selected)

## Requirements

- Home Assistant 2025.2.0+
- Bluetooth adapter on your HA host (for local BLE commands)
- [F-LinX / Force-Door garage door controller](https://www.myforcedoor.com/prodetails/49/546.html) connected to WiFi
