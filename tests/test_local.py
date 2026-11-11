"""Local cameras with no Blink account, no Firebase, no cloud: a synthetic RTSP camera end to end.

ffmpeg plays the camera (an RTSP server in listen mode streaming a moving test picture). The only HTTP the
integration may make is to Blink Vision, mocked here; Blink and FCM fail the test if anything touches them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
import shutil
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.blink_instant_events.const import DOMAIN
from custom_components.blink_instant_events.providers.rtsp.stream import StreamOptions, ffmpeg_args
from custom_components.blink_instant_events.providers.rtsp.url import InvalidUrl, parse, redact, with_credentials
from homeassistant.components.camera import async_get_image
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .rtsp_fake import FakeRtspCamera

FFMPEG = shutil.which("ffmpeg") or pytest.importorskip("imageio_ffmpeg").get_ffmpeg_exe()
VISION = "http://vision.test:8000"
PKG = "custom_components.blink_instant_events"


# ------------------------------------------------------------------ addresses


@pytest.mark.parametrize(
    "url",
    [
        "http://cam/stream",
        "file:///etc/passwd",
        "concat:rtsp://a|file:/etc/passwd",
        "rtsp://",
        "rtsp://-i/x",
        "rtsp://cam:99999/x",
        "rtsp://cam/x y",
        "rtsp://cam/x\ny",
    ],
)
def test_rejected_addresses(url: str) -> None:
    """Only rtsp(s) with a real host; nothing ffmpeg could read as another protocol or an option."""
    with pytest.raises(InvalidUrl):
        parse(url)


def test_credentials_are_split_joined_and_scrubbed() -> None:
    """A login in the address moves out of it, goes back in encoded, and never reaches a log line."""
    url, user, password = parse("RTSP://admin:p%40ss:w0rd@192.168.1.20:554/stream2?ch=1")
    assert (url, user, password) == ("rtsp://192.168.1.20:554/stream2?ch=1", "admin", "p@ss:w0rd")
    full = with_credentials(url, user, password)
    assert full == "rtsp://admin:p%40ss%3Aw0rd@192.168.1.20:554/stream2?ch=1"
    line = f"[rtsp] {full}: 401 Unauthorized (p@ss:w0rd)"
    assert "w0rd" not in redact(line, password) and "rtsp://***@192.168.1.20" in redact(line, password)
    assert parse("rtsp://[fe80::1]:8554/live")[0] == "rtsp://[fe80::1]:8554/live"


def test_ffmpeg_only_opens_rtsp() -> None:
    """The protocol whitelist is on every reader, and recording copies video without re-encoding."""
    args = ffmpeg_args("rtsp://cam/x", StreamOptions(keyframes_only=True))
    assert args[args.index("-protocol_whitelist") + 1] == "rtsp,rtsps,rtp,srtp,udp,tcp,tls"
    assert args.index("-skip_frame") < args.index("-i")
    assert "segment" not in args


# ------------------------------------------------------------------ end to end


@pytest.fixture
async def rtsp_camera(hass: HomeAssistant, socket_enabled: None, tmp_path: Path) -> AsyncGenerator[FakeRtspCamera]:
    """A moving test picture served over RTSP on localhost."""
    camera = FakeRtspCamera(FFMPEG, tmp_path)
    await camera.start()
    yield camera
    await camera.stop()


@pytest.fixture
def no_cloud() -> AsyncGenerator[None]:
    """Blink, its push client and its REST calls fail the test if they are ever created."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a local camera touched Blink or Firebase")

    with (
        patch(f"{PKG}.providers.blink.Blink", side_effect=forbidden),
        patch(f"{PKG}.providers.blink.listener._FcmPushClient", side_effect=forbidden),
        patch(f"{PKG}.providers.blink.api.BlinkAccount", side_effect=forbidden),
        patch(f"{PKG}.providers.blink.api.PushAuth", side_effect=forbidden),
    ):
        yield


async def _until(check: Callable[[], bool], seconds: float = 30) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.2)


async def test_rtsp_camera_without_blink(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, rtsp_camera: FakeRtspCamera, no_cloud: None
) -> None:
    """Stream → motion → Blink Vision → person/known sensors and event → saved clip; nothing else online."""
    aioclient_mock.get(f"{VISION}/health", json={"ok": True})
    aioclient_mock.post(
        f"{VISION}/analyze",
        json={
            "faces": [{"name": "Rohan", "match": "known", "similarity": 0.71, "box": [40, 40, 50, 50]}],
            "objects": [{"label": "person", "group": "person", "score": 0.9, "box": [20, 20, 120, 200]}],
        },
    )
    vision = MockConfigEntry(domain=DOMAIN, data={"provider": "vision", "vision_url": VISION, "vision_token": "t0k"})
    camera = MockConfigEntry(
        domain=DOMAIN,
        title="Porch",
        unique_id=rtsp_camera.url,
        data={"provider": "rtsp", "url": rtsp_camera.url},
        options={"fps": 5, "sensitivity": 0.5, "cooldown": 0, "record": "events", "pre": 1, "post": 1},
    )
    with (
        patch(f"{PKG}.providers.rtsp.get_ffmpeg_manager", return_value=MagicMock(binary=FFMPEG)),
        patch(f"{PKG}.providers.rtsp.stream.BACKOFF", (0.5,)),  # the server needs a moment to listen
        patch(f"{PKG}.providers.local.watcher.COLLECT_SECONDS", 0.5),
    ):
        for entry in (vision, camera):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await _until(lambda: hass.states.get("binary_sensor.porch_stream").state == "on")
        picture = await async_get_image(hass, "camera.porch")
        assert picture.content.startswith(b"\xff\xd8")

        await _until(lambda: hass.states.get("binary_sensor.porch_known_person").state == "on")
        assert hass.states.get("binary_sensor.porch_person").state == "on"
        assert hass.states.get("binary_sensor.porch_unknown_person").state == "off"
        last = hass.states.get("sensor.porch_last_detection")
        assert last.state == "Rohan" and last.attributes["identity"] == "known"
        assert last.attributes["location"] == "local" and last.attributes["provider"] == "rtsp"
        assert hass.states.get("image.porch_last_detection").state != "unavailable"
        analyze = next(call for call in aioclient_mock.mock_calls if str(call[1]).endswith("/analyze"))
        assert analyze[3]["Authorization"] == "Bearer t0k"

        await _until(lambda: hass.states.get("event.porch_event").attributes.get("event_type") == "recording")
        clip = hass.states.get("event.porch_event").attributes["recording"]
        assert clip.startswith("media-source://media_source/local/blink_instant_events/porch/")

        assert {call[1].host for call in aioclient_mock.mock_calls} == {"vision.test"}
        reader = camera.runtime_data.reader
        assert await hass.config_entries.async_unload(camera.entry_id)
        assert camera.state is ConfigEntryState.NOT_LOADED
        assert reader._process.returncode is not None  # ffmpeg was stopped, not left behind


async def test_reader_reconnects(hass: HomeAssistant, rtsp_camera: FakeRtspCamera) -> None:
    """A camera that goes away comes back on its own: ffmpeg is started again with backoff."""
    from custom_components.blink_instant_events.providers.rtsp.stream import RtspReader  # noqa: PLC0415

    frames: list[bytes] = []

    async def on_frame(jpeg: bytes) -> None:
        frames.append(jpeg)

    reader = RtspReader(FFMPEG, "rtsp://127.0.0.1:1/none", None, StreamOptions(fps=5), on_frame, lambda: None, "t")
    with patch(f"{PKG}.providers.rtsp.stream.BACKOFF", (0.3,)):
        task = asyncio.create_task(reader.run())
        await _until(lambda: reader.restarts >= 2, 20)  # nothing listens on port 1
        assert not reader.connected and reader.last_error
        reader._url = rtsp_camera.url  # the camera is back (at its address)
        await _until(lambda: reader.connected, 30)
        assert frames and frames[-1].startswith(b"\xff\xd8")
        await reader.stop()
        await asyncio.wait_for(task, 10)


async def test_rtsp_config_flow(hass: HomeAssistant) -> None:
    """Address checked, login moved out of it, one frame read; wrong login says so."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "rtsp"})
    form = {"name": "Porch", "url": "file:///etc/passwd"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["errors"] == {"url": "not_rtsp"}

    probe = f"{PKG}.config_flow.async_probe_rtsp"
    with patch(probe, return_value="invalid_auth") as checked:
        form = {"name": "Porch", "url": "rtsp://admin:bad@192.168.1.20/stream2"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["errors"] == {"base": "invalid_auth"}
    assert checked.call_args.args[1] == "rtsp://admin:bad@192.168.1.20/stream2"

    with (
        patch(probe, return_value=None),
        patch(f"{PKG}.providers.rtsp.async_setup_entry", return_value=True),
    ):
        form = {"name": "Porch", "url": "rtsp://192.168.1.20/stream2", "username": "admin", "password": "good"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "provider": "rtsp",
        "url": "rtsp://192.168.1.20/stream2",
        "username": "admin",
        "password": "good",
    }


async def test_probe_tells_a_wrong_login(hass: HomeAssistant, socket_enabled: None, tmp_path: Path) -> None:
    """The config flow's one-frame check: right login reads, wrong login is invalid_auth, nobody is cannot_connect."""
    from custom_components.blink_instant_events.providers.rtsp.stream import async_probe  # noqa: PLC0415

    camera = FakeRtspCamera(FFMPEG, tmp_path, login=("admin", "s3cret"))
    await camera.start()
    try:
        assert await async_probe(FFMPEG, with_credentials(camera.url, "admin", "s3cret"), "s3cret") is None
        assert await async_probe(FFMPEG, with_credentials(camera.url, "admin", "nope"), "nope") == "invalid_auth"
    finally:
        await camera.stop()
    assert await async_probe(FFMPEG, camera.url, None, timeout=5) == "cannot_connect"


async def test_home_assistant_camera_without_blink(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_cloud: None
) -> None:
    """A camera from another integration (how ONVIF cameras come in): its motion sensor starts a detection."""
    from homeassistant.components.camera import Image  # noqa: PLC0415

    aioclient_mock.get(f"{VISION}/health", json={"ok": True})
    aioclient_mock.post(
        f"{VISION}/analyze",
        json={"faces": [{"name": None, "match": "unknown", "similarity": 0.1, "box": [1, 1, 9, 9]}], "objects": []},
    )
    picture = Image("image/jpeg", (await hass.async_add_executor_job(_jpeg)))
    hass.states.async_set("binary_sensor.door_motion", "off")
    vision = MockConfigEntry(domain=DOMAIN, data={"provider": "vision", "vision_url": VISION})
    door = MockConfigEntry(
        domain=DOMAIN,
        title="Door",
        data={"provider": "camera", "camera": "camera.door", "motion": "binary_sensor.door_motion"},
        options={"frames": 2, "cooldown": 0},
    )
    with patch(f"{PKG}.providers.camera.async_get_image", return_value=picture) as grab:
        for entry in (vision, door):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.door_unknown_person").state == "off"

        hass.states.async_set("binary_sensor.door_motion", "on")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert grab.call_count == 2
    assert hass.states.get("event.door_event").attributes["event_type"] == "detection"
    assert hass.states.get("binary_sensor.door_unknown_person").state == "on"
    assert hass.states.get("sensor.door_last_detection").state == "An unknown person"
    assert {call[1].host for call in aioclient_mock.mock_calls} == {"vision.test"}


def _jpeg() -> bytes:
    import io  # noqa: PLC0415

    from PIL import Image as PILImage  # noqa: PLC0415

    out = io.BytesIO()
    PILImage.new("RGB", (64, 48), (90, 120, 200)).save(out, "JPEG")
    return out.getvalue()


async def test_addon_discovery(hass: HomeAssistant) -> None:
    """The add-on announces itself with its token; confirming makes the Vision entry; a re-announce updates it."""
    from homeassistant.config_entries import SOURCE_HASSIO  # noqa: PLC0415
    from homeassistant.helpers.service_info.hassio import HassioServiceInfo  # noqa: PLC0415

    def info(token: str) -> HassioServiceInfo:
        config = {"host": "local-blink-vision", "port": 8000, "token": token}
        return HassioServiceInfo(config=config, name="Blink Vision", slug="local_blink_vision", uuid="abc")

    with (
        patch(f"{PKG}.providers.vision.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=info("t1"))
        assert result["step_id"] == "hassio_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            "provider": "vision",
            "vision_url": "http://local-blink-vision:8000",
            "vision_token": "t1",
        }
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=info("t2"))
        await hass.async_block_till_done()  # the update reloads the entry
    assert result["type"] is FlowResultType.ABORT
    assert hass.config_entries.async_entries(DOMAIN)[0].data["vision_token"] == "t2"


async def test_face_and_snapshot_actions(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_read_only_user, tmp_path: Path, no_cloud: None
) -> None:
    """Teach from a camera or an allowed file (admins only), refuse other files, analyze any camera now."""
    from homeassistant.components.camera import Image  # noqa: PLC0415
    from homeassistant.core import Context  # noqa: PLC0415
    from homeassistant.exceptions import ServiceValidationError, Unauthorized  # noqa: PLC0415
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    aioclient_mock.get(f"{VISION}/health", json={"ok": True})
    aioclient_mock.post(f"{VISION}/people/Rohan%20J", json={"name": "Rohan J", "photos": 1})
    aioclient_mock.post(
        f"{VISION}/analyze",
        json={"faces": [], "objects": [{"label": "car", "group": "vehicle", "score": 0.8, "box": [1, 1, 9, 9]}]},
    )
    vision = MockConfigEntry(domain=DOMAIN, data={"provider": "vision", "vision_url": VISION})
    vision.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    picture = Image("image/jpeg", await hass.async_add_executor_job(_jpeg))
    allowed = tmp_path / "faces"
    allowed.mkdir()
    (allowed / "rohan.jpg").write_bytes(picture.content)
    hass.config.allowlist_external_dirs = {str(allowed)}

    with patch(f"{PKG}.ai.services.async_get_image", return_value=picture):
        answer = await hass.services.async_call(
            DOMAIN, "enroll_face", {"name": "Rohan J", "camera": "camera.door"}, blocking=True, return_response=True
        )
        assert answer == {"name": "Rohan J", "photos": 1}
        answer = await hass.services.async_call(
            DOMAIN,
            "enroll_face",
            {"name": "Rohan J", "file": str(allowed / "rohan.jpg"), "force": True},
            blocking=True,
            return_response=True,
        )
        assert aioclient_mock.mock_calls[-1][1].query == {"force": "1"}
        with pytest.raises(ServiceValidationError, match="not in a folder"):
            await hass.services.async_call(
                DOMAIN, "enroll_face", {"name": "x", "file": "/etc/passwd"}, blocking=True, return_response=True
            )
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                "delete_face",
                {"name": "Rohan J"},
                blocking=True,
                context=Context(user_id=hass_read_only_user.id),
            )
        found = await hass.services.async_call(
            DOMAIN, "process_snapshot", {"camera": "camera.door"}, blocking=True, return_response=True
        )
    assert found["summary"] == "A vehicle" and found["labels"] == ["vehicle"]
