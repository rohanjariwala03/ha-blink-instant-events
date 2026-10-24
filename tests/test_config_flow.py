"""Config flow: email/password, 2FA PIN, reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from blinkpy.auth import BlinkTwoFARequiredError, LoginError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_instant_events.api import ANDROID_CLIENT, callback_code, sign_in_request
from custom_components.blink_instant_events.const import CONF_AUTH, CONF_FCM, CONF_PUSH_AUTH, DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import AUTH, FCM


@pytest.fixture
def mock_auth() -> MagicMock:
    """blinkpy Auth that asks for 2FA, then succeeds."""
    with patch("custom_components.blink_instant_events.config_flow.Auth") as cls:
        auth = cls.return_value
        auth.startup = AsyncMock(side_effect=BlinkTwoFARequiredError)
        auth.complete_2fa_login = AsyncMock(side_effect=[False, True])
        auth.account_id = 99
        auth.region_id = "u001"
        auth.login_attributes = {**AUTH, "password": "secret"}
        yield auth


@pytest.fixture(autouse=True)
def no_setup() -> MagicMock:
    """Don't start the integration after the flow."""
    with patch("custom_components.blink_instant_events.async_setup_entry", return_value=True) as m:
        yield m


async def test_user_flow_with_2fa(hass: HomeAssistant, mock_auth: MagicMock) -> None:
    """Email/password → wrong PIN → right PIN → entry without the password."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "secret"}
    )
    assert result["step_id"] == "2fa"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": "000000"})
    assert result["errors"] == {"base": "invalid_pin"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "me@example.com"
    assert result["result"].unique_id == "99"
    assert "password" not in result["data"][CONF_AUTH]


async def test_bad_password(hass: HomeAssistant, mock_auth: MagicMock) -> None:
    """Wrong password shows invalid_auth."""
    mock_auth.startup.side_effect = LoginError
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_keeps_fcm(hass: HomeAssistant, mock_auth: MagicMock) -> None:
    """Reauth replaces the Blink session and keeps the FCM identity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="me@example.com",
        unique_id="99",
        data={CONF_AUTH: {"token": "old"}, CONF_FCM: {"keep": True}},
    )
    entry.add_to_hass(hass)
    mock_auth.startup.side_effect = None
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "secret"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_FCM] == {"keep": True}
    assert entry.data[CONF_AUTH]["token"] == AUTH["token"]


def test_callback_code() -> None:
    """Only the redirect for this sign-in's state yields a code."""
    url, pending = sign_in_request()
    assert f"client_id={ANDROID_CLIENT}" in url and pending["state"] in url
    good = f"https://applinks.blink.com/signin/callback?code=abc&state={pending['state']}"
    assert callback_code(f"  {good}\n", pending["state"]) == "abc"
    assert callback_code(good.replace("state=", "state=x"), pending["state"]) is None
    assert callback_code(good.replace("applinks.blink.com", "evil.example"), pending["state"]) is None
    assert (
        callback_code(
            f"https://applinks.blink.com/signin/callback?error=denied&state={pending['state']}", pending["state"]
        )
        is None
    )


async def test_push_sign_in(hass: HomeAssistant) -> None:
    """Options › Sign in for instant events stores the Android session and reloads."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="me@example.com", unique_id="99", data={CONF_AUTH: AUTH, CONF_FCM: FCM}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "push_sign_in"})
    url = result["description_placeholders"]["url"]
    state = url.split("state=")[1].split("&")[0]

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"callback_url": "https://x"})
    assert result["errors"] == {"base": "bad_callback"}

    push = {"token": "t", "refresh_token": "r", "expiration_date": 1, "account_id": 98, "client_id": "1"}
    signed_in = AsyncMock(return_value=push)
    callback = f"https://applinks.blink.com/signin/callback?code=c&state={state}"
    with patch("custom_components.blink_instant_events.config_flow.async_push_sign_in", signed_in):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"callback_url": callback})
        assert result["errors"] == {"base": "wrong_account"}
        push["account_id"] = 99
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            result = await hass.config_entries.options.async_configure(result["flow_id"], {"callback_url": callback})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert signed_in.await_args.args[1] == "c"
    assert entry.data[CONF_PUSH_AUTH] == push
    reload.assert_called_once_with(entry.entry_id)
