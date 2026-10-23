"""One "Last detection" picture per camera: the best frame of its newest analyzed clip, boxes drawn."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from blinkpy.camera import BlinkCamera

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import BlinkInstantConfigEntry
from .const import SIGNAL_DETECTION
from .coordinator import BlinkCoordinator
from .entity import BlinkCameraEntity
from .vision import Vision

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one per camera; they stay empty until Blink Vision has looked at a clip."""
    data = entry.runtime_data
    async_add_entities(
        BlinkDetectionImage(hass, data.coordinator, data.vision, cam) for cam in data.coordinator.blink.cameras.values()
    )


class BlinkDetectionImage(BlinkCameraEntity, ImageEntity):
    """Who or what Blink Vision saw last on this camera."""

    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, coordinator: BlinkCoordinator, vision: Vision, camera: BlinkCamera) -> None:
        """Initialize."""
        BlinkCameraEntity.__init__(self, coordinator, camera, "last_detection")
        ImageEntity.__init__(self, hass)
        self._vision = vision

    @property
    def available(self) -> bool:
        """Only while someone could look at it (the camera's own state doesn't matter)."""
        return self._row is not None

    @property
    def _row(self) -> dict[str, Any] | None:
        return self._vision.latest(self._slug)

    @property
    def image_last_updated(self) -> datetime | None:
        """When the clip was recorded."""
        row = self._row
        return dt_util.parse_datetime(row["created_at"]) if row else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The summary and what it is made of."""
        row = self._row
        if row is None:
            return None
        return {k: row[k] for k in ("summary", "people", "unknown_faces", "labels", "known_only")} | {
            "clip_id": row["id"]
        }

    async def async_image(self) -> bytes | None:
        """The annotated frame."""
        row = self._row
        if row is None:
            return None
        path = self._vision.image_path(row["id"])
        return await self.hass.async_add_executor_job(lambda: path.read_bytes() if path.is_file() else None)

    async def async_added_to_hass(self) -> None:
        """Update when this camera gets a new detection."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DETECTION.format(self.coordinator.config_entry.entry_id), self._handle
            )
        )

    @callback
    def _handle(self, slug: str) -> None:
        if slug == self._slug:
            self.async_write_ha_state()
