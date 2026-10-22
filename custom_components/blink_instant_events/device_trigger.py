"""Device triggers: "Motion detected" and "Doorbell pressed"."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.event import ATTR_EVENT_TYPE, DOMAIN as EVENT_DOMAIN
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_ENTITY_ID, CONF_PLATFORM, CONF_TYPE, STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HassJob, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_DOORBELL, EVENT_MOTION

TRIGGER_TYPES = (EVENT_MOTION, EVENT_DOORBELL)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id_or_uuid,
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
    }
)


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    """List triggers for the device's Blink event entity."""
    registry = er.async_get(hass)
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_ENTITY_ID: entry.id,
            CONF_TYPE: trigger_type,
        }
        for entry in er.async_entries_for_device(registry, device_id)
        if entry.platform == DOMAIN and entry.domain == EVENT_DOMAIN
        for trigger_type in TRIGGER_TYPES
        if trigger_type in (entry.capabilities or {}).get("event_types", [])
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Fire when the event entity records an event of this type."""
    entity_id = er.async_resolve_entity_id(er.async_get(hass), config[CONF_ENTITY_ID])
    job = HassJob(action)

    @callback
    def state_changed(event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        # A restored state (old None/unavailable) is history, not a new event.
        if old is None or old.state == STATE_UNAVAILABLE or new is None:
            return
        if new.state == old.state or new.state == STATE_UNAVAILABLE:
            return
        if new.attributes.get(ATTR_EVENT_TYPE) != config[CONF_TYPE]:
            return
        hass.async_run_hass_job(
            job,
            {
                "trigger": {
                    **trigger_info["trigger_data"],
                    CONF_PLATFORM: "device",
                    CONF_DOMAIN: DOMAIN,
                    CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                    CONF_ENTITY_ID: entity_id,
                    CONF_TYPE: config[CONF_TYPE],
                    "from_state": old,
                    "to_state": new,
                    "description": f"Blink {config[CONF_TYPE]}",
                }
            },
        )

    return async_track_state_change_event(hass, [entity_id], state_changed)
