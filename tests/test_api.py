"""The shared Blink session renews once at a time and stores every rotation."""

from __future__ import annotations

import asyncio

from blinkpy.auth import UnauthorizedError
from blinkpy.helpers.constants import OAUTH_TOKEN_URL
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.blink_instant_events.api import BlinkAuth, async_push_sign_in, sign_in_request
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import AUTH


async def test_concurrent_callers_share_one_renewal(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Two requests needing a token renew once; the rotated token is saved."""
    aioclient_mock.post(OAUTH_TOKEN_URL, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    saved: list[dict] = []
    auth = BlinkAuth(async_get_clientsession(hass), {**AUTH, "expiration_date": 0}, saved.append)
    assert await asyncio.gather(auth.async_token(), auth.async_token()) == ["new", "new"]
    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][2]["client_id"] == "ios"
    assert saved[-1]["refresh_token"] == "r2"
    assert "password" not in saved[-1]


async def test_rejected_refresh_means_sign_in_again(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Blink refusing the refresh token is an auth failure, not a network error."""
    aioclient_mock.post(OAUTH_TOKEN_URL, status=401)
    auth = BlinkAuth(async_get_clientsession(hass), {**AUTH, "expiration_date": 0}, lambda _: None)
    with pytest.raises(UnauthorizedError):
        await auth.async_token()


async def test_cancelled_caller_still_saves_the_rotation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An app hanging up mid-renewal must not lose the refresh token Blink already rotated."""
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    async def slow(method: str, url: object, data: object) -> AiohttpClientMockResponse:
        await asyncio.sleep(0.05)
        return AiohttpClientMockResponse(
            method, url, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
        )

    aioclient_mock.post(OAUTH_TOKEN_URL, side_effect=slow)
    saved: list[dict] = []
    auth = BlinkAuth(async_get_clientsession(hass), {**AUTH, "expiration_date": 0}, saved.append)
    caller = asyncio.ensure_future(auth.async_token())
    await asyncio.sleep(0.01)
    caller.cancel()
    await asyncio.sleep(0.1)
    assert saved[-1]["refresh_token"] == "r2"
    assert await auth.async_token() == "new"
    assert aioclient_mock.call_count == 1


async def test_push_sign_in(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The code is traded with the hardware id it was issued to; account, region and client follow."""
    aioclient_mock.post(OAUTH_TOKEN_URL, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    aioclient_mock.get(
        "https://rest-prod.immedia-semi.com/api/v1/users/tier_info", json={"tier": "u062", "account_id": 99}
    )
    aioclient_mock.get(
        "https://rest-u062.immedia-semi.com/api/v3/accounts/99/homescreen", json={}, headers={"Client-Id": "456"}
    )
    _, pending = sign_in_request()
    push = await async_push_sign_in(async_get_clientsession(hass), "code", pending)
    form = aioclient_mock.mock_calls[0][2]
    assert form["hardware_id"] == pending["hardware_id"] and form["client_id"] == "android"
    assert form["code_verifier"] == pending["verifier"]
    assert push | {"expiration_date": 0} == {
        "token": "a",
        "refresh_token": "r",
        "expiration_date": 0,
        "hardware_id": pending["hardware_id"],
        "region_id": "u062",
        "account_id": 99,
        "client_id": "456",
    }
