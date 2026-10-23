"""Polls Blink through blinkpy on the session the push listener also uses.

Refresh handling adapted from Blink Live View Proxy's proxy/blink_proxy/devices.py,
Copyright (c) 2026 Teethree89, MIT License (see NOTICE).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
import logging
from typing import Any

from blinkpy.auth import UnauthorizedError
from blinkpy.blinkpy import Blink
from blinkpy.camera import BlinkCamera
from blinkpy.sync_module import BlinkSyncModule

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    AUTO_SNAPSHOT_MIN_SECONDS,
    CONF_SNAPSHOT_AFTER_MOTION,
    DOMAIN,
    EVENT_MOTION,
    MAX_POLL_SECONDS,
    POLL_SECONDS,
    PUSH_REFRESH_DELAY,
)

_LOGGER = logging.getLogger(__name__)

# blinkpy's stand-ins for a network without a sync module; their "armed" lives on the homescreen.
SYNCLESS_TYPES = ("BlinkOwl", "BlinkLotus")
# A new snapshot's URL can arrive before the image behind it downloads (seen on a Mini).
SNAPSHOT_RETRIES = 3
SNAPSHOT_RETRY_SECONDS = 2


def busy(answer: Any) -> bool:
    """Blink answers 307 while the camera is mid-command or streaming a live view."""
    return isinstance(answer, dict) and answer.get("code") == 307


def drop_cached_videos(blink: Blink) -> None:
    """Stop blinkpy downloading every camera's newest clip on each refresh; nothing here uses it."""
    for camera in blink.cameras.values():
        camera._cached_video = b""  # noqa: SLF001


def sync_armed(sync: BlinkSyncModule) -> bool | None:
    """The network's armed state, without blinkpy's side effect of marking the module unavailable."""
    if type(sync).__name__ in SYNCLESS_TYPES:
        for network in sync.blink.homescreen.get("networks") or []:
            if str(network.get("id")) == str(sync.network_id) and isinstance(network.get("armed"), bool):
                return network["armed"]
    try:
        armed = sync.network_info["network"]["armed"]
    except (KeyError, TypeError):
        return None
    return armed if isinstance(armed, bool) else None


class BlinkCoordinator(DataUpdateCoordinator[None]):
    """Every 5 minutes, and again shortly after a push or an action. Entities read blinkpy's objects."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, blink: Blink) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_SECONDS),
        )
        self.blink = blink
        # Throttling is ours (interval + backoff); blinkpy's own refresh_rate check would skip pushes.
        blink.refresh_rate = 0
        self.poll: dict[str, Any] = {
            "state": "starting",
            "interval": POLL_SECONDS,
            "failures": 0,
            "polls": 0,
            "last_error": None,
        }
        self.last_success: datetime | None = None
        # Pending one-shot timers: "refresh", or a camera id for its snapshot after motion.
        self._timers: dict[str, CALLBACK_TYPE] = {}
        # camera id -> (last automatic snapshot, how many so far), for the battery measurement.
        self.auto_snapshots: dict[str, tuple[datetime, int]] = {}
        entry.async_on_unload(self._cancel_timers)

    async def _async_update_data(self) -> None:
        try:
            if self.blink.urls is None:
                self.blink.setup_urls()
                if not await self.blink.setup_post_verify():
                    raise RuntimeError("Blink did not list the cameras")
                self.blink.last_refresh = int(dt_util.utcnow().timestamp())
            else:
                drop_cached_videos(self.blink)
                await self.blink.refresh()
            drop_cached_videos(self.blink)
            # blinkpy swallows most request failures; a refresh where no module answered is not fresh data.
            if self.blink.sync and not any(sync.available for sync in self.blink.sync.values()):
                raise RuntimeError("Blink answered, but no sync module refreshed")
        except UnauthorizedError as err:
            self._failed("auth_failed", err)
            raise ConfigEntryAuthFailed("Blink signed this session out") from err
        except Exception as err:  # noqa: BLE001 - blinkpy raises AttributeError/KeyError on throttled or empty answers
            self._failed("backoff", err)
            raise UpdateFailed(f"Blink refresh failed: {self.poll['last_error']}") from err
        self.poll |= {"state": "ok", "failures": 0, "polls": self.poll["polls"] + 1, "last_error": None}
        self.poll["interval"] = POLL_SECONDS
        self.update_interval = timedelta(seconds=POLL_SECONDS)
        self.last_success = dt_util.utcnow()

    def _failed(self, state: str, err: Exception) -> None:
        failures = self.poll["failures"] + 1
        interval = min(POLL_SECONDS * 2**failures, MAX_POLL_SECONDS)
        self.poll |= {
            "state": state,
            "failures": failures,
            "interval": interval,
            "last_error": f"{type(err).__name__}: {err}".rstrip(": "),
        }
        self.update_interval = timedelta(seconds=interval)
        # The coordinator skips its listeners when one failure follows another; the poll sensor must still move.
        self.async_update_listeners()

    @callback
    def async_on_push(self, camera_id: str, event_type: str) -> None:
        """After a push: one refresh shortly, and optionally a new snapshot once the clip is done."""
        self._later("refresh", self.async_request_refresh)
        camera = next((cam for cam in self.blink.cameras.values() if str(cam.camera_id) == camera_id), None)
        last, count = self.auto_snapshots.get(camera_id, (None, 0))
        now = dt_util.utcnow()
        if (
            event_type != EVENT_MOTION
            or camera is None
            or not self.config_entry.options.get(CONF_SNAPSHOT_AFTER_MOTION)
            or (last and (now - last).total_seconds() < AUTO_SNAPSHOT_MIN_SECONDS)
        ):
            return
        self.auto_snapshots[camera_id] = (now, count + 1)

        async def _snap() -> None:
            try:
                await self.async_snap(camera)
            except Exception as err:  # noqa: BLE001 - best effort; the next poll reports real trouble
                _LOGGER.info("No snapshot after motion on %s: %s", camera.name, err)

        self._later(camera_id, _snap)

    @callback
    def _later(self, key: str, job: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Run `job` once, PUSH_REFRESH_DELAY from now; a burst of pushes collapses into one run."""
        if cancel := self._timers.pop(key, None):
            cancel()

        @callback
        def _run(_now: datetime) -> None:
            self._timers.pop(key, None)
            self.config_entry.async_create_background_task(self.hass, job(), f"blink_instant_events {key}")

        self._timers[key] = async_call_later(self.hass, PUSH_REFRESH_DELAY, _run)

    @callback
    def _cancel_timers(self) -> None:
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    def camera(self, serial: str) -> BlinkCamera | None:
        """The blinkpy camera with this serial (blinkpy may rebuild its objects)."""
        return next((cam for cam in self.blink.cameras.values() if cam.serial == serial), None)

    def syncs(self) -> dict[str, BlinkSyncModule]:
        """One sync module per network; a sync-less network has a blinkpy entry per camera."""
        found: dict[str, BlinkSyncModule] = {}
        for sync in self.blink.sync.values():
            found.setdefault(str(sync.network_id), sync)
        return found

    async def async_refresh_camera(self, camera: BlinkCamera) -> None:
        """Re-read one camera after acting on it, instead of refreshing everything."""
        await self.blink.get_homescreen()
        sync = camera.sync
        info = await sync.get_camera_info(camera.camera_id, unique_info=sync.get_unique_info(camera.name))
        drop_cached_videos(self.blink)
        await camera.update(info, force_cache=False, expire_clips=False)
        drop_cached_videos(self.blink)
        self.async_update_listeners()

    async def async_snap(self, camera: BlinkCamera) -> None:
        """Have the camera take a new picture (wakes it: battery) and wait until it can be downloaded."""
        before = camera._cached_image  # noqa: SLF001
        await self.async_action("take a new snapshot", camera.snap_picture)
        # snap_picture downloads the thumbnail it already knew; the new URL arrives with the camera's next state.
        await self.async_refresh_camera(camera)
        for _ in range(SNAPSHOT_RETRIES):
            if camera._cached_image and camera._cached_image != before:  # noqa: SLF001
                break
            await asyncio.sleep(SNAPSHOT_RETRY_SECONDS)
            media = await camera.get_media()
            if media is not None and media.status == 200:
                camera._cached_image = await media.read()  # noqa: SLF001
        self.async_update_listeners()
        if camera._cached_image == before:  # noqa: SLF001
            raise HomeAssistantError("Blink took the picture, but it is not downloadable yet; try again shortly")

    async def async_action(self, what: str, call: Callable[[], Any]) -> Any:
        """Run one Blink command, turning Blink's refusals into errors the UI shows."""
        try:
            answer = await call()
        except UnauthorizedError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"Could not {what}: Blink signed this session out") from err
        except TimeoutError as err:
            raise HomeAssistantError(f"Could not {what}: Blink did not answer") from err
        if busy(answer):
            raise HomeAssistantError(f"Could not {what}: the camera is busy, usually because a live view is open")
        if not answer:
            raise HomeAssistantError(f"Could not {what}: Blink did not accept the command")
        return answer
