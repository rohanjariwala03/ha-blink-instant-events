"""Finding the Blink account among the integration's entries (local cameras have entries too)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ...const import CONF_PROVIDER, DOMAIN, PROVIDER_BLINK


def loaded_blink_entries(hass: HomeAssistant) -> list[ConfigEntry[Any]]:
    """Every loaded Blink account."""
    return [
        entry
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if entry.data.get(CONF_PROVIDER, PROVIDER_BLINK) == PROVIDER_BLINK and getattr(entry, "runtime_data", None)
    ]


def loaded_blink_entry(hass: HomeAssistant) -> ConfigEntry[Any] | None:
    """The first loaded Blink account (for what isn't about one camera)."""
    return next(iter(loaded_blink_entries(hass)), None)


def blink_entry_for(hass: HomeAssistant, slug: str) -> ConfigEntry[Any] | None:
    """The Blink account with this camera (slugs are unique across accounts, see index_cameras)."""
    return next((entry for entry in loaded_blink_entries(hass) if entry.runtime_data.live.camera(slug)), None)
