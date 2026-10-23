# F-LinX Garage Door

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/b12e/flinx-garage-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/b12e/flinx-garage-ha/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Usage counter](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://raw.githubusercontent.com/b12e/hacs-b12e-usage-stats/refs/heads/main/usage.json&query=$.integrations.flinx_garage.total_usage) 

Home Assistant (HACS) integration for F-LinX-controlled garage doors.

## Features

- **Real-time state** via MQTT - door position, LED status, operation count
- **Local commands** via Bluetooth - works without internet
- **Remote commands** via cloud API - fallback when Bluetooth is not available
- Garage door cover entity (open / close / stop / set position)
- **Native partial open** - a button that drives the door to its partial open height, where the opener stops by itself
- **Partial open height** - choose that height from Home Assistant (0-160 cm, the same presets as the F-LinX app)
- LED light entity (on / off), and its **light off delay** (1-9 minutes)
- Operation count sensor

By default, commands are sent via Bluetooth first, falling back to the cloud API automatically when BLE is unavailable. You can change this under **Configure → Connection mode** (see below).

One integration entry covers one F-LinX account, with all of its doors. Add or remove doors later under **Configure → Manage devices**.

### Connection mode

**Configure → Connection mode** decides which connections the integration may use. It applies to every door on the account.

| Mode | Commands | State |
| --- | --- | --- |
| **Bluetooth only** | Bluetooth only; a command fails if the opener is out of range | Bluetooth command replies only |
| **Bluetooth preferred, cloud fallback** (default) | Bluetooth, then the cloud | MQTT push, cloud poll and Bluetooth |
| **Cloud preferred, Bluetooth fallback** | The cloud, then Bluetooth | MQTT push, cloud poll and Bluetooth |
| **Cloud only** | The cloud only; Bluetooth is switched off entirely | MQTT push and cloud poll |

> **Bluetooth only is fully local** - Home Assistant never contacts the F-LinX cloud, so there is no MQTT push and the periodic cloud poll is switched off and hidden. The door's position is then only updated by commands Home Assistant sends itself: if the door is opened another way - an RF remote, the wall button, the F-LinX app or the auto-close timer - Home Assistant will not see it, and the state stays stale until it next sends a command. The position is also unknown after a restart until the first command.

**Cloud only** frees your Bluetooth adapter's or proxy's connection slot, at the cost of the local position stream, which makes set position less precise.

### Bluetooth with more than one door

Your account reports which opener belongs to each door, so each door only ever talks to its own opener. With a single door configured, any `Noru_*` / `opener_*` opener in range is used. If a door has no opener reported and more than one door is configured, that door uses the cloud rather than risk sending a command to the wrong door.

### Native partial open

The **Partial open** button sends the opener's own partial-open command, the same one as the **Partial** button in the F-LinX app. The door drives to the partial open height and stops by itself, so nothing in Home Assistant has to watch the position and send a stop. When the door is already further open, it closes down to that height instead, so it lands there from any position.

The same command is available as an action:

```yaml
action: flinx_garage.partial_open
target:
  entity_id: cover.garage_door
```

### Partial open height and light off delay

Two settings stored in the opener can be changed from Home Assistant, under the device's **Configuration** section:

| Entity | Values | Notes |
| --- | --- | --- |
| **Partial open height** | 0, 2, 4, 8, 12, 20, 40, 60, 80, 100, 120, 140, 160 cm | The heights the F-LinX app offers. Changing it does not move the door. With Home Assistant set to US customary units the options read in inches, with the app's cm alongside (e.g. `24 in (60 cm)`). |
| **Light off delay** | 1-9 minutes | How long the light stays on after the door stops. |

Both are stored in the opener itself, so they are shared with the F-LinX app and with a remote's partial button: whatever you set last, from anywhere, is what the door uses. How far a given height opens your door (as a percentage) depends on the door - on one door 60 cm is 31% and 80 cm is 42%.

Like commands, settings are written over the connections your connection mode allows - Bluetooth, the cloud, or one with the other as the fallback - and are only shown as changed once the opener confirms them.

### Set position (any percentage)

You can drive the door to a specific percentage with the cover position slider or the `cover.set_cover_position` action. This leaves the partial open height alone.

> **Note:** This is **best-effort, software-based positioning.** The controller has no native "open to X%" command, so the integration sends a normal open/close and issues a stop once the live position reaches the target. It therefore lands *close* to the requested percentage (typically within a few percent) rather than exactly on it, and the door visibly moves and then stops. `0%` and `100%` use the dedicated close/open commands and reach the hard limits precisely.

### Dashboard shortcuts

A cover entity has no presets of its own, but the dashboard can put one-tap positions next to it:

- **Partial open:** add the **Partial open** button entity to a card, or use it as a tile card's tap action.
- **Favorite positions:** the tile card's **Cover favorite positions** feature (`type: cover-position-favorite`) shows a list of favorite positions for the cover. Edit them in the cover's **More info** dialog by pressing and holding a favorite. They use set position above, so they work for any percentage and leave the partial open height alone.

```yaml
type: tile
entity: cover.garage_door
features:
  - type: cover-open-close
  - type: cover-position-favorite
```

## Installation

### Home Assistant Marketplace (formerly HACS)
1. [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=b12e&repository=flinx-garage-ha&category=integration)
2. Search for "F-LinX Garage Door" and click Download. Follow the on-screen instructions.
3. Restart Home Assistant
4. Go to Settings → Devices & services → + Add Integration → F-LinX Garage Door
5. Follow the on-screen instructions


### Manual

1. Copy `custom_components/flinx_garage/` into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

### Configuration

1. Go to **Settings → Devices & Services → Add Integration → F-LinX Garage Door**
2. Enter your F-LinX account credentials
3. Select which doors to add (all doors on your account are pre-selected)

## Requirements

- Home Assistant 2025.2.0+
- Bluetooth adapter on your HA host (for local BLE commands) or an ESPHome based Bluetooth Proxy in range of your garage door
- A garage door that works with the F-LinX app - either directly or via the USB dongle ([F-LinX / Force-Door garage door controller](https://www.myforcedoor.com/prodetails/49/546.html)) connected to WiFi
