"""Diagnostics with every credential redacted, from the entry's provider."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PROVIDER, PROVIDER_BLINK
from .providers.blink import diagnostics as blink

DIAGNOSTICS = {PROVIDER_BLINK: blink.async_get_config_entry_diagnostics}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the provider's redacted diagnostics."""
    return await DIAGNOSTICS[entry.data.get(CONF_PROVIDER, PROVIDER_BLINK)](hass, entry)
