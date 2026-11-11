"""Camera Vision without its models: tokens, the known/uncertain/unknown bands, enrolment checks, the HTTP API.

The face detector is stood in for (`faces` returns rows and features the test chooses); OpenCV still does
the decoding and the sharpness check. Run with the add-on's requirements: pytest blink_vision.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import stat
import threading
from typing import Any

import cv2
import numpy as np
import pytest
import server


def unit(*values: float) -> np.ndarray:
    vector = np.zeros(128)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


ROHAN = unit(1, 0)
CLOSE_TO_ROHAN = unit(1, 0.9)  # cosine ~0.74 with ROHAN
HALF_LIKE_ROHAN = unit(1, 3.0)  # cosine ~0.32: the uncertain band
STRANGER = unit(0, 1)


def row(size: int = 120, score: float = 0.95, x: int = 10) -> np.ndarray:
    return np.array([x, 10, size, size, *([0] * 10), score], dtype=np.float32)


def photo(sharp: bool = True) -> np.ndarray:
    """A 300x300 picture: noise is sharp, a flat grey is not."""
    rng = np.random.default_rng(1)
    return rng.integers(0, 255, (300, 300, 3), dtype=np.uint8) if sharp else np.full((300, 300, 3), 128, np.uint8)


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty face store and the default settings for every test."""
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "PEOPLE", tmp_path / "people")
    monkeypatch.setattr(server, "SETTINGS", dict(server.SETTINGS))
    server.known.clear()
    return tmp_path


def use_faces(monkeypatch: pytest.MonkeyPatch, *found: tuple[np.ndarray, np.ndarray]) -> None:
    monkeypatch.setattr(server, "faces", lambda image: list(found))


def test_bands() -> None:
    """Known above same_person, uncertain (with a candidate) above uncertain_from, else unknown."""
    server.known["Rohan"] = ROHAN[np.newaxis]
    assert server.identify(CLOSE_TO_ROHAN) | {"similarity": 0} == {
        "name": "Rohan",
        "match": "known",
        "candidate": None,
        "similarity": 0,
    }
    maybe = server.identify(HALF_LIKE_ROHAN)
    assert (maybe["name"], maybe["match"], maybe["candidate"]) == (None, "uncertain", "Rohan")
    assert server.identify(STRANGER)["match"] == "unknown"


def test_enrolment_quality(monkeypatch: pytest.MonkeyPatch) -> None:
    """No face, two faces, small, unclear or blurry photos are refused with a reason; nothing is stored."""
    for found, image, reason in [
        ((), photo(), "No face"),
        (((row(), ROHAN), (row(110, x=150), STRANGER)), photo(), "More than one face"),
        (((row(40), ROHAN),), photo(), "too small"),
        (((row(score=0.7), ROHAN),), photo(), "isn't clear"),
        (((row(), ROHAN),), photo(sharp=False), "blurry"),
    ]:
        use_faces(monkeypatch, *found)
        with pytest.raises(server.Refused, match=reason):
            server.enrol("Rohan", image)
    assert not server.known and not (server.PEOPLE / "Rohan").exists()


def test_enrolment_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second photo adds; the same photo again doesn't; someone else's face needs force."""
    use_faces(monkeypatch, (row(), ROHAN))
    assert server.enrol("Rohan", photo()) == {"name": "Rohan", "photos": 1, "duplicate": False}
    assert server.enrol("Rohan", photo())["duplicate"] is True
    use_faces(monkeypatch, (row(), CLOSE_TO_ROHAN))
    assert server.enrol("Rohan", photo())["photos"] == 2
    with pytest.raises(server.Refused, match="already matches Rohan") as refused:
        server.enrol("Priya", photo())
    assert refused.value.status == 409
    assert server.enrol("Priya", photo(), force=True)["photos"] == 1
    assert stat.S_IMODE((server.PEOPLE / "Priya").stat().st_mode) == 0o700


def test_token_and_settings(store: Path) -> None:
    """The token is made once, kept private and reused; options override numbers only."""
    first = server.load_token()
    assert len(first) >= 40 and server.load_token() == first
    assert stat.S_IMODE((store / "token").stat().st_mode) == 0o600
    (store / "options.json").write_text(json.dumps({"same_person": 0.42, "uncertain_from": True, "other": 1}))
    server.load_settings()
    assert server.SETTINGS["same_person"] == 0.42 and server.SETTINGS["uncertain_from"] == 0.30


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The HTTP server on a free port, token "t0k"."""
    monkeypatch.setattr(server, "TOKEN", "t0k")
    monkeypatch.setattr(server, "objects", lambda image: [{"label": "dog", "group": "animal", "score": 0.8}])
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method: str, path: str, body: bytes | None = None, token: str | None = "t0k") -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers={"Authorization": f"Bearer {token}"} if token else {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, json.loads(data) if resp.getheader("Content-Type") == "application/json" else data

    yield call
    httpd.shutdown()


def test_http_api(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Health is open; everything else needs the token; analyze, enrol, list, picture, forget."""
    jpeg = cv2.imencode(".jpg", photo())[1].tobytes()
    assert api("GET", "/health", token=None) == (200, {"ok": True})
    assert api("GET", "/people", token=None)[0] == 401
    assert api("GET", "/people", token="wrong")[0] == 401
    assert api("POST", "/analyze", jpeg, token=None)[0] == 401

    use_faces(monkeypatch, (row(), ROHAN))
    assert api("POST", "/people/Rohan%20J", jpeg) == (200, {"name": "Rohan J", "photos": 1, "duplicate": False})
    status, answer = api("POST", "/analyze", jpeg)
    assert status == 200 and answer["faces"][0]["match"] == "known" and answer["objects"][0]["label"] == "dog"
    assert api("GET", "/people") == (200, {"people": [{"name": "Rohan J", "photos": 1}]})
    status, picture = api("GET", "/people/Rohan%20J.jpg")
    assert status == 200 and picture[:2] == b"\xff\xd8"
    assert api("POST", "/analyze", b"not a picture")[0] == 422
    assert api("POST", "/people/..%2Fescape", jpeg)[0] == 404
    assert api("DELETE", "/people/Rohan%20J") == (200, {"deleted": "Rohan J"})
    assert api("GET", "/people") == (200, {"people": []})
