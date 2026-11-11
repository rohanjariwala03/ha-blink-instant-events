"""Diagnostics for local cameras: state and counters, never the address's credentials."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant

from ...ai.pipeline import get_pipeline
from ...const import CONF_VISION_TOKEN
from . import LocalConfigEntry

# The address can name the camera's private host and path; the user name is half a login.
TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_URL, CONF_VISION_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LocalConfigEntry) -> dict[str, Any]:
    """Entry, stream and detection state."""
    data = entry.runtime_data
    reader, recorder, watcher = data.reader, data.recorder, data.watcher
    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "smart_detection": get_pipeline(hass).enabled,
            "motion": watcher.motion,
            "analyses": watcher.analyses,
            "skipped_for_cooldown": watcher.skipped,
            "stream": None
            if reader is None
            else {
                "connected": reader.connected,
                "restarts": reader.restarts,
                "dropped_frames": reader.dropped,
                "last_error": reader.last_error,  # already scrubbed of the password
                "fps": reader.options.fps,
                "width": reader.options.width,
                "keyframes_only": reader.options.keyframes_only,
            },
            "recording": None
            if recorder is None
            else {"mode": recorder.options.mode, "paused_for_space": recorder.low_space},
        },
        TO_REDACT,
    )
