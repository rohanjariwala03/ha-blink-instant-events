"""The one Blink session: blinkpy's Auth for polling, plus the push-client REST calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout
from blinkpy.auth import Auth, UnauthorizedError
from blinkpy.helpers.constants import OAUTH_SCOPE, OAUTH_TOKEN_URL, OAUTH_TOKEN_USER_AGENT, OAUTH_V2_CLIENT_ID

from .const import APP_VERSION, CLIENT_NAME

TIMEOUT = ClientTimeout(total=20)


class BlinkAuth(Auth):
    """blinkpy Auth that renews like the Blink app and never signs in with a password.

    blinkpy's own in-request renewal uses the old "android" grant without the
    hardware id; Blink issued this session to the v2 "ios" client, so renewal
    goes through that grant instead. One renewal at a time: Blink rotates the
    refresh token, so two concurrent renewals would leave one of them holding
    a dead token. Every rotation is handed to `on_update` to be stored.
    """

    def __init__(
        self,
        session: ClientSession,
        login_data: dict[str, Any],
        on_update: Callable[[dict[str, Any]], None],
    ) -> None:
        """Initialize with blinkpy `login_attributes` (no password)."""
        super().__init__(login_data, no_prompt=True, session=session)
        self._on_update = on_update
        self._renewal: asyncio.Task[None] | None = None

    async def refresh_tokens(self, refresh: bool = False) -> bool:
        """Renew the access token; UnauthorizedError means sign in again.

        Every caller waits on one renewal task, shielded: a caller cancelled mid-request (an app
        that hung up) must not drop the rotated refresh token Blink has already issued.
        """
        if self._renewal is None or self._renewal.done():
            if self.token and not self.need_refresh():
                return True
            self._renewal = asyncio.get_running_loop().create_task(self._renew())
        await asyncio.shield(self._renewal)
        return True

    async def _renew(self) -> None:
        async with self.session.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": OAUTH_V2_CLIENT_ID,
                "scope": OAUTH_SCOPE,
                "hardware_id": self.hardware_id,
            },
            headers={"User-Agent": OAUTH_TOKEN_USER_AGENT, "Accept": "*/*"},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status in (400, 401, 403):
                raise UnauthorizedError
            resp.raise_for_status()
            data = await resp.json()
        self.token = data["access_token"]
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.expires_in = data.get("expires_in", 3600)
        self.expiration_date = time.time() + self.expires_in
        self._on_update(dict(self.login_attributes))

    async def async_token(self) -> str:
        """A valid access token."""
        if not self.token or self.need_refresh():
            await self.refresh_tokens(refresh=True)
        return str(self.token)


class BlinkAccount:
    """The push-client calls blinkpy has no function for."""

    def __init__(self, session: ClientSession, auth: BlinkAuth) -> None:
        """Initialize on the shared session."""
        self._session = session
        self.auth = auth

    async def _call(self, method: str, path: str, body: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {await self.auth.async_token()}"}
        if self.auth.client_id:
            headers["Client-Id"] = str(self.auth.client_id)
        async with self._session.request(
            method,
            f"https://rest-{self.auth.region_id}.immedia-semi.com{path}",
            json=body,
            headers=headers,
            timeout=TIMEOUT,
        ) as resp:
            if resp.status == 401:
                raise UnauthorizedError
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def async_associate(self, fcm_token: str) -> None:
        """Tell Blink to push this account's notifications to our FCM token.

        The client id comes from blinkpy's homescreen request, which the
        coordinator makes before the listener starts.
        """
        await self._call(
            "POST",
            f"/clients/{self.auth.client_id}/update",
            {
                "app_version": APP_VERSION,
                "os_version": "14",
                "client_name": CLIENT_NAME,
                "notification_key": fcm_token,
            },
        )

    async def async_send_receipt(self, unique_id: str) -> None:
        """Acknowledge a push that asked for a delivery receipt."""
        await self._call(
            "POST",
            "/api/v2/notification",
            {"transaction_id": unique_id, "status": "RECEIVED"},
        )
