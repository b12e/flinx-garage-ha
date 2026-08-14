# Changelog

## 3.0.0

### Added
- **Account-based setup** - log in once; all devices on your account are listed and added in a single integration entry.
- **Single API session** - all devices now share one Bit Door session, allowing multiple devices to work using the same account.
- **Manage devices** option - add or remove doors later without re-adding the integration.
- Bluetooth discovery: an F-LinX opener in range now offers to set the integration up.

### Changed
- Existing setups upgrade in place: pre-3.0 entries are converted to the new account format automatically, keeping their entity IDs, names, areas and history. Two entries for the same account are merged into one.
- BLE discovery now also matches `opener_*` device names (in addition to `Noru_*`).
- Each door now only talks to its own opener, matched on the `bluetoothName` the cloud reports for it (or the address encoded in that name). With several doors configured and no opener reported for one, that door uses the cloud rather than risk commanding the wrong one.
- Minimum Home Assistant version is now 2025.2.0.

### Fixed
- A Bluetooth command that the opener never answers no longer fails the action with `Timeout waiting for BluetoothGATTWriteResponse ... after 30.0s`. Writes are bounded at 5 seconds and fall back to the cloud, and a proxy's own transport errors are handled rather than raised at whoever pressed the button.
- A command that reached neither Bluetooth nor the cloud is now reported as failed instead of appearing to succeed. The cloud's own explanation is passed through, so a rate limit reads as `Could not open the door: the F-LINX cloud said "Too frequent operation, please try again later"`.
- Commands issued while a Bluetooth link is being torn down no longer fail with `'NoneType' object has no attribute 'write_gatt_char'`.
- Config flow, options and action descriptions now show their proper text instead of raw translation keys - Home Assistant loads custom integration translations from `translations/en.json`, which was missing (only `strings.json` was shipped, and that file is never read at runtime).
- Corrected the `manifest.json` key order so the integration passes Home Assistant's hassfest validation.
- The options flow no longer logs in behind the coordinators' back, which revoked the session they were using (the API allows one session per account).
- Removing a door now removes its device and entities instead of leaving them behind as unavailable.
- Adding the same account a second time with different capitalisation is now recognised as already configured, instead of creating a duplicate entry whose entities collide.
- Cloud calls reuse Home Assistant's shared HTTP session instead of opening a new one per request, per door.

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
