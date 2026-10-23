"""Fixtures: a configured entry with blinkpy, Blink REST and FCM mocked out."""

from __future__ import annotations

from collections.abc import Generator
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from blinkpy.camera import BlinkCamera
from blinkpy.sync_module import BlinkSyncModule
from firebase_messaging import FcmPushClientRunState
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests.structures import CaseInsensitiveDict

from custom_components.blink_instant_events.const import CONF_AUTH, CONF_FCM, DOMAIN
from homeassistant.core import HomeAssistant

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
    """Push-client REST calls."""
    base = "custom_components.blink_instant_events.api.BlinkAccount"
    with (
        patch(f"{base}.async_associate", AsyncMock()) as associate,
        patch(f"{base}.async_send_receipt", AsyncMock()) as receipt,
    ):
        yield MagicMock(associate=associate, receipt=receipt)


def _camera(sync: MagicMock, **attrs: object) -> MagicMock:
    cam = MagicMock(spec=BlinkCamera)
    cam.sync = sync
    cam.async_arm = AsyncMock(return_value={"id": 1})
    cam.update = AsyncMock()
    cam.snap_picture = AsyncMock(return_value={"id": 2})
    cam.last_record = None
    cam.version = "1.0"
    cam._cached_image = cam.image_from_cache = b"jpeg-" + str(attrs["serial"]).encode()
    cam.thumbnail = None
    cam.status = "online"
    for key, value in attrs.items():
        setattr(cam, key, value)
    cam.attributes = {"name": attrs["name"], "serial": attrs["serial"], "thumbnail": "https://rest-u001/thumb?ts=1"}
    return cam


@pytest.fixture
def mock_blink() -> Generator[MagicMock]:
    """A blinkpy Blink with one sync module, an outdoor camera and a doorbell."""
    blink = MagicMock()
    blink.urls = None
    blink.homescreen = {}
    blink.setup_urls.side_effect = lambda: setattr(blink, "urls", SimpleNamespace(base_url="https://rest-u001"))
    blink.setup_post_verify = AsyncMock(return_value=True)
    blink.refresh = AsyncMock(return_value=True)
    blink.get_homescreen = AsyncMock()

    sync = MagicMock(spec=BlinkSyncModule)
    sync.blink = blink
    sync.name = "home"
    sync.network_id = "200001"
    sync.serial = "SYNC-0001"
    sync.version = "4.0"
    sync.status = "online"
    sync.available = True
    sync.local_storage = False
    sync._local_storage = {"manifest": [], "last_manifest_id": None}
    sync.network_info = {"network": {"armed": False}}
    sync.attributes = {"name": "home", "serial": "SYNC-0001"}
    sync.get_network_info = AsyncMock(return_value=True)
    sync.get_camera_info = AsyncMock(return_value={"id": "100001"})
    sync.get_unique_info.return_value = None

    async def arm(value: bool) -> dict:
        sync.network_info = {"network": {"armed": value}}
        return {"id": 3, "network_id": 200001}

    sync.async_arm = AsyncMock(side_effect=arm)
    blink.sync = CaseInsensitiveDict({"home": sync})
    blink.cameras = CaseInsensitiveDict(
        {
            "Backyard": _camera(
                sync,
                name="Backyard",
                camera_id="100001",
                serial="G8T1-0001",
                network_id="200001",
                camera_type="",
                product_type="sedona",
                temperature=70,
                temperature_calibrated=68,
                battery_state="ok",
                battery_level=3,
                battery_voltage=165,
                wifi_strength=-55,
                arm=True,
            ),
            "Front Door": _camera(
                sync,
                name="Front Door",
                camera_id="100002",
                serial="G8T1-0002",
                network_id="200001",
                camera_type="doorbell",
                product_type="tulip",
                temperature=None,
                temperature_calibrated=None,
                battery_state="low",
                battery_level=None,
                battery_voltage=None,
                wifi_strength=4,
                arm=False,
            ),
        }
    )
    # Camera settings, read from Blink's cloud: night vision on auto (code 2).
    settings = AsyncMock(return_value={"camera": [{"illuminator_enable": 2}]})
    with (
        patch("custom_components.blink_instant_events.Blink", return_value=blink),
        patch("custom_components.blink_instant_events.controls.blink_api.request_camera_info", settings),
    ):
        yield blink


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_fcm: MagicMock,
    mock_account: MagicMock,
    mock_blink: MagicMock,
) -> MockConfigEntry:
    """Set up the integration."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
