"""Local Camera Vision: Blink cameras with instant push events, and smart detection on your own hardware."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .ai.services import async_setup_services as async_setup_ai_services
from .ai.views import async_register_views
from .const import DOMAIN
from .providers.blink.tv import async_setup_services
from .providers.registry import provider_of

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Actions and the smart-detection API exist even while no camera is set up."""
    async_setup_services(hass)
    async_setup_ai_services(hass)
    async_register_views(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the entry's camera provider."""
    return await provider_of(entry).async_setup_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry's camera provider."""
    return await provider_of(entry).async_unload_entry(hass, entry)
