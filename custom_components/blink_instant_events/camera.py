"""The camera platform: each provider's own entities."""

from __future__ import annotations

from .const import PROVIDER_BLINK, PROVIDER_RTSP
from .providers.blink import camera as blink
from .providers.registry import platform
from .providers.rtsp import camera as rtsp

async_setup_entry = platform({PROVIDER_BLINK: blink.async_setup_entry, PROVIDER_RTSP: rtsp.async_setup_entry})
