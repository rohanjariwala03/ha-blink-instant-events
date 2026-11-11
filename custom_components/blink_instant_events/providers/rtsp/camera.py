"""RTSP cameras: the camera entity (live view through Home Assistant's stream, pictures from the reader)."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ...const import SIGNAL_STREAM
from ..local import LocalConfigEntry
from . import stream_url

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: LocalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """One camera entity."""
    async_add_entities([RtspCamera(entry)])


class RtspCamera(Camera):
    """The stream, and its newest frame as the picture."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: LocalConfigEntry) -> None:
        """Initialize."""
        super().__init__()
        self._entry = entry
        self._data = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_camera"
        self._attr_device_info = self._data.device_info
        self.content_type = "image/jpeg"

    @property
    def available(self) -> bool:
        """While frames arrive."""
        return bool(self._data.reader and self._data.reader.connected)

    async def async_added_to_hass(self) -> None:
        """Follow the stream's state."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STREAM.format(self._entry.entry_id), self._update)
        )

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    async def stream_source(self) -> str | None:
        """The address with credentials (Home Assistant's stream redacts them in its own logs)."""
        return stream_url(self._entry)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """The newest frame (scaled to the detection width)."""
        return self._data.reader.latest if self._data.reader else None
