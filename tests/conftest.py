"""Fixtures: a configured entry with Blink REST and FCM mocked out."""

from __future__ import annotations

from collections.abc import Generator
import time
from unittest.mock import AsyncMock, MagicMock, patch

from firebase_messaging import FcmPushClientRunState
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_instant_events.const import CONF_AUTH, CONF_FCM, DOMAIN
from homeassistant.core import HomeAssistant

DEVICES = [
    {
        "id": "100001",
        "name": "Backyard",
        "serial": "G8T1-0001",
        "type": "sedona",
        "network_id": 200001,
        "doorbell": False,
    },
    {
        "id": "100002",
        "name": "Front Door",
        "serial": "G8T1-0002",
        "type": "tulip",
        "network_id": 200001,
        "doorbell": True,
    },
]
AUTH = {
    "username": "me@example.com",
    "token": "access-token",
    "refresh_token": "refresh-token",
    "expiration_date": time.time() + 3600,
    "hardware_id": "8D6F3F3E-0000-4000-8000-000000000000",
    "region_id": "u001",
    "account_id": 99,
}
FCM = {"fcm": {"registration": {"token": "fcm-token"}}, "gcm": {"android_id": "1", "security_token": "2"}}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A signed-in entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="me@example.com",
        unique_id="99",
        data={CONF_AUTH: AUTH, CONF_FCM: FCM},
    )


@pytest.fixture
def mock_fcm() -> Generator[MagicMock]:
    """FcmPushClient that 'connects' instantly."""
    with patch("custom_components.blink_instant_events.listener.FcmPushClient") as cls:
        client = cls.return_value
        client.checkin_or_register = AsyncMock(return_value="fcm-token")
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.is_started.return_value = True
        client.run_state = FcmPushClientRunState.STARTED
        client.credentials = FCM
        yield cls


@pytest.fixture
def mock_account() -> Generator[MagicMock]:
    """Blink REST calls."""
    base = "custom_components.blink_instant_events.api.BlinkAccount"
    with (
        patch(f"{base}.async_get_devices", AsyncMock(return_value=DEVICES)) as devices,
        patch(f"{base}.async_associate", AsyncMock()) as associate,
        patch(f"{base}.async_send_receipt", AsyncMock()) as receipt,
    ):
        yield MagicMock(devices=devices, associate=associate, receipt=receipt)


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_fcm: MagicMock, mock_account: MagicMock
) -> MockConfigEntry:
    """Set up the integration."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
