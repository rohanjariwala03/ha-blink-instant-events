"""HTTP API for the app, under /api/blink_instant_events/. See docs/app-api.md."""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import ClientError, web
from blinkpy.auth import UnauthorizedError

from homeassistant.components.http import KEY_AUTHENTICATED, HomeAssistantView
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import TemperatureConverter

from . import controls
from .clips import API as CLIPS_API, clip_id, public_clip
from .const import DOMAIN
from .liveview import TOKEN_TTL_SECONDS, LiveViews, control_row, ptt_supported, talk

if TYPE_CHECKING:
    from . import BlinkData

CLIP_ID = re.compile(r"[0-9a-f]{24}")  # never let a decoded "/" reach a path join
SEGMENT = re.compile(r"segment_\d{5}\.ts")
CAMERA_API = "/api/blink_instant_events/cameras/{slug}"
NO_STORE = {"Cache-Control": "no-store"}
KEEP = {"Cache-Control": "private, max-age=86400"}  # an id names fixed bytes


def async_register_views(hass: HomeAssistant) -> None:
    """Once per Home Assistant run; views cannot be unregistered."""
    if hass.data.setdefault(DOMAIN, {}).get("views"):
        return
    for view in (
        ClipsView,
        ClipFileView,
        TokenView,
        PlaylistView,
        SegmentView,
        StopView,
        TalkView,
        ControlsView,
        LastLiveviewView,
    ):
        hass.http.register_view(view())
    hass.data[DOMAIN]["views"] = True


def runtime(request: web.Request) -> BlinkData:
    """The loaded account (one per Home Assistant)."""
    entries = request.app["hass"].config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise web.HTTPServiceUnavailable(text="Blink Instant Events is not loaded\n")
    return entries[0].runtime_data


# What blinkpy and Blink's odd answers raise; reported as 502 rather than escaping as 500.
BLINK_ERRORS = (ClientError, RuntimeError, TimeoutError, KeyError, ValueError, TypeError, OSError)


def blink_failed(request: web.Request, err: Exception) -> web.HTTPException:
    """A 503 (and Home Assistant's reauth prompt) when Blink signed us out, else a 502 with the reason."""
    if isinstance(err, UnauthorizedError):
        request.app["hass"].config_entries.async_loaded_entries(DOMAIN)[0].async_start_reauth(request.app["hass"])
        return web.HTTPServiceUnavailable(text="Blink signed this session out; re-authenticate in Home Assistant\n")
    return web.HTTPBadGateway(text=f"Blink did not answer as expected: {err}\n")


def _number(request: web.Request, key: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(request.query.get(key, default))))
    except ValueError:
        return default


def clip_query(request: web.Request, sources: set[str]) -> dict[str, Any]:
    """The listing arguments, clamped; `source` checked against an allow-list."""
    source = request.query.get("source", "both")
    if source not in sources:
        raise web.HTTPBadRequest(text=f"source must be one of {', '.join(sorted(sources))}\n")
    return {
        "source": source,
        "camera": request.query.get("camera") or None,
        "hours": _number(request, "hours", 24, 0.1, 24 * 30),
        "pages": int(_number(request, "pages", 3, 1, 10)),
        "limit": int(_number(request, "limit", 20, 1, 100)),
    }


class ClipsView(HomeAssistantView):
    """GET /clips?source=both|local|cloud&hours=&limit=&pages=&camera=."""

    url = CLIPS_API
    name = "api:blink_instant_events:clips"

    async def get(self, request: web.Request) -> web.Response:
        """List clips, newest first."""
        clips = runtime(request).clips
        query = clip_query(request, {"both", "cloud", "local"})
        try:
            rows = await clips.async_list(**query)
        except (UnauthorizedError, *BLINK_ERRORS) as err:
            raise blink_failed(request, err) from err
        hass = request.app["hass"]
        args = {k: v for k, v in query.items() if k in ("hours", "pages", "limit")}
        listed = []
        for clip in rows:
            cached = await hass.async_add_executor_job(clips.cached, clip_id(clip), ".jpg") is not None
            listed.append(public_clip(clip, args, cached))
        return web.json_response(
            {"count": len(listed), "source": query["source"], "hours": query["hours"], "clips": listed},
            headers=NO_STORE,
        )


class ClipFileView(HomeAssistantView):
    """GET /clips/<id>.mp4 and /clips/<id>.jpg, served from the cache (byte ranges work)."""

    url = CLIPS_API + "/{key}.{ext:mp4|jpg}"
    name = "api:blink_instant_events:clip_file"

    async def get(self, request: web.Request, key: str, ext: str) -> web.FileResponse:
        """One clip or its first frame."""
        if not CLIP_ID.fullmatch(key):
            raise web.HTTPNotFound
        clips = runtime(request).clips
        hass = request.app["hass"]
        query = clip_query(request, {"cloud", "local"})
        try:
            clip = None
            if await hass.async_add_executor_job(clips.cached, key, f".{ext}") is None:
                clip = await clips.async_find(key, query)
                if clip is None:
                    raise web.HTTPNotFound(text="Clip is no longer listed by Blink\n")
            path = await (clips.async_clip if ext == "mp4" else clips.async_thumbnail)(key, clip)
        except LookupError as err:  # evicted from the cache between the check and the read
            raise web.HTTPNotFound(text="Clip left the cache; list clips again\n") from err
        except (UnauthorizedError, *BLINK_ERRORS) as err:
            raise blink_failed(request, err) from err
        return web.FileResponse(path, headers={**KEEP, "Content-Type": "video/mp4" if ext == "mp4" else "image/jpeg"})


def live(request: web.Request, slug: str) -> LiveViews:
    """The live views, if this request may watch `slug`: Home Assistant auth or the camera's token."""
    views = runtime(request).live
    # 403, not 401: a stale token must not count as a failed login and get the phone's IP banned.
    # Checked before the camera lookup so strangers can't probe which cameras exist.
    if not request.get(KEY_AUTHENTICATED) and not views.token_ok(request.query.get("token", ""), slug):
        raise web.HTTPForbidden(text="Missing or expired camera token\n")
    if views.camera(slug) is None:
        raise web.HTTPNotFound(text=f"Unknown camera: {slug}\n")
    return views


class TokenView(HomeAssistantView):
    """GET or POST /cameras/<slug>/token (bearer auth): a 10-minute token for this camera."""

    url = CAMERA_API + "/token"
    name = "api:blink_instant_events:token"

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """Issue a token and a session id (the app may also make its own session id)."""
        views = live(request, slug)
        return web.json_response(
            {"token": views.issue_token(slug), "expires_in": TOKEN_TTL_SECONDS, "session": secrets.token_urlsafe(8)},
            headers=NO_STORE,
        )

    post = get


class PlaylistView(HomeAssistantView):
    """GET /cameras/<slug>/hls/index.m3u8?token=&session=: starts the camera on first fetch."""

    url = CAMERA_API + "/hls/index.m3u8"
    name = "api:blink_instant_events:hls_playlist"
    requires_auth = False

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """The playlist, with the token on every segment (players don't carry the query over)."""
        views = live(request, slug)
        try:
            session = await views.get_or_start(slug, request.query.get("session", ""))
            await session.wait_ready()
        except (UnauthorizedError, *BLINK_ERRORS) as err:
            await views.async_stop(slug)
            raise blink_failed(request, err) from err
        text = await request.app["hass"].async_add_executor_job(session.playlist.read_text)
        token = quote(request.query.get("token", ""), safe="")
        lines = [f"{line}?token={token}" if line and not line.startswith("#") else line for line in text.splitlines()]
        return web.Response(
            text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl", headers=NO_STORE
        )


class SegmentView(HomeAssistantView):
    """GET /cameras/<slug>/hls/segment_NNNNN.ts?token=."""

    url = CAMERA_API + "/hls/{filename}"
    name = "api:blink_instant_events:hls_segment"
    requires_auth = False

    async def get(self, request: web.Request, slug: str, filename: str) -> web.FileResponse:
        """One segment of the running session."""
        views = live(request, slug)
        session = views.sessions.get(slug)
        if not SEGMENT.fullmatch(filename) or session is None:
            raise web.HTTPNotFound
        session.touch()
        path = session.directory / filename
        if not await request.app["hass"].async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={**NO_STORE, "Content-Type": "video/mp2t"})


class StopView(HomeAssistantView):
    """POST /cameras/<slug>/stop?token=: free the camera now."""

    url = CAMERA_API + "/stop"
    name = "api:blink_instant_events:stop"
    requires_auth = False

    async def post(self, request: web.Request, slug: str) -> web.Response:
        """End the session; the copy becomes the last live view."""
        stopped = await live(request, slug).async_stop(slug)
        return web.json_response({"stopped": stopped, "slug": slug}, headers=NO_STORE)


class TalkView(HomeAssistantView):
    """WebSocket /cameras/<slug>/ptt?token=&session=: push-to-talk on the viewer's live view."""

    url = CAMERA_API + "/ptt"
    name = "api:blink_instant_events:ptt"
    requires_auth = False

    async def get(self, request: web.Request, slug: str) -> web.WebSocketResponse:
        """Bridge the app's microphone to the camera's speaker."""
        views = live(request, slug)
        camera = views.camera(slug)
        if camera is None or not ptt_supported(camera):
            raise web.HTTPBadRequest(text="Push-to-talk is not supported on this camera\n")
        stream = views.viewer_stream(slug, request.query.get("session", ""))
        if stream is None:
            raise web.HTTPConflict(text="Start live view first; talk rides its session\n")
        return await talk(request, views.ffmpeg, stream)


class ControlsView(HomeAssistantView):
    """GET/POST /cameras/<slug>/controls?token=&session=: night vision, flood light, volume, temperature."""

    url = CAMERA_API + "/controls"
    name = "api:blink_instant_events:controls"
    requires_auth = False

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """The camera's settings; also reports, once, what was written after the last live view."""
        views, camera = self._camera(request, slug)
        blink = camera.sync.blink
        try:
            async with asyncio.timeout(20):
                state = await controls.fetch_state(
                    blink,
                    control_row(camera),
                    views.viewer_stream(slug, request.query.get("session", "")),
                    views.deferred,
                )
        except (UnauthorizedError, *BLINK_ERRORS) as err:
            raise blink_failed(request, err) from err
        if (result := views.deferred.take_result(slug)) is not None:
            state["deferred_result"] = result
        return self._answer(request, state)

    async def post(self, request: web.Request, slug: str) -> web.Response:
        """Change controls; `rejected`, `busy`, `pending` and `deferred` say what did not land (yet)."""
        views, camera = self._camera(request, slug)
        if (request.content_length or 0) > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length or 0)
        try:
            body = await request.json()
        except ValueError as err:
            raise web.HTTPBadRequest(text="Body must be JSON\n") from err
        try:
            async with asyncio.timeout(30):
                state = await controls.apply_changes(
                    camera.sync.blink,
                    control_row(camera),
                    body,
                    views.viewer_stream(slug, request.query.get("session", "")),
                    views.deferred,
                )
        except controls.ControlError as err:
            raise web.HTTPBadRequest(text=f"{err}\n") from err
        except (UnauthorizedError, *BLINK_ERRORS) as err:
            raise blink_failed(request, err) from err
        return self._answer(request, state)

    @staticmethod
    def _camera(request: web.Request, slug: str) -> tuple[LiveViews, Any]:
        views = live(request, slug)
        return views, views.camera(slug)

    @staticmethod
    def _answer(request: web.Request, state: dict[str, Any]) -> web.Response:
        """Blink reports °F; answer in the unit Home Assistant is set to."""
        fahrenheit = state.get("temperature_f")
        unit = request.app["hass"].config.units.temperature_unit
        state["temperature"] = (
            None
            if fahrenheit is None
            else round(TemperatureConverter.convert(fahrenheit, UnitOfTemperature.FAHRENHEIT, unit))
        )
        state["temperature_unit"] = None if fahrenheit is None else unit
        return web.json_response(state, headers=NO_STORE)


class LastLiveviewView(HomeAssistantView):
    """GET /cameras/<slug>/last-liveview ({available, ended_at}) and /last-liveview.mp4."""

    url = CAMERA_API + "/last-liveview"
    extra_urls = [CAMERA_API + "/last-liveview.mp4"]
    name = "api:blink_instant_events:last_liveview"
    requires_auth = False

    async def get(self, request: web.Request, slug: str) -> web.StreamResponse:
        """The info, or the file."""
        views = live(request, slug)
        if not request.path.endswith(".mp4"):
            return web.json_response(await views.last_liveview(slug), headers=NO_STORE)
        try:
            path = await views.last_liveview_mp4(slug)
        except RuntimeError as err:
            raise web.HTTPBadGateway(text=f"{err}\n") from err
        if path is None:
            raise web.HTTPNotFound(text="No live view saved yet\n")
        return web.FileResponse(
            path,
            headers={
                **NO_STORE,
                "Content-Type": "video/mp4",
                "Content-Disposition": f'attachment; filename="{slug}_last_liveview.mp4"',
            },
        )
