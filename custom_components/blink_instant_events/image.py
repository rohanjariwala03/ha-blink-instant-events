"""The image platform: each provider's own entities."""

from __future__ import annotations

from .const import PROVIDER_BLINK
from .providers import platform
from .providers.blink import image as blink

async_setup_entry = platform({PROVIDER_BLINK: blink.async_setup_entry})
