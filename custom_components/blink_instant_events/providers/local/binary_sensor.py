"""Local cameras: motion and stream connection (RTSP), and the shared detection sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ...ai.entities import detection_binary_sensors
from ...const import SIGNAL_MOTION, SIGNAL_STREAM
from . import LocalConfigEntry, LocalData

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: LocalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """A Home Assistant camera already has its motion sensor; an RTSP stream gets one, and a connection sensor."""
    data = entry.runtime_data
    entities: list[Entity] = list(detection_binary_sensors(data.target))
    if data.reader is not None:
        entities += [LocalMotion(entry), StreamConnected(entry)]
    async_add_entities(entities)


class _LocalEntity(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: LocalConfigEntry, key: str, signal: str) -> None:
        self._data: LocalData = entry.runtime_data
        self._signal = signal.format(entry.entry_id)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = self._data.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class LocalMotion(_LocalEntity):
    """Motion the stream itself showed, measured here (no camera event needed)."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, entry: LocalConfigEntry) -> None:
        """Initialize."""
        super().__init__(entry, "motion", SIGNAL_MOTION)

    @property
    def is_on(self) -> bool:
        """Moving now (or in the last MOTION_RESET_SECONDS)."""
        return self._data.watcher.motion


class StreamConnected(_LocalEntity):
    """On while frames arrive from the camera."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: LocalConfigEntry) -> None:
        """Initialize."""
        super().__init__(entry, "stream_connected", SIGNAL_STREAM)

    @property
    def is_on(self) -> bool:
        """Frames are arriving."""
        return bool(self._data.reader and self._data.reader.connected)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Why not, and how often it had to reconnect."""
        reader = self._data.reader
        assert reader is not None
        return {"last_error": reader.last_error, "restarts": reader.restarts, "dropped_frames": reader.dropped}
