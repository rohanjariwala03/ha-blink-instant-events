"""Camera settings as entities: night vision, flood light, lamp brightness, speaker volume.

They are read from Blink's cloud (which does not wake the camera) every 30 minutes and right after a change, and
written through controls.py, the code behind the live view's settings sheet. So a change Blink calls busy while a
live view holds the camera is kept and written when it ends, exactly as in the app.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from blinkpy.auth import UnauthorizedError
from blinkpy.camera import BlinkCamera

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import controls
from .const import BLINK_ERRORS
from .coordinator import BlinkCoordinator
from .entity import BlinkCameraEntity, camera_slug
from .liveview import LiveViews, control_row

_LOGGER = logging.getLogger(__name__)

SETTINGS_INTERVAL = timedelta(minutes=30)


class BlinkSettings(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Each camera's settings, by slug, in controls.py's shape."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, blink: BlinkCoordinator, live: LiveViews) -> None:
        """Initialize."""
        super().__init__(
            hass, _LOGGER, config_entry=entry, name="Blink camera settings", update_interval=SETTINGS_INTERVAL
        )
        self.blink = blink
        self.live = live

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Read every camera; one that fails keeps its last known settings."""
        data = dict(self.data or {})
        failed: list[str] = []
        for camera in self.blink.blink.cameras.values():
            slug = camera_slug(camera)
            try:
                async with asyncio.timeout(20):
                    data[slug] = await controls.fetch_state(
                        camera.sync.blink, control_row(camera), self.live.viewer_stream(slug, ""), self.live.deferred
                    )
            except (UnauthorizedError, *BLINK_ERRORS) as err:
                _LOGGER.debug("Could not read the settings of %s: %s", camera.name, err)
                failed.append(camera.name)
        if failed and len(failed) == len(self.blink.blink.cameras):
            raise UpdateFailed(f"Blink did not answer for {', '.join(failed)}")
        return data

    async def async_set(self, camera: BlinkCamera, changes: dict[str, Any]) -> None:
        """Change settings; raise if Blink refused or the camera was busy."""
        slug = camera_slug(camera)
        try:
            async with asyncio.timeout(30):
                state = await controls.apply_changes(
                    camera.sync.blink,
                    control_row(camera),
                    changes,
                    self.live.viewer_stream(slug, ""),
                    self.live.deferred,
                )
        except controls.ControlError as err:
            raise HomeAssistantError(str(err)) from err
        except (UnauthorizedError, *BLINK_ERRORS) as err:
            raise HomeAssistantError(f"Blink did not answer: {err}") from err
        self.async_set_updated_data({**(self.data or {}), slug: state})
        if busy := state.get("busy"):
            raise HomeAssistantError(f"{camera.name} is busy ({', '.join(busy)}); try again in a minute")
        if rejected := state.get("rejected"):
            raise HomeAssistantError(f"Blink did not accept: {', '.join(rejected)}")


class BlinkSettingEntity(BlinkCameraEntity):
    """One setting of one camera: available while the camera is and Blink reported the setting."""

    _setting: str

    def __init__(self, settings: BlinkSettings, camera: BlinkCamera, key: str) -> None:
        """Initialize."""
        super().__init__(settings.blink, camera, key)
        self.settings = settings

    async def async_added_to_hass(self) -> None:
        """Also follow the settings coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(self.settings.async_add_listener(self.async_write_ha_state))

    @property
    def value(self) -> Any:
        """The setting as Blink last reported it (or as just asked, while held for after a live view)."""
        return (self.settings.data or {}).get(self._slug, {}).get(self._setting)

    @property
    def available(self) -> bool:
        """Unavailable until Blink has reported the setting."""
        return super().available and self.value is not None

    async def _async_set(self, value: Any) -> None:
        await self.settings.async_set(self.camera, {self._setting: value})


def offered(settings: BlinkSettings, setting: str) -> list[BlinkCamera]:
    """The cameras whose model has this setting."""
    return [
        camera
        for camera in settings.blink.blink.cameras.values()
        if controls.capabilities(str(camera.product_type or ""))[setting]
    ]
