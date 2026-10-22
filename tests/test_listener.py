"""Setup errors, reconnect with backoff, re-association, reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError
from firebase_messaging import FcmPushClientRunState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_instant_events.api import BlinkAuthError
from custom_components.blink_instant_events.const import CONF_FCM
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant


async def test_setup_retries_when_blink_down(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_fcm: MagicMock, mock_account: MagicMock
) -> None:
    """Network errors → retry later; rejected session → reauth."""
    mock_account.devices.side_effect = ClientError
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    mock_account.devices.side_effect = BlinkAuthError
    await hass.config_entries.async_reload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(config_entry.async_get_active_flows(hass, {"reauth"}))


async def test_reconnect_with_backoff(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_fcm: MagicMock, mock_account: MagicMock
) -> None:
    """A dead client is replaced; a failed attempt delays the next one."""
    listener = setup_entry.runtime_data
    client = mock_fcm.return_value
    client.run_state = FcmPushClientRunState.STOPPED
    client.checkin_or_register = AsyncMock(side_effect=RuntimeError("no gcm"))

    await listener._async_check()
    assert listener._failures == 1
    await listener._async_check()  # inside backoff window: no new attempt
    assert client.checkin_or_register.await_count == 1

    listener._next_try = 0
    client.checkin_or_register = AsyncMock(return_value="new-token")
    await listener._async_check()
    assert listener._failures == 0
    mock_account.associate.assert_awaited_with("new-token")


async def test_periodic_reassociate_and_reauth(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_account: MagicMock
) -> None:
    """Re-associates after 12 h; an auth error then starts reauth."""
    listener = setup_entry.runtime_data
    listener._associated_at -= 13 * 3600
    await listener._async_check()
    mock_account.associate.assert_awaited_with("fcm-token")

    listener._associated_at -= 13 * 3600
    mock_account.associate.side_effect = BlinkAuthError
    await listener._async_check()
    await hass.async_block_till_done()
    assert any(setup_entry.async_get_active_flows(hass, {"reauth"}))


async def test_new_fcm_credentials_saved(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Re-registered FCM credentials persist in the entry."""
    setup_entry.runtime_data._save_credentials({"fcm": "new"})
    assert setup_entry.data[CONF_FCM] == {"fcm": "new"}
