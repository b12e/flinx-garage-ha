"""Account-level session manager for F-LINX Garage Door.

The Bit Door API allows only one active session per account. FlinxAccount is
the single owner of that session: it logs in once and hands out the token, and
re-logins under a lock when the token is invalidated (e.g. on HTTP 401).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .const import API_BASE_URL, API_VERSION

_LOGGER = logging.getLogger(__name__)


class FlinxAccount:
    """Owns the credentials and the single API token for one account."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    async def async_get_token(self, session: aiohttp.ClientSession) -> str | None:
        """Return the current token, logging in once if needed.

        The lock guarantees at most one login in flight, so concurrent
        callers never create competing sessions.
        """
        if self._token:
            return self._token
        
        async with self._token_lock:
            if self._token:
                return self._token
            await self._login(session)
            return self._token

    def async_invalidate_token(self) -> None:
        """Drop the cached token so the next API call forces a re-login."""
        self._token = None

    async def _login(self, session: aiohttp.ClientSession) -> None:
        url = f"{API_BASE_URL}/app/user/login"
        headers = {"api-version": API_VERSION, "Content-Type": "application/json"}
        payload = {"username": self._username, "password": self._password}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, dict) and data.get("code") == 200:
                        token = data.get("data", {}).get("token")
                        if token:
                            self._token = token
                            return
                _LOGGER.debug("API login failed: status=%s", resp.status)
        except (aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("API login error: %s", err)
