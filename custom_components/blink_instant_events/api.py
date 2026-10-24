"""Blink sessions: blinkpy's Auth for polling, the Android-client session Blink pushes to, and the push REST calls."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from aiohttp import ClientSession, ClientTimeout
from blinkpy.auth import Auth, UnauthorizedError
from blinkpy.helpers.constants import OAUTH_SCOPE, OAUTH_TOKEN_URL, OAUTH_TOKEN_USER_AGENT, OAUTH_V2_CLIENT_ID

from .const import APP_VERSION, CLIENT_NAME

TIMEOUT = ClientTimeout(total=20)

# Blink pushes to FCM tokens only for clients of its Android app; blinkpy's session belongs to the
# v2 "ios" client, whose notification key Blink treats as an Apple one. So pushes get a session of
# their own, signed in on Blink's hosted page as the Android app.
ANDROID_CLIENT = "android"
AUTHORIZE_URL = "https://api.oauth.blink.com/oauth/v2/authorize"
REDIRECT_URI = "https://applinks.blink.com/signin/callback"
USER_AGENT = "Blink/60.0 (Home Assistant)"  # Blink's Cloudflare turns away aiohttp's default agent


def sign_in_request() -> tuple[str, dict[str, str]]:
    """Blink's hosted sign-in page for the Android client, and what finishing it needs."""
    pending = {
        "hardware_id": str(uuid.uuid4()).upper(),
        "state": secrets.token_urlsafe(32),
        "verifier": secrets.token_urlsafe(64),
    }
    challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).rstrip(b"=")
    params = {
        "client_id": ANDROID_CLIENT,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "client",
        "state": pending["state"],
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
        "prompt": "login",
        "hardware_id": pending["hardware_id"],
        "app_brand": "blink",
        "app_version": "Version 60.0",
        "device_brand": "Home Assistant",
        "device_model": CLIENT_NAME,
        "device_os_version": "Android 14",
        "dark_mode": "false",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", pending


def callback_code(url: str, state: str) -> str | None:
    """The authorization code in the address Blink redirected to, if it is ours."""
    parsed = urlsplit(url.strip())
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "applinks.blink.com"
        or parsed.path != "/signin/callback"
        or query.get("state") != [state]
        or len(query.get("code", [])) != 1
    ):
        return None
    return query["code"][0]


async def _token(session: ClientSession, form: dict[str, str]) -> dict[str, Any]:
    async with session.post(
        OAUTH_TOKEN_URL, data=form, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT
    ) as resp:
        if resp.status in (400, 401):
            raise UnauthorizedError
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return {
        "token": data["access_token"],
        "refresh_token": data.get("refresh_token") or form.get("refresh_token"),
        "expiration_date": time.time() + int(data.get("expires_in") or 3600),
    }


async def async_push_sign_in(session: ClientSession, code: str, pending: dict[str, str]) -> dict[str, Any]:
    """Trade the code for the Android session, then find its account, region and Blink client id."""
    auth = await _token(
        session,
        {
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": pending["verifier"],
            "client_id": ANDROID_CLIENT,
        },
    )
    headers = {"Authorization": f"Bearer {auth['token']}", "User-Agent": USER_AGENT}
    async with session.get(
        "https://rest-prod.immedia-semi.com/api/v1/users/tier_info", headers=headers, timeout=TIMEOUT
    ) as resp:
        resp.raise_for_status()
        info = await resp.json(content_type=None)
    region, account = info["tier"], info["account_id"]
    async with session.get(
        f"https://rest-{region}.immedia-semi.com/api/v3/accounts/{account}/homescreen", headers=headers, timeout=TIMEOUT
    ) as resp:
        resp.raise_for_status()
        client = resp.headers["Client-Id"]
    return {
        **auth,
        "hardware_id": pending["hardware_id"],
        "region_id": region,
        "account_id": account,
        "client_id": client,
    }


class PushAuth:
    """The Android-client session; renews like the Android app and stores each rotation."""

    def __init__(
        self, session: ClientSession, data: dict[str, Any], on_update: Callable[[dict[str, Any]], None]
    ) -> None:
        """Initialize with what async_push_sign_in returned (as stored)."""
        self._session = session
        self.data = data
        self._on_update = on_update
        self._lock = asyncio.Lock()

    @property
    def client_id(self) -> str:
        """Blink's id for this client."""
        return str(self.data["client_id"])

    @property
    def region_id(self) -> str:
        """The REST tier, e.g. u062."""
        return str(self.data["region_id"])

    async def async_token(self) -> str:
        """A valid access token; UnauthorizedError means sign in again."""
        # ponytail: one renewal at a time is enough here; only the listener uses this session.
        async with self._lock:
            if time.time() > self.data["expiration_date"] - 60:
                self.data = {
                    **self.data,
                    **await _token(
                        self._session,
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self.data["refresh_token"],
                            "client_id": ANDROID_CLIENT,
                            "scope": "client",
                        },
                    ),
                }
                self._on_update(self.data)
        return str(self.data["token"])


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

    def __init__(self, session: ClientSession, auth: PushAuth) -> None:
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

        The client is the Android one from async_push_sign_in.
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
