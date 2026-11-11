"""Local cameras: the last-detection picture."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ...ai.entities import DetectionImage
from . import LocalConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: LocalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """One per camera."""
    async_add_entities([DetectionImage(hass, entry.runtime_data.target)])
