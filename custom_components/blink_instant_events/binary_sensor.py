"""Push connection, per-camera motion (from push) and battery, per-network connectivity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from blinkpy.camera import BlinkCamera
from blinkpy.sync_module import BlinkSyncModule

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import BlinkInstantConfigEntry
from .const import EVENT_MOTION, MOTION_RESET_SECONDS, SIGNAL_CONNECTION, SIGNAL_EVENT
from .coordinator import BlinkCoordinator
from .entity import BlinkCameraEntity, BlinkSyncEntity, hub_device_info

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the push connection sensor and the per-device sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = [BlinkPushConnected(entry)]
    for cam in coordinator.blink.cameras.values():
        entities += [
            BlinkMotion(coordinator, cam),
            BlinkBattery(coordinator, cam, "battery"),
            BlinkCameraConnected(coordinator, cam, "camera_connection"),
        ]
    entities += [BlinkSyncConnected(coordinator, sync, "connection") for sync in coordinator.syncs().values()]
    async_add_entities(entities)


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
        self._attr_device_info = hub_device_info(entry.unique_id)

    @property
    def is_on(self) -> bool:
        """Return whether pushes can arrive."""
        return self._entry.runtime_data.listener.connected

    async def async_added_to_hass(self) -> None:
        """Refresh whenever the listener checks its connection."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self._entry.entry_id),
                self.async_write_ha_state,
            )
        )


class BlinkMotion(BlinkCameraEntity, BinarySensorEntity):
    """On for 30 s after each motion push: instant, unlike Blink's clip-based polling."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, coordinator: BlinkCoordinator, camera: BlinkCamera) -> None:
        """Initialize."""
        super().__init__(coordinator, camera, "motion")
        self._camera_id = str(camera.camera_id)
        self._attr_is_on = False
        self._last_push: datetime | None = None
        self._reset: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        """Pushes keep arriving while a poll fails, so only the camera has to exist."""
        return self.coordinator.camera(self._serial) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`last_record`: the later of the last motion push and the last clip Blink listed."""
        clip = dt_util.parse_datetime(str(self.camera.last_record or ""))
        if clip and clip.tzinfo is None:
            clip = clip.replace(tzinfo=dt_util.UTC)
        times = [t for t in (self._last_push, clip) if t is not None]
        return {"last_record": max(times).isoformat() if times else None}

    async def async_added_to_hass(self) -> None:
        """Listen for this camera's pushes."""
        await super().async_added_to_hass()
        signal = SIGNAL_EVENT.format(self.coordinator.config_entry.entry_id, self._camera_id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self._handle))
        self.async_on_remove(self._cancel_reset)

    @callback
    def _handle(self, event_type: str, _attributes: dict[str, Any]) -> None:
        if event_type != EVENT_MOTION:
            return
        self._cancel_reset()
        self._last_push = dt_util.utcnow()
        self._attr_is_on = True
        self.async_write_ha_state()
        self._reset = async_call_later(self.hass, MOTION_RESET_SECONDS, self._clear)

    @callback
    def _clear(self, _now: datetime) -> None:
        self._reset = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _cancel_reset(self) -> None:
        if self._reset:
            self._reset()
            self._reset = None


class BlinkBattery(BlinkCameraEntity, BinarySensorEntity):
    """On when Blink calls the battery low."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY

    @property
    def is_on(self) -> bool | None:
        """Blink reports "ok" or "low"."""
        state = self.camera.battery_state
        return None if state is None else str(state).lower() != "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Blink's own words for it."""
        return {"battery_state": self.camera.battery_state, "battery_level": self.camera.battery_level}


class BlinkSyncConnected(BlinkSyncEntity, BinarySensorEntity):
    """On while Blink can reach the sync module; condition automations on this, not the arm state."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BlinkCoordinator, sync: BlinkSyncModule, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator, sync, key)
        self._attr_translation_key = key

    @property
    def is_on(self) -> bool | None:
        """Blink's status is "online" or "offline"."""
        status = self.sync.status
        return str(status).lower() == "online" if status else None


class BlinkCameraConnected(BlinkCameraEntity, BinarySensorEntity):
    """Whether Blink can reach the camera. While it can't, the camera's other entities are unavailable."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Stays available to say the camera is offline."""
        return self.listed

    @property
    def is_on(self) -> bool | None:
        """Blink's status: "online" or "done" when reachable, "offline" when not."""
        status = self.camera.status
        return None if status is None else status != "offline"
