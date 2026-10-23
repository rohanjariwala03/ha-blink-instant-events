"""Download the models at build time and check them against pinned hashes."""

import hashlib
from pathlib import Path
import sys
from urllib.request import urlopen

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
MODELS = {
    "face_detection_yunet/face_detection_yunet_2023mar.onnx": (
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
    ),
    "face_recognition_sface/face_recognition_sface_2021dec.onnx": (
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
    ),
    "object_detection_yolox/object_detection_yolox_2022nov.onnx": (
        "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
    ),
}

target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
for path, digest in MODELS.items():
    data = urlopen(ZOO + path, timeout=120).read()
    if hashlib.sha256(data).hexdigest() != digest:
        sys.exit(f"{path}: unexpected sha256")
    (target / Path(path).name).write_bytes(data)
