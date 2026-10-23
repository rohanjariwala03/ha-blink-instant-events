"""Night vision per camera: auto, on, off."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .settings import BlinkSettingEntity, offered

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One select per camera that has night vision (all of them)."""
    settings = entry.runtime_data.settings
    async_add_entities(BlinkNightVision(settings, cam, "night_vision") for cam in offered(settings, "night_vision"))


class BlinkNightVision(BlinkSettingEntity, SelectEntity):
    """The camera's infrared: auto, always on, or off."""

    _setting = "night_vision"
    _attr_options = ["auto", "on", "off"]
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self) -> str | None:
        """As Blink reports it."""
        return self.value

    async def async_select_option(self, option: str) -> None:
        """Change it."""
        await self._async_set(option)
