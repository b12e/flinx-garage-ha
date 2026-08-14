"""Config flow for F-LINX Garage Door integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .account import CannotConnect, FlinxAccount
from .const import (
    BLE_NAME_PREFIXES,
    CONF_BLE_ADDRESS,
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DEVICES,
    CONF_DOOR_ALIAS,
    CONF_POLL_INTERVAL,
    DEFAULT_DOOR_ALIAS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    ENTRY_VERSION,
    POLL_INTERVAL_CHOICES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

# Selector value standing for "don't use Bluetooth for this door".
BLE_ADDRESS_NONE = "none"


def _poll_interval_label(seconds: int) -> str:
    """Human label for a poll-interval choice."""
    if seconds == 0:
        return "Off (MQTT only)"
    if seconds < 3600:
        minutes = seconds // 60
        return f"Every {minutes} minute{'s' if minutes != 1 else ''}"
    return "Every hour"


def _device_options(devices: list[dict[str, Any]]) -> list[SelectOptionDict]:
    """Selector options for a list of devices as the cloud API returns them."""
    return [
        SelectOptionDict(
            value=device["deviceCode"],
            label=f"{device.get('doorAlias') or 'Garage'} ({device['deviceCode']})",
        )
        for device in devices
    ]


def _devices_selector(options: list[SelectOptionDict]) -> SelectSelector:
    """Multi-select over the account's doors."""
    return SelectSelector(
        SelectSelectorConfig(
            options=options, multiple=True, mode=SelectSelectorMode.DROPDOWN
        )
    )


def _entry_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert cloud API devices into the entry's device format."""
    return [
        {
            CONF_DEVICE_CODE: device["deviceCode"],
            CONF_DEV_KEY: device["devKey"],
            CONF_DOOR_ALIAS: device.get("doorAlias") or DEFAULT_DOOR_ALIAS,
        }
        for device in devices
    ]


def _door_field(device: dict[str, Any]) -> str:
    """Form field key for one configured door.

    Dynamic field keys have no translation, so the frontend renders the key
    itself — which is why this reads as a label.
    """
    alias = device.get(CONF_DOOR_ALIAS) or DEFAULT_DOOR_ALIAS
    return f"{alias} ({device[CONF_DEVICE_CODE]})"


class FlinxGarageConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for F-LINX Garage Door."""

    VERSION = ENTRY_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FlinxGarageOptionsFlow:
        """Get the options flow for this handler."""
        return FlinxGarageOptionsFlow()

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Credentials step. On success, fetch devices and move to selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            account = FlinxAccount(username=username, password=password)
            session = async_get_clientsession(self.hass)
            try:
                token = await account.async_login(session)
                devices = (
                    await account.async_query_devices(session, token) if token else None
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                if devices is None:
                    errors["base"] = "invalid_auth"
                elif not devices:
                    errors["base"] = "no_devices"
                else:
                    # Claim the account before the user picks doors, so an
                    # already-configured account aborts right away instead of
                    # after the whole flow.
                    await self.async_set_unique_id(username.lower())
                    self._abort_if_unique_id_configured()
                    self._username = username
                    self._password = password
                    self._devices = devices
                    return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick which doors to add (all pre-selected)."""
        options = _device_options(self._devices)
        default = [option["value"] for option in options]
        errors: dict[str, str] = {}

        if user_input is not None:
            chosen = set(user_input[CONF_DEVICES])
            selected = [d for d in self._devices if d["deviceCode"] in chosen]
            if selected:
                return self.async_create_entry(
                    title=self._username,
                    data={
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_DEVICES: _entry_devices(selected),
                    },
                )
            errors["base"] = "no_devices_selected"
            default = sorted(chosen)

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICES, default=default): _devices_selector(
                        options
                    ),
                }
            ),
            errors=errors,
        )


class FlinxGarageOptionsFlow(config_entries.OptionsFlow):
    """F-LINX options: periodic cloud poll, which doors, Bluetooth binding."""

    def __init__(self) -> None:
        self._account_devices: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose which aspect of the integration to configure."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["poll_interval", "devices", "bluetooth"],
        )

    # -----------------------------------------------------------------
    # Periodic cloud poll
    # -----------------------------------------------------------------

    async def async_step_poll_interval(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure the optional periodic cloud poll."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL])},
            )

        current = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=str(seconds), label=_poll_interval_label(seconds)
                    )
                    for seconds in POLL_INTERVAL_CHOICES
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

        return self.async_show_form(
            step_id="poll_interval",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=str(current)): selector,
                }
            ),
        )

    # -----------------------------------------------------------------
    # Which doors
    # -----------------------------------------------------------------

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-read the account's doors and add/remove them."""
        if user_input is not None:
            chosen = set(user_input[CONF_DEVICES])
            selected = [d for d in self._account_devices if d["deviceCode"] in chosen]
            if not selected:
                return self.async_show_form(
                    step_id="devices",
                    data_schema=self._devices_schema(sorted(chosen)),
                    errors={"base": "no_devices_selected"},
                )
            return self._async_save_devices(selected)

        try:
            devices = await self._async_fetch_account_devices()
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")

        if devices is None:
            return self.async_abort(reason="invalid_auth")
        if not devices:
            return self.async_abort(reason="no_devices")

        self._account_devices = devices
        configured = {d[CONF_DEVICE_CODE] for d in self.config_entry.data[CONF_DEVICES]}
        available = {d["deviceCode"] for d in devices}
        return self.async_show_form(
            step_id="devices",
            data_schema=self._devices_schema(sorted(configured & available)),
        )

    def _devices_schema(self, default: list[str]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_DEVICES, default=default): _devices_selector(
                    _device_options(self._account_devices)
                ),
            }
        )

    async def _async_fetch_account_devices(self) -> list[dict[str, Any]] | None:
        """Re-read the account's doors, reusing the live session if there is one.

        Going through the entry's own FlinxAccount matters: the API allows a
        single session per account, so logging in separately here would revoke
        the token the coordinators are holding. Returns None when the login was
        rejected, [] when the account genuinely has no doors.
        """
        session = async_get_clientsession(self.hass)
        account = self._async_live_account() or FlinxAccount(
            username=self.config_entry.data[CONF_USERNAME],
            password=self.config_entry.data[CONF_PASSWORD],
        )

        token = await account.async_login(session)
        devices = await account.async_query_devices(session, token) if token else None
        if devices is None:
            # A cached token may have been revoked server-side, which looks the
            # same as a rejected one from here — force one fresh login before
            # reporting the account as unusable.
            token = await account.async_login(session, force=True)
            devices = (
                await account.async_query_devices(session, token) if token else None
            )
        return devices

    @callback
    def _async_live_account(self) -> FlinxAccount | None:
        """Return the loaded entry's FlinxAccount, if the entry is loaded."""
        data = (self.hass.data.get(DOMAIN) or {}).get(self.config_entry.entry_id)
        return data.get("account") if data else None

    @callback
    def _async_save_devices(self, selected: list[dict[str, Any]]) -> FlowResult:
        """Store the new door list and clean up the doors that were dropped."""
        entry = self.config_entry
        configured = {d[CONF_DEVICE_CODE]: d for d in entry.data[CONF_DEVICES]}

        devices = []
        for device in _entry_devices(selected):
            # Keep a door's bound BLE address across a re-selection.
            previous = configured.get(device[CONF_DEVICE_CODE], {})
            if address := previous.get(CONF_BLE_ADDRESS):
                device[CONF_BLE_ADDRESS] = address
            devices.append(device)

        device_registry = dr.async_get(self.hass)
        for code in set(configured) - {d[CONF_DEVICE_CODE] for d in devices}:
            if device_entry := device_registry.async_get_device(
                identifiers={(DOMAIN, code)}
            ):
                # Removing the device takes its entities with it, so a dropped
                # door doesn't linger as "unavailable" forever.
                _LOGGER.debug("Removing device registry entry for door %s", code)
                device_registry.async_remove_device(device_entry.id)

        # async_update_entry fires the update listener registered in
        # async_setup_entry, which reloads the entry — no explicit reload here.
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: devices}
        )
        return self.async_create_entry(title="", data=dict(entry.options))

    # -----------------------------------------------------------------
    # Bluetooth binding
    # -----------------------------------------------------------------

    async def async_step_bluetooth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Bind each door to a specific BLE peripheral.

        Advertisements carry nothing that identifies a device code, so with more
        than one door the coordinator refuses to guess which peripheral is which
        (it would end up writing frames built from another door's key). This is
        where that mapping is made.
        """
        entry = self.config_entry
        configured = entry.data[CONF_DEVICES]

        if user_input is not None:
            devices = []
            for device in configured:
                address = user_input.get(_door_field(device), BLE_ADDRESS_NONE)
                updated = {
                    key: value
                    for key, value in device.items()
                    if key != CONF_BLE_ADDRESS
                }
                if address != BLE_ADDRESS_NONE:
                    updated[CONF_BLE_ADDRESS] = address
                devices.append(updated)

            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICES: devices}
            )
            return self.async_create_entry(title="", data=dict(entry.options))

        return self.async_show_form(
            step_id="bluetooth",
            data_schema=self._bluetooth_schema(configured),
        )

    def _bluetooth_schema(self, configured: list[dict[str, Any]]) -> vol.Schema:
        discovered = []
        # The bluetooth integration is only set up when the host has an adapter
        # (or a proxy), and this form has to work either way.
        if "bluetooth" in self.hass.config.components:
            discovered = [
                service_info
                for service_info in bluetooth.async_discovered_service_info(
                    self.hass, connectable=True
                )
                if service_info.name
                and service_info.name.startswith(BLE_NAME_PREFIXES)
            ]

        schema: dict[Any, Any] = {}
        for device in configured:
            current = device.get(CONF_BLE_ADDRESS)
            options = [
                SelectOptionDict(
                    value=BLE_ADDRESS_NONE, label="Cloud only (no Bluetooth)"
                ),
                *(
                    SelectOptionDict(
                        value=service_info.address,
                        label=f"{service_info.name} ({service_info.address})",
                    )
                    for service_info in discovered
                ),
            ]
            # Keep an already-bound address selectable even when it isn't
            # advertising right now, so opening this form can't silently unbind.
            if current and all(option["value"] != current for option in options):
                options.append(
                    SelectOptionDict(value=current, label=f"{current} (not seen now)")
                )

            schema[
                vol.Required(_door_field(device), default=current or BLE_ADDRESS_NONE)
            ] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )

        return vol.Schema(schema)
