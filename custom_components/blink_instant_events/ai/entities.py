"""Per-camera detection entities every provider adds: who or what was seen, as sensors automations can use.

Binary sensors turn on when a detection shows their kind and off HOLD_SECONDS later; the sensor and image
show the newest detection. They read the shared pipeline, so they look the same for Blink and local cameras.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.image import ImageEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..const import SIGNAL_DETECTION
from .pipeline import PUBLIC, get_pipeline

HOLD_SECONDS = 60


def _person_unrecognized(row: dict[str, Any]) -> bool:
    """An unknown face, or a person whose face nobody could match (seen from behind counts)."""
    if row.get("identity") == "unknown":
        return True
    return "person" in row["labels"] and not row["people"] and not row.get("uncertain")


KINDS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "person": lambda row: "person" in row["labels"],
    "known_person": lambda row: bool(row["people"]),
    "unknown_person": _person_unrecognized,
    "uncertain_person": lambda row: bool(row.get("uncertain")),
    "vehicle": lambda row: "vehicle" in row["labels"],
    "pet": lambda row: "pet" in row["labels"],
}
PERSON_KINDS = {"person", "known_person", "unknown_person", "uncertain_person"}


@dataclass(frozen=True, slots=True)
class Target:
    """Whose detections, and which device the entities belong to."""

    entry_id: str
    slug: str
    unique_prefix: str
    device_info: DeviceInfo


def detection_binary_sensors(target: Target) -> list[DetectionBinarySensor]:
    """person, known_person, unknown_person, uncertain_person, vehicle and pet for one camera."""
    return [DetectionBinarySensor(target, kind) for kind in KINDS]


class DetectionEntity(Entity):
    """Follows the camera's newest detection."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, target: Target, key: str) -> None:
        """Initialize."""
        self._target = target
        self._attr_translation_key = key
        self._attr_unique_id = f"{target.unique_prefix}_{key}"
        self._attr_device_info = target.device_info

    @property
    def _row(self) -> dict[str, Any] | None:
        return get_pipeline(self.hass).latest(self._target.entry_id, self._target.slug)

    async def async_added_to_hass(self) -> None:
        """Follow new detections."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_DETECTION.format(self._target.entry_id), self._on_signal)
        )

    @callback
    def _on_signal(self, slug: str) -> None:
        if slug == self._target.slug:
            self._on_detection()

    @callback
    def _on_detection(self) -> None:
        self.async_write_ha_state()


class DetectionBinarySensor(DetectionEntity, BinarySensorEntity):
    """On for HOLD_SECONDS after a detection of this kind."""

    def __init__(self, target: Target, kind: str) -> None:
        """Initialize."""
        super().__init__(target, kind)
        self._kind = kind
        self._off: CALLBACK_TYPE | None = None
        if kind in PERSON_KINDS:
            self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    async def async_added_to_hass(self) -> None:
        """Follow detections; drop the pending off-switch on removal."""
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel)

    @property
    def is_on(self) -> bool:
        """The newest detection is recent and shows this kind."""
        row = self._row
        if row is None or not KINDS[self._kind](row):
            return False
        analyzed = dt_util.parse_datetime(row["analyzed_at"])
        return analyzed is not None and (dt_util.utcnow() - analyzed).total_seconds() < HOLD_SECONDS

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """What the detection said."""
        row = self._row
        if row is None:
            return None
        return {
            "summary": row["summary"],
            "people": row["people"],
            "confidence": row.get("confidence"),
            "detection_id": row["id"],
        }

    @callback
    def _on_detection(self) -> None:
        self._cancel()
        self._off = async_call_later(self.hass, HOLD_SECONDS, self._expire)
        self.async_write_ha_state()

    @callback
    def _expire(self, _now: datetime) -> None:
        self._off = None
        self.async_write_ha_state()

    @callback
    def _cancel(self) -> None:
        if self._off:
            self._off()
            self._off = None


class LastDetectionSensor(DetectionEntity, SensorEntity):
    """The newest detection's sentence ("Rohan and a vehicle"), with everything it is made of."""

    def __init__(self, target: Target) -> None:
        """Initialize."""
        super().__init__(target, "last_detection")

    @property
    def native_value(self) -> str | None:
        """The summary."""
        row = self._row
        return row["summary"][:255] if row else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The parts of the summary, and when."""
        row = self._row
        if row is None:
            return None
        return {k: row.get(k) for k in PUBLIC} | {
            "detection_id": row["id"],
            "created_at": row["created_at"],
            "provider": row.get("provider"),
            "location": row.get("location"),
        }


class DetectionImage(DetectionEntity, ImageEntity):
    """The best frame of the newest detection, boxes drawn."""

    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, target: Target) -> None:
        """Initialize."""
        DetectionEntity.__init__(self, target, "last_detection")
        ImageEntity.__init__(self, hass)

    @property
    def available(self) -> bool:
        """Once there is something to show."""
        return self._row is not None

    @property
    def image_last_updated(self) -> datetime | None:
        """When the event happened."""
        row = self._row
        return dt_util.parse_datetime(row["created_at"]) if row else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The summary and what it is made of."""
        row = self._row
        return None if row is None else {k: row.get(k) for k in PUBLIC} | {"detection_id": row["id"]}

    @callback
    def _on_detection(self) -> None:
        self._attr_image_last_updated = self.image_last_updated
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """The annotated frame."""
        row = self._row
        if row is None:
            return None
        path = get_pipeline(self.hass).image_path(row["id"])
        return await self.hass.async_add_executor_job(lambda: path.read_bytes() if path.is_file() else None)
