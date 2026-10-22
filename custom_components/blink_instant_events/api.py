"""Blink REST calls: session refresh, device list, push client association."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout
from blinkpy.helpers.constants import OAUTH_SCOPE, OAUTH_TOKEN_URL, OAUTH_TOKEN_USER_AGENT, OAUTH_V2_CLIENT_ID

from .const import APP_VERSION, CLIENT_NAME

TIMEOUT = ClientTimeout(total=20)
DEVICE_KINDS = ("cameras", "doorbells", "owls")


class BlinkAuthError(Exception):
    """Blink rejected the stored session; the user must sign in again."""


class BlinkAccount:
    """A Blink session restored from blinkpy's login attributes."""

    def __init__(
        self,
        session: ClientSession,
        auth: dict[str, Any],
        on_update: Callable[[dict[str, Any]], None],
    ) -> None:
        """Initialize with blinkpy `login_attributes` (no password)."""
        self._session = session
        self.auth = auth
        self._on_update = on_update
        self.client_id: str | None = None

    @property
    def _rest(self) -> str:
        return f"https://rest-{self.auth['region_id']}.immedia-semi.com"

    async def _token(self) -> str:
        """Return a valid access token, refreshing it when close to expiry."""
        if (self.auth.get("expiration_date") or 0) - time.time() > 300:
            return self.auth["token"]
        async with self._session.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.auth["refresh_token"],
                "client_id": OAUTH_V2_CLIENT_ID,
                "scope": OAUTH_SCOPE,
                "hardware_id": self.auth["hardware_id"],
            },
            headers={"User-Agent": OAUTH_TOKEN_USER_AGENT, "Accept": "*/*"},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status in (400, 401, 403):
                raise BlinkAuthError
            resp.raise_for_status()
            data = await resp.json()
        self.auth = {
            **self.auth,
            "token": data["access_token"],
            "refresh_token": data.get("refresh_token") or self.auth["refresh_token"],
            "expires_in": data.get("expires_in", 3600),
            "expiration_date": time.time() + data.get("expires_in", 3600),
        }
        self._on_update(self.auth)
        return self.auth["token"]

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        if self.client_id:
            headers["Client-Id"] = self.client_id
        async with self._session.request(
            method, self._rest + path, json=body, headers=headers, timeout=TIMEOUT
        ) as resp:
            if resp.status == 401:
                raise BlinkAuthError
            resp.raise_for_status()
            return await resp.json(content_type=None), resp.headers.get("Client-Id")

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return cameras/doorbells/minis and learn this session's Blink client id."""
        home, client_id = await self._call("GET", f"/api/v3/accounts/{self.auth['account_id']}/homescreen")
        self.client_id = client_id or self.client_id
        return [
            {
                "id": str(dev["id"]),
                "name": dev.get("name") or str(dev["id"]),
                "serial": dev.get("serial") or str(dev["id"]),
                "type": dev.get("type"),
                "network_id": dev.get("network_id"),
                "doorbell": kind == "doorbells",
            }
            for kind in DEVICE_KINDS
            for dev in home.get(kind, [])
        ]

    async def async_associate(self, fcm_token: str) -> None:
        """Tell Blink to push this account's notifications to our FCM token."""
        if not self.client_id:
            await self.async_get_devices()
        await self._call(
            "POST",
            f"/clients/{self.client_id}/update",
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
