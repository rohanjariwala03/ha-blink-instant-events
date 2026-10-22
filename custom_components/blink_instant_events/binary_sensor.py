"""Diagnostic sensor: is the Blink push channel connected?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .const import DOMAIN, SIGNAL_CONNECTION

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the connection sensor."""
    async_add_entities([BlinkPushConnected(entry)])


class BlinkPushConnected(BinarySensorEntity):
    """On while the FCM connection is up and associated with Blink."""

    _attr_has_entity_name = True
    _attr_translation_key = "push_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: BlinkInstantConfigEntry) -> None:
        """Initialize."""
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}_push_connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            manufacturer="Blink",
            name="Blink Instant Events",
        )

    @property
    def is_on(self) -> bool:
        """Return whether pushes can arrive."""
        return self._entry.runtime_data.connected

    async def async_added_to_hass(self) -> None:
        """Refresh whenever the listener checks its connection."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self._entry.entry_id),
                self.async_write_ha_state,
            )
        )
