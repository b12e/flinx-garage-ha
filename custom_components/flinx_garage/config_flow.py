"""Config flow for F-LINX Garage Door integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    API_BASE_URL,
    API_VERSION,
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DEVICES,
    CONF_DOOR_ALIAS,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    POLL_INTERVAL_CHOICES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot reach the F-LINX API."""


async def _login(session: aiohttp.ClientSession, username: str, password: str) -> str | None:
    """Log in and return the Bearer token (or None).

    Returns None when the credentials are rejected; raises CannotConnect on a
    network-level failure so the flow can tell the two apart.
    """
    url = f"{API_BASE_URL}/app/user/login"
    headers = {"api-version": API_VERSION, "Content-Type": "application/json"}
    payload = {"username": username, "password": password}
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("code") != 200:
                return None
            return data["data"]["token"]
    except aiohttp.ClientError as err:
        _LOGGER.debug("Login error: %s", err)
        raise CannotConnect from err


async def _query_devices(
    session: aiohttp.ClientSession, token: str
) -> list[dict[str, Any]]:
    """Fetch device list (with devKey) for the logged-in user.

    Returns an empty list when the token is rejected or no devices exist;
    raises CannotConnect on a network-level failure.
    """
    url = f"{API_BASE_URL}/device/queryDevice"
    headers = {
        "api-version": API_VERSION,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(url, json={}, headers=headers) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            if data.get("code") != 200:
                return []
            devices = data.get("data") or []
            return [d for d in devices if d.get("deviceCode") and d.get("devKey")]
    except aiohttp.ClientError as err:
        _LOGGER.debug("queryDevice error: %s", err)
        raise CannotConnect from err


def _poll_interval_label(seconds: int) -> str:
    """Human label for a poll-interval choice."""
    if seconds == 0:
        return "Off (MQTT only)"
    if seconds < 3600:
        minutes = seconds // 60
        return f"Every {minutes} minute{'s' if minutes != 1 else ''}"
    return "Every hour"


class FlinxGarageConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for F-LINX Garage Door."""

    VERSION = 3

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
            try:
                async with aiohttp.ClientSession() as session:
                    token = await _login(
                        session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                    )
                    devices = await _query_devices(session, token) if token else []
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                if not token:
                    errors["base"] = "invalid_auth"
                elif not devices:
                    errors["base"] = "no_devices"
                else:
                    self._username = user_input[CONF_USERNAME]
                    self._password = user_input[CONF_PASSWORD]
                    self._devices = devices
                    return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    def _device_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(
                value=d["deviceCode"],
                label=f"{d.get('doorAlias') or 'Garage'} ({d['deviceCode']})",
            )
            for d in self._devices
        ]

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick which devices to add (all pre-selected)."""
        options = self._device_options()
        selector = SelectSelector(
            SelectSelectorConfig(
                options=options, multiple=True, mode=SelectSelectorMode.DROPDOWN
            )
        )

        if user_input is not None:
            chosen = set(user_input[CONF_DEVICES])
            selected = [d for d in self._devices if d["deviceCode"] in chosen]
            if not selected:
                return self.async_show_form(
                    step_id="devices",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_DEVICES, default=sorted(chosen)
                            ): selector,
                        }
                    ),
                    errors={"base": "no_devices_selected"},
                )
            return await self._create_entry(selected)

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICES, default=[o["value"] for o in options]
                    ): selector,
                }
            ),
        )

    async def _create_entry(self, selected_devices: list[dict[str, Any]]) -> FlowResult:
        await self.async_set_unique_id(self._username)
        self._abort_if_unique_id_configured()

        devices = [
            {
                CONF_DEVICE_CODE: d["deviceCode"],
                CONF_DEV_KEY: d["devKey"],
                CONF_DOOR_ALIAS: d.get("doorAlias") or "F-LINX Garage Door",
            }
            for d in selected_devices
        ]

        return self.async_create_entry(
            title=self._username,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_DEVICES: devices,
            },
        )


class FlinxGarageOptionsFlow(config_entries.OptionsFlow):
    """Handle F-LINX Garage Door options (periodic cloud poll, devices)."""

    def __init__(self) -> None:
        self._account_devices: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose which aspect of the integration to configure."""
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "poll_interval": "Poll Interval", 
                "devices": "Devices"
            },
        )

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

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-login, list account devices, and add/remove doors."""
        if user_input is not None:
            chosen = set(user_input[CONF_DEVICES])
            selected = [
                d for d in self._account_devices if d["deviceCode"] in chosen
            ]
            if not selected:
                return self.async_show_form(
                    step_id="devices",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_DEVICES, default=sorted(chosen)
                            ): SelectSelector(
                                SelectSelectorConfig(
                                    options=self._account_device_options(),
                                    multiple=True,
                                    mode=SelectSelectorMode.DROPDOWN,
                                )
                            ),
                        }
                    ),
                    errors={"base": "no_devices_selected"},
                )
            devices = [
                {
                    CONF_DEVICE_CODE: d["deviceCode"],
                    CONF_DEV_KEY: d["devKey"],
                    CONF_DOOR_ALIAS: d.get("doorAlias") or "F-LINX Garage Door",
                }
                for d in selected
            ]
            entry = self.config_entry
            data = {**entry.data, CONF_DEVICES: devices}
            # async_update_entry is a sync callback (returns bool) and fires
            # the reload listener registered in async_setup_entry, so no
            # explicit reload is needed here.
            self.hass.config_entries.async_update_entry(entry, data=data)
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        try:
            async with aiohttp.ClientSession() as session:
                account = (self.hass.data.get(DOMAIN) or {}).get(
                    self.config_entry.entry_id, {}
                ).get("account")
                token = (
                    await account.async_get_token(session)
                    if account is not None
                    else None
                )
                used_cached = token is not None
                if not token:
                    token = await _login(
                        session,
                        self.config_entry.data[CONF_USERNAME],
                        self.config_entry.data[CONF_PASSWORD],
                    )
                if not token:
                    return self.async_abort(reason="invalid_auth")
                account_devices = await _query_devices(session, token)
                if not account_devices and used_cached:
                    # A cached account token may have been revoked server-side,
                    # which _query_devices reports as an empty list; re-login
                    # once so a stale session isn't shown as "no devices".
                    account.async_invalidate_token()
                    token = await _login(
                        session,
                        self.config_entry.data[CONF_USERNAME],
                        self.config_entry.data[CONF_PASSWORD],
                    )
                    if not token:
                        return self.async_abort(reason="invalid_auth")
                    account_devices = await _query_devices(session, token)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")

        if not account_devices:
            return self.async_abort(reason="no_devices")

        self._account_devices = account_devices
        current_codes = {
            d[CONF_DEVICE_CODE] for d in self.config_entry.data[CONF_DEVICES]
        }
        options = self._account_device_options()
        default = sorted(current_codes & {o["value"] for o in options})

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICES, default=default
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    def _account_device_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(
                value=d["deviceCode"],
                label=f"{d.get('doorAlias') or 'Garage'} ({d['deviceCode']})",
            )
            for d in self._account_devices
        ]
