"""Phase 3 end to end: a fake Blink IMMIS server -> real ffmpeg -> HLS, talk, controls, last live view.

Needs ffmpeg: the system one, else imageio-ffmpeg's; skipped without either.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import datetime
from http import HTTPStatus
from pathlib import Path
import shutil
import ssl
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

# A system ffmpeg first: imageio's static build segfaults resolving tcp:// on GitHub's runners.
FFMPEG = shutil.which("ffmpeg") or pytest.importorskip("imageio_ffmpeg").get_ffmpeg_exe()
BASE = "/api/blink_instant_events/cameras/backyard"


def _tls_context(directory: Path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "immis.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    return context


def _camera_ts(directory: Path) -> bytes:
    """Six seconds of H.264 + AAC MPEG-TS, like a camera sends."""
    out = directory / "camera.ts"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15"]
        + ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "6", "-c:v", "libx264", "-g", "15"]
        + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(out)],
        check=True,
    )
    return out.read_bytes()


class FakeImmis:
    """Accepts one live-view connection, streams TS in IMMI frames, records what the client sends."""

    def __init__(self, ts: bytes) -> None:
        self.ts = ts
        self.auth: bytes = b""
        self.frames: list[tuple[int, int, int]] = []  # (type, sequence, length) from the client

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.auth = await reader.readexactly(122)
        listen = asyncio.create_task(self._listen(reader))
        writer.write(bytes([0x15]) + (0).to_bytes(4, "big") + (0).to_bytes(4, "big"))  # lamp reports "off"
        try:
            for seq, start in enumerate(range(0, len(self.ts), 188 * 7)):
                chunk = self.ts[start : start + 188 * 7]
                writer.write(bytes([0]) + seq.to_bytes(4, "big") + len(chunk).to_bytes(4, "big") + chunk)
                await writer.drain()
                await asyncio.sleep(0.001)
        except ConnectionError:
            pass  # the client stopped the live view while we were still sending
        await listen  # stay connected until the client hangs up

    async def _listen(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                header = await reader.readexactly(9)
                length = int.from_bytes(header[5:9], "big")
                await reader.readexactly(length)
                self.frames.append((header[0], int.from_bytes(header[1:5], "big"), length))
        except (asyncio.IncompleteReadError, ConnectionError):
            return


@pytest.fixture
async def immis(hass: HomeAssistant, tmp_path: Path) -> AsyncGenerator[tuple[FakeImmis, int]]:
    """The fake server on a free port."""
    context = await hass.async_add_executor_job(_tls_context, tmp_path)
    fake = FakeImmis(await hass.async_add_executor_job(_camera_ts, tmp_path))
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0, ssl=context)
    yield fake, server.sockets[0].getsockname()[1]
    server.close()


async def test_live_view_end_to_end(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    mock_blink: MagicMock,
    immis: tuple[FakeImmis, int],
    hass_client,
    hass_client_no_auth,
) -> None:
    """Token, playlist starts the camera, segments, controls, talk, stop, last live view as MP4."""
    fake, port = immis
    live = setup_entry.runtime_data.live
    live.ffmpeg = FFMPEG
    answer = {
        "server": f"immis://127.0.0.1:{port}/CONNID123__IMDS_xyz?client_id=4242",
        "command_id": 77,
        "polling_interval": 60,
        "liveview_token": "lv-token",
    }
    running = {"status_code": 908, "commands": [{"id": 77, "state_condition": "running"}]}
    config = {"camera": [{"illuminator_enable": 2}], "signals": {"temp": 68}}
    with (
        patch("custom_components.blink_instant_events.liveview.blink_api.http_post", AsyncMock(return_value=answer)),
        patch("blinkpy.livestream.api.request_command_status", AsyncMock(return_value=running)),
        patch("blinkpy.livestream.api.request_command_done", AsyncMock()) as done,
        patch(
            "custom_components.blink_instant_events.controls.blink_api.request_camera_info",
            AsyncMock(return_value=config),
        ),
    ):
        authed = await hass_client()
        token = (await (await authed.post(f"{BASE}/token")).json())["token"]
        app = await hass_client_no_auth()
        assert (await app.get(f"{BASE}/hls/index.m3u8?token=wrong")).status == HTTPStatus.FORBIDDEN

        resp = await app.get(f"{BASE}/hls/index.m3u8?token={token}&session=s1")
        assert resp.status == HTTPStatus.OK, await resp.text()
        playlist = await resp.text()
        segment = next(line for line in playlist.splitlines() if line.startswith("segment_"))
        assert segment.endswith(f"?token={token}")
        assert b"lv-token" in fake.auth and b"CONNID123" in fake.auth and (4242).to_bytes(4, "big") in fake.auth
        resp = await app.get(f"{BASE}/hls/{segment}")
        assert resp.status == HTTPStatus.OK
        assert (await resp.read())[:1] == b"\x47"

        controls = await (await app.get(f"{BASE}/controls?token={token}&session=s1")).json()
        assert controls["night_vision"] == "auto"
        assert controls["capabilities"]["flood_light"] is False
        assert (controls["temperature"], controls["temperature_unit"]) == (20, "°C")

        ws = await app.ws_connect(f"{BASE}/ptt?token={token}&session=s1")
        await ws.send_json({"type": "start", "sampleRate": 48000})
        assert (await ws.receive_json(timeout=10))["type"] == "started"
        await ws.send_bytes(bytes(48000 * 2))  # one second of silence
        assert (await ws.receive_json(timeout=10))["type"] == "listening"
        await ws.send_json({"type": "stop"})
        assert (await ws.receive_json(timeout=10))["type"] == "stopped"
        await ws.close()
        assert (0x17, 3, 0) in fake.frames and (0x17, 4, 0) in fake.frames
        assert any(kind == 0x05 for kind, _seq, _len in fake.frames)

        assert (await (await app.post(f"{BASE}/stop?token={token}")).json())["stopped"] is True
        done.assert_awaited()  # Blink told the live view is over, freeing the camera

    info = await (await authed.get(f"{BASE}/last-liveview")).json()
    assert info["available"] is True and info["ended_at"]
    resp = await authed.get(f"{BASE}/last-liveview.mp4")
    assert resp.status == HTTPStatus.OK
    assert b"ftyp" in (await resp.read())[:16]


async def test_idle_session_is_reaped(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """A viewer that walked away frees the camera after IDLE_SECONDS; a watched one keeps it."""
    live = setup_entry.runtime_data.live
    idle, watched = MagicMock(running=True, last_touch=0.0), MagicMock(running=True, last_touch=1e12)
    idle.stop = AsyncMock()
    watched.stop = AsyncMock()
    live.sessions = {"backyard": idle, "front_door": watched}
    await live._reap(None)
    idle.stop.assert_awaited_once()
    watched.stop.assert_not_awaited()
    assert list(live.sessions) == ["front_door"]


async def test_tokens_are_per_camera(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """A token works for its own camera only."""
    live = setup_entry.runtime_data.live
    token = live.issue_token("backyard")
    assert live.token_ok(token, "backyard")
    assert not live.token_ok(token, "front_door")
    assert not live.token_ok("guess", "backyard")
