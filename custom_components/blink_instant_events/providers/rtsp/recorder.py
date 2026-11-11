"""Recording for RTSP cameras, without re-encoding: event clips with time before and after, or everything.

The reader's ffmpeg copies the video into short segments. In "events" mode they are a rolling buffer in
Home Assistant's cache: a motion event waits `post` seconds, then joins the segments from `pre` seconds
before it into one MP4 under the media folder (Media panel › My media › blink_instant_events). In
"continuous" mode the one-minute segments are the recording. Either way recordings older than
`keep_days` go, the oldest go first above `max_gb`, and recording stops while the disk has less than
MIN_FREE_BYTES free, so the SSD never fills up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import shutil
import time

from homeassistant.core import HomeAssistant

from ...const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MODES = ("off", "events", "continuous")
EVENT_SEGMENT_SECONDS = 2
CONTINUOUS_SEGMENT_SECONDS = 60
MIN_FREE_BYTES = 2 * 1024**3
CLEANUP_EVERY = timedelta(seconds=30)
BUFFER_SLACK_SECONDS = 30  # segments kept beyond pre + post, for events still being cut


@dataclass(frozen=True, slots=True)
class RecordOptions:
    """What to keep."""

    mode: str = "off"
    pre: int = 5
    post: int = 10
    keep_days: int = 7
    max_gb: float = 10.0


def media_root(hass: HomeAssistant) -> Path:
    """Where recordings go: Home Assistant's local media folder (/media on Home Assistant OS)."""
    return Path(hass.config.media_dirs.get("local") or hass.config.path("media")) / DOMAIN


def _files(folder: Path, suffix: str) -> list[tuple[float, int, Path]]:
    """(mtime, size, path) of the folder's files, oldest first."""
    if not folder.is_dir():
        return []
    found = []
    for path in folder.rglob(f"*{suffix}"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        found.append((stat.st_mtime, stat.st_size, path))
    return sorted(found)


class Recorder:
    """One camera's recordings."""

    def __init__(self, hass: HomeAssistant, ffmpeg: str, slug: str, label: str, options: RecordOptions) -> None:
        """Initialize; async_prepare before the reader starts writing."""
        self.hass = hass
        self.ffmpeg = ffmpeg
        self.label = label
        self.options = options
        self.clips = media_root(hass) / slug
        self.continuous = options.mode == "continuous"
        self.segments = (
            self.clips / "continuous" if self.continuous else Path(hass.config.cache_path(DOMAIN, "segments", slug))
        )
        self.segment_seconds = CONTINUOUS_SEGMENT_SECONDS if self.continuous else EVENT_SEGMENT_SECONDS
        self.low_space = False

    @property
    def enabled(self) -> bool:
        """Recording is on, and there is room for it."""
        return self.options.mode != "off" and not self.low_space

    async def async_prepare(self) -> None:
        """Create the folders and start from an empty buffer."""

        def prepare() -> None:
            if not self.continuous:
                shutil.rmtree(self.segments, ignore_errors=True)
            self.segments.mkdir(parents=True, exist_ok=True)
            self.clips.mkdir(parents=True, exist_ok=True)

        await self.hass.async_add_executor_job(prepare)

    def media_content_id(self, path: Path) -> str:
        """The clip as the Media panel and media players address it."""
        return f"media-source://media_source/local/{path.relative_to(media_root(self.hass).parent).as_posix()}"

    async def async_clip(self, start: datetime) -> Path | None:
        """After `post` seconds: the event from `pre` seconds before `start`, as one MP4 (None if nothing)."""
        if self.continuous or not self.enabled:
            return None
        await asyncio.sleep(self.options.post + self.segment_seconds)  # the last segment must be closed
        begin = start.timestamp() - self.options.pre
        end = start.timestamp() + self.options.post
        files = await self.hass.async_add_executor_job(_files, self.segments, ".ts")
        # The newest segment is still being written; each one's mtime is when it ended.
        parts = [path for mtime, _size, path in files[:-1] if mtime >= begin and mtime - self.segment_seconds <= end]
        if not parts:
            return None
        local = start.astimezone()
        target = self.clips / local.strftime("%Y-%m-%d") / f"{local.strftime('%H%M%S')}.mp4"
        return await self._join(parts, target)

    async def _join(self, parts: list[Path], target: Path) -> Path | None:
        listing = target.with_suffix(".txt")
        partial = target.with_suffix(".part")

        def write_listing() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            listing.write_text("".join(f"file '{path}'\n" for path in parts))

        await self.hass.async_add_executor_job(write_listing)
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            *("-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)),
            *("-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(partial)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(process.communicate(), 120)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        ok = process.returncode == 0
        await self.hass.async_add_executor_job(self._finish, listing, partial, target, ok)
        if not ok:
            _LOGGER.warning("%s: could not save the event clip: %s", self.label, err.decode(errors="replace")[-200:])
            return None
        return target

    @staticmethod
    def _finish(listing: Path, partial: Path, target: Path, ok: bool) -> None:
        listing.unlink(missing_ok=True)
        if ok:
            os.replace(partial, target)
        else:
            partial.unlink(missing_ok=True)

    async def async_cleanup(self) -> bool:
        """Retention and free space; True when recording has to pause or may resume (restart the reader)."""
        low = await self.hass.async_add_executor_job(self._cleanup)
        if low == self.low_space:
            return False
        self.low_space = low
        _LOGGER.warning(
            "%s: %s", self.label, "under 2 GB free: recording paused" if low else "space again: recording resumed"
        )
        return True

    def _cleanup(self) -> bool:
        now = time.time()
        if not self.continuous:
            keep = self.options.pre + self.options.post + BUFFER_SLACK_SECONDS
            for mtime, _size, path in _files(self.segments, ".ts")[:-1]:
                if now - mtime > keep:
                    path.unlink(missing_ok=True)
        recordings = [row for row in _files(self.clips, "") if row[2].suffix in (".mp4", ".ts")]
        if not self.continuous:
            recordings = [row for row in recordings if row[2].suffix == ".mp4"]
        else:
            recordings = recordings[:-1]  # never the segment being written
        cutoff = now - self.options.keep_days * 86400
        total = sum(size for _mtime, size, _path in recordings)
        limit = self.options.max_gb * 1024**3
        for mtime, size, path in recordings:
            free = shutil.disk_usage(self.clips).free
            if mtime >= cutoff and total <= limit and free >= MIN_FREE_BYTES:
                break
            path.unlink(missing_ok=True)
            total -= size
        return shutil.disk_usage(self.clips).free < MIN_FREE_BYTES
