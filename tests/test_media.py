"""Phase 2: snapshot camera, snapshot button, snapshot after motion, clips API."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import json
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.blink_instant_events.const import CONF_SNAPSHOT_AFTER_MOTION
from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


async def test_snapshot_camera(hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock) -> None:
    """camera.<name> serves the cached thumbnail; attributes the app reads."""
    cam = mock_blink.cameras["Backyard"]
    cam.thumbnail = "https://rest-u001/api/v3/media/thumbnail.jpg?ts=1775306079&ext="
    cam._cached_image = cam.image_from_cache = b"jpeg-1"  # a new picture brings its own time
    setup_entry.runtime_data.coordinator.async_update_listeners()
    state = hass.states.get("camera.backyard")
    assert state.attributes["snapshot_of"] == "backyard"
    assert state.attributes["snapshot_id"]
    assert state.attributes["snapshot_taken"].startswith("2026-04-04")
    assert state.attributes["entity_picture"].startswith("/api/camera_proxy/camera.backyard")
    assert (await async_get_image(hass, "camera.backyard")).content == b"jpeg-1"
    assert hass.states.get("camera.front_door").attributes["snapshot_of"] == "front_door"


async def test_refresh_snapshot_button(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock
) -> None:
    """The button asks Blink for a picture and waits for the new image."""
    cam = mock_blink.cameras["Backyard"]

    async def new_picture(*_a: object, **_k: object) -> None:
        cam._cached_image = b"jpeg-new"

    cam.update = AsyncMock(side_effect=new_picture)
    await hass.services.async_call("button", "press", {"entity_id": "button.backyard_refresh_snapshot"}, blocking=True)
    cam.snap_picture.assert_awaited_once()


async def test_snapshot_after_motion(hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock) -> None:
    """Off by default; when on, one snapshot 30 s after motion, at most every 5 minutes."""
    listener = setup_entry.runtime_data.listener
    cam = mock_blink.cameras["Backyard"]
    cam.update = AsyncMock(side_effect=lambda *a, **k: setattr(cam, "_cached_image", object()))

    def motion(n: int) -> None:
        body = {"created_at": dt_util.utcnow().isoformat(), "event_type": "pir", "camera_id": 100001}
        listener._on_push({"data": json.dumps(body)}, f"p{n}")

    motion(1)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    cam.snap_picture.assert_not_awaited()

    hass.config_entries.async_update_entry(setup_entry, options={CONF_SNAPSHOT_AFTER_MOTION: True})
    motion(2)
    motion(3)  # inside the 5-minute window: no second snapshot
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert cam.snap_picture.await_count == 1


async def test_clips_api(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,  # before setup_entry: the integration's session must be the mock
    setup_entry: MockConfigEntry,
    mock_blink: MagicMock,
    hass_client,
    hass_client_no_auth,
) -> None:
    """Listing shape, then a download cached on first fetch; bearer auth required."""
    created = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
    media = {"media": [{"device_name": "Backyard", "media": "/clip/1.mp4", "created_at": created, "size": 1234}]}
    aioclient_mock.get("https://rest-u001/clip/1.mp4", content=b"mp4-bytes")
    client = await hass_client()
    with patch(
        "custom_components.blink_instant_events.clips.blink_api.request_videos",
        AsyncMock(side_effect=[media, {"media": []}]),
    ):
        resp = await client.get("/api/blink_instant_events/clips?source=both&hours=720&limit=100&pages=10")
    assert resp.status == HTTPStatus.OK
    (clip,) = (await resp.json())["clips"]
    assert {k: clip[k] for k in ("source", "slug", "camera_name", "size", "cached")} == {
        "source": "cloud",
        "slug": "backyard",
        "camera_name": "Backyard",
        "size": 1234,
        "cached": False,
    }
    assert clip["download_url"].startswith(f"/api/blink_instant_events/clips/{clip['id']}.mp4?")
    assert clip["thumbnail_url"].startswith(f"/api/blink_instant_events/clips/{clip['id']}.jpg?")

    for _ in range(2):  # the second request is served from disk
        resp = await client.get(clip["download_url"])
        assert resp.status == HTTPStatus.OK
        assert await resp.read() == b"mp4-bytes"
    assert aioclient_mock.call_count == 1

    anonymous = await hass_client_no_auth()
    assert (await anonymous.get(clip["download_url"])).status == HTTPStatus.UNAUTHORIZED
