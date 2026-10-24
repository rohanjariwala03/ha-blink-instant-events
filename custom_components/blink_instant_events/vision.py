"""Smart detection: every new clip goes through Blink Vision (the add-on in this repo) for people, faces,
vehicles and animals. Results fire as a `detection` event on the camera's event entity, keep the best
frame (boxes drawn) for notifications, and label the clip in the clips listing.

A push starts looking for the motion's clip right away; Blink usually lists it 15-40 s later. Without
pushes, a poll that shows a new clip does the same, just later.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout
from blinkpy.camera import BlinkCamera
from PIL import Image, ImageDraw

from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from .clips import Clips, clip_id
from .const import (
    BLINK_ERRORS,
    CONF_LIVE_LOOK,
    CONF_VISION_URL,
    DOMAIN,
    EVENT_DETECTION,
    SIGNAL_DETECTION,
    SIGNAL_EVENT,
)
from .coordinator import BlinkCoordinator
from .entity import camera_slug

if TYPE_CHECKING:
    from .liveview import LiveViews

_LOGGER = logging.getLogger(__name__)

API = "/api/blink_instant_events/detections"
ADDON_SLUG = "blink_vision"
ADDON_PORT = 8000
TIMEOUT = ClientTimeout(total=30)
FRAMES = 10  # one a second from the start of the clip
WAIT_FOR_CLIP = (10, 10, 10, 15, 15, 20, 30, 40)  # after a push: ~2.5 min of looking, most often found by ~30 s
KEEP = timedelta(days=7)
KEEP_ROWS = 1000
LOOK_SECONDS = 8
GONE = "Gone before the camera looked"  # of live view after a push, when there are no clips to read
BACKFILL = timedelta(minutes=15)  # clips older than this at startup are not analyzed
PROBE_INTERVAL = timedelta(minutes=10)
IMAGE_TTL = timedelta(days=2)  # how long the signed image link in an event works
COLORS = {
    "known": (46, 204, 113),
    "face": (255, 149, 0),
    "person": (10, 132, 255),
    "vehicle": (191, 90, 242),
    "animal": (255, 214, 10),
}


def summarize(frames: list[dict[str, Any]]) -> dict[str, Any]:
    """One answer for a clip from its frames: who, how many unknown faces, which kinds of things."""
    people = sorted({face["name"] for frame in frames for face in frame["faces"] if face["name"]})
    unknown = max((sum(not face["name"] for face in frame["faces"]) for frame in frames), default=0)
    labels = {obj["group"] for frame in frames for obj in frame["objects"]}
    if people or unknown:
        labels.add("person")
    parts = list(people)
    if unknown:
        parts.append(f"{unknown} unknown people" if unknown > 1 else "an unknown person")
    elif "person" in labels and not people:
        parts.append("a person")
    parts += [
        f"a {label}" if label == "vehicle" else f"an {label}" for label in ("vehicle", "animal") if label in labels
    ]
    text = f"{', '.join(parts[:-1])} and {parts[-1]}" if len(parts) > 1 else (parts[0] if parts else "nothing found")
    return {
        "summary": text[0].upper() + text[1:],
        "people": people,
        "unknown_faces": unknown,
        "labels": sorted(labels),
        # Only people you know were seen: their faces, and no face anyone else's.
        "known_only": bool(people) and not unknown,
    }


def best_frame(frames: list[dict[str, Any]]) -> int:
    """The frame that shows the most: the most faces, then the biggest ones, then people, then anything."""

    def area(rows: list[dict[str, Any]]) -> float:
        return sum(row["box"][2] * row["box"][3] for row in rows)

    def rank(frame: dict[str, Any]) -> tuple[float, ...]:
        persons = [obj for obj in frame["objects"] if obj["group"] == "person"]
        return len(frame["faces"]), area(frame["faces"]), area(persons), area(frame["objects"])

    return max(range(len(frames)), key=lambda i: rank(frames[i])) if frames else 0


def annotate(jpeg: bytes, frame: dict[str, Any]) -> bytes:
    """The frame with a box around each face (named if known) and each person, vehicle and animal."""
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(image)
    width = max(2, image.width // 320)
    for obj in frame["objects"]:
        x, y, w, h = obj["box"]
        draw.rectangle((x, y, x + w, y + h), outline=COLORS[obj["group"]], width=width)
    for face in frame["faces"]:
        x, y, w, h = face["box"]
        color = COLORS["known" if face["name"] else "face"]
        draw.rectangle((x, y, x + w, y + h), outline=color, width=width)
        label = face["name"] or "Unknown"
        left, top, right, bottom = draw.textbbox((x, y), label, font_size=max(14, image.height // 30))
        draw.rectangle((left - 4, top - (bottom - top) - 8, right + 4, top - 2), fill=color)
        draw.text((x, top - (bottom - top) - 6), label, fill=(0, 0, 0), font_size=max(14, image.height // 30))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


def split_jpegs(data: bytes) -> list[bytes]:
    """ffmpeg's MJPEG pipe as separate JPEGs (0xFFD9 can't occur inside one: entropy data stuffs 0xFF)."""
    return [part + b"\xff\xd9" for part in data.split(b"\xff\xd9") if part.startswith(b"\xff\xd8")]


class Vision:
    """Owns the add-on connection, the analysis queue and the stored results."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: BlinkCoordinator, clips: Clips, ffmpeg: str
    ) -> None:
        """Initialize; call async_start once setup is done."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.clips = clips
        self.ffmpeg = ffmpeg
        self.url: str | None = None
        self.live: LiveViews | None = None  # set once live view is set up; used when there are no clips
        self.rows: dict[str, dict[str, Any]] = {}  # clip id -> detection
        self.root = clips.root.parent / "detections"
        self._store: Store[list[dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.detections")
        self._lock = asyncio.Lock()  # one clip at a time, and scans never race each other
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._records: dict[str, str] = {}  # camera serial -> last_record seen, to notice new clips on a poll
        self._started = dt_util.utcnow()

    @property
    def enabled(self) -> bool:
        """Whether the add-on answered."""
        return self.url is not None

    async def async_start(self) -> None:
        """Load past results, find the add-on, follow polls."""
        rows = await self._store.async_load() or []
        self.rows = {row["id"]: row for row in rows}
        await self._async_probe()
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._async_probe, PROBE_INTERVAL))
        self.entry.async_on_unload(self.coordinator.async_add_listener(self._on_refresh))
        self.entry.async_on_unload(self._cancel_watchers)

    async def _candidates(self) -> list[str]:
        if configured := self.entry.options.get(CONF_VISION_URL):
            return [configured.rstrip("/")]
        hosts = []
        try:
            from homeassistant.components.hassio import get_supervisor_client  # noqa: PLC0415 - only on HA OS

            hosts = [
                addon.slug.replace("_", "-")
                for addon in await get_supervisor_client(self.hass).addons.list()
                if addon.slug.endswith(ADDON_SLUG)
            ]
        except Exception as err:  # noqa: BLE001 - no Supervisor (container installs): only a configured URL works
            _LOGGER.debug("No Supervisor to find Blink Vision: %s", err)
        return [f"http://{host}:{ADDON_PORT}" for host in hosts]

    async def _async_probe(self, _now: datetime | None = None) -> None:
        found = None
        for url in await self._candidates():
            try:
                async with async_get_clientsession(self.hass).get(
                    f"{url}/health", timeout=ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        found = url
                        break
            except (ClientError, TimeoutError):
                continue
        if found != self.url:
            _LOGGER.info(
                "Blink Vision %s", f"found at {found}" if found else "is not reachable; smart detection is off"
            )
        self.url = found

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
            new = [clip for clip in reversed(clips) if clip_id(clip) not in self.rows and clip["created_at"] >= since]
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
            key = hashlib.sha256(f"live|{slug}|{created.isoformat()}".encode()).hexdigest()[:24]
            async with self._lock:
                await self._analyze_file(key, path, camera.name, slug, created, live=True)
        except (*BLINK_ERRORS, LookupError) as err:
            _LOGGER.warning("Could not look at %s with live view: %s", camera.name, err)

    async def _analyze(self, clip: dict[str, Any]) -> None:
        key = clip_id(clip)
        path = await self.clips.async_clip(key, clip)
        await self._analyze_file(key, path, clip["camera_name"], clip["slug"], clip["created_at"])

    async def _analyze_file(
        self, key: str, path: Path, camera_name: str, slug: str, created: datetime, live: bool = False
    ) -> None:
        jpegs = split_jpegs(await self._frames(path))
        if not jpegs or self.url is None:
            raise RuntimeError("no frames" if not jpegs else "Blink Vision went away")
        session = async_get_clientsession(self.hass)
        frames = []
        for jpeg in jpegs:
            async with session.post(f"{self.url}/analyze", data=jpeg, timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                frames.append(await resp.json())
        best = best_frame(frames)
        image = await self.hass.async_add_executor_job(annotate, jpegs[best], frames[best])
        await self.hass.async_add_executor_job(self._save_image, key, image)
        row = {
            "id": key,
            "camera": camera_name,
            "slug": slug,
            "created_at": created.isoformat(),
            "analyzed_at": dt_util.utcnow().isoformat(),
            **summarize(frames),
        }
        if live and not row["labels"]:
            row["summary"] = GONE  # a clip shows the motion itself; a look starts seconds after it
        self.rows[key] = row
        self._prune()
        self._store.async_delay_save(lambda: list(self.rows.values()), 5)
        self._announce(row)

    async def _frames(self, path: Path) -> bytes:
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            *("-hide_banner", "-loglevel", "error", "-i", str(path)),
            *("-vf", "fps=1,scale='min(1280,iw)':-2", "-frames:v", str(FRAMES)),
            *("-q:v", "3", "-c:v", "mjpeg", "-f", "image2pipe", "-"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 60)
        finally:
            if process.returncode is None:
                process.kill()
        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg could not read the clip: {err.decode(errors='replace')[-200:]}")
        return out

    def image_path(self, key: str) -> Path:
        """Where a clip's annotated best frame is kept."""
        return self.root / f"{key}.jpg"

    def _save_image(self, key: str, data: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.image_path(key).write_bytes(data)

    def _prune(self) -> None:
        cutoff = (dt_util.utcnow() - KEEP).isoformat()
        rows = sorted(self.rows.values(), key=lambda row: row["created_at"], reverse=True)
        keep = [row for row in rows if row["created_at"] >= cutoff][:KEEP_ROWS]
        dropped = [row["id"] for row in rows[len(keep) :]]
        self.rows = {row["id"]: row for row in keep}
        if dropped:
            self.hass.async_add_executor_job(self._unlink, dropped)

    def _unlink(self, keys: list[str]) -> None:
        for key in keys:
            self.image_path(key).unlink(missing_ok=True)

    @callback
    def _announce(self, row: dict[str, Any]) -> None:
        """The detection event (with a signed image link a phone can open) and the image entity."""
        camera = self.coordinator.blink.cameras.get(row["camera"])
        image = async_sign_path(self.hass, f"{API}/{row['id']}.jpg", IMAGE_TTL, use_content_user=True)
        attributes = {
            "camera": row["camera"],
            "clip_id": row["id"],
            "clip_created_at": row["created_at"],
            "summary": row["summary"],
            "people": row["people"],
            "unknown_faces": row["unknown_faces"],
            "labels": row["labels"],
            "known_only": row["known_only"],
            "image": image,
        }
        if camera is not None:
            async_dispatcher_send(
                self.hass, SIGNAL_EVENT.format(self.entry.entry_id, str(camera.camera_id)), EVENT_DETECTION, attributes
            )
        async_dispatcher_send(self.hass, SIGNAL_DETECTION.format(self.entry.entry_id), row["slug"])

    def latest(self, slug: str) -> dict[str, Any] | None:
        """The camera's newest detection."""
        return max(
            (row for row in self.rows.values() if row["slug"] == slug), key=lambda row: row["created_at"], default=None
        )

    def public(self, key: str) -> dict[str, Any] | None:
        """What the clips listing shows about a clip."""
        row = self.rows.get(key)
        if row is None:
            return None
        return {k: row[k] for k in ("summary", "people", "unknown_faces", "labels", "known_only")} | {
            "image_url": f"{API}/{key}.jpg"
        }

    async def async_request(self, method: str, path: str, data: bytes | None = None) -> tuple[int, bytes, str]:
        """Pass a people request through to the add-on: (status, body, content type)."""
        if self.url is None:
            await self._async_probe()
        if self.url is None:
            return 503, b'{"error": "Blink Vision is not installed or not running"}', "application/json"
        async with async_get_clientsession(self.hass).request(
            method, f"{self.url}{path}", data=data, timeout=TIMEOUT
        ) as resp:
            return resp.status, await resp.read(), resp.content_type
