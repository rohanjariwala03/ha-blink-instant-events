"""Finding the Blink account among the integration's entries (local cameras have entries too)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ...const import CONF_PROVIDER, DOMAIN, PROVIDER_BLINK


def loaded_blink_entry(hass: HomeAssistant) -> ConfigEntry[Any] | None:
    """The Blink account, if it is loaded (there is at most one)."""
    return next(
        (
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if entry.data.get(CONF_PROVIDER, PROVIDER_BLINK) == PROVIDER_BLINK
        ),
        None,
    )
