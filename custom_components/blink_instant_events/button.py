"""The button platform: each provider's own entities."""

from __future__ import annotations

from .const import PROVIDER_BLINK
from .providers.blink import button as blink
from .providers.registry import platform

async_setup_entry = platform({PROVIDER_BLINK: blink.async_setup_entry})
