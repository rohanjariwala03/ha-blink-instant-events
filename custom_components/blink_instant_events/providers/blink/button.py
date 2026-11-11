"""Per camera: ask Blink for a new snapshot."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .entity import BlinkCameraEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One button per camera."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        BlinkSnapshotButton(coordinator, cam, "refresh_snapshot") for cam in coordinator.blink.cameras.values()
    )


class BlinkSnapshotButton(BlinkCameraEntity, ButtonEntity):
    """Wakes the camera for one picture, so it costs battery."""

    async def async_press(self) -> None:
        """Take the picture; the snapshot camera shows it once downloaded."""
        await self.coordinator.async_snap(self.camera)
