"""HTTP API for smart detection, under /api/blink_instant_events/: detection pictures and the people Blink
Vision knows. Works for every provider, with or without a Blink account. See docs/app-api.md."""

from __future__ import annotations

import re
from urllib.parse import quote

from aiohttp import ClientError, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .pipeline import API as DETECTIONS_API, get_pipeline

DETECTION_ID = re.compile(r"[0-9a-f]{24}")  # never let a decoded "/" reach a path join
PEOPLE_API = "/api/blink_instant_events/people"
MAX_PHOTO = 10 * 1024 * 1024
NO_STORE = {"Cache-Control": "no-store"}
KEEP = {"Cache-Control": "private, max-age=86400"}  # an id names fixed bytes


def async_register_views(hass: HomeAssistant) -> None:
    """Once per Home Assistant run."""
    for view in (DetectionImageView, PeopleView, PersonView):
        hass.http.register_view(view())


def _admin(request: web.Request, what: str) -> None:
    if not request["hass_user"].is_admin:
        raise web.HTTPForbidden(text=f"Only an administrator can {what}\n")


class DetectionImageView(HomeAssistantView):
    """GET /detections/<id>.jpg: the frame Camera Vision judged, boxes drawn (signed links work too)."""

    url = DETECTIONS_API + "/{key}.jpg"
    name = "api:blink_instant_events:detection_image"

    async def get(self, request: web.Request, key: str) -> web.FileResponse:
        """The picture."""
        if not DETECTION_ID.fullmatch(key):
            raise web.HTTPNotFound
        hass = request.app["hass"]
        path = get_pipeline(hass).image_path(key)
        if not await hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={**KEEP, "Content-Type": "image/jpeg"})


async def to_vision(request: web.Request, method: str, path: str, data: bytes | None = None) -> web.Response:
    """Answer with what Camera Vision answered."""
    try:
        status, body, kind = await get_pipeline(request.app["hass"]).async_request(method, path, data)
    except (ClientError, TimeoutError) as err:
        raise web.HTTPBadGateway(text=f"Camera Vision did not answer: {type(err).__name__}\n") from err
    return web.Response(status=status, body=body, content_type=kind, headers=NO_STORE)


class PeopleView(HomeAssistantView):
    """GET /people: the people Camera Vision knows, with how many photos of each."""

    url = PEOPLE_API
    name = "api:blink_instant_events:people"

    async def get(self, request: web.Request) -> web.Response:
        """List."""
        return await to_vision(request, "GET", "/people")


class PersonView(HomeAssistantView):
    """POST /people/<name> (a JPEG: learn this face), DELETE /people/<name>, GET /people/<name>.jpg."""

    url = PEOPLE_API + "/{name}"
    name = "api:blink_instant_events:person"

    async def get(self, request: web.Request, name: str) -> web.Response:
        """The face from the first photo (admins only: it is a face picture)."""
        if not name.endswith(".jpg"):
            raise web.HTTPNotFound
        _admin(request, "see enrolled faces")
        return await to_vision(request, "GET", f"/people/{quote(name)}")

    async def post(self, request: web.Request, name: str) -> web.Response:
        """Add a photo of this person (admins only: it decides whose visits stay quiet)."""
        _admin(request, "teach faces")
        if not 0 < (request.content_length or 0) <= MAX_PHOTO:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_PHOTO, actual_size=request.content_length or 0)
        force = "?force=1" if request.query.get("force") == "1" else ""
        return await to_vision(request, "POST", f"/people/{quote(name)}{force}", await request.read())

    async def delete(self, request: web.Request, name: str) -> web.Response:
        """Forget this person."""
        _admin(request, "forget faces")
        return await to_vision(request, "DELETE", f"/people/{quote(name)}")
