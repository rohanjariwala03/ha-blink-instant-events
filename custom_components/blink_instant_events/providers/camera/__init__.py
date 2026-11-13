"""The Home Assistant camera provider: smart detection for a camera another integration already provides.

This is how ONVIF cameras are added: Home Assistant's ONVIF integration discovers them, reads their
profiles and stream address, and turns their motion events into a binary sensor. Pick that camera and that
sensor here; when the sensor turns on, a few pictures go through the shared pipeline. Generic cameras,
Reolink, UniFi Protect and others work the same way.
"""

from __future__ import annotations

import logging

from homeassistant.components.camera import async_get_image
from homeassistant.const import STATE_ON, Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from ...ai.pipeline import async_start_pipeline
from ...const import CONF_CAMERA, CONF_COOLDOWN, CONF_FRAMES, CONF_MOTION, PROVIDER_CAMERA
from ..local import LocalConfigEntry, LocalData, local_device, local_source
from ..local.watcher import DEFAULT_COOLDOWN, Watcher

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.EVENT, Platform.IMAGE, Platform.SENSOR]
IMAGE_TIMEOUT = 10


async def async_setup_entry(hass: HomeAssistant, entry: LocalConfigEntry) -> bool:
    """Follow the motion sensor."""
    camera, motion = entry.data[CONF_CAMERA], entry.data[CONF_MOTION]

    async def grab() -> bytes | None:
        try:
            return (await async_get_image(hass, camera, timeout=IMAGE_TIMEOUT)).content
        except HomeAssistantError as err:
            _LOGGER.debug("%s gave no picture: %s", camera, err)
            return None

    watcher = Watcher(
        hass,
        entry,
        local_source(entry, PROVIDER_CAMERA),
        grab=grab,
        frames=int(entry.options.get(CONF_FRAMES, 5)),
        cooldown=int(entry.options.get(CONF_COOLDOWN, DEFAULT_COOLDOWN)),
    )
    entry.runtime_data = LocalData(
        watcher.source, watcher, local_device(entry, "Home Assistant camera"), camera_entity=camera
    )
    await async_start_pipeline(hass, entry)

    @callback
    def on_motion(event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None:
            watcher.set_motion(state.state == STATE_ON)

    entry.async_on_unload(async_track_state_change_event(hass, motion, on_motion))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: LocalConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LocalConfigEntry) -> bool:
    """Nothing runs besides the platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
