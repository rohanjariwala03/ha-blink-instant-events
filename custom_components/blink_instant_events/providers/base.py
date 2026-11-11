"""What every camera provider hands the rest of the integration: which camera, and what happened.

The AI pipeline only ever sees a `Source` and JPEG frames; it never learns whether they came from Blink's
cloud, an RTSP stream or another Home Assistant camera.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

type Location = Literal["cloud", "local"]


@dataclass(frozen=True, slots=True)
class Source:
    """One camera, as the pipeline and the shared entities key it."""

    entry_id: str
    provider: str  # "blink", "rtsp", "camera"
    camera_id: str  # what the provider's event entity listens on
    slug: str  # stable and URL-safe: the detection image and history key
    name: str
    location: Location  # where its pictures are fetched from


@dataclass(frozen=True, slots=True)
class CameraEvent:
    """The unified event every provider fires on the camera's event entity."""

    camera_id: str
    provider: str
    event_type: str  # "motion", "doorbell_press", "detection", ...
    source: Location
    video_available: bool = False
    timestamp: datetime = field(default_factory=dt_util.utcnow)

    def attributes(self) -> dict[str, Any]:
        """The fields as event attributes (ISO timestamp)."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


class Provider(Protocol):
    """A provider package: `providers/<name>/__init__.py`, plus one module per platform in PLATFORMS."""

    PLATFORMS: list[Platform]

    async def async_setup_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> bool:
        """Connect, load the cameras, start events; raise ConfigEntryNotReady/AuthFailed as usual."""

    async def async_unload_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> bool:
        """Stop everything started in setup."""
