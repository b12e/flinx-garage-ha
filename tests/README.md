# Tests

Standalone checks that run against a real Home Assistant instance from the
local virtualenv — no `pytest-homeassistant-custom-component` needed. They boot
just enough of HA to hold config entries and the entity/device registries, and
mock out MQTT, Bluetooth and the cloud API.

```bash
.venv/bin/python -m tests.run_all
```

Individual modules run the same way, e.g. `.venv/bin/python -m tests.test_migration`.

The virtualenv needs `homeassistant` plus the integration's own requirements
(`bleak`, `bleak-retry-connector`, `paho-mqtt`, `pycryptodome`, `aiohttp`).
A non-zero exit code means at least one check failed.

| Module | Covers |
| --- | --- |
| `test_migration.py` | `async_migrate_entry`: a single pre-3.0 entry, two pre-3.0 entries for one account merging into one, a pre-3.0 entry absorbed into an existing 3.0 entry, a version 1 entry with no device key, a downgrade, and re-running the migration. Asserts entity IDs survive and the registries are re-keyed onto the device code. |
| `test_ble.py` | Which BLE peripheral a door resolves to: the opener name the cloud reports, the address encoded in that name, the single-door prefix fallback, and cloud-only otherwise. Asserts two doors never resolve to the same peripheral. |
| `test_setup.py` | `async_setup_entry` / `async_unload_entry`: one coordinator per door, the shared API session, the first refresh applying cloud state, and teardown via the entry's on-unload callbacks. |
| `test_flows.py` | Config flow (credential errors, unique ID normalisation, door selection, Bluetooth discovery) and options flow (device add/remove, including registry cleanup). |

`harness.py` holds the HA bootstrap and the `check()` / `summary()` helpers;
`stubs.py` fakes the `usb` integration's dependencies so
`homeassistant.components.bluetooth` can be imported.
