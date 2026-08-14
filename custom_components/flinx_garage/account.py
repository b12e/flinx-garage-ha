"""Account-level session manager for F-LINX Garage Door.

The Bit Door API allows only one active session per account. FlinxAccount is
the single owner of that session: it logs in once and hands out the token, and
re-logins under a lock when the token is invalidated (e.g. on HTTP 401).

Every login and device-list call goes through here — the config and options
flows included — so a flow can never quietly log in behind the coordinators'
back and invalidate the session they are holding.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from homeassistant.exceptions import HomeAssistantError

from .const import API_BASE_URL, API_VERSION

_LOGGER = logging.getLogger(__name__)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot reach the F-LINX API."""


class FlinxAccount:
    """Owns the credentials and the single API token for one account."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    async def async_get_token(self, session: aiohttp.ClientSession) -> str | None:
        """Return the current token, logging in once if needed.

        Never raises: on the command and poll paths a missing token just means
        "try again later". Use async_login when you need to tell a rejected
        login apart from an unreachable API (i.e. in a config flow).
        """
        try:
            return await self.async_login(session)
        except CannotConnect as err:
            _LOGGER.debug("API login error: %s", err)
            return None

    async def async_login(
        self, session: aiohttp.ClientSession, *, force: bool = False
    ) -> str | None:
        """Log in if needed and return the token, or None if it was rejected.

        The lock guarantees at most one login in flight, so concurrent callers
        never create competing sessions. Pass force=True to replace a cached
        token the server may already have revoked.

        Raises CannotConnect when the API can't be reached.
        """
        if self._token and not force:
            return self._token

        async with self._token_lock:
            if self._token and not force:
                return self._token
            self._token = await self._request_token(session)
            return self._token

    def async_invalidate_token(self) -> None:
        """Drop the cached token so the next API call forces a re-login."""
        self._token = None

    async def async_query_devices(
        self, session: aiohttp.ClientSession, token: str
    ) -> list[dict[str, Any]] | None:
        """Return the doors on this account, or None if the token was rejected.

        An empty list means the account genuinely has no doors; callers need to
        tell that apart from a dead session to decide whether re-logging in is
        worth a try. Raises CannotConnect when the API can't be reached.
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
                    _LOGGER.debug("queryDevice failed: status=%s", resp.status)
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("queryDevice error: %s", err)
            raise CannotConnect from err

        if not isinstance(data, dict) or data.get("code") != 200:
            _LOGGER.debug("queryDevice rejected: %s", data)
            return None

        return [
            device
            for device in data.get("data") or []
            if isinstance(device, dict)
            and device.get("deviceCode")
            and device.get("devKey")
        ]

    async def _request_token(self, session: aiohttp.ClientSession) -> str | None:
        url = f"{API_BASE_URL}/app/user/login"
        headers = {"api-version": API_VERSION, "Content-Type": "application/json"}
        payload = {"username": self._username, "password": self._password}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.debug("API login failed: status=%s", resp.status)
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("API login error: %s", err)
            raise CannotConnect from err

        if not isinstance(data, dict) or data.get("code") != 200:
            _LOGGER.debug("API login rejected: %s", data)
            return None

        # Tolerate an unexpected payload shape rather than raising: a missing
        # token reads the same as rejected credentials to the caller.
        token = (data.get("data") or {}).get("token")
        if not token:
            _LOGGER.debug("API login returned no token: %s", data)
            return None
        return token
