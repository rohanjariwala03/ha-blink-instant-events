"""Camera temperature, Wi-Fi signal and battery voltage, plus the poll's health."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from blinkpy.camera import BlinkCamera

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BlinkInstantConfigEntry
from .coordinator import BlinkCoordinator
from .entity import BlinkCameraEntity, hub_device_info

PARALLEL_UPDATES = 0


def _number(value: Any) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else value


def _temperature(cam: BlinkCamera) -> float | None:
    return (
        _number(cam.temperature_calibrated)
        if _number(cam.temperature_calibrated) is not None
        else _number(cam.temperature)
    )


def _voltage(cam: BlinkCamera) -> float | None:
    value = _number(cam.battery_voltage)  # hundredths of a volt
    return None if value is None else round(value / 100, 2)


@dataclass(frozen=True, kw_only=True)
class BlinkSensorDescription(SensorEntityDescription):
    """How to read one camera value."""

    value: Callable[[BlinkCamera], float | None]


TEMPERATURE = BlinkSensorDescription(
    key="temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,  # as Blink reports it
    suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    suggested_display_precision=0,
    state_class=SensorStateClass.MEASUREMENT,
    value=_temperature,
)
VOLTAGE = BlinkSensorDescription(
    key="battery_voltage",
    device_class=SensorDeviceClass.VOLTAGE,
    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value=_voltage,
)
WIFI_DBM = BlinkSensorDescription(
    key="wifi_signal",
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value=lambda cam: _number(cam.wifi_strength),
)
# Some models report 0-5 bars instead of dBm; the unit has to be fixed per entity.
WIFI_BARS = BlinkSensorDescription(
    key="wifi_signal",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value=lambda cam: _number(cam.wifi_strength),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors each camera actually reports, and the poll sensor."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [BlinkPollSensor(coordinator)]
    for cam in coordinator.blink.cameras.values():
        wifi = _number(cam.wifi_strength)
        for description in (TEMPERATURE, VOLTAGE, WIFI_DBM if wifi is not None and wifi < 0 else WIFI_BARS):
            # A sensor that could only ever say "unknown" is left out.
            if description.value(cam) is not None:
                entities.append(BlinkCameraSensor(coordinator, cam, description))
    async_add_entities(entities)


class BlinkCameraSensor(BlinkCameraEntity, SensorEntity):
    """One reading from one camera."""

    entity_description: BlinkSensorDescription

    def __init__(self, coordinator: BlinkCoordinator, camera: BlinkCamera, description: BlinkSensorDescription) -> None:
        """Initialize."""
        super().__init__(coordinator, camera, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """The value from the last refresh."""
        return self.entity_description.value(self.camera)


class BlinkPollSensor(CoordinatorEntity[BlinkCoordinator], SensorEntity):
    """When Blink was last refreshed, and why not if the poll is failing."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_refresh"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BlinkCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_last_refresh"
        self._attr_device_info = hub_device_info(entry.unique_id)

    @property
    def available(self) -> bool:
        """Always: a failing poll is exactly what this sensor reports."""
        return True

    @property
    def native_value(self) -> datetime | None:
        """The last successful refresh."""
        return self.coordinator.last_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """state ("ok", "backoff", "auth_failed"), interval, failures, polls, last_error."""
        return dict(self.coordinator.poll)
