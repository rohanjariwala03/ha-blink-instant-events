"""Copies new Blink clips to the media folder and keeps them for a number of days.

Clips come from Blink's cloud or the Sync Module's USB drive (both fetched through Blink, see clips.py).
They are copied one at a time, at most once an hour, and never while a live view is open: each USB clip
makes the Sync Module upload it first, and a busy module makes the Blink app say "system busy".
Files land in Media › My media › blink_instant_events › blink › <camera> › <date>, named by time and source.
Whether a clip was copied is the file being there, so nothing else needs storing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from ...const import BLINK_ERRORS, CONF_ARCHIVE, CONF_ARCHIVE_DAYS, DEFAULT_ARCHIVE_DAYS
from ..rtsp.recorder import MIN_FREE_BYTES, _files, media_root
from .clips import Clips, clip_id
from .liveview import LiveViews

_LOGGER = logging.getLogger(__name__)

EVERY = timedelta(hours=1)
FIRST_RUN_SECONDS = 120  # after setup, once the first poll and the push client have settled
LOOKBACK_HOURS = 25  # a little over a day, so a run missed while Home Assistant was off is caught up
PAGES = 3
LIMIT = 100


def target(root: Path, clip: dict[str, Any]) -> Path:
    """Where a clip goes: <camera>/<local date>/<HHMMSS>-<cloud|local>.mp4."""
    created: datetime = dt_util.as_local(clip["created_at"])
    return root / clip["slug"] / created.strftime("%Y-%m-%d") / f"{created.strftime('%H%M%S')}-{clip['source']}.mp4"


class Archiver:
    """One Blink account's clip archive."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, clips: Clips, live: LiveViews) -> None:
        """Initialize; async_start begins the hourly runs."""
        self.hass = hass
        self.entry = entry
        self.clips = clips
        self.live = live
        self.root = media_root(hass) / "blink"
        self.copied = 0  # this session, for diagnostics

    def async_start(self) -> None:
        """Hourly, and once shortly after setup."""
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._async_tick, EVERY))
        self.entry.async_on_unload(async_call_later(self.hass, FIRST_RUN_SECONDS, self._async_tick))

    async def _async_tick(self, _now: datetime | None = None) -> None:
        if not self.entry.options.get(CONF_ARCHIVE):
            return
        self.entry.async_create_background_task(self.hass, self.async_run(), "blink_instant_events archive")

    async def async_run(self) -> int:
        """Copy the clips not archived yet, then remove old ones; how many were copied."""
        if self.live.sessions:
            return 0  # next hour; a live view and a USB upload don't mix
        try:
            clips = await self.clips.async_list("both", None, hours=LOOKBACK_HOURS, pages=PAGES, limit=LIMIT)
        except BLINK_ERRORS as err:
            _LOGGER.debug("Could not list Blink clips to archive: %s", err)
            return 0
        copied = 0
        for clip in sorted(clips, key=lambda clip: clip["created_at"]):
            path = target(self.root, clip)
            if await self.hass.async_add_executor_job(path.is_file):
                continue
            if self.live.sessions or await self.hass.async_add_executor_job(self._low_space):
                break
            try:
                source = await self.clips.async_clip(clip_id(clip), clip)
            except (*BLINK_ERRORS, LookupError) as err:
                _LOGGER.warning(
                    "Could not archive the %s clip from %s: %s", clip["camera_name"], clip["created_at"], err
                )
                continue
            await self.hass.async_add_executor_job(self._copy, source, path, clip["created_at"].timestamp())
            copied += 1
        self.copied += copied
        await self.hass.async_add_executor_job(self._cleanup)
        return copied

    def _low_space(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        if low := shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
            _LOGGER.warning("Under 2 GB free: Blink clips are not archived until there is room")
        return low

    @staticmethod
    def _copy(source: Path, path: Path, created: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        shutil.copyfile(source, partial)
        os.utime(partial, (created, created))  # retention counts from when the clip was recorded
        os.replace(partial, path)

    def _cleanup(self) -> None:
        days = int(self.entry.options.get(CONF_ARCHIVE_DAYS, DEFAULT_ARCHIVE_DAYS))
        cutoff = time.time() - days * 86400
        for mtime, _size, path in _files(self.root, ".mp4"):
            if mtime >= cutoff:
                break
            path.unlink(missing_ok=True)
