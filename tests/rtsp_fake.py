"""A fake RTSP camera on localhost: ffmpeg encodes a moving test picture to RTP, this serves it over RTSP.

ffmpeg can play an RTSP client but not an RTSP server for players, so the server half is here: OPTIONS,
DESCRIBE (ffmpeg's own SDP), SETUP with TCP interleaving, PLAY, then every RTP packet ffmpeg sends over UDP
goes to each playing client as an interleaved frame. Optional Basic auth answers 401 to a wrong login.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path


class FakeRtspCamera:
    """Start with `start()`, stop with `stop()`; `url` is where it serves."""

    def __init__(self, ffmpeg: str, directory: Path, login: tuple[str, str] | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.sdp_path = directory / "camera.sdp"
        self.login = login
        self.port = 0
        self.connections = 0
        self._playing: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._process: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return f"rtsp://127.0.0.1:{self.port}/live"

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        camera = self

        class Relay(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, _addr: object) -> None:
                frame = b"$\x00" + len(data).to_bytes(2, "big") + data
                for writer in list(camera._playing):
                    if writer.is_closing():
                        camera._playing.discard(writer)
                    else:
                        writer.write(frame)

        self._udp, _ = await loop.create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        udp_port = self._udp.get_extra_info("sockname")[1]
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            *("-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10"),
            *("-t", "120", "-c:v", "libx264", "-g", "10", "-tune", "zerolatency", "-pix_fmt", "yuv420p"),
            *("-sdp_file", str(self.sdp_path), "-f", "rtp", f"rtp://127.0.0.1:{udp_port}?pkt_size=1200"),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(100):
            if self.sdp_path.exists() and self.sdp_path.stat().st_size:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("ffmpeg wrote no SDP")

    async def stop(self) -> None:
        """The camera goes away: listening stops and every client is dropped."""
        if self._server:
            self._server.close()
        for writer in list(self._playing):
            writer.close()
        self._playing.clear()
        if self._udp:
            self._udp.close()
        if self._process and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()

    def _authorized(self, headers: dict[str, str]) -> bool:
        if self.login is None:
            return True
        expected = base64.b64encode(":".join(self.login).encode()).decode()
        return headers.get("authorization") == f"Basic {expected}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":  # the client's RTCP, interleaved: skip it
                    head = await reader.readexactly(3)
                    await reader.readexactly(int.from_bytes(head[1:3], "big"))
                    continue
                request = first + await reader.readuntil(b"\r\n\r\n")
                lines = request.decode().split("\r\n")
                method = lines[0].split(" ")[0]
                headers = {
                    k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)
                }
                reply = [f"CSeq: {headers.get('cseq', '0')}"]
                if not self._authorized(headers):
                    writer.write(self._answer("401 Unauthorized", reply + ['WWW-Authenticate: Basic realm="cam"']))
                    continue
                body = b""
                if method == "OPTIONS":
                    reply.append("Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER")
                elif method == "DESCRIBE":
                    body = self.sdp_path.read_bytes().rstrip(b"\r\n") + b"\r\na=control:trackID=0\r\n"
                    reply += ["Content-Type: application/sdp", f"Content-Base: {self.url}/"]
                elif method == "SETUP":
                    reply += ["Transport: RTP/AVP/TCP;unicast;interleaved=0-1", "Session: 1234"]
                elif method == "PLAY":
                    reply.append("Session: 1234")
                    writer.write(self._answer("200 OK", reply))
                    self._playing.add(writer)
                    continue
                elif method == "TEARDOWN":
                    writer.write(self._answer("200 OK", reply))
                    break
                writer.write(self._answer("200 OK", reply, body))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            self._playing.discard(writer)
            writer.close()

    @staticmethod
    def _answer(status: str, headers: list[str], body: bytes = b"") -> bytes:
        if body:
            headers = [*headers, f"Content-Length: {len(body)}"]
        return f"RTSP/1.0 {status}\r\n{''.join(h + chr(13) + chr(10) for h in headers)}\r\n".encode() + body
