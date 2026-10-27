"""Setup errors, reconnect with backoff, re-association, reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError
from blinkpy.auth import UnauthorizedError
from firebase_messaging import FcmPushClientRunState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_instant_events.const import CONF_FCM, CONF_PUSH_AUTH, DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir


async def test_setup_retries_when_blink_down(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_fcm: MagicMock,
    mock_account: MagicMock,
    mock_blink: MagicMock,
) -> None:
    """Network errors → retry later; rejected session → reauth."""
    mock_blink.setup_post_verify.side_effect = ClientError
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    mock_blink.setup_post_verify.side_effect = UnauthorizedError
    mock_blink.urls = None
    await hass.config_entries.async_reload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(config_entry.async_get_active_flows(hass, {"reauth"}))


async def test_reconnect_with_backoff(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_fcm: MagicMock, mock_account: MagicMock
) -> None:
    """A dead client is replaced; a failed attempt delays the next one."""
    listener = setup_entry.runtime_data.listener
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


async def test_unload_after_push_never_started(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_fcm: MagicMock
) -> None:
    """A client that never got to start (Blink unreachable) is not stopped: firebase_messaging would raise."""
    client = mock_fcm.return_value
    client.run_state = FcmPushClientRunState.CREATED
    client.stop.reset_mock()
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    client.stop.assert_not_awaited()


async def test_periodic_reassociate_and_reauth(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_account: MagicMock
) -> None:
    """Re-associates after 12 h; an auth error then asks for the push sign-in again."""
    listener = setup_entry.runtime_data.listener
    listener._associated_at -= 13 * 3600
    await listener._async_check()
    mock_account.associate.assert_awaited_with("fcm-token")

    listener._associated_at -= 13 * 3600
    mock_account.associate.side_effect = UnauthorizedError
    await listener._async_check()
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"push_sign_in_{setup_entry.entry_id}")
    assert not any(setup_entry.async_get_active_flows(hass, {"reauth"}))


async def test_new_fcm_credentials_saved(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Re-registered FCM credentials persist in the entry."""
    setup_entry.runtime_data.listener._save_credentials({"fcm": "new"})
    assert setup_entry.data[CONF_FCM] == {"fcm": "new"}


async def test_no_push_sign_in_yet(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_fcm: MagicMock,
    mock_account: MagicMock,
    mock_blink: MagicMock,
) -> None:
    """Without the Android sign-in: set up anyway, no push client, a repair asks for it."""
    data = dict(config_entry.data)
    data.pop(CONF_PUSH_AUTH)
    config_entry = MockConfigEntry(domain=DOMAIN, title=config_entry.title, unique_id="99", data=data)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED
    mock_fcm.assert_not_called()
    mock_account.associate.assert_not_awaited()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"push_sign_in_{config_entry.entry_id}")
    assert hass.states.get("binary_sensor.blink_instant_events_push_connection").state == "off"
