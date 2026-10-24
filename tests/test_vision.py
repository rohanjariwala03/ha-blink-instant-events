"""Smart detection (Blink Vision), the people API and show_on_tv."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import io
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed, async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.blink_instant_events.const import CONF_LIVE_LOOK, CONF_VISION_URL
from custom_components.blink_instant_events.vision import best_frame, split_jpegs, summarize
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

VISION = "http://vision.test:8000"
JSON = {"Content-Type": "application/json"}
FFMPEG = shutil.which("ffmpeg") or pytest.importorskip("imageio_ffmpeg").get_ffmpeg_exe()


def frame(faces: list[str | None] = (), objects: list[str] = ()) -> dict:
    groups = {"person": "person", "car": "vehicle", "dog": "animal"}
    return {
        "faces": [{"name": n, "box": [10, 10, 40, 40]} for n in faces],
        "objects": [{"label": o, "group": groups[o], "box": [0, 0, 100, 200]} for o in objects],
    }


@pytest.mark.parametrize(
    ("frames", "summary", "known_only"),
    [
        ([frame(["Rohan"], ["person"])], "Rohan", True),
        ([frame(["Rohan"]), frame([None])], "Rohan and an unknown person", False),
        ([frame([None, None])], "2 unknown people", False),
        ([frame(objects=["person"])], "A person", False),
        ([frame(objects=["car"]), frame(objects=["dog"])], "A vehicle and an animal", False),
        ([frame(["Rohan", "Priya"], ["car"])], "Priya, Rohan and a vehicle", True),
        ([frame()], "Nothing found", False),
    ],
)
def test_summarize(frames: list[dict], summary: str, known_only: bool) -> None:
    """One sentence for a clip, and whether only known people were in it."""
    result = summarize(frames)
    assert result["summary"] == summary
    assert result["known_only"] is known_only


def test_best_frame_and_split() -> None:
    """Faces beat bodies beat anything; the MJPEG pipe splits into whole JPEGs."""
    assert best_frame([frame(objects=["car"]), frame(objects=["person"]), frame([None])]) == 2
    assert best_frame([frame(objects=["car"]), frame(objects=["person"])]) == 1
    assert split_jpegs(b"\xff\xd8a\xff\xd9\xff\xd8b\xff\xd9") == [b"\xff\xd8a\xff\xd9", b"\xff\xd8b\xff\xd9"]


@pytest.fixture
def clip_file(tmp_path: Path) -> Path:
    """A 3 s test clip."""
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=10",
            "-t",
            "3",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


async def test_detection_end_to_end(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_fcm: MagicMock,
    mock_account: MagicMock,
    mock_blink: MagicMock,
    aioclient_mock: AiohttpClientMocker,
    hass_client,
    clip_file: Path,
) -> None:
    """Motion push -> clip -> frames -> Blink Vision -> event, image entity, clip label, picture."""
    aioclient_mock.get(f"{VISION}/health", json={"ok": True})
    aioclient_mock.post(
        f"{VISION}/analyze",
        json={
            "faces": [{"name": "Rohan", "box": [50, 50, 60, 60]}],
            "objects": [{"label": "person", "group": "person", "box": [20, 20, 200, 300]}],
        },
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_VISION_URL: VISION})
    clip = {
        "source": "cloud",
        "slug": "backyard",
        "camera_name": "Backyard",
        "created_at": dt_util.utcnow(),
        "size": 1,
        "url": "https://rest-u001/clip.mp4",
    }
    with (
        patch("custom_components.blink_instant_events.vision.WAIT_FOR_CLIP", (0,)),
        patch("custom_components.blink_instant_events.clips.Clips.async_list", AsyncMock(return_value=[clip])),
        patch("custom_components.blink_instant_events.clips.Clips.async_clip", AsyncMock(return_value=clip_file)),
        patch("custom_components.blink_instant_events.clips.Clips.cached", return_value=None),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        data = config_entry.runtime_data
        data.vision.ffmpeg = FFMPEG
        assert data.vision.enabled

        body = {"created_at": dt_util.utcnow().isoformat(), "event_type": "pir", "camera_id": 100001}
        data.listener._on_push({"data": json.dumps(body)}, "p1")
        await hass.async_block_till_done(wait_background_tasks=True)

        event = hass.states.get("event.backyard_blink_event")
        assert event.attributes["event_type"] == "detection"
        assert event.attributes["summary"] == "Rohan"
        assert event.attributes["known_only"] is True
        assert "authSig=" in event.attributes["image"]

        image = hass.states.get("image.backyard_last_detection")
        assert image.state != "unavailable"
        assert image.attributes["people"] == ["Rohan"]

        client = await hass_client()
        listed = await (await client.get("/api/blink_instant_events/clips?source=cloud")).json()
        assert listed["smart_detection"] is True
        assert listed["clips"][0]["detection"]["summary"] == "Rohan"
        resp = await client.get(listed["clips"][0]["detection"]["image_url"])
        assert resp.status == HTTPStatus.OK
        picture = Image.open(io.BytesIO(await resp.read()))
        assert picture.size == (640, 360)
        # Analyzed once: a second push finds nothing new.
        calls = aioclient_mock.call_count
        data.listener._on_push({"data": json.dumps(body | {"unique_id": "x2"})}, "p2")
        await hass.async_block_till_done(wait_background_tasks=True)
        assert aioclient_mock.call_count == calls


async def test_live_look(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_fcm: MagicMock,
    mock_account: MagicMock,
    mock_blink: MagicMock,
    aioclient_mock: AiohttpClientMocker,
    clip_file: Path,
) -> None:
    """No clips: a push opens live view, reads its video, and leaves a viewer's session alone."""
    aioclient_mock.get(f"{VISION}/health", json={"ok": True})
    aioclient_mock.post(
        f"{VISION}/analyze",
        json={"faces": [], "objects": [{"label": "car", "group": "vehicle", "box": [20, 20, 200, 100]}]},
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_VISION_URL: VISION, CONF_LIVE_LOOK: True})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    vision = config_entry.runtime_data.vision
    vision.ffmpeg = FFMPEG
    live = vision.live = MagicMock(sessions={}, viewers={})
    session = MagicMock(wait_ready=AsyncMock())

    async def start(slug: str, viewer: str) -> MagicMock:
        live.sessions[slug] = session
        return session

    live.get_or_start = AsyncMock(side_effect=start)
    live.async_stop = AsyncMock(side_effect=lambda slug: live.sessions.pop(slug))
    live.last_liveview_mp4 = AsyncMock(return_value=clip_file)
    body = {"created_at": dt_util.utcnow().isoformat(), "event_type": "pir", "camera_id": 100001}
    with patch("custom_components.blink_instant_events.vision.LOOK_SECONDS", 0):
        config_entry.runtime_data.listener._on_push({"data": json.dumps(body)}, "p1")
        await hass.async_block_till_done(wait_background_tasks=True)
        live.async_stop.assert_awaited_once_with("backyard")
        assert hass.states.get("event.backyard_blink_event").attributes["summary"] == "A vehicle"

        # Someone opened the camera during the look: their session stays up and isn't read.
        live.get_or_start = AsyncMock(
            side_effect=lambda slug, viewer: live.viewers.update({f"{slug}:app": 1}) or session
        )
        live.async_stop.reset_mock()
        config_entry.runtime_data.listener._on_push({"data": json.dumps(body | {"unique_id": "x2"})}, "p2")
        await hass.async_block_till_done(wait_background_tasks=True)
        live.async_stop.assert_not_awaited()
        assert len(vision.rows) == 1


async def test_people_api(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_fcm: MagicMock,
    mock_account: MagicMock,
    mock_blink: MagicMock,
    aioclient_mock: AiohttpClientMocker,
    hass_client,
) -> None:
    """Passed through to Blink Vision; 503 when it isn't running."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client()
    resp = await client.get("/api/blink_instant_events/people")
    assert resp.status == HTTPStatus.SERVICE_UNAVAILABLE

    aioclient_mock.get(f"{VISION}/health", json={"ok": True})
    aioclient_mock.get(f"{VISION}/people", json={"people": [{"name": "Rohan J", "photos": 2}]}, headers=JSON)
    aioclient_mock.post(f"{VISION}/people/Rohan%20J", json={"name": "Rohan J", "photos": 3}, headers=JSON)
    aioclient_mock.delete(f"{VISION}/people/Rohan%20J", json={"deleted": "Rohan J"}, headers=JSON)
    hass.config_entries.async_update_entry(config_entry, options={CONF_VISION_URL: VISION})
    config_entry.runtime_data.vision.url = None  # re-probed on the next request
    assert (await (await client.get("/api/blink_instant_events/people")).json())["people"][0]["photos"] == 2
    resp = await client.post("/api/blink_instant_events/people/Rohan%20J", data=b"\xff\xd8jpeg")
    assert (await resp.json())["photos"] == 3
    assert (await client.delete("/api/blink_instant_events/people/Rohan%20J")).status == HTTPStatus.OK


async def test_show_on_tv(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Casts the live view's HLS URL as a live stream; leaves a TV that is off alone."""
    play = async_mock_service(hass, "media_player", "play_media")
    async_mock_service(hass, "media_player", "media_stop")
    hass.states.async_set("media_player.tv", "on")
    await hass.services.async_call(
        "blink_instant_events",
        "show_on_tv",
        {"camera": "camera.front_door", "media_player": "media_player.tv"},
        blocking=True,
    )
    assert len(play) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert "/api/blink_instant_events/cameras/front_door/hls/index.m3u8?token=" in play[0].data["media_content_id"]
    assert play[0].data["extra"]["stream_type"] == "LIVE"

    hass.states.async_set("media_player.tv", "off")
    await hass.services.async_call(
        "blink_instant_events",
        "show_on_tv",
        {"camera": "camera.front_door", "media_player": "media_player.tv"},
        blocking=True,
    )
    assert len(play) == 1


@pytest.fixture(autouse=True)
def internal_url(hass: HomeAssistant) -> None:
    """show_on_tv needs a local URL for the TV to fetch."""
    hass.config.internal_url = "http://192.168.1.2:8123"
