"""Camera settings as entities, and offline cameras."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

APPLY = "custom_components.blink_instant_events.settings.controls.apply_changes"


async def test_night_vision(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Every camera gets a night vision select, read from Blink and written through controls.py."""
    assert hass.states.get("select.backyard_night_vision").state == "auto"
    assert hass.states.get("select.front_door_night_vision").state == "auto"
    # Neither model has a flood light, lamp brightness or speaker volume setting.
    assert hass.states.get("switch.backyard_flood_light") is None
    assert hass.states.get("number.backyard_speaker_volume") is None

    apply = AsyncMock(return_value={"night_vision": "off", "rejected": [], "busy": []})
    with patch(APPLY, apply):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": "select.backyard_night_vision", "option": "off"}, blocking=True
        )
    assert apply.await_args.args[2] == {"night_vision": "off"}
    assert hass.states.get("select.backyard_night_vision").state == "off"


async def test_busy_camera_says_so(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """A change Blink calls busy raises, so the UI shows it didn't land."""
    apply = AsyncMock(return_value={"night_vision": "auto", "rejected": ["night_vision"], "busy": ["night_vision"]})
    with patch(APPLY, apply), pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": "select.backyard_night_vision", "option": "on"}, blocking=True
        )


async def test_offline_camera(hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock) -> None:
    """While Blink calls a camera offline its entities are unavailable; its connection sensor says why."""
    assert hass.states.get("binary_sensor.backyard_connection").state == "on"
    backyard = next(cam for cam in mock_blink.cameras.values() if cam.name == "Backyard")
    backyard.status = "offline"
    await setup_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.backyard_connection").state == "off"
    assert hass.states.get("switch.backyard_motion_detection").state == "unavailable"
    assert hass.states.get("sensor.backyard_temperature").state == "unavailable"
    assert hass.states.get("switch.front_door_motion_detection").state != "unavailable"
