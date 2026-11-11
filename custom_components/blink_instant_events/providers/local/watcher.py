"""Motion to detection for local cameras: a motion event at once, then a few frames through the pipeline.

A stream (RTSP) pushes every frame in and reports motion it measured; a Home Assistant camera follows a
motion sensor and is asked for pictures. Either way one analysis runs at a time per camera, at most once
per `cooldown` seconds, and motion that never stops doesn't analyze forever.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
import logging
import time

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ...ai.pipeline import FRAMES, detection_key, get_pipeline
from ...const import EVENT_MOTION, MOTION_RESET_SECONDS, SIGNAL_EVENT, SIGNAL_MOTION
from ..base import CameraEvent, Source

_LOGGER = logging.getLogger(__name__)

DEFAULT_COOLDOWN = 30
COLLECT_SECONDS = 3  # of video after motion starts, on top of the frames just before it


class Watcher:
    """One local camera's motion state and its analyses."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        source: Source,
        *,
        grab: Callable[[], Awaitable[bytes | None]] | None = None,
        frames: int = 5,
        cooldown: int = DEFAULT_COOLDOWN,
        on_motion: Callable[[datetime], None] | None = None,
    ) -> None:
        """`grab` takes a picture when there is no stream pushing frames in."""
        self.hass = hass
        self.entry = entry
        self.source = source
        self.pipeline = get_pipeline(hass)
        self.motion = False
        self.analyses = 0
        self.skipped = 0
        self._grab = grab
        self._frames = max(1, min(frames, FRAMES))
        self._cooldown = cooldown
        self._on_motion = on_motion
        self._recent: deque[bytes] = deque(maxlen=self._frames)
        self._reset: CALLBACK_TYPE | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_analysis = -float("inf")
        entry.async_on_unload(self._cancel)

    @callback
    def frame(self, jpeg: bytes) -> None:
        """A frame from the stream (the newest few are what an analysis reads)."""
        self._recent.append(jpeg)

    @callback
    def motion_seen(self) -> None:
        """The stream moved: motion until MOTION_RESET_SECONDS without any."""
        if self._reset:
            self._reset()
        self._reset = async_call_later(self.hass, MOTION_RESET_SECONDS, self._quiet)
        self.set_motion(True)

    @callback
    def _quiet(self, _now: datetime) -> None:
        self._reset = None
        self.set_motion(False)

    @callback
    def set_motion(self, on: bool) -> None:
        """Motion as a sensor reports it."""
        if on != self.motion:
            self.motion = on
            if on:
                event = CameraEvent(self.source.camera_id, self.source.provider, EVENT_MOTION, self.source.location)
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_EVENT.format(self.entry.entry_id, self.source.camera_id),
                    EVENT_MOTION,
                    event.attributes() | {"camera": self.source.name},
                )
                if self._on_motion:
                    self._on_motion(event.timestamp)
            async_dispatcher_send(self.hass, SIGNAL_MOTION.format(self.entry.entry_id))
        if on:
            self._maybe_analyze()

    @callback
    def _maybe_analyze(self) -> None:
        if (self._task and not self._task.done()) or not self.pipeline.enabled:
            return
        if time.monotonic() - self._last_analysis < self._cooldown:
            self.skipped += 1
            return
        self._last_analysis = time.monotonic()
        self._task = self.entry.async_create_background_task(
            self.hass, self.async_analyze(), f"{self.source.name} detection"
        )

    async def async_analyze(self, created: datetime | None = None) -> dict | None:
        """Look now: the frames around this moment through the pipeline."""
        created = created or dt_util.utcnow()
        jpegs: list[bytes] = []
        try:
            if self._grab is not None:
                for index in range(self._frames):
                    if index:
                        await asyncio.sleep(1)
                    if picture := await self._grab():
                        jpegs.append(picture)
            else:
                await asyncio.sleep(COLLECT_SECONDS)
                jpegs = list(self._recent)
            key = detection_key(self.source.entry_id, self.source.slug, created.isoformat())
            row = await self.pipeline.async_analyze(self.source, key, jpegs, created)
        except (ClientError, RuntimeError, OSError, TimeoutError, ValueError) as err:
            _LOGGER.warning("%s: could not analyze the motion: %s", self.source.name, err)
            return None
        self.analyses += 1
        return row

    @callback
    def _cancel(self) -> None:
        if self._reset:
            self._reset()
        if self._task:
            self._task.cancel()
