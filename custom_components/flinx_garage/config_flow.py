"""Config flow for F-LINX Garage Door integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    API_BASE_URL,
    API_VERSION,
    CONF_DEVICE_CODE,
    CONF_DEV_KEY,
    CONF_DOOR_ALIAS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _login(session: aiohttp.ClientSession, username: str, password: str) -> str | None:
    """Log in and return the Bearer token (or None)."""
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
        return None


async def _query_devices(
    session: aiohttp.ClientSession, token: str
) -> list[dict[str, Any]]:
    """Fetch device list (with devKey) for the logged-in user."""
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
        return []


class FlinxGarageConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for F-LINX Garage Door."""

    VERSION = 2

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
            async with aiohttp.ClientSession() as session:
                token = await _login(
                    session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                if not token:
                    errors["base"] = "invalid_auth"
                else:
                    devices = await _query_devices(session, token)
                    if not devices:
                        errors["base"] = "no_devices"
                    else:
                        self._username = user_input[CONF_USERNAME]
                        self._password = user_input[CONF_PASSWORD]
                        self._devices = devices

                        if len(devices) == 1:
                            return await self._create_entry(devices[0])
                        return await self.async_step_select_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick which device (when the user has multiple)."""
        options = {
            d["deviceCode"]: f"{d.get('doorAlias') or 'Garage'} ({d['deviceCode']})"
            for d in self._devices
        }

        if user_input is not None:
            chosen_code = user_input[CONF_DEVICE_CODE]
            device = next(
                (d for d in self._devices if d["deviceCode"] == chosen_code), None
            )
            if device is None:
                return self.async_abort(reason="device_not_found")
            return await self._create_entry(device)

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_CODE): vol.In(options)}
            ),
        )

    async def _create_entry(self, device: dict[str, Any]) -> FlowResult:
        await self.async_set_unique_id(device["deviceCode"])
        self._abort_if_unique_id_configured()

        alias = device.get("doorAlias") or "F-LINX Garage Door"

        return self.async_create_entry(
            title=alias,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_DEVICE_CODE: device["deviceCode"],
                CONF_DEV_KEY: device["devKey"],
                CONF_DOOR_ALIAS: alias,
            },
        )
