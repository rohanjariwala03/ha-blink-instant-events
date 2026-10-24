"""Blink Vision: who and what is in a picture, on your own hardware.

POST /analyze           JPEG body -> {"objects": [...], "faces": [...]}
GET  /people            -> {"people": [{"name", "photos"}]}
POST /people/<name>     JPEG body: learn this face (the largest one in the photo)
DELETE /people/<name>   forget everyone's photos of <name>
GET  /people/<name>.jpg the face from the first photo
GET  /health

Models from the OpenCV Model Zoo: YuNet finds faces, SFace tells them apart, YOLOX-S finds people,
vehicles and animals. Only reachable on Home Assistant's internal network (no ports are published).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
import time
from urllib.parse import unquote

import cv2
import numpy as np

MODELS = Path(os.environ.get("MODELS", "/models"))
PEOPLE = Path(os.environ.get("DATA", "/data")) / "people"
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY = 10 * 1024 * 1024
NAME = re.compile(r"[^\W_][\w .'-]{0,39}")

FACE_SCORE = 0.8
MIN_FACE = 32  # px; smaller faces can't be told apart reliably
MIN_ENROL_FACE = 60
SAME_PERSON = 0.363  # SFace cosine similarity threshold (OpenCV's recommendation)
OBJECT_SCORE = 0.45
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
detector = cv2.FaceDetectorYN.create(
    str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), FACE_SCORE, 0.3, 5000
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

known: dict[str, np.ndarray] = {}  # name -> (n, 128) unit vectors


def load_people() -> None:
    """Read every stored face into memory."""
    found = {}
    for folder in sorted(PEOPLE.glob("*")):
        feats = [np.load(f) for f in sorted(folder.glob("*.npy"))]
        if feats:
            found[folder.name] = np.vstack(feats)
    known.clear()
    known.update(found)


def decode(body: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Not an image OpenCV can read (send JPEG or PNG)")
    return image


def faces(image: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """(row, unit feature) for each face found, largest first. Call with the lock held."""
    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    _, rows = detector.detect(image)
    found = []
    for row in sorted(rows if rows is not None else [], key=lambda r: -r[2] * r[3]):
        if min(row[2], row[3]) < MIN_FACE:
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
    keep = cv2.dnn.NMSBoxesBatched(boxes.tolist(), best.tolist(), classes.tolist(), OBJECT_SCORE, NMS)
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


def analyze(image: np.ndarray) -> dict:
    with lock:
        found_faces, found_objects = faces(image), objects(image)
    named = []
    for row, feature in found_faces:
        name, similarity = None, 0.0
        for person, feats in known.items():
            score = float((feats @ feature).max())
            if score > similarity:
                name, similarity = person, score
        named.append(
            {
                "name": name if similarity >= SAME_PERSON else None,
                "similarity": round(similarity, 3),
                "score": round(float(row[-1]), 3),
                "box": box(*row[:4]),
            }
        )
    height, width = image.shape[:2]
    return {"width": width, "height": height, "objects": found_objects, "faces": named}


def enrol(name: str, image: np.ndarray) -> int:
    with lock:
        found = faces(image)
    if not found:
        raise ValueError("No face found in this photo")
    row, feature = found[0]
    if min(row[2], row[3]) < MIN_ENROL_FACE:
        raise ValueError("The face is too small; use a closer photo")
    folder = PEOPLE / name
    folder.mkdir(parents=True, exist_ok=True)
    np.save(folder / f"{time.time_ns()}.npy", feature)
    if not (folder / "face.jpg").exists():
        x, y, w, h = (int(v) for v in row[:4])
        pad = int(max(w, h) * 0.3)
        crop = image[max(0, y - pad) : y + h + pad, max(0, x - pad) : x + w + pad]
        cv2.imwrite(str(folder / "face.jpg"), cv2.resize(crop, (256, 256 * crop.shape[0] // crop.shape[1])))
    load_people()
    return len(known[name])


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict | bytes, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            raise ValueError("Send one image, at most 10 MB")
        return self.rfile.read(length)

    def _name(self) -> str | None:
        """The <name> in /people/<name>[.jpg], if it's a safe folder name."""
        raw = unquote(self.path.split("?")[0].removeprefix("/people/")).removesuffix(".jpg").strip()
        return raw if NAME.fullmatch(raw) else None

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, {"ok": True, "people": len(known)})
        elif path == "/people":
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
        path = self.path.split("?")[0]
        try:
            if path == "/analyze":
                self._send(200, analyze(decode(self._body())))
            elif path.startswith("/people/") and (name := self._name()):
                self._send(200, {"name": name, "photos": enrol(name, decode(self._body()))})
            else:
                self._send(404, {"error": "Not found"})
        except ValueError as err:
            self._send(422, {"error": str(err)})

    def do_DELETE(self) -> None:
        name = self._name() if self.path.startswith("/people/") else None
        if not name or not (PEOPLE / name).is_dir():
            self._send(404, {"error": "No such person"})
            return
        shutil.rmtree(PEOPLE / name)
        load_people()
        self._send(200, {"deleted": name})

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet: Home Assistant asks after every motion."""


if __name__ == "__main__":
    PEOPLE.mkdir(parents=True, exist_ok=True)
    load_people()
    print(f"Blink Vision listening on :{PORT}, knows {len(known)} people", flush=True)
    # As PID 1, Python ignores SIGTERM; without this, Docker kills the add-on and the Supervisor shows an error.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
