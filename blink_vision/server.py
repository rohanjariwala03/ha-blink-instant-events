"""Camera Vision: who and what is in a picture, on your own hardware.

POST /analyze           JPEG body -> {"objects": [...], "faces": [...]}
GET  /people            -> {"people": [{"name", "photos"}]}
POST /people/<name>     JPEG body: learn this face (the largest one in the photo); ?force=1 past a look-alike
POST /people/<name>/not JPEG body: this face is not <name>; it stops matching them (their photos are kept)
DELETE /people/<name>   forget everyone's photos of <name>
GET  /people/<name>.jpg the face from the first photo
GET  /health            -> {"ok": true} (the only call without the token)

Every other call needs `Authorization: Bearer <token>`. The token comes from BLINK_VISION_TOKEN, else it is
made once and kept in /data/token; as an add-on it is handed to Home Assistant through Supervisor discovery.

Models from the OpenCV Model Zoo: YuNet finds faces, SFace tells them apart, YOLOX-S finds people,
vehicles and animals. As an add-on it is only reachable on Home Assistant's internal network.

Recognition is a likelihood: a face at least `same_person` similar to someone's photos is that person,
one between `uncertain_from` and `same_person` is only possibly them, anything less is unknown. A face at least
as similar to one of a person's "not them" faces as to their photos is not them.
"""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
import urllib.request

import cv2
import numpy as np

MODELS = Path(os.environ.get("MODELS", "/models"))
DATA = Path(os.environ.get("DATA", "/data"))
PEOPLE = DATA / "people"
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY = 10 * 1024 * 1024
MAX_PHOTOS = 20  # per person; more adds little and slows every match
NAME = re.compile(r"[^\W_][\w .'-]{0,39}")
SAME_PHOTO = 0.95  # a second photo this close to one already kept adds nothing

# Defaults; the add-on's options (/data/options.json) can change each one.
SETTINGS: dict[str, float] = {
    "face_score": 0.8,
    "min_face": 32,  # px; smaller faces can't be told apart reliably
    "min_enrol_face": 80,
    "min_enrol_score": 0.9,
    "min_sharpness": 60.0,  # variance of the Laplacian of the face crop; blurrier photos are refused
    # In detection, an unmatched face blurrier than this is "unclear", not "unknown": a blurred frame of
    # someone you know must not raise an unknown-person alert. Video frames are softer than photos.
    "min_detect_sharpness": 25.0,
    "same_person": 0.363,  # SFace cosine similarity threshold (OpenCV's recommendation)
    "uncertain_from": 0.30,
    "object_score": 0.45,
}
NMS = 0.5
SIZE = 640

COCO = (
    "person bicycle car motorcycle airplane bus train truck boat traffic_light fire_hydrant stop_sign "
    "parking_meter bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack umbrella handbag "
    "tie suitcase frisbee skis snowboard sports_ball kite baseball_bat baseball_glove skateboard surfboard "
    "tennis_racket bottle wine_glass cup fork knife spoon bowl banana apple sandwich orange broccoli carrot "
    "hot_dog pizza donut cake chair couch potted_plant bed dining_table toilet tv laptop mouse remote keyboard "
    "cell_phone microwave oven toaster sink refrigerator book clock vase scissors teddy_bear hair_drier toothbrush"
).split()
GROUPS = {
    "person": "person",
    **dict.fromkeys(("bicycle", "car", "motorcycle", "bus", "truck"), "vehicle"),
    **dict.fromkeys(("bird", "cat", "dog", "horse", "sheep", "cow", "bear"), "animal"),
}

# ponytail: one lock around the models (OpenCV nets aren't thread-safe); a pool if one hub ever needs more.
lock = threading.Lock()
detector: Any = None
recognizer: Any = None
yolox: Any = None
GRID = STRIDE = np.zeros((0, 2))
TOKEN = ""

known: dict[str, np.ndarray] = {}  # name -> (n, 128) unit vectors
not_them: dict[str, np.ndarray] = {}  # name -> faces someone said were not them


class Refused(ValueError):
    """A request that can't be done as asked: (HTTP status, message)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def load_settings() -> None:
    """The add-on's options over the defaults (numbers only; anything else is ignored)."""
    path = DATA / "options.json"
    if path.is_file():
        for key, value in json.loads(path.read_text()).items():
            if key in SETTINGS and isinstance(value, (int, float)) and not isinstance(value, bool):
                SETTINGS[key] = float(value)


def load_token() -> str:
    """BLINK_VISION_TOKEN, else the one kept in /data/token (made on first start, readable only by us)."""
    if token := os.environ.get("BLINK_VISION_TOKEN", "").strip():
        return token
    path = DATA / "token"
    if path.is_file():
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    path.touch(mode=0o600)
    path.write_text(token)
    print(f"Made a token for Home Assistant: {token}", flush=True)  # once; the Supervisor hands it over too
    return token


def announce() -> None:
    """As an add-on: tell Home Assistant where we are and the token (Supervisor discovery)."""
    supervisor = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor:
        return
    body = json.dumps(
        {"service": "blink_instant_events", "config": {"host": socket.gethostname(), "port": PORT, "token": TOKEN}}
    ).encode()
    request = urllib.request.Request(
        "http://supervisor/discovery",
        data=body,
        headers={"Authorization": f"Bearer {supervisor}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            print(f"Announced to Home Assistant ({resp.status})", flush=True)
    except OSError as err:
        print(f"Could not announce to Home Assistant: {err}; add Camera Vision there by hand", flush=True)


def load_models() -> None:
    """The three networks and YOLOX's anchor grid."""
    global detector, recognizer, yolox, GRID, STRIDE  # noqa: PLW0603
    detector = cv2.FaceDetectorYN.create(
        str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), SETTINGS["face_score"], 0.3, 5000
    )
    recognizer = cv2.FaceRecognizerSF.create(str(MODELS / "face_recognition_sface_2021dec.onnx"), "")
    yolox = cv2.dnn.readNet(str(MODELS / "object_detection_yolox_2022nov.onnx"))
    grids, strides = [], []
    for stride in (8, 16, 32):
        side = SIZE // stride
        xs, ys = np.meshgrid(np.arange(side), np.arange(side))
        grids.append(np.stack((xs, ys), 2).reshape(-1, 2))
        strides.append(np.full((side * side, 1), stride))
    GRID, STRIDE = np.concatenate(grids), np.concatenate(strides)


def load_people() -> None:
    """Read every stored face into memory."""
    found, wrong = {}, {}
    for folder in sorted(PEOPLE.glob("*")):
        feats = [np.load(f) for f in sorted(folder.glob("*.npy"))]
        if feats:
            found[folder.name] = np.vstack(feats)
        if negatives := [np.load(f) for f in sorted(folder.glob("not/*.npy"))]:
            wrong[folder.name] = np.vstack(negatives)
    known.clear()
    known.update(found)
    not_them.clear()
    not_them.update(wrong)


def decode(body: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise Refused(422, "Not an image OpenCV can read (send JPEG or PNG)")
    return image


def faces(image: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """(row, unit feature) for each face found, largest first. Call with the lock held."""
    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    _, rows = detector.detect(image)
    found = []
    for row in sorted(rows if rows is not None else [], key=lambda r: -r[2] * r[3]):
        if min(row[2], row[3]) < SETTINGS["min_face"]:
            continue
        feature = recognizer.feature(recognizer.alignCrop(image, row)).flatten()
        found.append((row, feature / np.linalg.norm(feature)))
    return found


def objects(image: np.ndarray) -> list[dict]:
    """People, vehicles and animals. Call with the lock held."""
    height, width = image.shape[:2]
    ratio = min(SIZE / height, SIZE / width)
    padded = np.full((SIZE, SIZE, 3), 114, np.float32)
    resized = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2RGB), (int(width * ratio), int(height * ratio)))
    padded[: resized.shape[0], : resized.shape[1]] = resized
    yolox.setInput(padded.transpose(2, 0, 1)[np.newaxis])
    out = yolox.forward()[0]
    centers = (out[:, :2] + GRID) * STRIDE / ratio
    sizes = np.exp(out[:, 2:4]) * STRIDE / ratio
    scores = out[:, 4:5] * out[:, 5:]
    classes, best = scores.argmax(1), scores.max(1)
    boxes = np.hstack((centers - sizes / 2, sizes))
    keep = cv2.dnn.NMSBoxesBatched(boxes.tolist(), best.tolist(), classes.tolist(), SETTINGS["object_score"], NMS)
    found = []
    for i in np.array(keep).flatten():
        label = COCO[classes[i]]
        if label in GROUPS:
            x, y, w, h = boxes[i]
            found.append(
                {"label": label, "group": GROUPS[label], "score": round(float(best[i]), 3), "box": box(x, y, w, h)}
            )
    return found


def box(x: float, y: float, w: float, h: float) -> list[int]:
    return [round(float(v)) for v in (x, y, w, h)]


def best_match(feature: np.ndarray, skip: str | None = None) -> tuple[str | None, float]:
    """The stored person this face is most like, and how much (cosine similarity)."""
    name, similarity = None, 0.0
    for person, feats in known.items():
        if person == skip:
            continue
        score = float((feats @ feature).max())
        if person in not_them and float((not_them[person] @ feature).max()) >= score:
            continue  # closer to a face someone said wasn't them
        if score > similarity:
            name, similarity = person, score
    return name, similarity


def identify(feature: np.ndarray) -> dict[str, Any]:
    """known (name), uncertain (candidate) or unknown, with the similarity behind it."""
    name, similarity = best_match(feature)
    if name and similarity >= SETTINGS["same_person"]:
        match = {"name": name, "match": "known", "candidate": None}
    elif name and similarity >= SETTINGS["uncertain_from"]:
        match = {"name": None, "match": "uncertain", "candidate": name}
    else:
        match = {"name": None, "match": "unknown", "candidate": None}
    return match | {"similarity": round(similarity, 3)}


def analyze(image: np.ndarray) -> dict:
    with lock:
        found_faces, found_objects = faces(image), objects(image)
    named = []
    for row, feature in found_faces:
        face = identify(feature) | {"score": round(float(row[-1]), 3), "box": box(*row[:4])}
        if face["match"] != "known" and sharpness(image, row) < SETTINGS["min_detect_sharpness"]:
            face |= {"match": "unclear", "candidate": None}
        named.append(face)
    height, width = image.shape[:2]
    return {"width": width, "height": height, "objects": found_objects, "faces": named}


def sharpness(image: np.ndarray, row: np.ndarray) -> float:
    """How sharp the face is: variance of the Laplacian of its grey crop."""
    x, y, w, h = (max(0, int(v)) for v in row[:4])
    crop = cv2.cvtColor(image[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(crop, cv2.CV_64F).var()) if crop.size else 0.0


def enrol(name: str, image: np.ndarray, force: bool = False) -> dict[str, Any]:
    """Keep this photo's face for `name`, if it is a good, unambiguous photo of someone not already known."""
    with lock:
        found = faces(image)
    if not found:
        raise Refused(422, "No face found in this photo")
    row, feature = found[0]
    if len(found) > 1 and found[1][0][2] * found[1][0][3] > 0.5 * row[2] * row[3]:
        raise Refused(422, "More than one face in this photo; use one with only this person")
    if min(row[2], row[3]) < SETTINGS["min_enrol_face"]:
        raise Refused(422, "The face is too small; use a closer photo")
    if float(row[-1]) < SETTINGS["min_enrol_score"]:
        raise Refused(422, "The face isn't clear enough; use a photo facing the camera")
    if sharpness(image, row) < SETTINGS["min_sharpness"]:
        raise Refused(422, "The photo is too blurry")
    other, similarity = best_match(feature, skip=name)
    if other and similarity >= SETTINGS["same_person"] and not force:
        raise Refused(409, f"This face already matches {other}; send it again with ?force=1 if it is {name}")
    stored = known.get(name)
    if stored is not None and float((stored @ feature).max()) >= SAME_PHOTO:
        return {"name": name, "photos": len(stored), "duplicate": True}
    if stored is not None and len(stored) >= MAX_PHOTOS:
        raise Refused(409, f"{name} has {MAX_PHOTOS} photos already; forget them and start again to replace")
    folder = PEOPLE / name
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    np.save(folder / f"{time.time_ns()}.npy", feature)
    if not (folder / "face.jpg").exists():
        x, y, w, h = (int(v) for v in row[:4])
        pad = int(max(w, h) * 0.3)
        crop = image[max(0, y - pad) : y + h + pad, max(0, x - pad) : x + w + pad]
        cv2.imwrite(str(folder / "face.jpg"), cv2.resize(crop, (256, 256 * crop.shape[0] // crop.shape[1])))
    load_people()
    return {"name": name, "photos": len(known[name]), "duplicate": False}


def not_this_person(name: str, image: np.ndarray) -> dict[str, Any]:
    """Remember this face (the largest in the picture) as not `name`, so it stops matching them."""
    if name not in known:
        raise Refused(404, f"No such person: {name}")
    with lock:
        found = faces(image)
    if not found:
        raise Refused(422, "No face found in this picture")
    feature = found[0][1]
    stored = not_them.get(name)
    if stored is not None and float((stored @ feature).max()) >= SAME_PHOTO:
        return {"name": name, "not_them": len(stored), "duplicate": True}
    if stored is not None and len(stored) >= MAX_PHOTOS:
        raise Refused(409, f"{name} has {MAX_PHOTOS} faces marked as not them; forget them and start again")
    folder = PEOPLE / name / "not"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    np.save(folder / f"{time.time_ns()}.npy", feature)
    load_people()
    return {"name": name, "not_them": len(not_them[name]), "duplicate": False}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict | bytes, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        """The token, compared in constant time; 401 and False without it."""
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if TOKEN and hmac.compare_digest(given.encode(), TOKEN.encode()):
            return True
        self._send(401, {"error": "Missing or wrong token"})
        return False

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            raise Refused(413, "Send one image, at most 10 MB")
        return self.rfile.read(length)

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _name(self, suffix: str = "") -> str | None:
        """The <name> in /people/<name><suffix>[.jpg], if it's a safe folder name."""
        path = self._path().removeprefix("/people/")
        if suffix:
            if not path.endswith(suffix):
                return None
            path = path.removesuffix(suffix)
        raw = unquote(path).removesuffix(".jpg").strip()
        return raw if NAME.fullmatch(raw) else None

    def do_GET(self) -> None:
        path = self._path()
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if not self._authorized():
            return
        if path == "/people":
            self._send(200, {"people": [{"name": n, "photos": len(f)} for n, f in sorted(known.items())]})
        elif path.startswith("/people/") and path.endswith(".jpg") and (name := self._name()):
            photo = PEOPLE / name / "face.jpg"
            if photo.is_file():
                self._send(200, photo.read_bytes(), "image/jpeg")
            else:
                self._send(404, {"error": "No photo"})
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        path = self._path()
        try:
            if path == "/analyze":
                self._send(200, analyze(decode(self._body())))
            elif path.startswith("/people/") and (name := self._name("/not")):
                self._send(200, not_this_person(name, decode(self._body())))
            elif path.startswith("/people/") and (name := self._name()):
                force = parse_qs(urlsplit(self.path).query).get("force") == ["1"]
                self._send(200, enrol(name, decode(self._body()), force))
            else:
                self._send(404, {"error": "Not found"})
        except Refused as err:
            self._send(err.status, {"error": str(err)})

    def do_DELETE(self) -> None:
        if not self._authorized():
            return
        name = self._name() if self._path().startswith("/people/") else None
        if not name or not (PEOPLE / name).is_dir():
            self._send(404, {"error": "No such person"})
            return
        with lock:
            shutil.rmtree(PEOPLE / name)
            load_people()
        self._send(200, {"deleted": name})

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet: Home Assistant asks after every motion."""


def main() -> None:
    global TOKEN  # noqa: PLW0603
    DATA.mkdir(parents=True, exist_ok=True)
    PEOPLE.mkdir(mode=0o700, parents=True, exist_ok=True)
    load_settings()
    TOKEN = load_token()
    load_models()
    load_people()
    print(f"Camera Vision listening on :{PORT}, knows {len(known)} people", flush=True)
    announce()
    # As PID 1, Python ignores SIGTERM; without this, Docker kills the add-on and the Supervisor shows an error.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104 - the internal network only


if __name__ == "__main__":
    main()
