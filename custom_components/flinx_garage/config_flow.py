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
    API_KEY_BLE_NAME,
    CONF_BLE_NAME,
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
    """Convert cloud API devices into the entry's device format.

    The API reports each door's opener under `bluetoothName`, which is what
    lets a door be matched to one peripheral instead of any nearby opener.
    """
    entry_devices = []
    for device in devices:
        entry_device = {
            CONF_DEVICE_CODE: device["deviceCode"],
            CONF_DEV_KEY: device["devKey"],
            CONF_DOOR_ALIAS: device.get("doorAlias") or DEFAULT_DOOR_ALIAS,
        }
        ble_name = device.get(API_KEY_BLE_NAME)
        if isinstance(ble_name, str) and ble_name.strip():
            entry_device[CONF_BLE_NAME] = ble_name.strip()
        entry_devices.append(entry_device)
    return entry_devices


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

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle an opener seen over Bluetooth.

        Setup needs cloud credentials either way, so discovery only labels the
        card with the opener's name and hands over to the credentials step. One
        account covers every door on it, so once an entry exists — or a flow is
        already running — further discoveries are dropped.
        """
        _LOGGER.debug(
            "Bluetooth discovery: %s (%s)", discovery_info.name, discovery_info.address
        )
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if self._async_in_progress():
            return self.async_abort(reason="already_in_progress")

        self.context["title_placeholders"] = {
            "name": discovery_info.name or "F-LINX opener"
        }
        return await self.async_step_user()

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
    """F-LINX options: the periodic cloud poll, and which doors are configured."""

    def __init__(self) -> None:
        self._account_devices: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose which aspect of the integration to configure."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["poll_interval", "devices"],
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

        devices = _entry_devices(selected)

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
