"""Not a camera: where Blink Vision runs and the token it wants. Made by itself when the add-on announces
itself through the Supervisor, or by hand for an add-on elsewhere (a Docker container, another machine)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from ...ai.pipeline import async_start_pipeline, get_pipeline

PLATFORMS: list[Platform] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Look for the add-on at this address (smart detection stays off until it answers)."""
    await async_start_pipeline(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_reprobe))
    return True


async def _async_reprobe(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await get_pipeline(hass).async_probe()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Probe again without this address."""
    hass.async_create_task(get_pipeline(hass).async_probe())
    return True
