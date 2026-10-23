"""Shared base for entities that belong to one Blink camera or sync module."""

from __future__ import annotations

from blinkpy.camera import BlinkCamera
from blinkpy.sync_module import BlinkSyncModule

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import BlinkCoordinator


def hub_device_info(unique_id: str | None) -> DeviceInfo:
    """The account-level device the diagnostics entities sit on."""
    return DeviceInfo(identifiers={(DOMAIN, str(unique_id))}, manufacturer="Blink", name="Blink Instant Events")


def camera_slug(camera: BlinkCamera) -> str:
    """The camera's name as the HTTP API and the app spell it, e.g. "front_door"."""
    return slugify(camera.name)


def camera_device_info(camera: BlinkCamera) -> DeviceInfo:
    """The device the event entity already created, keyed by serial."""
    return DeviceInfo(
        identifiers={(DOMAIN, camera.serial)},
        manufacturer="Blink",
        model=camera.product_type,
        name=camera.name,
        serial_number=camera.serial,
        sw_version=camera.version,
    )


class BlinkCameraEntity(CoordinatorEntity[BlinkCoordinator]):
    """One entity of one camera."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BlinkCoordinator, camera: BlinkCamera, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._serial = camera.serial
        self._slug = camera_slug(camera)
        self._attr_unique_id = f"{camera.serial}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = camera_device_info(camera)

    @property
    def camera(self) -> BlinkCamera:
        """The current blinkpy object (only read while available)."""
        camera = self.coordinator.camera(self._serial)
        assert camera is not None
        return camera

    @property
    def available(self) -> bool:
        """Unavailable when the poll failed or Blink stopped listing the camera."""
        return super().available and self.coordinator.camera(self._serial) is not None


class BlinkSyncEntity(CoordinatorEntity[BlinkCoordinator]):
    """One entity of one sync module network."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BlinkCoordinator, sync: BlinkSyncModule, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._network_id = str(sync.network_id)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_sync_{self._network_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"sync_{self._network_id}")},
            manufacturer="Blink",
            model="Sync Module",
            name=f"Blink {sync.name}",
            serial_number=sync.serial,
            sw_version=sync.version,
        )

    @property
    def sync(self) -> BlinkSyncModule:
        """The current blinkpy object (only read while available)."""
        return self.coordinator.syncs()[self._network_id]

    @property
    def available(self) -> bool:
        """Unavailable when the poll failed or the network is gone."""
        return super().available and self._network_id in self.coordinator.syncs()
