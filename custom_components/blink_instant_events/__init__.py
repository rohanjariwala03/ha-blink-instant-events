"""Blink Instant Events: real-time Blink motion and doorbell events over FCM push."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BlinkAccount, BlinkAuthError
from .const import CONF_AUTH
from .listener import BlinkPushListener

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT]

type BlinkInstantConfigEntry = ConfigEntry[BlinkPushListener]


async def async_setup_entry(hass: HomeAssistant, entry: BlinkInstantConfigEntry) -> bool:
    """Sign in with the stored Blink session, start the push listener."""

    @callback
    def save_auth(auth: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_AUTH: auth})

    account = BlinkAccount(async_get_clientsession(hass), dict(entry.data[CONF_AUTH]), save_auth)
    listener: BlinkPushListener | None = None
    try:
        listener = BlinkPushListener(hass, entry, account, await account.async_get_devices())
        await listener.async_start()
    except (BlinkAuthError, ClientError, TimeoutError, RuntimeError) as err:
        if listener:
            await listener.async_stop()
        if isinstance(err, BlinkAuthError):
            raise ConfigEntryAuthFailed from err
        raise ConfigEntryNotReady(f"Blink push setup failed: {err}") from err

    entry.runtime_data = listener
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BlinkInstantConfigEntry) -> bool:
    """Stop the listener and unload platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
