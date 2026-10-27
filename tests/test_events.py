"""Pushes become events; triggers, dedupe, staleness, receipts, diagnostics."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import async_get_device_automations
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.blink_instant_events.const import DOMAIN
from custom_components.blink_instant_events.listener import classify, flatten
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util


def push(**fields: object) -> dict[str, str]:
    """An Android-style push: JSON payload nested under `data`."""
    body = {"created_at": dt_util.utcnow().isoformat(), **fields}
    return {"data": json.dumps(body)}


def entity_id(hass: HomeAssistant, serial: str) -> str:
    """Event entity for a camera serial."""
    return er.async_get(hass).async_get_entity_id("event", DOMAIN, serial)


def test_classify() -> None:
    """Blink event types map to HA event types."""
    assert classify({"event_type": "button_press"}) == "doorbell_press"
    for raw in ("cloud_motion", "cam2sm_motion", "pir", "cv_motion"):
        assert classify({"event_type": raw}) == "motion"
    assert classify({"event_type": "Low Battery"}) == "low_battery"
    assert classify({}) is None
    assert flatten({"message": '{"data": {"a": 1}}'})["a"] == 1


async def test_doorbell_and_motion(hass: HomeAssistant, setup_entry, mock_account: MagicMock) -> None:
    """Doorbell press by camera id, motion by camera name, both with attributes."""
    listener = setup_entry.runtime_data.listener
    door = entity_id(hass, "G8T1-0002")
    listener._on_push(push(event_type="button_press", camera_id=100002, unique_id="u1", return_receipt="true"), "p1")
    await hass.async_block_till_done()
    state = hass.states.get(door)
    assert state.attributes["event_type"] == "doorbell_press"
    assert state.attributes["camera"] == "Front Door"
    assert state.attributes["blink_event_type"] == "button_press"
    mock_account.receipt.assert_awaited_once_with("u1")

    outdoor = entity_id(hass, "G8T1-0001")
    listener._on_push(
        push(
            event_type="cam2sm_motion",
            camera_name="Backyard",
            category="motion",
            detection_type="person",
            detections=["person"],
            message="",
        ),
        "p2",
    )
    await hass.async_block_till_done()
    state = hass.states.get(outdoor)
    assert state.attributes["event_type"] == "motion"
    assert state.attributes["category"] == "motion"
    assert state.attributes["detection_type"] == "person"
    assert state.attributes["detections"] == ["person"]
    assert "message" not in state.attributes
    assert "doorbell_press" not in state.attributes["event_types"]


async def test_duplicate_stale_unknown_ignored(hass: HomeAssistant, setup_entry) -> None:
    """Same unique_id twice, old pushes and unknown cameras don't fire."""
    listener = setup_entry.runtime_data.listener
    door = entity_id(hass, "G8T1-0002")
    listener._on_push(push(event_type="button_press", camera_id=100002, unique_id="u1"), "p1")
    await hass.async_block_till_done()
    first = hass.states.get(door).state

    listener._on_push(push(event_type="button_press", camera_id=100002, unique_id="u1"), "p2")
    old = (dt_util.utcnow() - timedelta(minutes=10)).isoformat()
    listener._on_push(push(event_type="button_press", camera_id=100002, created_at=old), "p3")
    listener._on_push(push(event_type="button_press", camera_id=1), "p4")
    await hass.async_block_till_done()
    assert hass.states.get(door).state == first


async def test_raw_event_type(hass: HomeAssistant, setup_entry) -> None:
    """Unknown Blink types are exposed under their raw name."""
    listener = setup_entry.runtime_data.listener
    listener._on_push(push(event_type="camera_offline", camera_id=100001), "p1")
    await hass.async_block_till_done()
    state = hass.states.get(entity_id(hass, "G8T1-0001"))
    assert state.attributes["event_type"] == "camera_offline"


async def test_device_triggers(hass: HomeAssistant, setup_entry) -> None:
    """Doorbell device offers both triggers, and the doorbell trigger fires."""
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "G8T1-0002"), setup_entry.entry_id)
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, device.id)
    ours = [t for t in triggers if t["domain"] == DOMAIN]
    assert {t["type"] for t in ours} == {"motion", "doorbell_press", "detection"}

    calls: list = []
    hass.services.async_register("test", "record", lambda call: calls.append(call))
    press = next(t for t in ours if t["type"] == "doorbell_press")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {automation.DOMAIN: {"trigger": press, "action": {"action": "test.record"}}},
    )
    listener = setup_entry.runtime_data.listener
    listener._on_push(push(event_type="pir", camera_id=100002), "p1")
    await hass.async_block_till_done()
    assert not calls
    listener._on_push(push(event_type="button_press", camera_id=100002), "p2")
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_device_per_serial(hass: HomeAssistant, setup_entry) -> None:
    """Each camera is a device keyed by its Blink serial."""
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "G8T1-0001"), setup_entry.entry_id)
    assert device.name == "Backyard"
    assert device.serial_number == "G8T1-0001"


async def test_connection_sensor_and_diagnostics(hass: HomeAssistant, hass_client, setup_entry) -> None:
    """Connection sensor is on; diagnostics leak no secrets."""
    sensor = er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, "99_push_connected")
    assert hass.states.get(sensor).state == "on"

    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)
    text = json.dumps(diag)
    for secret in ("access-token", "refresh-token", "fcm-token", "me@example.com", "8D6F3F3E", "G8T1"):
        assert secret not in text
