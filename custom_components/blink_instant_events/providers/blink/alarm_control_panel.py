"""Arm and disarm each Blink sync module network."""

from __future__ import annotations

import asyncio
from typing import Any

from blinkpy.sync_module import BlinkSyncModule

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .coordinator import SYNCLESS_TYPES, BlinkCoordinator, sync_armed
from .entity import BlinkSyncEntity

PARALLEL_UPDATES = 0
CONFIRM_TRIES = 5
CONFIRM_SECONDS = 2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One panel per network."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(BlinkAlarm(coordinator, sync) for sync in coordinator.syncs().values())


class BlinkAlarm(BlinkSyncEntity, AlarmControlPanelEntity):
    """The system arm switch the Blink app shows. Blink has one armed state: armed_away."""

    _attr_name = None
    _attr_supported_features = AlarmControlPanelEntityFeature.ARM_AWAY
    _attr_code_arm_required = False

    def __init__(self, coordinator: BlinkCoordinator, sync: BlinkSyncModule) -> None:
        """Initialize."""
        super().__init__(coordinator, sync, "arm")
        self._transient: AlarmControlPanelState | None = None

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Arming/disarming while the command runs, then what Blink reports."""
        if self._transient:
            return self._transient
        armed = sync_armed(self.sync)
        if armed is None:
            return None
        return AlarmControlPanelState.ARMED_AWAY if armed else AlarmControlPanelState.DISARMED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """An offline module keeps its last arm state, so `status` says whether it is really watching."""
        return {"status": self.sync.status}

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm."""
        await self._async_set(True)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Disarm."""
        await self._async_set(False)

    async def _async_set(self, armed: bool) -> None:
        sync = self.sync
        self._transient = AlarmControlPanelState.ARMING if armed else AlarmControlPanelState.DISARMING
        self.async_write_ha_state()
        try:
            await self.coordinator.async_action("arm" if armed else "disarm", lambda: sync.async_arm(armed))
            # Blink applies it within a few seconds; read it back instead of waiting for the next poll.
            for _ in range(CONFIRM_TRIES):
                await sync.get_network_info()
                if type(sync).__name__ in SYNCLESS_TYPES:
                    await sync.blink.get_homescreen()
                if sync_armed(sync) is armed:
                    break
                await asyncio.sleep(CONFIRM_SECONDS)
        finally:
            self._transient = None
            self.coordinator.async_update_listeners()
