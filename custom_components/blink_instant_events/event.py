"""One event entity per Blink camera: motion, doorbell_press, or Blink's raw type."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .const import DOMAIN, EVENT_DOORBELL, EVENT_MOTION, SIGNAL_EVENT

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an event entity for every camera, doorbell and mini."""
    async_add_entities(BlinkInstantEvent(entry, device) for device in entry.runtime_data.devices.values())


class BlinkInstantEvent(EventEntity):
    """Fires as soon as Blink pushes an event for this camera."""

    _attr_has_entity_name = True
    _attr_translation_key = "blink_event"
    _attr_should_poll = False

    def __init__(self, entry: BlinkInstantConfigEntry, device: dict[str, Any]) -> None:
        """Initialize."""
        self._signal = SIGNAL_EVENT.format(entry.entry_id, device["id"])
        self._attr_unique_id = device["serial"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device["serial"])},
            manufacturer="Blink",
            model=device["type"],
            name=device["name"],
            serial_number=device["serial"],
        )
        self._attr_event_types = [EVENT_MOTION] + ([EVENT_DOORBELL] if device["doorbell"] else [])

    async def async_added_to_hass(self) -> None:
        """Subscribe to pushes for this camera."""
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._handle))

    @callback
    def _handle(self, event_type: str, attributes: dict[str, Any]) -> None:
        if event_type not in self._attr_event_types:
            # Blink sent a type we don't know yet; expose it under its raw name.
            self._attr_event_types = [*self._attr_event_types, event_type]
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
