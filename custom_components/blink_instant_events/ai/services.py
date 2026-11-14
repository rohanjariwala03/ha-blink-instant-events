"""Actions for smart detection, for any camera: teach, correct or forget a face, set where detections count,
analyze a camera's picture now.

Teaching, correcting, forgetting and zones are admin-only (they decide whose visits stay quiet). A photo comes
from a camera entity, a file in a folder Home Assistant allows (allowlist_external_dirs, e.g. /media), or a face
in a past detection (its `detection_id`; `face` picks one by the number on the picture when there are several).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError
import voluptuous as vol

from homeassistant.components.camera import async_get_image
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util, slugify

from ..const import CONF_CAMERA, DOMAIN
from ..providers.base import Source
from .pipeline import detection_key, get_pipeline
from .views import MAX_PHOTO

DETECTION = vol.Match(r"^[0-9a-f]{24}$")  # detection ids are 24 hex characters; nothing else may reach a file path
PHOTO = vol.Schema(
    {
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=40)),
        vol.Exclusive("camera", "photo"): cv.entity_domain("camera"),
        vol.Exclusive("file", "photo"): cv.string,
        vol.Exclusive("detection", "photo"): DETECTION,
        vol.Optional("face", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
        vol.Optional("force", default=False): cv.boolean,
    }
)
NAME = vol.Schema({vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=40))})
NOT_THEM = vol.Schema(
    {vol.Required("detection"): DETECTION, vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=40))}
)
PERCENT = vol.All(vol.Coerce(float), vol.Range(min=0, max=100))


def _polygon(points: list) -> list:
    if len(points) in (1, 2):
        raise vol.Invalid("A zone needs at least 3 points (or none, for the whole picture)")
    return points


ZONE = vol.Schema(
    {
        vol.Required("camera"): cv.entity_id,
        vol.Optional("zone", default=[]): vol.All(
            cv.ensure_list, [vol.ExactSequence([PERCENT, PERCENT])], vol.Length(max=50), _polygon
        ),
    }
)
SNAPSHOT = vol.Schema({vol.Required("camera"): cv.entity_domain("camera")})


async def _picture(hass: HomeAssistant, data: dict[str, Any]) -> bytes:
    if camera := data.get("camera"):
        try:
            return (await async_get_image(hass, camera, timeout=10)).content
        except HomeAssistantError as err:
            raise HomeAssistantError(f"{camera} gave no picture: {err}") from err
    if detection := data.get("detection"):
        path = get_pipeline(hass).face_path(detection, data.get("face", 1))
        face = await hass.async_add_executor_job(lambda: path.read_bytes() if path.is_file() else None)
        if face is None:
            raise ServiceValidationError(
                f"Detection {detection} has no face {data.get('face', 1)} to learn (or is older than 7 days)"
            )
        return face
    if not (file := data.get("file")):
        raise ServiceValidationError("Give a camera, a file or a detection")

    def read() -> bytes:
        path = Path(file)
        if not hass.config.is_allowed_path(str(path)):
            raise ServiceValidationError(f"{file} is not in a folder Home Assistant allows")
        if not path.is_file() or path.stat().st_size > MAX_PHOTO:
            raise ServiceValidationError(f"{file} is not a photo of at most 10 MB")
        return path.read_bytes()

    return await hass.async_add_executor_job(read)


async def _vision(hass: HomeAssistant, method: str, path: str, body: bytes | None = None) -> dict[str, Any]:
    try:
        status, answer, _kind = await get_pipeline(hass).async_request(method, path, body)
    except (ClientError, TimeoutError) as err:
        raise HomeAssistantError(f"Camera Vision did not answer: {type(err).__name__}") from err
    text = answer.decode(errors="replace")
    if status == 404 and "Not found" in text:
        raise HomeAssistantError("Camera Vision is too old for this; update the Camera Vision add-on")
    if status != 200:
        raise HomeAssistantError(f"Camera Vision said {status}: {text[:200]}")
    try:
        return json.loads(text)
    except ValueError as err:
        raise HomeAssistantError("Camera Vision answered something other than JSON") from err


def _camera_source(hass: HomeAssistant, entity_id: str) -> Source:
    """The camera behind an entity: one of ours (any entity of the camera's device), or one a local entry reads."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.data.get(CONF_CAMERA) == entity_id:
            return entry.runtime_data.source
    registered = er.async_get(hass).async_get(entity_id)
    device = dr.async_get(hass).async_get(registered.device_id) if registered and registered.device_id else None
    entry = hass.config_entries.async_get_entry(registered.config_entry_id) if registered else None
    if device and entry and entry.domain == DOMAIN and entry.state is ConfigEntryState.LOADED:
        data = entry.runtime_data
        if source := getattr(data, "source", None):
            return source
        ids = {value for domain, value in device.identifiers if domain == DOMAIN}
        if camera := next((cam for cam in data.coordinator.blink.cameras.values() if cam.serial in ids), None):
            return data.vision.source(camera)
    raise ServiceValidationError(f"{entity_id} is not a camera Local Camera Vision watches")


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions (once per Home Assistant run)."""

    async def enroll_face(call: ServiceCall) -> ServiceResponse:
        photo = await _picture(hass, call.data)
        force = "?force=1" if call.data["force"] else ""
        return await _vision(hass, "POST", f"/people/{quote(call.data['name'], safe='')}{force}", photo)

    async def delete_face(call: ServiceCall) -> ServiceResponse:
        return await _vision(hass, "DELETE", f"/people/{quote(call.data['name'], safe='')}")

    async def not_this_person(call: ServiceCall) -> ServiceResponse:
        detection, name = call.data["detection"], call.data["name"]
        path = get_pipeline(hass).named_face_path(detection, name)
        face = await hass.async_add_executor_job(lambda: path.read_bytes() if path.is_file() else None)
        if face is None:
            raise ServiceValidationError(
                f"Detection {detection} matched nobody called {name} (or is older than 7 days)"
            )
        return await _vision(hass, "POST", f"/people/{quote(name, safe='')}/not", face)

    async def set_zone(call: ServiceCall) -> None:
        source = _camera_source(hass, call.data["camera"])
        pipeline = get_pipeline(hass)
        await pipeline.async_load()
        zone = [(x / 100, y / 100) for x, y in call.data["zone"]]
        pipeline.set_zone(f"{source.entry_id}/{source.slug}", zone or None)

    async def process_snapshot(call: ServiceCall) -> ServiceResponse:
        camera = call.data["camera"]
        pipeline = get_pipeline(hass)
        await pipeline.async_load()
        if not pipeline.enabled:
            await pipeline.async_probe()
        picture = await _picture(hass, call.data)
        state = hass.states.get(camera)
        source = Source("", "camera", camera, slugify(camera), state.name if state else camera, "local")
        now = dt_util.utcnow()
        try:
            row = await pipeline.async_analyze(
                source, detection_key(camera, now.isoformat()), [picture], now, drop_parked_vehicles=False
            )
        except (ClientError, RuntimeError) as err:
            raise HomeAssistantError(f"Could not analyze {camera}: {err}") from err
        return {k: v for k, v in row.items() if k not in ("entry_id",)}

    async_register_admin_service(
        hass, DOMAIN, "enroll_face", enroll_face, PHOTO, supports_response=SupportsResponse.OPTIONAL
    )
    async_register_admin_service(
        hass, DOMAIN, "delete_face", delete_face, NAME, supports_response=SupportsResponse.OPTIONAL
    )
    async_register_admin_service(
        hass, DOMAIN, "not_this_person", not_this_person, NOT_THEM, supports_response=SupportsResponse.OPTIONAL
    )
    async_register_admin_service(hass, DOMAIN, "set_zone", set_zone, ZONE)
    hass.services.async_register(
        DOMAIN, "process_snapshot", process_snapshot, SNAPSHOT, supports_response=SupportsResponse.OPTIONAL
    )
