"""Config flow: email/password, 2FA PIN, reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from blinkpy.auth import BlinkTwoFARequiredError, LoginError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_instant_events.const import CONF_AUTH, CONF_FCM, DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import AUTH


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
