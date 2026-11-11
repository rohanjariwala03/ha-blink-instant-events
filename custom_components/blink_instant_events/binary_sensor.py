"""The binary sensor platform: each provider's own entities."""

from __future__ import annotations

from .const import PROVIDER_BLINK, PROVIDER_CAMERA, PROVIDER_RTSP
from .providers.blink import binary_sensor as blink
from .providers.local import binary_sensor as local
from .providers.registry import platform

async_setup_entry = platform(
    {
        PROVIDER_BLINK: blink.async_setup_entry,
        PROVIDER_RTSP: local.async_setup_entry,
        PROVIDER_CAMERA: local.async_setup_entry,
    }
)
