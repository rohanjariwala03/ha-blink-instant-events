"""Per camera: the latest Blink thumbnail, and the live-view entity the app finds by `proxy_slug`.

Snapshot id and capture time adapted from Blink Live View Proxy's proxy/blink_proxy/devices.py,
Copyright (c) 2026 Teethree89, MIT License (see NOTICE).
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

from blinkpy.camera import BlinkCamera

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .coordinator import BlinkCoordinator
from .entity import BlinkCameraEntity
from .liveview import ptt_supported

PARALLEL_UPDATES = 0


def snapshot_id(camera: BlinkCamera) -> str | None:
    """Changes whenever the picture does (blinkpy records a new URL even when the download failed)."""
    image = camera._cached_image  # noqa: SLF001
    return hashlib.sha1(image, usedforsecurity=False).hexdigest()[:12] if image else None


def thumbnail_time(camera: BlinkCamera) -> datetime | None:
    """The capture time Blink puts in the thumbnail URL (`...thumbnail.jpg?ts=1775306079`)."""
    try:
        stamp = int(parse_qs(urlsplit(str(camera.thumbnail or "")).query).get("ts", ["0"])[0])
        return datetime.fromtimestamp(stamp, tz=UTC) if stamp > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """A snapshot camera and a live-view camera per Blink camera."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        entity
        for cam in coordinator.blink.cameras.values()
        for entity in (BlinkSnapshotCamera(coordinator, cam), BlinkLiveCamera(coordinator, cam))
    )


class BlinkSnapshotCamera(BlinkCameraEntity, Camera):
    """The picture Blink last took: after motion, on request, or a clip's first frame."""

    _attr_name = None

    def __init__(self, coordinator: BlinkCoordinator, camera: BlinkCamera) -> None:
        """Initialize."""
        BlinkCameraEntity.__init__(self, coordinator, camera, "snapshot")
        Camera.__init__(self)
        self._attr_translation_key = None
        self.content_type = "image/jpeg"
        # The capture time stays with the picture it came with; see snapshot_id.
        self._seen: tuple[str | None, datetime | None] = (None, None)

    def _taken(self) -> datetime | None:
        image_id = snapshot_id(self.camera)
        if image_id != self._seen[0]:
            self._seen = (image_id, thumbnail_time(self.camera))
        return self._seen[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`snapshot_of` is the slug the HTTP API uses; the live camera carries `proxy_slug` instead."""
        taken = self._taken()
        return {
            "snapshot_of": self._slug,
            "snapshot_id": snapshot_id(self.camera),
            "snapshot_taken": taken.isoformat() if taken else None,
            "last_record": self.camera.last_record,
        }

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """The cached thumbnail; Blink is only asked on refresh or a snapshot request."""
        return self.camera.image_from_cache


class BlinkLiveCamera(BlinkCameraEntity, Camera):
    """Where the app reads live-view facts; the stream itself is the HLS API (docs/app-api.md)."""

    def __init__(self, coordinator: BlinkCoordinator, camera: BlinkCamera) -> None:
        """Initialize."""
        BlinkCameraEntity.__init__(self, coordinator, camera, "live_view")
        Camera.__init__(self)
        self.content_type = "image/jpeg"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The proxy's attribute names, so the app only changes its URL."""
        camera = self.camera
        return {
            "proxy_slug": self._slug,
            "blink_camera_id": str(camera.camera_id),
            "network_id": str(camera.network_id),
            "camera_type": camera.camera_type or "default",
            "product_type": camera.product_type,
            "ptt_supported": ptt_supported(camera),
        }

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """The snapshot, as a poster frame."""
        return self.camera.image_from_cache
