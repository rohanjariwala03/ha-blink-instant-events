"""The blueprints in blueprints/: valid for Home Assistant, and they act on the right detections."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

BLUEPRINTS = Path(__file__).parent.parent / "blueprints" / "automation" / "local_camera_vision"


@pytest.fixture
async def phone(hass: HomeAssistant) -> str:
    """A phone with the Home Assistant app, as a device the blueprints can notify."""
    entry = MockConfigEntry(domain="mobile_app", data={"device_name": "Phone", "webhook_id": "w"})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("mobile_app", "phone")}, name="Phone"
    )
    return device.id


async def automation(hass: HomeAssistant, name: str, inputs: dict) -> None:
    folder = Path(hass.config.path("blueprints/automation/local_camera_vision"))
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINTS / name, folder / name)
    config = {"automation": {"use_blueprint": {"path": f"local_camera_vision/{name}", "input": inputs}}}
    assert await async_setup_component(hass, "automation", config)
    await hass.async_block_till_done()
    [automation_id] = hass.states.async_entity_ids("automation")
    assert hass.states.get(automation_id).state == "on", f"{name} did not load"


def fire(hass: HomeAssistant, entity_id: str, **attributes: object) -> None:
    hass.states.async_set(entity_id, f"2026-11-11T12:00:0{len(attributes) % 10}", attributes)


async def test_who_is_at_the_door(hass: HomeAssistant, phone: str) -> None:
    """A doorbell detection notifies and speaks the sentence; motion detections don't."""
    notify = async_mock_service(hass, "notify", "mobile_app_phone")
    speak = async_mock_service(hass, "tts", "speak")
    hass.states.async_set("event.door", "2026-11-11T11:00:00")
    await automation(
        hass,
        "who_is_at_the_door.yaml",
        {"doorbell": "event.door", "phone": phone, "speakers": ["media_player.kitchen"], "voice": "tts.piper"},
    )
    seen = {"event_type": "detection", "camera": "Front Door", "image": "/api/x.jpg"}
    fire(hass, "event.door", **seen, doorbell=False, at_the_door=None)
    await hass.async_block_till_done()
    assert not notify
    fire(hass, "event.door", **seen, doorbell=True, at_the_door="Rohan is at the door", x=1)
    await hass.async_block_till_done()
    assert notify[0].data["message"] == "Rohan is at the door"
    assert notify[0].data["data"]["image"] == "/api/x.jpg"
    assert speak[0].data["message"] == "Rohan is at the door"
    assert speak[0].data["media_player_entity_id"] == ["media_player.kitchen"]


async def test_person_alert_teaches_on_reply(hass: HomeAssistant, phone: str) -> None:
    """Quiet for family; a stranger's alert has a "This is…" reply that teaches the face."""
    notify = async_mock_service(hass, "notify", "mobile_app_phone")
    enroll = async_mock_service(hass, "blink_instant_events", "enroll_face", response={"name": "Alex"})
    hass.states.async_set("event.porch", "2026-11-11T11:00:00")
    await automation(hass, "person_alert.yaml", {"camera": "event.porch", "phone": phone})
    base = {"event_type": "detection", "camera": "Porch", "image": "/api/x.jpg", "labels": ["person"]}
    fire(hass, "event.porch", **base, known_only=True, summary="Rohan")
    await hass.async_block_till_done()
    assert not notify

    detection = "a" * 24
    fire(
        hass,
        "event.porch",
        **base,
        known_only=False,
        summary="An unknown person",
        faces_to_teach=1,
        detection_id=detection,
        x=1,
    )
    # Not async_block_till_done: the run now waits (for hours) for the answer.
    for _ in range(100):
        if notify:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # and its wait_for_trigger is listening
    assert notify[0].data["message"] == "An unknown person"
    [button] = notify[0].data["data"]["actions"]
    assert (button["action"], button["behavior"]) == (f"LCV_TEACH_{detection}", "textInput")

    hass.bus.async_fire("mobile_app_notification_action", {"action": "LCV_TEACH_other", "reply_text": "Bob"})
    hass.bus.async_fire("mobile_app_notification_action", {"action": button["action"], "reply_text": " Alex "})
    await hass.async_block_till_done()
    assert [call.data for call in enroll] == [{"name": "Alex", "detection": detection}]
    assert notify[1].data["message"] == "Got it: that was Alex."


async def test_doorbell_on_tv(hass: HomeAssistant) -> None:
    """A press casts the camera; a detection doesn't."""
    show = async_mock_service(hass, "blink_instant_events", "show_on_tv")
    hass.states.async_set("event.door", "2026-11-11T11:00:00")
    await automation(
        hass, "doorbell_on_tv.yaml", {"doorbell": "event.door", "camera": "camera.front_door", "tv": "media_player.tv"}
    )
    fire(hass, "event.door", event_type="detection")
    fire(hass, "event.door", event_type="doorbell_press", x=1)
    await hass.async_block_till_done()
    assert [call.data for call in show] == [
        {"camera": "camera.front_door", "media_player": "media_player.tv", "duration": 60}
    ]
