"""Which provider package an entry belongs to, and platforms that hand each entry to it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..const import CONF_PROVIDER, PROVIDER_BLINK, PROVIDER_CAMERA, PROVIDER_RTSP, PROVIDER_VISION
from . import blink, camera, rtsp, vision

PROVIDERS: dict[str, ModuleType] = {
    PROVIDER_BLINK: blink,
    PROVIDER_RTSP: rtsp,
    PROVIDER_CAMERA: camera,
    PROVIDER_VISION: vision,
}

type PlatformSetup = Callable[[HomeAssistant, Any, AddConfigEntryEntitiesCallback], Awaitable[None]]


def provider_of(entry: ConfigEntry) -> ModuleType:
    """The provider package for this entry."""
    return PROVIDERS[entry.data.get(CONF_PROVIDER, PROVIDER_BLINK)]


def platform(setups: dict[str, PlatformSetup]) -> PlatformSetup:
    """A platform's async_setup_entry that hands each entry to its provider's module for that platform."""

    async def async_setup_entry(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
    ) -> None:
        if setup := setups.get(entry.data.get(CONF_PROVIDER, PROVIDER_BLINK)):
            await setup(hass, entry, async_add_entities)

    return async_setup_entry
