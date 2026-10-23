"""Blink cloud and Sync Module local-storage clips: listing, fetch-once cache, first-frame thumbnails.

Adapted from Blink Live View Proxy's proxy/blink_proxy/clips.py and clip_cache.py,
Copyright (c) 2026 Teethree89, MIT License (see NOTICE).

A local clip is not a file anyone can read part of: blinkpy's prepare_download() makes the Sync
Module upload the whole clip to Blink's cloud, then it comes back over HTTPS. So each clip is
fetched once, one at a time (several in flight is what gets an account rate-limited), kept under
.cache/ (not backed up, oldest evicted past the cap), and served from disk, which also gives the
byte ranges iOS needs to play an MP4.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import hashlib
import logging
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientTimeout
from blinkpy import api as blink_api
from blinkpy.blinkpy import Blink

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .api import BlinkAuth

_LOGGER = logging.getLogger(__name__)

API = "/api/blink_instant_events/clips"
CACHE_MAX_BYTES = 256 * 1024 * 1024
INDEX_TTL_SECONDS = 15 * 60
DOWNLOAD_TIMEOUT = ClientTimeout(total=120)
THUMBNAIL_WIDTH = 480


def parse_time(value: str | datetime) -> datetime:
    """Blink times, always timezone-aware UTC."""
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).astimezone(UTC)


def clip_id(clip: dict[str, Any]) -> str:
    """A stable, opaque id for a listed clip."""
    identity = "|".join(
        str(part)
        for part in (
            clip["source"],
            clip["slug"],
            clip["camera_name"],
            clip["created_at"].isoformat(),
            clip.get("size", ""),
            getattr(clip.get("item"), "id", ""),
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def public_clip(clip: dict[str, Any], query: dict[str, Any], cached: bool) -> dict[str, Any]:
    """The listing row the app reads; URLs carry the query so a miss can re-list."""
    key = clip_id(clip)
    args = urlencode({**query, "source": clip["source"], "camera": clip["slug"]})
    return {
        "id": key,
        "source": clip["source"],
        "slug": clip["slug"],
        "camera_name": clip["camera_name"],
        "created_at": clip["created_at"].isoformat(),
        "size": clip.get("size"),
        "download_url": f"{API}/{key}.mp4?{args}",
        "thumbnail_url": f"{API}/{key}.jpg?{args}",
        "cached": cached,
    }


class Clips:
    """Lists clips from Blink and keeps the ones someone opened."""

    def __init__(self, hass: HomeAssistant, blink: Blink, ffmpeg: str) -> None:
        """Initialize."""
        self.hass = hass
        self.blink = blink
        self.ffmpeg = ffmpeg
        self.root = Path(hass.config.cache_path("blink_instant_events", "clips"))
        self._index: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._download_slot = asyncio.Semaphore(1)

    def _slug(self, camera_name: str) -> str:
        camera = self.blink.cameras.get(camera_name)
        return slugify(camera.name if camera else camera_name)

    async def async_list(
        self, source: str, camera: str | None, hours: float, pages: int, limit: int
    ) -> list[dict[str, Any]]:
        """Newest first, from the cloud, the Sync Module, or both."""
        since = datetime.now(UTC) - timedelta(hours=hours)
        clips: list[dict[str, Any]] = []
        if source in ("both", "cloud"):
            clips += await self._cloud(since, pages)
        if source in ("both", "local"):
            clips += await self._local(since)
        clips = [clip for clip in clips if not camera or clip["slug"] == camera]
        clips.sort(key=lambda clip: clip["created_at"], reverse=True)
        clips = clips[:limit]
        expires = time.monotonic() + INDEX_TTL_SECONDS
        self._index = {k: v for k, v in self._index.items() if v[0] > time.monotonic()}
        self._index |= {clip_id(clip): (expires, clip) for clip in clips}
        return clips

    async def _cloud(self, since: datetime, pages: int) -> list[dict[str, Any]]:
        clips = []
        for page in range(1, pages + 1):
            response = await blink_api.request_videos(self.blink, time=since.timestamp(), page=page)
            media = response.get("media") if isinstance(response, dict) else None
            if not media:
                break
            for item in media:
                name, path, created = item.get("device_name"), item.get("media"), item.get("created_at")
                if item.get("deleted") or not (name and path and created) or parse_time(created) < since:
                    continue
                clips.append(
                    {
                        "source": "cloud",
                        "slug": self._slug(name),
                        "camera_name": name,
                        "created_at": parse_time(created),
                        "size": item.get("size"),
                        "url": f"{self.blink.urls.base_url}{path}",
                    }
                )
        return clips

    async def _local(self, since: datetime) -> list[dict[str, Any]]:
        clips = []
        for sync in self.blink.sync.values():
            if not sync.local_storage:
                continue
            await sync.update_local_storage_manifest()
            storage = sync._local_storage  # noqa: SLF001
            for item in list(storage["manifest"]):
                created = parse_time(item.created_at)
                if created < since:
                    continue
                clips.append(
                    {
                        "source": "local",
                        "slug": self._slug(item.name),
                        "camera_name": item.name,
                        "created_at": created,
                        "size": item.size,
                        "url": f"{self.blink.urls.base_url}{item.url(storage['last_manifest_id'])}",
                        "item": item,
                    }
                )
        return clips

    async def async_find(self, key: str, query: dict[str, Any]) -> dict[str, Any] | None:
        """The clip behind an id: from the last listing, else from a fresh one with the same query."""
        entry = self._index.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        await self.async_list(**query)
        entry = self._index.get(key)
        return entry[1] if entry else None

    def cached(self, key: str, suffix: str = ".mp4") -> Path | None:
        """The file if it is on disk (executor only)."""
        path = self.root / f"{key}{suffix}"
        return path if path.is_file() and path.stat().st_size > 0 else None

    async def async_clip(self, key: str, clip: dict[str, Any] | None) -> Path:
        """The cached copy, fetched from Blink first if needed."""
        async with self._locks.setdefault(key, asyncio.Lock()):
            if path := await self.hass.async_add_executor_job(self.cached, key):
                await self.hass.async_add_executor_job(os.utime, path)  # counts as recently used
                return path
            if clip is None:
                raise LookupError(key)
            async with self._download_slot:
                if clip.get("item") is not None:
                    await clip["item"].prepare_download(self.blink)
                auth: BlinkAuth = self.blink.auth  # type: ignore[assignment]
                headers = {"Authorization": f"Bearer {await auth.async_token()}"}
                async with auth.session.get(clip["url"], headers=headers, timeout=DOWNLOAD_TIMEOUT) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
            if not data:
                raise RuntimeError("Blink returned an empty clip")
            return await self.hass.async_add_executor_job(self._store, key, data)

    def _store(self, key: str, data: bytes) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{key}.mp4"
        partial = target.with_suffix(".part")
        partial.write_bytes(data)
        os.replace(partial, target)
        self._prune(keep=target)
        return target

    def _prune(self, keep: Path) -> None:
        """Oldest-used first until the cache fits."""
        files = sorted((p for p in self.root.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= CACHE_MAX_BYTES:
                break
            if path == keep:
                continue
            total -= path.stat().st_size
            with contextlib.suppress(OSError):
                path.unlink()

    async def async_thumbnail(self, key: str, clip: dict[str, Any] | None) -> Path:
        """The clip's first frame as a JPEG, cut once with ffmpeg and kept beside it."""
        if path := await self.hass.async_add_executor_job(self.cached, key, ".jpg"):
            return path
        source = await self.async_clip(key, clip)
        async with self._locks.setdefault(f"{key}.jpg", asyncio.Lock()):
            return await self._cut(key, source)

    async def _cut(self, key: str, source: Path) -> Path:
        if path := await self.hass.async_add_executor_job(self.cached, key, ".jpg"):
            return path
        target = self.root / f"{key}.jpg"
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            *("-hide_banner", "-loglevel", "error", "-y", "-i", str(source)),
            *("-frames:v", "1", "-vf", f"scale={THUMBNAIL_WIDTH}:-2", "-q:v", "4", "-f", "image2", str(target)),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
        if process.returncode != 0 or not await self.hass.async_add_executor_job(self.cached, key, ".jpg"):
            _LOGGER.warning("ffmpeg could not cut a frame from clip %s: %s", key, err.decode(errors="replace")[-300:])
            raise RuntimeError("Could not read a frame from the clip")
        return target
