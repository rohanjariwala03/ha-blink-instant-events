"""Camera providers: where cameras, pictures and motion come from.

Each provider is a package with `async_setup_entry`, `async_unload_entry` and `PLATFORMS`, plus one module
per platform it offers. A config entry names its provider in `entry.data["provider"]`; entries made before
there were providers are Blink accounts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..const import CONF_PROVIDER, PROVIDER_BLINK
from . import blink

PROVIDERS: dict[str, ModuleType] = {PROVIDER_BLINK: blink}

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
