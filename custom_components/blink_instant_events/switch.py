"""Per-camera motion detection (the Blink app's camera toggle), and the flood light on floodlight cameras."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlinkInstantConfigEntry
from .entity import BlinkCameraEntity
from .settings import BlinkSettingEntity, offered

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlinkInstantConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One switch per camera."""
    coordinator = entry.runtime_data.coordinator
    settings = entry.runtime_data.settings
    async_add_entities(
        [
            *(BlinkMotionSwitch(coordinator, cam, "motion_detection") for cam in coordinator.blink.cameras.values()),
            *(BlinkFloodLight(settings, cam, "flood_light") for cam in offered(settings, "flood_light")),
        ]
    )


class BlinkMotionSwitch(BlinkCameraEntity, SwitchEntity):
    """On while the camera records motion (when its system is armed)."""

    @property
    def is_on(self) -> bool | None:
        """blinkpy keeps "unknown" when Blink leaves the field out."""
        enabled = self.camera.arm
        return enabled if isinstance(enabled, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable motion detection."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable motion detection."""
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        camera = self.camera
        what = f"turn motion detection {'on' if enabled else 'off'}"
        await self.coordinator.async_action(what, lambda: camera.async_arm(enabled))
        await self.coordinator.async_refresh_camera(camera)
        # Blink can accept a command and not apply it (seen right after a live view).
        if self.camera.arm is not enabled:
            raise HomeAssistantError(
                f"Blink accepted the request to {what} but did not apply it; try again in a minute"
            )


class BlinkFloodLight(BlinkSettingEntity, SwitchEntity):
    """The floodlight's lamp. During a live view it goes over the live session, as in the Blink app."""

    _setting = "flood_light"

    @property
    def is_on(self) -> bool | None:
        """As Blink reports it."""
        return self.value

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Light it."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Put it out."""
        await self._async_set(False)
