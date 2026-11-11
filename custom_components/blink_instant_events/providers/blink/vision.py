"""When Blink cameras get looked at: every new clip, or a few seconds of live view without a subscription.

A push starts looking for the motion's clip right away; Blink usually lists it 15-40 s later. Without
pushes, a poll that shows a new clip does the same, just later. The looking itself is ai/pipeline.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from blinkpy.camera import BlinkCamera

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util, slugify

from ...ai.pipeline import PROBE_INTERVAL, Pipeline, detection_key, get_pipeline
from ...const import BLINK_ERRORS, CONF_LIVE_LOOK, DOMAIN, PROVIDER_BLINK
from ..base import Source
from .clips import Clips, clip_id
from .coordinator import BlinkCoordinator
from .entity import camera_slug

if TYPE_CHECKING:
    from .liveview import LiveViews

_LOGGER = logging.getLogger(__name__)

WAIT_FOR_CLIP = (10, 10, 10, 15, 15, 20, 30, 40)  # after a push: ~2.5 min of looking, most often found by ~30 s
LOOK_SECONDS = 8
GONE = "Gone before the camera looked"  # of live view after a push, when there are no clips to read
BACKFILL = timedelta(minutes=15)  # clips older than this at startup are not analyzed


class Vision:
    """Blink's triggers for the shared pipeline: pushes, polls and live looks."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: BlinkCoordinator, clips: Clips) -> None:
        """Initialize; call async_start once setup is done."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.clips = clips
        self.pipeline: Pipeline = get_pipeline(hass)
        self.live: LiveViews | None = None  # set once live view is set up; used when there are no clips
        self._lock = asyncio.Lock()  # scans of this account never race each other
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._records: dict[str, str] = {}  # camera serial -> last_record seen, to notice new clips on a poll
        self._started = dt_util.utcnow()

    @property
    def enabled(self) -> bool:
        """Whether the add-on answered."""
        return self.pipeline.enabled

    async def async_start(self) -> None:
        """Load past results, find the add-on, follow polls."""
        await self.pipeline.async_load()
        await self.pipeline.async_probe()
        self.entry.async_on_unload(async_track_time_interval(self.hass, self.pipeline.async_probe, PROBE_INTERVAL))
        self.entry.async_on_unload(self.coordinator.async_add_listener(self._on_refresh))
        self.entry.async_on_unload(self._cancel_watchers)

    def source(self, camera: BlinkCamera) -> Source:
        """The camera as the pipeline sees it."""
        return Source(
            self.entry.entry_id, PROVIDER_BLINK, str(camera.camera_id), camera_slug(camera), camera.name, "cloud"
        )

    # ------------------------------------------------------------- triggers

    @callback
    def async_on_push(self, camera_id: str, _event_type: str) -> None:
        """Motion or a doorbell press: look for its clip until it shows up."""
        camera = next((cam for cam in self.coordinator.blink.cameras.values() if str(cam.camera_id) == camera_id), None)
        if camera is None or not self.enabled:
            return
        if task := self._watchers.get(camera.serial):
            task.cancel()
        self._watchers[camera.serial] = self.entry.async_create_background_task(
            self.hass, self._watch(camera), f"{DOMAIN} vision {camera.name}"
        )

    async def _watch(self, camera: BlinkCamera) -> None:
        try:
            if self.entry.options.get(CONF_LIVE_LOOK):
                await self._look(camera)
                return
            for delay in WAIT_FOR_CLIP:
                await asyncio.sleep(delay)
                if await self.async_scan(camera.name):
                    return
            _LOGGER.debug("No new clip from %s after a push", camera.name)
        finally:
            if self._watchers.get(camera.serial) is asyncio.current_task():
                del self._watchers[camera.serial]

    @callback
    def _on_refresh(self) -> None:
        """A poll that lists a newer clip than last time (pushes or not) gets it analyzed."""
        for camera in self.coordinator.blink.cameras.values():
            record = str(camera.last_record)
            seen = self._records.get(camera.serial)
            self._records[camera.serial] = record
            if seen is not None and record != seen and self.enabled and camera.serial not in self._watchers:
                self.entry.async_create_background_task(
                    self.hass, self.async_scan(camera.name), f"{DOMAIN} vision poll {camera.name}"
                )

    @callback
    def _cancel_watchers(self) -> None:
        for task in self._watchers.values():
            task.cancel()

    # ------------------------------------------------------------- analysis

    async def async_scan(self, camera_name: str) -> int:
        """Analyze this camera's clips that are new since startup; how many there were."""
        async with self._lock:
            try:
                clips = await self.clips.async_list("both", slugify(camera_name), hours=1, pages=1, limit=5)
            except BLINK_ERRORS as err:
                _LOGGER.debug("Could not list clips for %s: %s", camera_name, err)
                return 0
            since = self._started - BACKFILL
            rows = self.pipeline.rows
            new = [clip for clip in reversed(clips) if clip_id(clip) not in rows and clip["created_at"] >= since]
            for clip in new:
                try:
                    await self._analyze(clip)
                except (*BLINK_ERRORS, LookupError) as err:
                    _LOGGER.warning("Could not analyze the %s clip from %s: %s", camera_name, clip["created_at"], err)
            return len(new)

    async def _look(self, camera: BlinkCamera) -> None:
        """No clips without a Blink subscription: watch a few seconds of live view and read that instead."""
        slug = camera_slug(camera)
        if self.live is None or slug in self.live.sessions:
            return  # someone is already watching it
        created = dt_util.utcnow()
        try:
            session = await self.live.get_or_start(slug, "")
            try:
                await session.wait_ready()
                await asyncio.sleep(LOOK_SECONDS)
            finally:
                # A viewer who joined meanwhile keeps the session; their live view isn't ours to end or read.
                mine = self.live.sessions.get(slug) is session and not any(
                    viewer.startswith(f"{slug}:") for viewer in self.live.viewers
                )
                if mine:
                    await self.live.async_stop(slug)
            if not mine or (path := await self.live.last_liveview_mp4(slug)) is None:
                return
            await self._analyze_file(detection_key("live", slug, created.isoformat()), path, camera, created, GONE)
        except (*BLINK_ERRORS, LookupError) as err:
            _LOGGER.warning("Could not look at %s with live view: %s", camera.name, err)

    async def _analyze(self, clip: dict[str, Any]) -> None:
        key = clip_id(clip)
        camera = self.coordinator.blink.cameras.get(clip["camera_name"])
        if camera is None:
            raise LookupError(f"Blink no longer lists {clip['camera_name']}")
        await self._analyze_file(key, await self.clips.async_clip(key, clip), camera, clip["created_at"])

    async def _analyze_file(
        self, key: str, path: Path, camera: BlinkCamera, created: datetime, empty: str | None = None
    ) -> None:
        jpegs = await self.pipeline.async_frames(path)
        await self.pipeline.async_analyze(self.source(camera), key, jpegs, created, empty=empty)
