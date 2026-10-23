"""blink_instant_events.show_on_tv: a camera's live view on a TV (Cast), e.g. when the doorbell rings."""

from __future__ import annotations

from urllib.parse import quote

import voluptuous as vol

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_MEDIA_STOP,
    STATE_OFF,
    STATE_STANDBY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN

SERVICE_SHOW_ON_TV = "show_on_tv"
SCHEMA = vol.Schema(
    {
        vol.Required("camera"): cv.entity_id,
        vol.Required("media_player"): cv.entity_id,
        vol.Optional("duration", default=60): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
        vol.Optional("only_if_on", default=True): cv.boolean,
    }
)
TV_OFF = {STATE_OFF, STATE_STANDBY, STATE_UNAVAILABLE, STATE_UNKNOWN}


def cast_entity(hass: HomeAssistant, entity_id: str) -> str:
    """The entity that can play a URL: this one if it's Cast, else the Cast entity on the same TV."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.platform == "cast" or entry.device_id is None:
        return entity_id
    devices = dr.async_get(hass)
    device = devices.async_get(entry.device_id)
    same = {entry.device_id}
    if device is not None:
        # Cast and the TV's own integration may make two devices for one TV; they share a MAC or a name.
        same |= {
            other.id
            for other in devices.devices.values()
            if (other.connections & device.connections)
            or (other.name_by_user or other.name) == (device.name_by_user or device.name)
        }
    return next(
        (
            other.entity_id
            for other in registry.entities.values()
            if other.platform == "cast" and other.device_id in same and other.domain == MEDIA_PLAYER_DOMAIN
        ),
        entity_id,
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the action (once per Home Assistant run)."""

    async def show_on_tv(call: ServiceCall) -> None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError("Blink Instant Events is not loaded")
        live = entries[0].runtime_data.live
        camera = hass.states.get(call.data["camera"])
        slug = camera and (camera.attributes.get("snapshot_of") or camera.attributes.get("proxy_slug"))
        if not slug or live.camera(slug) is None:
            raise ServiceValidationError(f"{call.data['camera']} is not a Blink Instant Events camera")
        tv = hass.states.get(call.data["media_player"])
        if call.data["only_if_on"] and (tv is None or tv.state in TV_OFF):
            return
        try:
            base = get_url(hass, allow_external=False, allow_cloud=False)
        except NoURLAvailableError as err:
            raise ServiceValidationError(
                "Set Home Assistant's local network URL first (Settings › System › Network)"
            ) from err
        url = (
            f"{base}/api/blink_instant_events/cameras/{slug}/hls/index.m3u8"
            f"?token={quote(live.issue_token(slug), safe='')}&session=tv"
        )
        target = cast_entity(hass, call.data["media_player"])
        await hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: target,
                ATTR_MEDIA_CONTENT_ID: url,
                ATTR_MEDIA_CONTENT_TYPE: "application/vnd.apple.mpegurl",
                ATTR_MEDIA_EXTRA: {"stream_type": "LIVE", "metadata": {"title": camera.name}},
            },
            blocking=True,
        )

        # The camera stops by itself once nobody fetches the stream (a phone may still be watching).
        async def stop(_now: object) -> None:
            await hass.services.async_call(MEDIA_PLAYER_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: target})

        async_call_later(hass, call.data["duration"], stop)

    hass.services.async_register(DOMAIN, SERVICE_SHOW_ON_TV, show_on_tv, schema=SCHEMA)
