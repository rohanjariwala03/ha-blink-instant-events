"""Diagnostics with every credential redacted, from the entry's provider."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PROVIDER, PROVIDER_BLINK, PROVIDER_CAMERA, PROVIDER_RTSP
from .providers.blink import diagnostics as blink
from .providers.local import diagnostics as local

DIAGNOSTICS = {
    PROVIDER_BLINK: blink.async_get_config_entry_diagnostics,
    PROVIDER_RTSP: local.async_get_config_entry_diagnostics,
    PROVIDER_CAMERA: local.async_get_config_entry_diagnostics,
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the provider's redacted diagnostics."""
    if (diagnostics := DIAGNOSTICS.get(entry.data.get(CONF_PROVIDER, PROVIDER_BLINK))) is None:
        return {"provider": entry.data.get(CONF_PROVIDER)}
    return await diagnostics(hass, entry)
