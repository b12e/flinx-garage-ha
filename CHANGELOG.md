# Changelog

## 3.0.0

### Added
- **Account-based setup** — log in once; all devices on your account are listed and added in a single integration entry.
- **Single API session** — all devices now share one Bit Door session, allowing multiple devices to work using the same account.
- **Manage devices** option — add or remove doors later without re-adding the integration.
- **Bluetooth** option — point each door at its own opener. Needed for local BLE commands once more than one door is configured.

### Changed
- Existing setups upgrade in place: pre-3.0 entries are converted to the new account format automatically, keeping their entity IDs, names, areas and history. Two entries for the same account are merged into one.
- BLE discovery now also matches `opener_*` device names (in addition to `Noru_*`).
- Commands only go over Bluetooth to a door the integration can positively identify (a bound address, or an advertised name ending in the device code). With several doors and no binding, that door uses the cloud rather than risk commanding the wrong one.
- Removed the Bluetooth discovery matchers from the manifest — setup needs account credentials, so the discovery flow they triggered could only fail.
- Minimum Home Assistant version is now 2025.2.0.

### Fixed
- The options flow no longer logs in behind the coordinators' back, which revoked the session they were using (the API allows one session per account).
- Removing a door now removes its device and entities instead of leaving them behind as unavailable.
- Adding the same account a second time with different capitalisation is now recognised as already configured, instead of creating a duplicate entry whose entities collide.
- The integration's UI text now actually renders — translations were only in `strings.json`, which Home Assistant reads for core integrations but not for custom ones.
- Cloud calls reuse Home Assistant's shared HTTP session instead of opening a new one per request, per door.

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
