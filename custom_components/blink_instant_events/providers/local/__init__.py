"""What the local providers (RTSP streams, Home Assistant cameras) share: the camera's runtime and entities.

Nothing here talks to Blink, Amazon, Google or any other cloud service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from ...ai.entities import Target
from ...const import DOMAIN
from ..base import Source
from .watcher import Watcher

if TYPE_CHECKING:
    from ..rtsp.recorder import Recorder
    from ..rtsp.stream import RtspReader


@dataclass
class LocalData:
    """One local camera, as its entities see it."""

    source: Source
    watcher: Watcher
    device_info: DeviceInfo
    reader: RtspReader | None = None  # RTSP: the stream
    recorder: Recorder | None = None  # RTSP: when recording is on
    camera_entity: str | None = None  # Home Assistant camera: the one it reads

    @property
    def target(self) -> Target:
        """Where the shared detection entities go."""
        return Target(self.source.entry_id, self.source.slug, self.source.entry_id, self.device_info)


type LocalConfigEntry = ConfigEntry[LocalData]


def local_source(entry: ConfigEntry, provider: str) -> Source:
    """The camera as the pipeline sees it; its id is the entry's."""
    return Source(
        entry.entry_id, provider, entry.entry_id, slugify(entry.title) or entry.entry_id, entry.title, "local"
    )


def local_device(entry: ConfigEntry, model: str) -> DeviceInfo:
    """One device per local camera entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Local camera", model=model
    )
