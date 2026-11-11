"""Local cameras: one event entity firing motion, detection and recording."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ...const import EVENT_DETECTION, EVENT_MOTION, EVENT_RECORDING, SIGNAL_EVENT
from . import LocalConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: LocalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """One per camera."""
    async_add_entities([LocalEvent(entry)])


class LocalEvent(EventEntity):
    """Fires the moment something happens on the camera."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "camera_event"

    def __init__(self, entry: LocalConfigEntry) -> None:
        """Initialize."""
        data = entry.runtime_data
        self._signal = SIGNAL_EVENT.format(entry.entry_id, data.source.camera_id)
        self._attr_unique_id = f"{entry.entry_id}_event"
        self._attr_device_info = data.device_info
        self._attr_event_types = [EVENT_MOTION, EVENT_DETECTION, *([EVENT_RECORDING] if data.recorder else [])]

    async def async_added_to_hass(self) -> None:
        """Subscribe."""
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._handle))

    @callback
    def _handle(self, event_type: str, attributes: dict[str, Any]) -> None:
        if event_type in self._attr_event_types:
            self._trigger_event(event_type, attributes)
            self.async_write_ha_state()
