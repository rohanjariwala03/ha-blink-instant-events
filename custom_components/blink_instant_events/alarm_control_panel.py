"""The alarm control panel platform: each provider's own entities."""

from __future__ import annotations

from .const import PROVIDER_BLINK
from .providers.blink import alarm_control_panel as blink
from .providers.registry import platform

async_setup_entry = platform({PROVIDER_BLINK: blink.async_setup_entry})
