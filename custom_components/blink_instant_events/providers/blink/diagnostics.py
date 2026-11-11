"""Diagnostics with every credential redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import BlinkInstantConfigEntry

TO_REDACT = {
    "username",
    "password",
    "token",
    "refresh_token",
    "hardware_id",
    "uid",
    "device_id",
    "account_id",
    "user_id",
    "client_id",
    "fcm",
    "unique_id",
    "title",
    "serial",
    "thumbnail",
    "video",
    "recent_clips",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: BlinkInstantConfigEntry) -> dict[str, Any]:
    """Return redacted entry data and listener state."""
    listener = entry.runtime_data.listener
    coordinator = entry.runtime_data.coordinator
    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "connected": listener.connected,
            "devices": list(listener.devices.values()),
            "last_event": listener.last_event,
            "poll": coordinator.poll,
            "snapshots_after_motion": {cam: count for cam, (_at, count) in coordinator.auto_snapshots.items()},
            "cameras": [cam.attributes for cam in coordinator.blink.cameras.values()],
            "sync_modules": [sync.attributes for sync in coordinator.syncs().values()],
        },
        TO_REDACT,
    )
