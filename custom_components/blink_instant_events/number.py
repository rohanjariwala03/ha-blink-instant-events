"""Flood light brightness and speaker volume, on the models that have them."""

from __future__ import annotations

from blinkpy.camera import BlinkCamera

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .controls import LAMP_BRIGHTNESS_RANGE, VOLUME_RANGE
from .settings import BlinkSettingEntity, BlinkSettings, offered

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Brightness on floodlights, volume on cameras with a speaker setting."""
    settings = entry.runtime_data.settings
    async_add_entities(
        [
            *(
                BlinkLevel(settings, cam, "brightness", LAMP_BRIGHTNESS_RANGE)
                for cam in offered(settings, "brightness")
            ),
            *(BlinkLevel(settings, cam, "volume", VOLUME_RANGE) for cam in offered(settings, "volume")),
        ]
    )


class BlinkLevel(BlinkSettingEntity, NumberEntity):
    """A 1-to-N setting, as the Blink app's slider."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1

    def __init__(self, settings: BlinkSettings, camera: BlinkCamera, setting: str, limits: tuple[int, int]) -> None:
        """Initialize."""
        super().__init__(settings, camera, setting)
        self._setting = setting
        self._attr_native_min_value, self._attr_native_max_value = limits

    @property
    def native_value(self) -> float | None:
        """As Blink reports it."""
        return self.value

    async def async_set_native_value(self, value: float) -> None:
        """Change it."""
        await self._async_set(int(value))
