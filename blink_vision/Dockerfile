FROM python:3.13-slim-bookworm

RUN pip install --no-cache-dir opencv-python-headless==4.12.0.88 numpy==2.2.6

# OpenCV Model Zoo: YuNet (MIT), SFace (Apache-2.0), YOLOX-S (Apache-2.0). Pinned by hash.
COPY fetch_models.py /
RUN python /fetch_models.py /models && rm /fetch_models.py

COPY server.py /
ENV MODELS=/models DATA=/data PYTHONUNBUFFERED=1
CMD ["python", "/server.py"]
