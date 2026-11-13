"""Blink cameras: the visitors calendar."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ...ai.entities import VisitorCalendar
from . import BlinkInstantConfigEntry
from .entity import detection_target

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BlinkInstantConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """One per camera."""
    async_add_entities(
        VisitorCalendar(detection_target(entry.entry_id, cam))
        for cam in entry.runtime_data.coordinator.blink.cameras.values()
    )
