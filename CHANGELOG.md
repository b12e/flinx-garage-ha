# Changelog

## 2.3.0

### Added
- **Set position / partial open** — a position slider on the garage cover plus the `cover.set_cover_position` action, so you can send the door to a specific percentage.
  - **Best-effort, software-based.** The controller has no native "open to X%" command, so the integration drives a normal open/close and issues a stop once the live position reaches the target. It lands *close* to the requested percentage (typically within a few %), not exactly on it, and the door visibly moves and then stops. `0%` and `100%` use the dedicated close/open commands and reach the hard limits precisely.
  - BLE-first, with a predictive stop that accounts for the door's coast to reduce overshoot.

### Fixed
- Bluetooth commands now actually control the door — the BLE command and auth frames were malformed and silently rejected by the device; corrected to match the controller's protocol.
- Open / close / stop / light no longer appear to succeed while doing nothing — a command BLE can't confirm now falls back to the cloud automatically.
- Fixed the constant `unparseable plaintext` log spam and stale state — the MQTT attribute-report parser now accepts the controller's current frame headers, restoring real-time push updates.
- Hardened the BLE connection — a stalled proxy link can no longer wedge reconnects (connect timeout + teardown), commands trigger an on-demand connect, and the connect lifecycle is logged.
