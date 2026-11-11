"""Smart detection for every camera, on your own hardware.

Providers decide when to look and hand over JPEG frames with a `Source`. This module sends them to Blink
Vision (the add-on in this repo: YuNet and SFace for faces, YOLOX-S for people, vehicles and animals), decides
who was seen, keeps the best frame with boxes drawn, stores the history and announces one detection.

Recognition is a likelihood, not proof of identity: a name counts only when it matched in at least
CONFIRM_FRAMES frames (or once, very strongly); anything weaker is reported as uncertain, never as known.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import io
import logging
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientTimeout
from PIL import Image, ImageDraw

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import CONF_VISION_TOKEN, CONF_VISION_URL, DOMAIN, EVENT_DETECTION, SIGNAL_DETECTION, SIGNAL_EVENT
from ..providers.base import CameraEvent, Source

_LOGGER = logging.getLogger(__name__)

API = "/api/blink_instant_events/detections"
ADDON_SLUG = "blink_vision"
ADDON_PORT = 8000
TIMEOUT = ClientTimeout(total=30)
FRAMES = 10  # one a second from the start of a clip
KEEP = timedelta(days=7)
KEEP_ROWS = 1000
PROBE_INTERVAL = timedelta(minutes=10)
IMAGE_TTL = timedelta(hours=24)  # how long the signed image link in an event (and so in history) works
CONFIRM_FRAMES = 2  # frames a name must match in before it counts as known
STRONG_MATCH = 0.5  # SFace cosine similarity at which one frame is enough (the add-on's threshold is 0.363)
PETS = {"cat", "dog"}
COLORS = {
    "known": (46, 204, 113),
    "uncertain": (255, 59, 48),
    "face": (255, 149, 0),
    "person": (10, 132, 255),
    "vehicle": (191, 90, 242),
    "animal": (255, 214, 10),
}


def _match(face: dict[str, Any]) -> str:
    """known, uncertain or unknown; add-ons before 0.2 only send a name (or None)."""
    return face.get("match") or ("known" if face.get("name") else "unknown")


def identities(frames: list[dict[str, Any]]) -> tuple[list[str], list[str], int, dict[str, float]]:
    """(confirmed names, uncertain names, most unknown faces in one frame, best similarity per name)."""
    hits: dict[str, int] = {}
    best: dict[str, float] = {}
    uncertain: set[str] = set()
    unknown = 0
    for frame in frames:
        unknown_here = 0
        for face in frame["faces"]:
            match = _match(face)
            if match == "known":
                name = face["name"]
                hits[name] = hits.get(name, 0) + 1
                best[name] = max(best.get(name, 0.0), float(face.get("similarity", 1.0)))
            elif match == "uncertain" and face.get("candidate"):
                uncertain.add(face["candidate"])
            else:
                unknown_here += 1
        unknown = max(unknown, unknown_here)
    known = sorted(name for name, count in hits.items() if count >= CONFIRM_FRAMES or best[name] >= STRONG_MATCH)
    uncertain = (uncertain | set(hits)) - set(known)
    return known, sorted(uncertain), unknown, {name: round(best[name], 3) for name in known}


def summarize(frames: list[dict[str, Any]]) -> dict[str, Any]:
    """One answer for an event from its frames: who, who maybe, how many unknown faces, which kinds of things."""
    people, maybe, unknown, matches = identities(frames)
    labels = {obj["group"] for frame in frames for obj in frame["objects"]}
    if any(obj["label"] in PETS for frame in frames for obj in frame["objects"]):
        labels.add("pet")
    if people or maybe or unknown:
        labels.add("person")
    parts = list(people) + [f"possibly {name}" for name in maybe]
    if unknown:
        parts.append(f"{unknown} unknown people" if unknown > 1 else "an unknown person")
    elif "person" in labels and not parts:
        parts.append("a person")
    parts += [
        f"a {label}" if label == "vehicle" else f"an {label}" for label in ("vehicle", "animal") if label in labels
    ]
    text = f"{', '.join(parts[:-1])} and {parts[-1]}" if len(parts) > 1 else (parts[0] if parts else "nothing found")
    scores = [obj.get("score", 0.0) for frame in frames for obj in frame["objects"]]
    return {
        "summary": text[0].upper() + text[1:],
        "people": people,
        "uncertain": maybe,
        "unknown_faces": unknown,
        "labels": sorted(labels),
        # The most cautious reading wins: one unknown face makes the event unknown.
        "identity": "unknown" if unknown else "uncertain" if maybe else "known" if people else None,
        "matches": matches,
        "confidence": round(max(scores), 3) if scores else None,
        # Only people you know were seen: their faces, confirmed, and no face anyone else's.
        "known_only": bool(people) and not unknown and not maybe,
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
    font_size = max(14, image.height // 30)
    for obj in frame["objects"]:
        x, y, w, h = obj["box"]
        draw.rectangle((x, y, x + w, y + h), outline=COLORS.get(obj["group"], COLORS["person"]), width=width)
    for face in frame["faces"]:
        x, y, w, h = face["box"]
        match = _match(face)
        color = COLORS["known" if match == "known" else "uncertain" if match == "uncertain" else "face"]
        draw.rectangle((x, y, x + w, y + h), outline=color, width=width)
        label = face["name"] if match == "known" else f"{face['candidate']}?" if face.get("candidate") else "Unknown"
        left, top, right, bottom = draw.textbbox((x, y), label, font_size=font_size)
        draw.rectangle((left - 4, top - (bottom - top) - 8, right + 4, top - 2), fill=color)
        draw.text((x, top - (bottom - top) - 6), label, fill=(0, 0, 0), font_size=font_size)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


def split_jpegs(data: bytes) -> list[bytes]:
    """ffmpeg's MJPEG pipe as separate JPEGs (0xFFD9 can't occur inside one: entropy data stuffs 0xFF)."""
    return [part + b"\xff\xd9" for part in data.split(b"\xff\xd9") if part.startswith(b"\xff\xd8")]


async def async_start_pipeline(hass: HomeAssistant, entry: ConfigEntry) -> Pipeline:
    """For one entry's lifetime: past detections loaded, the add-on found, and looked for again now and then."""
    pipeline = get_pipeline(hass)
    await pipeline.async_load()
    await pipeline.async_probe()
    entry.async_on_unload(async_track_time_interval(hass, pipeline.async_probe, PROBE_INTERVAL))
    return pipeline


def get_pipeline(hass: HomeAssistant) -> Pipeline:
    """The one pipeline every camera shares (one add-on, one history)."""
    data = hass.data.setdefault(DOMAIN, {})
    if "pipeline" not in data:
        data["pipeline"] = Pipeline(hass)
    return data["pipeline"]


class Pipeline:
    """Owns the add-on connection, the analysis lock and the stored detections."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize; async_load before the first analysis."""
        self.hass = hass
        self.ffmpeg = get_ffmpeg_manager(hass).binary
        self.url: str | None = None
        self.token: str | None = None
        self.rows: dict[str, dict[str, Any]] = {}  # detection id -> detection
        self.root = Path(hass.config.cache_path(DOMAIN, "detections"))
        self._store: Store[list[dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.detections")
        # ponytail: one analysis at a time across all cameras; the add-on runs one model at a time anyway.
        self._lock = asyncio.Lock()
        self._loaded = False

    @property
    def enabled(self) -> bool:
        """Whether the add-on answered."""
        return self.url is not None

    @property
    def busy(self) -> bool:
        """An analysis is running; a provider may drop frames rather than queue behind it."""
        return self._lock.locked()

    async def async_load(self) -> None:
        """Past detections, once per Home Assistant run."""
        if not self._loaded:
            self._loaded = True
            self.rows = {row["id"]: row for row in await self._store.async_load() or []}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def configured(self) -> list[tuple[str, str | None]]:
        """(url, token) set in any entry's options; then there is no need to ask the Supervisor."""
        return [
            (url.rstrip("/"), settings.get(CONF_VISION_TOKEN) or None)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if not entry.disabled_by
            for settings in ({**entry.data, **entry.options},)
            if (url := settings.get(CONF_VISION_URL))
        ]

    async def _candidates(self) -> list[tuple[str, str | None]]:
        if configured := self.configured():
            return configured
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
        return [(f"http://{host}:{ADDON_PORT}", None) for host in hosts]

    async def async_probe(self, _now: datetime | None = None) -> None:
        """Find the add-on (again)."""
        found: tuple[str, str | None] | None = None
        for url, token in await self._candidates():
            try:
                async with async_get_clientsession(self.hass).get(
                    f"{url}/health", timeout=ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        found = (url, token)
                        break
            except (ClientError, TimeoutError):
                continue
        if (found and found[0]) != self.url:
            _LOGGER.info(
                "Blink Vision %s", f"found at {found[0]}" if found else "is not reachable; smart detection is off"
            )
        self.url, self.token = found if found else (None, None)

    async def async_frames(self, path: Path, count: int = FRAMES) -> list[bytes]:
        """Up to `count` JPEGs, one a second, from a video file."""
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            *("-hide_banner", "-loglevel", "error", "-i", str(path)),
            *("-vf", "fps=1,scale='min(1280,iw)':-2", "-frames:v", str(count)),
            *("-q:v", "3", "-c:v", "mjpeg", "-f", "image2pipe", "-"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 60)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg could not read the video: {err.decode(errors='replace')[-200:]}")
        return split_jpegs(out)

    async def async_analyze(
        self, source: Source, key: str, jpegs: list[bytes], created: datetime, *, empty: str | None = None
    ) -> dict[str, Any]:
        """Analyze one event's frames, store and announce the detection. `empty` replaces "Nothing found"."""
        jpegs = list(dict.fromkeys(jpegs))  # a still camera sends the same picture more than once
        if not jpegs or self.url is None:
            raise RuntimeError("no frames" if not jpegs else "Blink Vision is not reachable")
        await self.async_load()
        async with self._lock:
            session = async_get_clientsession(self.hass)
            frames = []
            for jpeg in jpegs:
                async with session.post(
                    f"{self.url}/analyze", data=jpeg, headers=self._headers(), timeout=TIMEOUT
                ) as resp:
                    if resp.status == 401:
                        raise RuntimeError("Blink Vision refused the token; set it up again")
                    resp.raise_for_status()
                    frames.append(await resp.json())
            best = best_frame(frames)
            image = await self.hass.async_add_executor_job(annotate, jpegs[best], frames[best])
            await self.hass.async_add_executor_job(self._save_image, key, image)
        row = {
            "id": key,
            "entry_id": source.entry_id,
            "provider": source.provider,
            "location": source.location,
            "camera": source.name,
            "camera_id": source.camera_id,
            "slug": source.slug,
            "created_at": created.isoformat(),
            "analyzed_at": dt_util.utcnow().isoformat(),
            **summarize(frames),
        }
        if empty and not row["labels"]:
            row["summary"] = empty
        self.rows[key] = row
        self._prune()
        self._store.async_delay_save(lambda: list(self.rows.values()), 5)
        self._announce(row, source)
        return row

    def image_path(self, key: str) -> Path:
        """Where a detection's annotated best frame is kept."""
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
    def _announce(self, row: dict[str, Any], source: Source) -> None:
        """The detection event (with a signed image link a phone can open) and the per-camera entities."""
        image = async_sign_path(self.hass, f"{API}/{row['id']}.jpg", IMAGE_TTL, use_content_user=True)
        event = CameraEvent(source.camera_id, source.provider, EVENT_DETECTION, source.location, True)
        attributes = {
            **{k: v for k, v in asdict(event).items() if k not in ("camera_id", "event_type", "timestamp")},
            "camera": row["camera"],
            "detection_id": row["id"],
            "clip_id": row["id"],  # the name automations used before there were other providers
            "clip_created_at": row["created_at"],
            "timestamp": row["analyzed_at"],
            **{k: row[k] for k in PUBLIC},
            "image": image,
        }
        async_dispatcher_send(
            self.hass, SIGNAL_EVENT.format(source.entry_id, source.camera_id), EVENT_DETECTION, attributes
        )
        async_dispatcher_send(self.hass, SIGNAL_DETECTION.format(source.entry_id), source.slug)

    def latest(self, entry_id: str, slug: str) -> dict[str, Any] | None:
        """The camera's newest detection (rows from before 0.7 belong to the Blink entry)."""
        return max(
            (row for row in self.rows.values() if row["slug"] == slug and row.get("entry_id", entry_id) == entry_id),
            key=lambda row: row["created_at"],
            default=None,
        )

    def public(self, key: str) -> dict[str, Any] | None:
        """What a listing shows about a detection."""
        row = self.rows.get(key)
        if row is None:
            return None
        return {k: row.get(k) for k in PUBLIC} | {"image_url": f"{API}/{key}.jpg"}

    async def async_request(self, method: str, path: str, data: bytes | None = None) -> tuple[int, bytes, str]:
        """Pass a people request through to the add-on: (status, body, content type)."""
        if self.url is None:
            await self.async_probe()
        if self.url is None:
            return 503, b'{"error": "Blink Vision is not installed or not running"}', "application/json"
        async with async_get_clientsession(self.hass).request(
            method, f"{self.url}{path}", data=data, headers=self._headers(), timeout=TIMEOUT
        ) as resp:
            return resp.status, await resp.read(), resp.content_type


PUBLIC = (
    "summary",
    "people",
    "uncertain",
    "unknown_faces",
    "labels",
    "identity",
    "matches",
    "confidence",
    "known_only",
)


def detection_key(*parts: object) -> str:
    """A stable, opaque 24-hex id for a detection (the image URL accepts nothing else)."""
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:24]
