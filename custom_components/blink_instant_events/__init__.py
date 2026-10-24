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
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import BlinkAccount, BlinkAuth, PushAuth
from .clips import Clips
from .const import CONF_AUTH, CONF_PUSH_AUTH, DOMAIN
from .coordinator import BlinkCoordinator
from .listener import BlinkPushListener, ask_push_sign_in
from .liveview import LiveViews
from .settings import BlinkSettings
from .tv import async_setup_services
from .views import async_register_views
from .vision import Vision

PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
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
    vision: Vision


type BlinkInstantConfigEntry = ConfigEntry[BlinkData]


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Actions exist even while the account is being set up."""
    async_setup_services(hass)
    return True


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
    blink.auth = BlinkAuth(session, dict(entry.data[CONF_AUTH]), save_auth)
    coordinator = BlinkCoordinator(hass, entry, blink)
    await coordinator.async_config_entry_first_refresh()

    ffmpeg = get_ffmpeg_manager(hass).binary
    clips = Clips(hass, blink, ffmpeg)
    vision = Vision(hass, entry, coordinator, clips, ffmpeg)

    @callback
    def on_push(camera_id: str, event_type: str) -> None:
        coordinator.async_on_push(camera_id, event_type)
        vision.async_on_push(camera_id, event_type)

    @callback
    def save_push_auth(push_auth: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PUSH_AUTH: push_auth})

    # Without the Android sign-in Blink sends no pushes; everything else works from polling.
    push_auth = PushAuth(session, dict(entry.data.get(CONF_PUSH_AUTH) or {}), save_push_auth)
    listener = BlinkPushListener(hass, entry, BlinkAccount(session, push_auth), push_devices(blink), on_push)
    if not push_auth.data:
        ask_push_sign_in(hass, entry)
    else:
        ir.async_delete_issue(hass, DOMAIN, f"push_sign_in_{entry.entry_id}")
        try:
            await listener.async_start()
        except UnauthorizedError:
            await listener.async_stop()
            ask_push_sign_in(hass, entry)
        except (ClientError, TimeoutError, RuntimeError) as err:
            await listener.async_stop()
            raise ConfigEntryNotReady(f"Blink push setup failed: {err}") from err

    live = LiveViews(hass, coordinator, ffmpeg)
    settings = BlinkSettings(hass, entry, coordinator, live)
    await settings.async_refresh()  # not fatal: the setting entities stay unavailable until Blink answers
    await vision.async_start()  # not fatal either: smart detection stays off until Blink Vision answers
    entry.runtime_data = BlinkData(coordinator, listener, clips, live, settings, vision)
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
