"""How fast Blink Vision is on this machine: milliseconds per stage, so you know before buying an accelerator.

In the add-on:   docker exec -it addon_<repo>_blink_vision python /benchmark.py [photo.jpg] [runs]
Elsewhere:       MODELS=./models python benchmark.py photo.jpg 20

Without a photo it uses a synthetic 1280x720 picture: object detection costs the same, but there are no
faces to recognize, so the face stage only measures finding none. Use a real camera snapshot for both.
"""

from __future__ import annotations

import os
import platform
import statistics
import sys
import time

import cv2
import numpy as np
import server


def timed(runs: int, call) -> list[float]:
    call()  # warm-up: the first run allocates
    out = []
    for _ in range(runs):
        start = time.perf_counter()
        call()
        out.append((time.perf_counter() - start) * 1000)
    return out


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    image = cv2.imread(path) if path else np.random.default_rng(0).integers(0, 255, (720, 1280, 3), np.uint8)
    if image is None:
        sys.exit(f"Can't read {path}")
    server.load_models()
    threads = cv2.getNumThreads()
    print(f"{platform.machine()} {platform.processor() or ''}, {os.cpu_count()} CPUs, OpenCV {cv2.__version__}")
    print(f"picture {image.shape[1]}x{image.shape[0]}, {runs} runs, OpenCV threads {threads}")
    found = server.faces(image)
    print(f"faces found: {len(found)}, objects found: {len(server.objects(image))}")
    for label, call in (
        ("faces (YuNet + SFace)", lambda: server.faces(image)),
        ("objects (YOLOX-S 640)", lambda: server.objects(image)),
        ("one frame, both", lambda: (server.faces(image), server.objects(image))),
    ):
        times = timed(runs, call)
        print(f"{label:24} median {statistics.median(times):7.1f} ms   worst {max(times):7.1f} ms")
    print("A Blink clip or a local event reads up to 10 frames; multiply 'one frame' by the frames per detection.")


if __name__ == "__main__":
    main()
