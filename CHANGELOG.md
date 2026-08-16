# Changelog

## 3.0.0

### Added
- **Local state over Bluetooth** - the controller's reply to every Bluetooth command carries the same attribute report MQTT sends, so door position, LED and the cycle counter now update from Bluetooth as well. State no longer depends on the controller's WiFi holding up: when it drops off, MQTT and the cloud both go quiet, and previously the door could sit at 17% while Home Assistant still said "Closed" for the best part of a minute.
- **Set position uses those replies too** - the positioning loop reads the live local position while the door moves, leads the stop by 0.4s instead of 1.3s when readings are local, and corrects once if the door didn't land within tolerance. The stop's own reply reports where it settled.
- **Account-based setup** - log in once; all devices on your account are listed and added in a single integration entry.
- **Single API session** - all devices now share one Bit Door session, allowing multiple devices to work using the same account.
- **Manage devices** option - add or remove doors later without re-adding the integration.
- Bluetooth discovery: an F-LinX opener in range now offers to set the integration up.

### Changed
- Existing setups upgrade in place: pre-3.0 entries are converted to the new account format automatically, keeping their entity IDs, names, areas and history. Two entries for the same account are merged into one.
- BLE discovery now also matches `opener_*` device names (in addition to `Noru_*`).
- Each door now only talks to its own opener, matched on the `bluetoothName` the cloud reports for it. With several doors configured and no opener reported for one, that door uses the cloud rather than risk commanding the wrong one.
- Minimum Home Assistant version is now 2025.2.0.

### Fixed
- State no longer jumps backwards when a late report arrives. MQTT has been observed delivering a report up to 26 seconds after the moment it describes, which could overwrite a newer reading: a door being opened from 9% to 51% showed as `Closing 9%`, then `Open 9%`, then `Opening 51%`, having only ever opened. Reports are now ordered by the timestamp the controller stamps them with, not by when they arrive, and the undated REST snapshot is only believed when nothing dated is recent.
- Opening/closing is no longer inferred from a single reading. The controller's own position readings jitter (a real close reported 45, 42, 45, 40), and over Bluetooth it reports several times a second, so every blip used to flip the state. Direction now needs 4% of travel back from the furthest point reached, and a reading that arrives after a gap is treated as a resync rather than as movement.
- A Bluetooth command that the opener never answers no longer fails the action with `Timeout waiting for BluetoothGATTWriteResponse ... after 30.0s`. Writes are bounded at 5 seconds and fall back to the cloud, and a proxy's own transport errors are handled rather than raised at whoever pressed the button.
- A command the cloud refuses is now retried over Bluetooth once the opener is connected, instead of being lost. The door dropping off WiFi makes the cloud answer `Device is offline` while the opener itself is still reachable locally, which is exactly when local control matters.
- A command that reached neither Bluetooth nor the cloud is now reported as failed instead of appearing to succeed. The cloud's own explanation is passed through, so a rate limit reads as `Could not open the door: the F-LINX cloud said "Too frequent operation, please try again later"`.
- Commands issued while a Bluetooth link is being torn down no longer fail with `'NoneType' object has no attribute 'write_gatt_char'`.
- Config flow, options and action descriptions now show their proper text instead of raw translation keys - Home Assistant loads custom integration translations from `translations/en.json`, which was missing (only `strings.json` was shipped, and that file is never read at runtime).
- Corrected the `manifest.json` key order so the integration passes Home Assistant's hassfest validation.
- The options flow no longer logs in behind the coordinators' back, which revoked the session they were using (the API allows one session per account).
- Removing a door now removes its device and entities instead of leaving them behind as unavailable.
- Adding the same account a second time with different capitalisation is now recognised as already configured, instead of creating a duplicate entry whose entities collide.
- Cloud calls reuse Home Assistant's shared HTTP session instead of opening a new one per request, per door.

## 2.4.0
- Adds a 'refresh state' action
- Adds a config option for cloud polling, which is useful in case you also control your garage door using physical buttons or remotes

## 2.3.0

### Added
- **Set position / partial open** - a position slider on the garage cover plus the `cover.set_cover_position` action, so you can send the door to a specific percentage.
  - **Best-effort, software-based.** The controller has no native "open to X%" command, so the integration drives a normal open/close and issues a stop once the live position reaches the target. It lands *close* to the requested percentage (typically within a few %), not exactly on it, and the door visibly moves and then stops. `0%` and `100%` use the dedicated close/open commands and reach the hard limits precisely.
  - BLE-first, with a predictive stop that accounts for the door's coast to reduce overshoot.

### Fixed
- Bluetooth commands now actually control the door - the BLE command and auth frames were malformed and silently rejected by the device; corrected to match the controller's protocol.
- Open / close / stop / light no longer appear to succeed while doing nothing - a command BLE can't confirm now falls back to the cloud automatically.
- Fixed the constant `unparseable plaintext` log spam and stale state - the MQTT attribute-report parser now accepts the controller's current frame headers, restoring real-time push updates.
- Hardened the BLE connection - a stalled proxy link can no longer wedge reconnects (connect timeout + teardown), commands trigger an on-demand connect, and the connect lifecycle is logged.
