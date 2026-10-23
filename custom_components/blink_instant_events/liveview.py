"""Live view: Blink's IMMIS stream -> ffmpeg -> HLS for the app; push-to-talk; the last live view kept.

Adapted from Blink Live View Proxy's proxy/blink_proxy/blink.py, hls.py, ptt.py, liveview_cache.py
and routes.py, Copyright (c) 2026 Teethree89, MIT License (see NOTICE). The add-on's HTTP server is
replaced by Home Assistant views (views.py); ffmpeg is the one Home Assistant ships.

One Blink session per camera, shared by every viewer of that camera; cameras stream independently.
A session ends on /stop, after IDLE_SECONDS without a playlist or segment request, or when Blink
ends it. The camera streams (and drains its battery) for as long as the session lives.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import ssl
import time
from typing import Any
import urllib.parse

from aiohttp import WSMsgType, web
from blinkpy import api as blink_api
from blinkpy.camera import BlinkCamera
from blinkpy.livestream import BlinkLiveStream

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import controls
from .coordinator import BlinkCoordinator
from .entity import camera_slug
from .tsguard import AudioGuard

_LOGGER = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 600
IDLE_SECONDS = 15
REAP_EVERY = timedelta(seconds=5)
START_TIMEOUT = 30
# Blink's IMMI framing: 1-byte type, 4-byte sequence, 4-byte length.
IMMI_HEADER_BYTES = 9
MAX_IMMI_PAYLOAD = 1024 * 1024
MAX_BACKLOG_BYTES = 4 * 1024 * 1024
IMMI_AUDIO, IMMI_SESSION_CMD, IMMI_INLINE_CMD, IMMI_ACCESSORY = 0x05, 0x17, 0x14, 0x15
CMD_START_AUDIO, CMD_STOP_AUDIO, CMD_LIGHTS_ON, CMD_LIGHTS_OFF = 3, 4, 1, 2
# Push-to-talk: the app sends s16le mono PCM; the camera wants 16 kHz AAC-LC in IMMI audio frames.
PTT_RATE = 16_000
PTT_FRAME_TICKS = 1024 * 90_000 // PTT_RATE
# Families Blink serves over RTSP instead of IMMIS: no session commands, so no talk.
NO_TALK_PRODUCT_TYPES = {"xt", "white"}


def ptt_supported(camera: BlinkCamera) -> bool:
    """Every IMMIS family has a speaker the live-view session can drive."""
    return str(camera.product_type or "").casefold() not in NO_TALK_PRODUCT_TYPES


def redacted(response: dict[str, Any]) -> dict[str, Any]:
    """A liveview answer safe to log: no server path (connection id) and no token."""
    safe = {k: v for k, v in response.items() if k not in ("server", "liveview_token")}
    server = urllib.parse.urlparse(str(response.get("server", "")))
    safe["server"] = f"{server.scheme}://{server.hostname}:{server.port}/..."
    return safe


class ImmisStream(BlinkLiveStream):
    """blinkpy's IMMIS bridge, reading whole frames, honouring the liveview token, and able to talk."""

    def __init__(self, camera: BlinkCamera, response: dict[str, Any]) -> None:
        """Initialize."""
        super().__init__(camera, response)
        self.liveview_token = str(response.get("liveview_token") or "")
        self._write_lock = asyncio.Lock()
        self._guard = AudioGuard(str(camera.name or ""))
        self.flood_light: bool | None = None  # what the camera last reported over this session
        # Video Blink sends before ffmpeg has connected: blinkpy drops it, and with it the opening keyframe, so
        # ffmpeg waited for the next one (about 4 s on Blink's cameras) or failed to read the stream at all.
        self._backlog = bytearray()
        self.first_video_at: float | None = None  # monotonic times of Blink's first video and first keyframe,
        self.first_keyframe_at: float | None = None  # for the start-up timing in the debug log

    @staticmethod
    def _field(buffer: bytearray, value: str, length: int) -> None:
        payload = value.encode()[:length].ljust(length, b"\x00")
        buffer += len(payload).to_bytes(4, "big") + payload

    def get_auth_header(self) -> bytearray:
        """blinkpy's header, with the liveview token where blinkpy sends zeros."""
        header = bytearray([0x00, 0x00, 0x00, 0x28])
        self._field(header, self.camera.serial, 16)
        client_id = urllib.parse.parse_qs(self.target.query).get("client_id", ["0"])[0]
        header += int(client_id).to_bytes(4, "big") + bytes([0x01, 0x08])
        self._field(header, self.liveview_token, 64)
        self._field(header, self.target.path.split("/")[-1].split("__")[0], 16)
        header += bytes([0x00, 0x00, 0x00, 0x01])
        return header

    async def write_frame(self, msgtype: int, sequence: int, payload: bytes = b"") -> None:
        """Send one IMMI frame to Blink."""
        if self.target_writer is None or self.target_writer.is_closing():
            raise RuntimeError("Camera audio channel is no longer connected; start live view again")
        header = bytes([msgtype]) + (sequence & 0xFFFFFFFF).to_bytes(4, "big") + len(payload).to_bytes(4, "big")
        async with self._write_lock:
            self.target_writer.write(header + payload)
            await self.target_writer.drain()

    async def set_flood_light(self, on: bool) -> None:
        """The lamp, sent over the session the way the Blink app does (its config route is busy now)."""
        await self.write_frame(IMMI_INLINE_CMD, CMD_LIGHTS_ON if on else CMD_LIGHTS_OFF)

    async def _read_frame(self) -> tuple[int, int, bytes] | None:
        try:
            header = await self.target_reader.readexactly(IMMI_HEADER_BYTES)
            length = int.from_bytes(header[5:9], "big")
            if length > MAX_IMMI_PAYLOAD:
                return None
            payload = await self.target_reader.readexactly(length) if length else b""
        except asyncio.IncompleteReadError:
            return None
        return header[0], int.from_bytes(header[1:5], "big"), payload

    async def _send(self, data: bytes) -> None:
        if self.first_video_at is None:
            self.first_video_at = time.monotonic()
        if self.first_keyframe_at is None and (b"\x00\x00\x01\x65" in data or b"\x00\x00\x01\x25" in data):
            self.first_keyframe_at = time.monotonic()
        clients = [writer for writer in self.clients if not writer.is_closing()]
        if not clients:
            if len(self._backlog) < MAX_BACKLOG_BYTES:
                self._backlog += data
            return
        if self._backlog:
            data, self._backlog = bytes(self._backlog) + data, bytearray()
        for writer in clients:
            writer.write(data)
            await writer.drain()

    async def recv(self) -> None:
        """Copy complete MPEG-TS frames to ffmpeg; note the lamp reports."""
        try:
            while not self.target_reader.at_eof():
                frame = await self._read_frame()
                if frame is None:
                    break
                msgtype, sequence, payload = frame
                if msgtype == IMMI_ACCESSORY and sequence in (0, 1):
                    self.flood_light = sequence == 1
                elif msgtype == 0x00 and payload[:1] == b"\x47" and (data := self._guard.push(payload)):
                    await self._send(data)
                await asyncio.sleep(0)
            if tail := self._guard.flush():
                await self._send(tail)
        except ssl.SSLError as err:
            if err.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
                _LOGGER.warning("Live view TLS error on %s: %s", self.camera.name, err.reason)
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("Live view of %s ended: %s", self.camera.name, err)
        finally:
            if self.target_writer is not None and not self.target_writer.is_closing():
                self.target_writer.close()


async def request_liveview(camera: BlinkCamera) -> dict[str, Any]:
    """Ask Blink to open a session. Posted directly: blinkpy's helper waits up to 2 min on the command."""
    blink = camera.sync.blink
    kind = {"doorbell": "v2/accounts/{a}/networks/{n}/doorbells", "mini": "v2/accounts/{a}/networks/{n}/owls"}.get(
        camera.camera_type, "v6/accounts/{a}/networks/{n}/cameras"
    )
    url = (
        f"{blink.urls.base_url}/api/{kind.format(a=blink.account_id, n=camera.network_id)}/{camera.camera_id}/liveview"
    )
    response = await blink_api.http_post(
        blink, url, data=json.dumps({"intent": "liveview", "motion_event_start_time": None})
    )
    if not isinstance(response, dict) or not str(response.get("server", "")).startswith("immis://"):
        if isinstance(response, dict) and str(response.get("server", "")).startswith("rtsp"):
            raise RuntimeError("This camera streams over RTSP, which is not supported")
        message = response.get("message") if isinstance(response, dict) else None
        raise RuntimeError(f"Blink did not open a live view: {message or 'no stream server'}")
    _LOGGER.debug("Live view for %s: %s", camera.name, redacted(response))
    return response


class LiveSession:
    """One camera's Blink session and the ffmpeg cutting it into HLS (and a copy for /last-liveview)."""

    def __init__(self, views: LiveViews, camera: BlinkCamera) -> None:
        """Initialize."""
        self.views = views
        self.camera = camera
        self.slug = camera_slug(camera)
        self.directory = views.hls_root / self.slug
        self.playlist = self.directory / "index.m3u8"
        self.stream: ImmisStream | None = None
        self._feed: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._drainer: asyncio.Task[None] | None = None
        self._cache = views.cache_root / f"{self.slug}.ts.part"
        self.last_touch = time.monotonic()
        self.started_at = self.blink_answered_at = self.last_touch
        self._timed = False

    def touch(self) -> None:
        """A viewer is still here."""
        self.last_touch = time.monotonic()

    @property
    def running(self) -> bool:
        """ffmpeg is still cutting."""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Open the Blink session and start ffmpeg on it."""
        hass = self.views.hass
        self.started_at = time.monotonic()
        await hass.async_add_executor_job(self._fresh_dirs)
        self.stream = ImmisStream(self.camera, await request_liveview(self.camera))
        self.blink_answered_at = time.monotonic()
        await self.stream.start(host="127.0.0.1", port=None)
        self._feed = hass.async_create_background_task(self.stream.feed(), f"blink live view {self.slug}")
        streams = ("-map", "0:v:0", "-map", "0:a:0?")
        video = await self.views.hls_video()
        flag = "split_by_time" if "copy" in video else "independent_segments"
        self._process = await asyncio.create_subprocess_exec(
            self.views.ffmpeg,
            *("-hide_banner", "-loglevel", "verbose" if _LOGGER.isEnabledFor(logging.DEBUG) else "warning"),
            *("-flags", "low_delay", "-fflags", "nobuffer"),
            # The format is known (guessing it failed on some starts), and a short probe: ffmpeg's default reads
            # 5 MB first, which takes minutes at Blink's bitrate.
            *("-f", "mpegts", "-probesize", "1000000", "-analyzeduration", "500000", "-i", self.stream.url),
            *streams,
            *video,
            *("-c:a", "copy", "-f", "hls", "-hls_time", "1", "-hls_list_size", "6"),
            # Re-encoded segments each start with a keyframe; copied ones are cut by time instead.
            *("-hls_flags", "delete_segments+omit_endlist+program_date_time+" + flag),
            *("-hls_segment_filename", str(self.directory / "segment_%05d.ts"), str(self.playlist)),
            # The copy kept as the last live view: Blink's own video, untouched.
            *(*streams, "-c", "copy", "-f", "mpegts", str(self._cache)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._drainer = hass.async_create_background_task(self._drain(), f"blink ffmpeg log {self.slug}")
        _LOGGER.info("Live view started on %s", self.camera.name)

    def _fresh_dirs(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True)
        self.views.cache_root.mkdir(parents=True, exist_ok=True)

    async def _drain(self) -> None:
        assert self._process and self._process.stderr
        async for line in self._process.stderr:
            text = line.decode(errors="replace").strip()
            self._stderr.append(text)
            # With debug logging, ffmpeg runs verbose: its steps, timed from the start of the live view.
            _LOGGER.debug("ffmpeg %s +%.2f s: %s", self.slug, time.monotonic() - self.started_at, text)

    async def wait_ready(self) -> None:
        """Until ffmpeg has written the first playlist."""
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            self.touch()
            if not self.running:
                if self._drainer:  # let the last lines in: they say why
                    await asyncio.wait([self._drainer], timeout=1)
                raise RuntimeError(
                    f"ffmpeg stopped ({self._process.returncode}): {' | '.join(self._stderr) or 'no output'}"
                )
            if self._feed and self._feed.done():
                raise RuntimeError(f"Could not connect to the camera's stream: {self._feed.exception()!r}")
            if await self.views.hass.async_add_executor_job(_nonempty, self.playlist):
                if not self._timed and self.stream and self.stream.first_video_at:
                    self._timed = True
                    keyframe = self.stream.first_keyframe_at
                    _LOGGER.debug(
                        "Live view of %s ready in %.1f s: Blink answered %.1f s, first video %.1f s, keyframe %s",
                        self.camera.name,
                        time.monotonic() - self.started_at,
                        self.blink_answered_at - self.started_at,
                        self.stream.first_video_at - self.started_at,
                        f"{keyframe - self.started_at:.1f} s" if keyframe else "not seen",
                    )
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("The camera sent no video in time")

    async def stop(self) -> None:
        """End the Blink session, stop ffmpeg, publish the copy as the last live view."""
        if self.stream:
            self.stream.stop()  # ffmpeg reads EOF, finishes both outputs and exits by itself
        if self._process and self._process.returncode is None:
            try:
                await asyncio.wait_for(self._process.wait(), 3)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._feed:
            self._feed.cancel()
            await asyncio.gather(self._feed, return_exceptions=True)
        await self.views.hass.async_add_executor_job(self._finish)
        _LOGGER.info("Live view stopped on %s", self.camera.name)

    def _finish(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
        if _nonempty(self._cache):
            final = self._cache.with_suffix("")  # <slug>.ts; the previous one is replaced
            os.replace(self._cache, final)
            final.with_suffix(".mp4").unlink(missing_ok=True)
        else:
            self._cache.unlink(missing_ok=True)


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


@dataclass
class _Token:
    slug: str
    expires: float


class LiveViews:
    """Sessions, viewer tokens, held-back camera settings, and the last live view per camera."""

    def __init__(self, hass: HomeAssistant, coordinator: BlinkCoordinator, ffmpeg: str) -> None:
        """Initialize."""
        self.hass = hass
        self.coordinator = coordinator
        self.ffmpeg = ffmpeg
        self.hls_root = Path(hass.config.cache_path("blink_instant_events", "hls"))
        self.cache_root = Path(hass.config.cache_path("blink_instant_events", "liveviews"))
        self.sessions: dict[str, LiveSession] = {}
        self.viewers: dict[str, LiveSession] = {}  # "<slug>:<session>" -> session, for talk and controls
        self.deferred = controls.DeferredControls()
        self._tokens: dict[str, _Token] = {}
        self._hls_video: tuple[str, ...] | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._flushes: dict[str, asyncio.Task[None]] = {}

    async def hls_video(self) -> tuple[str, ...]:
        """How ffmpeg writes the HLS video.

        Blink's cameras send a keyframe only every ~4 s, and a player can only start (and catch up) at one, so
        copying the video kept phones 5-10 s behind and slow to start. Re-encoding it with a keyframe every second
        lets every 1 s segment start one; 720p at 15 fps costs a few percent of one core. Without libx264, the
        video is copied and cut by time as before.
        """
        if self._hls_video is None:
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg,
                *("-hide_banner", "-encoders"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            encoders, _ = await process.communicate()
            if b" libx264 " in encoders:
                self._hls_video = (
                    *("-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"),
                    *("-force_key_frames", "expr:gte(t,n_forced*1)", "-sc_threshold", "0"),
                    # About 3x Blink's own bitrate (keyframes every second cost bits); fine over cellular.
                    *("-crf", "27", "-maxrate", "900k", "-bufsize", "900k"),
                )
            else:
                _LOGGER.warning("ffmpeg has no libx264: live view is copied, and starts slower")
                self._hls_video = ("-c:v", "copy")
        return self._hls_video

    @callback
    def async_start(self) -> None:
        """Reap idle sessions every few seconds until unload."""
        self.coordinator.config_entry.async_on_unload(async_track_time_interval(self.hass, self._reap, REAP_EVERY))

    def camera(self, slug: str) -> BlinkCamera | None:
        """The camera the API calls `slug`."""
        return next((cam for cam in self.coordinator.blink.cameras.values() if camera_slug(cam) == slug), None)

    # Tokens: 10 minutes, one camera, renewed on use.
    def issue_token(self, slug: str) -> str:
        """A token for one camera's player."""
        now = time.monotonic()
        self._tokens = {k: t for k, t in self._tokens.items() if t.expires > now}
        token = secrets.token_urlsafe(32)
        self._tokens[token] = _Token(slug, now + TOKEN_TTL_SECONDS)
        return token

    def token_ok(self, token: str, slug: str) -> bool:
        """Valid for this camera; extends it."""
        found = self._tokens.get(token)
        if not found or found.slug != slug or found.expires <= time.monotonic():
            return False
        found.expires = time.monotonic() + TOKEN_TTL_SECONDS
        return True

    async def get_or_start(self, slug: str, viewer: str) -> LiveSession:
        """The camera's running session, started if needed, registered for this viewer."""
        camera = self.camera(slug)
        if camera is None:
            raise KeyError(slug)
        async with self._locks.setdefault(slug, asyncio.Lock()):
            session = self.sessions.get(slug)
            if session and not session.running:
                await self._end(slug)
                session = None
            if session is None:
                session = LiveSession(self, camera)
                try:
                    await session.start()
                except BaseException:
                    await session.stop()
                    raise
                self.sessions[slug] = session
            session.touch()
            if viewer:
                self.viewers[f"{slug}:{viewer}"] = session
        return session

    def viewer_stream(self, slug: str, viewer: str) -> ImmisStream | None:
        """The stream behind a viewer's session, or any open one on the camera."""
        session = self.viewers.get(f"{slug}:{viewer}") or self.sessions.get(slug)
        return session.stream if session and session.running else None

    async def async_stop(self, slug: str) -> bool:
        """End the camera's session now (the idle timeout is only a backstop)."""
        async with self._locks.setdefault(slug, asyncio.Lock()):
            return await self._end(slug)

    async def _end(self, slug: str) -> bool:
        session = self.sessions.pop(slug, None)
        if session is None:
            return False
        self.viewers = {k: v for k, v in self.viewers.items() if v is not session}

        async def finish() -> None:
            await session.stop()
            self._schedule_flush(slug)

        # Shielded: an app hanging up mid-stop must not lose the saved copy or the held-back settings.
        await asyncio.shield(self.coordinator.config_entry.async_create_task(self.hass, finish()))
        return True

    async def async_stop_all(self) -> None:
        """On unload."""
        for slug in list(self.sessions):
            await self.async_stop(slug)

    async def _reap(self, _now: datetime) -> None:
        now = time.monotonic()
        for slug, session in list(self.sessions.items()):
            if now - session.last_touch > IDLE_SECONDS or not session.running:
                await self.async_stop(slug)

    def _schedule_flush(self, slug: str) -> None:
        """Write the camera settings Blink refused as busy during the live view."""
        if not self.deferred.queued(slug) or (camera := self.camera(slug)) is None:
            return
        if (task := self._flushes.get(slug)) and not task.done():
            task.cancel()

        async def flush() -> None:
            await asyncio.sleep(controls.DEFERRED_SETTLE_SECONDS)
            changes = self.deferred.take(slug)
            try:
                result = await controls.flush_deferred(camera.sync.blink, control_row(camera), changes)
            except Exception as err:  # noqa: BLE001 - reported to the app on its next read
                result = {"applied": [], "failed": controls.control_names(changes), "busy": [], "error": str(err)}
            result.setdefault("at", controls.now_stamp())
            self.deferred.record(slug, result)

        entry = self.coordinator.config_entry
        self._flushes[slug] = entry.async_create_background_task(self.hass, flush(), f"blink deferred {slug}")

    async def last_liveview(self, slug: str) -> dict[str, Any]:
        """{available, ended_at, ...} for the camera's last finished live view."""
        path = self.cache_root / f"{slug}.ts"
        stat = await self.hass.async_add_executor_job(lambda: path.stat() if _nonempty(path) else None)
        if stat is None:
            return {"available": False, "slug": slug}
        ended = dt_util.utc_from_timestamp(stat.st_mtime)
        return {"available": True, "slug": slug, "ended_at": ended.isoformat(), "bytes": stat.st_size}

    async def last_liveview_mp4(self, slug: str) -> Path | None:
        """The last live view remuxed to MP4 (once), for saving to Photos."""
        ts, mp4 = self.cache_root / f"{slug}.ts", self.cache_root / f"{slug}.mp4"
        async with self._locks.setdefault(f"{slug}.mp4", asyncio.Lock()):
            fresh = await self.hass.async_add_executor_job(
                lambda: _nonempty(mp4) and mp4.stat().st_mtime >= ts.stat().st_mtime if _nonempty(ts) else None
            )
            if fresh is None:
                return None
            if fresh:
                return mp4
            part = mp4.with_suffix(".part")
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg,
                *("-hide_banner", "-loglevel", "error", "-y", "-i", str(ts), "-map", "0:v:0?", "-map", "0:a:0?"),
                *("-c", "copy", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", "-f", "mp4", str(part)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _out, err = await process.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
            if process.returncode != 0:
                raise RuntimeError(f"ffmpeg could not convert the live view: {err.decode(errors='replace')[-300:]}")
            await self.hass.async_add_executor_job(os.replace, part, mp4)
            return mp4


def control_row(camera: BlinkCamera) -> dict[str, Any]:
    """The camera as controls.py expects it."""
    return {
        "slug": camera_slug(camera),
        "name": camera.name,
        "id": str(camera.camera_id),
        "network_id": str(camera.network_id),
        "product_type": camera.product_type,
    }


class TalkBridge:
    """Microphone PCM from the app -> ffmpeg AAC -> IMMI audio frames on the viewer's session."""

    def __init__(self, ffmpeg: str, stream: ImmisStream, status: Any) -> None:
        """Initialize."""
        self.ffmpeg = ffmpeg
        self.stream = stream
        self.status = status
        self._process: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task[None] | None = None

    async def start(self, sample_rate: int) -> None:
        """Tell the camera audio is coming and start the encoder."""
        await self.stop(send_stop=False)
        await self.stream.write_frame(IMMI_SESSION_CMD, CMD_START_AUDIO)
        self._process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            # No input probing: ffmpeg's default analysis held the first ~5 s of speech back.
            *("-hide_banner", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0"),
            *("-f", "s16le", "-ar", str(max(8000, min(96000, sample_rate)))),
            *("-ac", "1", "-i", "pipe:0", "-ac", "1", "-ar", str(PTT_RATE), "-c:a", "aac", "-profile:a", "aac_low"),
            # The camera's decoder chokes on PNS frames and closes the session ~68 frames later.
            *("-aac_pns", "0", "-b:a", "40k", "-f", "adts", "pipe:1"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._pump = asyncio.create_task(self._forward())

    async def write(self, pcm: bytes) -> None:
        """Signed 16-bit mono PCM."""
        if self._process and self._process.stdin and self._process.returncode is None:
            self._process.stdin.write(pcm)
            await self._process.stdin.drain()

    async def stop(self, send_stop: bool = True) -> None:
        """Flush the encoder and tell the camera audio has ended."""
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            process.kill()
            await process.wait()
        if self._pump:
            await asyncio.gather(self._pump, return_exceptions=True)
        if send_stop:
            try:
                await self.stream.write_frame(IMMI_SESSION_CMD, CMD_STOP_AUDIO)
            except RuntimeError:
                pass

    async def _forward(self) -> None:
        assert self._process and self._process.stdout
        stdout, timestamp, listening = self._process.stdout, 0, False
        try:
            while True:
                header = await stdout.readexactly(7)
                if header[0] != 0xFF or header[1] & 0xF0 != 0xF0:
                    return
                length = ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
                frame = header + await stdout.readexactly(length - 7)
                await self.stream.write_frame(IMMI_AUDIO, timestamp, frame)
                timestamp = (timestamp + PTT_FRAME_TICKS) & 0xFFFFFFFF
                if not listening:
                    listening = True
                    await self.status({"type": "listening", "frames": 1})
        except (asyncio.IncompleteReadError, ValueError):
            return
        except RuntimeError as err:
            await self.status({"type": "error", "message": str(err)})


async def talk(request: web.Request, ffmpeg: str, stream: ImmisStream) -> web.WebSocketResponse:
    """The /ptt WebSocket: {"type":"start","sampleRate":N}, binary PCM, {"type":"stop"}."""
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
    await ws.prepare(request)

    async def status(payload: dict[str, Any]) -> None:
        if not ws.closed:
            await ws.send_json(payload)

    bridge = TalkBridge(ffmpeg, stream, status)
    try:
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try:
                    command = json.loads(message.data)
                except ValueError:
                    command = {}
                try:
                    if command.get("type") == "start":
                        await bridge.start(int(command.get("sampleRate") or 48000))
                        await status({"type": "started"})
                    elif command.get("type") == "stop":
                        await bridge.stop()
                        await status({"type": "stopped"})
                    else:
                        await status({"type": "error", "message": "Unknown command"})
                except (RuntimeError, OSError) as err:
                    await status({"type": "error", "message": str(err)})
            elif message.type == WSMsgType.BINARY:
                await bridge.write(message.data)
    finally:
        await bridge.stop()
    return ws
