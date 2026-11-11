"""The RTSP provider: any camera that serves an RTSP stream on your network, no account or cloud involved.

Motion is measured on the stream itself (how much of a small greyscale copy changed between frames), then
the frames around it go through the shared pipeline. Optional recording copies the video without
re-encoding (recorder.py). Home Assistant's own stream serves the live view from the same address.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from PIL import Image

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from ...ai.pipeline import async_start_pipeline
from ...const import (
    CONF_COOLDOWN,
    CONF_FPS,
    CONF_FRAMES,
    CONF_KEEP_DAYS,
    CONF_KEYFRAMES_ONLY,
    CONF_MAX_GB,
    CONF_POST,
    CONF_PRE,
    CONF_RECORD,
    CONF_SENSITIVITY,
    CONF_WIDTH,
    DOMAIN,
    EVENT_RECORDING,
    PROVIDER_RTSP,
    SIGNAL_EVENT,
    SIGNAL_STREAM,
)
from ..base import CameraEvent
from ..local import LocalConfigEntry, LocalData, local_device, local_source
from ..local.watcher import DEFAULT_COOLDOWN, Watcher
from .recorder import CLEANUP_EVERY, Recorder, RecordOptions
from .stream import RtspReader, StreamOptions, measure
from .url import with_credentials

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.EVENT, Platform.IMAGE, Platform.SENSOR]
HEALTH_EVERY = timedelta(seconds=15)

DEFAULTS: dict[str, Any] = {
    CONF_FPS: 2.0,
    CONF_WIDTH: 640,
    CONF_KEYFRAMES_ONLY: False,
    CONF_SENSITIVITY: 2.0,
    CONF_FRAMES: 6,
    CONF_COOLDOWN: DEFAULT_COOLDOWN,
    CONF_RECORD: "off",
    CONF_PRE: 5,
    CONF_POST: 10,
    CONF_KEEP_DAYS: 7,
    CONF_MAX_GB: 10.0,
}


def option(entry: ConfigEntry, key: str) -> Any:
    """The entry's option, or its default."""
    return entry.options.get(key, DEFAULTS[key])


def stream_url(entry: ConfigEntry) -> str:
    """The address with its credentials, for ffmpeg and Home Assistant's stream only."""
    return with_credentials(entry.data[CONF_URL], entry.data.get(CONF_USERNAME), entry.data.get(CONF_PASSWORD))


async def async_setup_entry(hass: HomeAssistant, entry: LocalConfigEntry) -> bool:
    """Start reading the stream; entities show it as disconnected until frames arrive."""
    ffmpeg = get_ffmpeg_manager(hass).binary
    source = local_source(entry, PROVIDER_RTSP)
    recording = RecordOptions(
        mode=option(entry, CONF_RECORD),
        pre=int(option(entry, CONF_PRE)),
        post=int(option(entry, CONF_POST)),
        keep_days=int(option(entry, CONF_KEEP_DAYS)),
        max_gb=float(option(entry, CONF_MAX_GB)),
    )
    recorder = Recorder(hass, ffmpeg, source.slug, entry.title, recording) if recording.mode != "off" else None
    if recorder:
        await recorder.async_prepare()
        await recorder.async_cleanup()

    @callback
    def on_motion(start: datetime) -> None:
        if recorder and recorder.enabled:
            entry.async_create_background_task(hass, save_clip(start), f"{DOMAIN} clip {entry.title}")

    async def save_clip(start: datetime) -> None:
        assert recorder
        if (path := await recorder.async_clip(start)) is None:
            return
        event = CameraEvent(source.camera_id, PROVIDER_RTSP, EVENT_RECORDING, "local", True, start)
        async_dispatcher_send(
            hass,
            SIGNAL_EVENT.format(entry.entry_id, source.camera_id),
            EVENT_RECORDING,
            event.attributes() | {"camera": source.name, "recording": recorder.media_content_id(path)},
        )

    watcher = Watcher(
        hass,
        entry,
        source,
        frames=int(option(entry, CONF_FRAMES)),
        cooldown=int(option(entry, CONF_COOLDOWN)),
        on_motion=on_motion,
    )
    sensitivity = float(option(entry, CONF_SENSITIVITY)) / 100
    previous: list[Image.Image] = []

    async def on_frame(jpeg: bytes) -> None:
        watcher.frame(jpeg)
        try:
            thumb, moved = await hass.async_add_executor_job(measure, previous[0] if previous else None, jpeg)
        except OSError:
            return  # a damaged frame; the next one will do
        previous[:] = [thumb]
        if moved >= sensitivity:
            watcher.motion_seen()

    @callback
    def on_state() -> None:
        async_dispatcher_send(hass, SIGNAL_STREAM.format(entry.entry_id))

    options = StreamOptions(
        fps=float(option(entry, CONF_FPS)),
        width=int(option(entry, CONF_WIDTH)),
        keyframes_only=bool(option(entry, CONF_KEYFRAMES_ONLY)),
        segments=recorder.segments if recorder and recorder.enabled else None,
        segment_seconds=recorder.segment_seconds if recorder else 2,
    )
    reader = RtspReader(
        ffmpeg, stream_url(entry), entry.data.get(CONF_PASSWORD), options, on_frame, on_state, entry.title
    )
    entry.runtime_data = LocalData(
        source, watcher, local_device(entry, "RTSP stream"), reader=reader, recorder=recorder
    )
    await async_start_pipeline(hass, entry)
    entry.async_create_background_task(hass, reader.run(), f"{DOMAIN} stream {entry.title}")

    @callback
    def health(_now: datetime) -> None:
        on_state()  # the connection sensor notices a stream that went quiet without ffmpeg exiting

    entry.async_on_unload(async_track_time_interval(hass, health, HEALTH_EVERY))

    if recorder:

        async def cleanup(_now: datetime) -> None:
            if await recorder.async_cleanup():  # low on space, or space again: restart with or without segments
                reader.options = replace(reader.options, segments=recorder.segments if recorder.enabled else None)
                await reader.restart()

        entry.async_on_unload(async_track_time_interval(hass, cleanup, CLEANUP_EVERY))

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LocalConfigEntry) -> bool:
    """Stop ffmpeg."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.reader:
        await entry.runtime_data.reader.stop()
    return unloaded
