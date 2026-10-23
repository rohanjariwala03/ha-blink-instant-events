"""Blink Instant Events: Blink cameras in Home Assistant, with real-time events over FCM push."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError
from blinkpy.auth import UnauthorizedError
from blinkpy.blinkpy import Blink

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BlinkAccount, BlinkAuth
from .clips import Clips
from .const import CONF_AUTH
from .coordinator import BlinkCoordinator
from .listener import BlinkPushListener
from .liveview import LiveViews
from .settings import BlinkSettings
from .views import async_register_views

PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class BlinkData:
    """What the platforms share."""

    coordinator: BlinkCoordinator
    listener: BlinkPushListener
    clips: Clips
    live: LiveViews
    settings: BlinkSettings


type BlinkInstantConfigEntry = ConfigEntry[BlinkData]


def push_devices(blink: Blink) -> list[dict[str, Any]]:
    """The cameras as the push listener matches them."""
    return [
        {
            "id": str(cam.camera_id),
            "name": cam.name,
            "serial": cam.serial,
            "type": cam.product_type,
            "network_id": int(cam.network_id) if str(cam.network_id).isdigit() else cam.network_id,
            "doorbell": cam.camera_type == "doorbell",
        }
        for cam in blink.cameras.values()
    ]


async def async_setup_entry(hass: HomeAssistant, entry: BlinkInstantConfigEntry) -> bool:
    """Restore the stored Blink session, load the cameras, start the push listener."""

    @callback
    def save_auth(auth: dict[str, Any]) -> None:
        auth.pop("password", None)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_AUTH: auth})

    session = async_get_clientsession(hass)
    blink = Blink(session=session)
    blink.auth = auth = BlinkAuth(session, dict(entry.data[CONF_AUTH]), save_auth)
    coordinator = BlinkCoordinator(hass, entry, blink)
    await coordinator.async_config_entry_first_refresh()

    listener = BlinkPushListener(
        hass, entry, BlinkAccount(session, auth), push_devices(blink), coordinator.async_on_push
    )
    try:
        await listener.async_start()
    except (UnauthorizedError, ClientError, TimeoutError, RuntimeError) as err:
        await listener.async_stop()
        if isinstance(err, UnauthorizedError):
            raise ConfigEntryAuthFailed from err
        raise ConfigEntryNotReady(f"Blink push setup failed: {err}") from err

    ffmpeg = get_ffmpeg_manager(hass).binary
    live = LiveViews(hass, coordinator, ffmpeg)
    settings = BlinkSettings(hass, entry, coordinator, live)
    await settings.async_refresh()  # not fatal: the setting entities stay unavailable until Blink answers
    entry.runtime_data = BlinkData(coordinator, listener, Clips(hass, blink, ffmpeg), live, settings)
    live.async_start()
    async_register_views(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BlinkInstantConfigEntry) -> bool:
    """Stop the listener and unload platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.live.async_stop_all()
        await entry.runtime_data.listener.async_stop()
    return unloaded
