"""One managed ffmpeg per RTSP camera: small JPEG frames for motion and detection, optionally the recording.

ffmpeg holds the camera's only RTSP connection from this integration (cameras allow few). It writes scaled
MJPEG frames at a low rate to a pipe and, when recording, copies the video into short MPEG-TS segments
without re-encoding. When it stops (camera offline, network gone) it is started again with backoff; when
the entry unloads it is terminated, then killed if it doesn't exit.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import io
import logging
from pathlib import Path
import time

from PIL import Image, ImageChops

from .url import PROTOCOLS, redact

_LOGGER = logging.getLogger(__name__)

READ_BYTES = 64 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024  # a frame bigger than this means we lost sync: start over
IO_TIMEOUT_US = 10_000_000  # ffmpeg gives up on a silent camera after 10 s
STOP_SECONDS = 3
BACKOFF = (2, 5, 10, 30, 60)
THUMB = (64, 36)  # what motion is measured on
PIXEL_DELTA = 25  # grey levels a thumbnail pixel must change by to count as moved


@dataclass(frozen=True, slots=True)
class StreamOptions:
    """How the camera is read; set in the entry's options."""

    fps: float = 2.0
    width: int = 640
    keyframes_only: bool = False  # decode only keyframes: far less CPU on a Pi, motion seen ~1 GOP later
    segments: Path | None = None  # where recording segments go, or None for no recording
    segment_seconds: int = 2


def ffmpeg_args(url: str, options: StreamOptions) -> list[str]:
    """The ffmpeg command line after the binary (url carries the credentials; never log it)."""
    args = ["-hide_banner", "-loglevel", "error", "-nostdin"]
    args += ["-rtsp_transport", "tcp", "-timeout", str(IO_TIMEOUT_US), "-protocol_whitelist", PROTOCOLS]
    if options.keyframes_only:
        args += ["-skip_frame", "nokey"]
    args += ["-i", url]
    args += ["-map", "0:v:0", "-an", "-vf", f"fps={options.fps},scale='min({options.width},iw)':-2"]
    args += ["-fps_mode", "passthrough", "-c:v", "mjpeg", "-q:v", "5", "-f", "image2pipe", "pipe:1"]
    if options.segments is not None:
        # Video only: many cameras send G.711 audio, which MPEG-TS and MP4 can't carry.
        args += ["-map", "0:v:0", "-an", "-c:v", "copy", "-f", "segment", "-segment_format", "mpegts"]
        args += ["-segment_time", str(options.segment_seconds), "-reset_timestamps", "1", "-strftime", "1"]
        args += [str(options.segments / "%Y%m%d-%H%M%S.ts")]
    return args


def thumbnail(jpeg: bytes) -> Image.Image:
    """A tiny greyscale copy to compare frames on (the JPEG decoder skips most of the work)."""
    image = Image.open(io.BytesIO(jpeg))
    image.draft("L", (THUMB[0] * 4, THUMB[1] * 4))
    return image.convert("L").resize(THUMB)


def changed(before: Image.Image, after: Image.Image) -> float:
    """The share of the picture that moved, 0 to 1."""
    moved = ImageChops.difference(before, after).point(lambda value: 255 if value > PIXEL_DELTA else 0)
    return moved.histogram()[255] / (THUMB[0] * THUMB[1])


def measure(previous: Image.Image | None, jpeg: bytes) -> tuple[Image.Image, float]:
    """This frame's thumbnail, and how much of it moved since `previous` (0 for the first frame)."""
    thumb = thumbnail(jpeg)
    return thumb, changed(previous, thumb) if previous is not None else 0.0


class RtspReader:
    """Keeps ffmpeg running on one camera and hands every frame to `on_frame` (dropping frames while busy)."""

    def __init__(
        self,
        ffmpeg: str,
        url: str,
        password: str | None,
        options: StreamOptions,
        on_frame: Callable[[bytes], Awaitable[None]],
        on_state: Callable[[], None],
        label: str,
    ) -> None:
        """Initialize; `url` has the credentials, `password` is what to scrub from ffmpeg's messages."""
        self.ffmpeg = ffmpeg
        self._url = url
        self._password = password
        self.options = options
        self._on_frame = on_frame
        self._on_state = on_state
        self.label = label
        self.latest: bytes | None = None
        self.last_frame_at: float | None = None
        self.last_error: str | None = None
        self.restarts = 0
        self.dropped = 0
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._busy = False
        self._stderr: deque[str] = deque(maxlen=10)

    @property
    def connected(self) -> bool:
        """Frames are arriving."""
        if self.last_frame_at is None or self._process is None or self._process.returncode is not None:
            return False
        return time.monotonic() - self.last_frame_at < max(15.0, 4 / self.options.fps)

    async def run(self) -> None:
        """Until stop(): run ffmpeg, and start it again after it ends."""
        failures = 0
        while not self._stopping:
            started = time.monotonic()
            try:
                await self._run_once()
            except OSError as err:  # ffmpeg missing or not executable
                self.last_error = f"Could not start ffmpeg: {err.strerror}"
            if self._stopping:
                break
            failures = 0 if time.monotonic() - started > 60 else failures + 1
            delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
            self.restarts += 1
            _LOGGER.warning("%s: stream stopped (%s); trying again in %s s", self.label, self.last_error, delay)
            self._on_state()
            await asyncio.sleep(delay)

    async def _run_once(self) -> None:
        self._stderr.clear()  # last_error says why this run ended, not every run before it
        self._process = process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            *ffmpeg_args(self._url, self.options),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        drain = asyncio.create_task(self._drain(process))
        try:
            await self._read(process)
        finally:
            await self._end(process)
            await asyncio.gather(drain, return_exceptions=True)
        self.last_error = " | ".join(self._stderr) or f"ffmpeg exited with {process.returncode}"

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout
        buffer = bytearray()
        while chunk := await process.stdout.read(READ_BYTES):
            buffer += chunk
            while (end := buffer.find(b"\xff\xd9")) >= 0:
                frame, buffer = bytes(buffer[: end + 2]), buffer[end + 2 :]
                if frame.startswith(b"\xff\xd8"):
                    await self._frame(frame)
            if len(buffer) > MAX_FRAME_BYTES:
                buffer.clear()

    async def _frame(self, jpeg: bytes) -> None:
        first = self.last_frame_at is None or not self.connected
        self.latest, self.last_frame_at = jpeg, time.monotonic()
        if first:
            self.last_error = None
            self._on_state()
        if self._busy:
            self.dropped += 1  # bounded: one frame in flight, the rest only refresh `latest`
            return
        self._busy = True
        try:
            await self._on_frame(jpeg)
        finally:
            self._busy = False

    async def _drain(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr
        async for line in process.stderr:
            text = redact(line.decode(errors="replace").strip(), self._password)
            if text:
                self._stderr.append(text)
                _LOGGER.debug("%s ffmpeg: %s", self.label, text)

    async def _end(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()

    async def restart(self) -> None:
        """End the current ffmpeg; run() starts the next one with the current options."""
        if self._process is not None:
            await self._end(self._process)

    async def stop(self) -> None:
        """Stop for good."""
        self._stopping = True
        if self._process is not None:
            await self._end(self._process)


async def async_probe(ffmpeg: str, url: str, password: str | None, timeout: float = 15) -> str | None:
    """None if one frame can be read from `url`, else the config-flow error key."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        *("-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp"),
        *("-timeout", str(IO_TIMEOUT_US), "-protocol_whitelist", PROTOCOLS, "-i", url),
        *("-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        return "cannot_connect"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode == 0:
        return None
    message = redact(err.decode(errors="replace"), password)
    _LOGGER.debug("RTSP probe failed: %s", message[-300:])
    return "invalid_auth" if "401" in message or "Unauthorized" in message else "cannot_connect"
