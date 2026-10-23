"""Phase 1: alarm panel, motion switch, sensors, push-driven motion, poll health."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock

from blinkpy.auth import UnauthorizedError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

POLL = "sensor.blink_instant_events_last_blink_refresh"


def push(**fields: object) -> dict[str, str]:
    """A motion push for the backyard camera."""
    return {"data": json.dumps({"created_at": dt_util.utcnow().isoformat(), **fields})}


async def test_entities_match_the_app(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Entity ids and values the iPhone app reads."""
    assert hass.states.get("alarm_control_panel.blink_home").state == "disarmed"
    assert hass.states.get("alarm_control_panel.blink_home").attributes["status"] == "online"
    assert hass.states.get("binary_sensor.blink_home_connection").state == "on"
    assert hass.states.get("switch.backyard_motion_detection").state == "on"
    assert hass.states.get("switch.front_door_motion_detection").state == "off"

    battery = hass.states.get("binary_sensor.front_door_battery")
    assert battery.state == "on"
    assert battery.attributes["battery_state"] == "low"
    assert hass.states.get("binary_sensor.backyard_battery").state == "off"

    temperature = hass.states.get("sensor.backyard_temperature")
    assert temperature.attributes["unit_of_measurement"] == "°C"
    assert float(temperature.state) == pytest.approx(20)  # calibrated 68 °F
    assert hass.states.get("sensor.backyard_battery_voltage").state == "1.65"
    assert hass.states.get("sensor.backyard_wifi_signal").attributes["unit_of_measurement"] == "dBm"
    bars = hass.states.get("sensor.front_door_wifi_signal")
    assert bars.state == "4"
    assert "unit_of_measurement" not in bars.attributes
    assert hass.states.get("sensor.front_door_temperature") is None  # doorbell reports none

    poll = hass.states.get(POLL)
    assert poll.state != "unknown"
    assert poll.attributes["state"] == "ok"
    assert poll.attributes["interval"] == 300
    assert hass.states.get("binary_sensor.backyard_motion").state == "off"


async def test_arm_shows_arming_then_armed(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock
) -> None:
    """Arming is transient 'arming' until Blink confirms."""
    seen: list[str] = []
    # A callback runs in order on the event loop; a plain function would run on a worker thread, out of order.
    hass.bus.async_listen(
        "state_changed",
        callback(
            lambda e: (
                seen.append(e.data["new_state"].state)
                if e.data["entity_id"] == "alarm_control_panel.blink_home"
                else None
            )
        ),
    )
    await hass.services.async_call(
        "alarm_control_panel", "alarm_arm_away", {"entity_id": "alarm_control_panel.blink_home"}, blocking=True
    )
    assert seen[0] == "arming"
    assert hass.states.get("alarm_control_panel.blink_home").state == "armed_away"
    mock_blink.sync["home"].async_arm.assert_awaited_with(True)


async def test_busy_camera_is_an_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock
) -> None:
    """Blink's 307 'busy' becomes a readable error, and the switch is re-read on success."""
    cam = mock_blink.cameras["Backyard"]
    cam.async_arm = AsyncMock(return_value={"code": 307})
    with pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.backyard_motion_detection"}, blocking=True
        )

    async def applied(*_a: object, **_k: object) -> None:
        cam.arm = False

    cam.async_arm = AsyncMock(return_value={"id": 5})
    cam.update = AsyncMock(side_effect=applied)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.backyard_motion_detection"}, blocking=True
    )
    assert hass.states.get("switch.backyard_motion_detection").state == "off"


async def test_motion_from_push(hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock) -> None:
    """A motion push turns motion on for 30 s and schedules one refresh."""
    listener = setup_entry.runtime_data.listener
    listener._on_push(push(event_type="pir", camera_id=100001), "p1")
    listener._on_push(push(event_type="pir", camera_id=100001), "p2")
    await hass.async_block_till_done()
    motion = hass.states.get("binary_sensor.backyard_motion")
    assert motion.state == "on"
    assert motion.attributes["last_record"]

    calls = mock_blink.refresh.await_count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.backyard_motion").state == "off"
    assert mock_blink.refresh.await_count == calls + 1  # two pushes, one refresh


async def test_poll_backoff_and_auth_failure(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mock_blink: MagicMock
) -> None:
    """Failures back off; a rejected session reports auth_failed and starts reauth."""
    coordinator = setup_entry.runtime_data.coordinator
    mock_blink.refresh.side_effect = AttributeError("'NoneType' object has no attribute 'json'")
    await coordinator.async_refresh()
    poll = hass.states.get(POLL).attributes
    assert (poll["state"], poll["failures"], poll["interval"]) == ("backoff", 1, 600)
    assert hass.states.get("switch.backyard_motion_detection").state == "unavailable"

    mock_blink.refresh.side_effect = UnauthorizedError
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(POLL).attributes["state"] == "auth_failed"
    assert any(setup_entry.async_get_active_flows(hass, {"reauth"}))

    mock_blink.refresh.side_effect = None
    await coordinator.async_refresh()
    poll = hass.states.get(POLL).attributes
    assert (poll["state"], poll["failures"], poll["interval"]) == ("ok", 0, 300)
