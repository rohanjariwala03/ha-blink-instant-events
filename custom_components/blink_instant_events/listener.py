"""FCM push listener: keeps the MCS connection up and turns Blink pushes into events."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import json
import logging
import time
from typing import Any

from aiohttp import ClientError
from firebase_messaging import FcmPushClient, FcmPushClientRunState

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util, slugify

from .api import BlinkAccount, BlinkAuthError
from .const import (
    CONF_FCM,
    DOORBELL_TYPES,
    EVENT_DOORBELL,
    EVENT_MOTION,
    FCM_CONFIG,
    MOTION_TYPES,
    SIGNAL_CONNECTION,
    SIGNAL_EVENT,
)

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(minutes=1)
REASSOCIATE_SECONDS = 12 * 3600  # also keeps the Blink refresh token in use
MAX_BACKOFF_SECONDS = 1800
STALE_SECONDS = 300  # pushes queued while HA was offline are not "now" anymore
NESTED_KEYS = ("data", "payload", "message", "notification")
ID_KEYS = ("camera_id", "doorbell_id", "owl_id", "device_id", "camera")
NAME_KEYS = ("camera_name", "device_name", "camera")
ATTR_KEYS = ("network_id", "created_at", "category", "media_id", "clip_id", "video_id", "thumbnail")
DEAD_STATES = {FcmPushClientRunState.STOPPING, FcmPushClientRunState.STOPPED}


def flatten(data: dict[str, Any]) -> dict[str, Any]:
    """Merge JSON nested in data/payload/message/notification into one dict."""
    flat = dict(data)
    for key in NESTED_KEYS:
        value = flat.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            flat |= flatten(value)
    return flat


def classify(payload: dict[str, Any]) -> str | None:
    """Map Blink's event_type to motion, doorbell_press or the raw type."""
    raw = str(payload.get("event_type") or "").strip().lower()
    if raw in DOORBELL_TYPES:
        return EVENT_DOORBELL
    if raw in MOTION_TYPES:
        return EVENT_MOTION
    return slugify(raw) or None


class BlinkPushListener:
    """Owns the FCM client and the Blink association for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        account: BlinkAccount,
        devices: list[dict[str, Any]],
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.account = account
        self.devices = {dev["id"]: dev for dev in devices}
        self.last_event: dict[str, Any] | None = None
        self._client: FcmPushClient | None = None
        self._seen: deque[str] = deque(maxlen=200)
        self._lock = asyncio.Lock()
        self._associated_at = 0.0
        self._failures = 0
        self._next_try = 0.0

    @property
    def connected(self) -> bool:
        """Whether the push channel is up and associated with Blink."""
        return bool(self._associated_at and self._client and self._client.is_started())

    async def async_start(self) -> None:
        """Connect once (errors propagate to setup), then supervise."""
        await self._async_connect()
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._async_check, CHECK_INTERVAL))

    async def async_stop(self) -> None:
        """Stop the FCM client."""
        if self._client:
            await self._client.stop()

    async def _async_connect(self) -> None:
        if self._client:
            await self._client.stop()
        self._associated_at = 0.0
        self._client = FcmPushClient(
            self._on_push,
            FCM_CONFIG,
            self.entry.data.get(CONF_FCM),
            self._save_credentials,
            http_client_session=async_get_clientsession(self.hass),
        )
        token = await self._client.checkin_or_register()
        await self._client.start()
        # Associate on every connect: a re-registration yields a new token.
        await self.account.async_associate(token)
        self._associated_at = time.monotonic()

    async def _async_check(self, _now: datetime | None = None) -> None:
        """Reconnect with backoff when the client died; re-associate periodically."""
        if self._lock.locked():
            return
        async with self._lock:
            try:
                if self._client and self._client.run_state not in DEAD_STATES:
                    if time.monotonic() - self._associated_at > REASSOCIATE_SECONDS:
                        await self.account.async_associate(self._client.credentials["fcm"]["registration"]["token"])
                        self._associated_at = time.monotonic()
                elif time.monotonic() >= self._next_try:
                    await self._async_connect()
                    self._failures = 0
            except BlinkAuthError:
                _LOGGER.warning("Blink session expired; sign in again")
                self.entry.async_start_reauth(self.hass)
            except (ClientError, TimeoutError, RuntimeError) as err:
                self._failures += 1
                delay = min(60 * 2**self._failures, MAX_BACKOFF_SECONDS)
                self._next_try = time.monotonic() + delay
                _LOGGER.warning("Blink push reconnect failed (%s); retry in %ss", err, delay)
            async_dispatcher_send(self.hass, SIGNAL_CONNECTION.format(self.entry.entry_id))

    @callback
    def _save_credentials(self, credentials: dict[str, Any]) -> None:
        self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_FCM: credentials})

    def _match(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        for key in ID_KEYS:
            if (dev := self.devices.get(str(payload.get(key)))) is not None:
                return dev
        names = {str(payload[key]).casefold() for key in NAME_KEYS if payload.get(key)}
        return next(
            (dev for dev in self.devices.values() if dev["name"].casefold() in names),
            None,
        )

    @callback
    def _on_push(self, data: dict[str, Any], persistent_id: str, _ctx: Any = None) -> None:
        payload = flatten(data)
        unique_id = str(payload.get("unique_id") or persistent_id)
        if unique_id in self._seen:
            return
        self._seen.append(unique_id)

        if payload.get("unique_id") and str(payload.get("return_receipt")).lower() in ("1", "true", "yes"):
            self.entry.async_create_background_task(
                self.hass, self._async_receipt(unique_id), "blink_instant_events receipt"
            )

        event_type = classify(payload)
        device = self._match(payload)
        self.last_event = {
            "received_at": dt_util.utcnow().isoformat(),
            "event_type": event_type,
            "matched": device is not None,
            "payload_keys": sorted(payload),
        }
        if event_type is None or device is None:
            _LOGGER.debug("Ignoring Blink push with keys %s", sorted(payload))
            return
        created = dt_util.parse_datetime(str(payload.get("created_at") or ""))
        if created and (dt_util.utcnow() - created).total_seconds() > STALE_SECONDS:
            _LOGGER.debug("Ignoring stale Blink %s push from %s", event_type, created)
            return

        attributes: dict[str, Any] = {
            "camera": device["name"],
            "camera_id": device["id"],
            "network_id": device["network_id"],
            "blink_event_type": payload.get("event_type"),
        }
        attributes |= {key: payload[key] for key in ATTR_KEYS if isinstance(payload.get(key), (str, int, float))}
        async_dispatcher_send(
            self.hass,
            SIGNAL_EVENT.format(self.entry.entry_id, device["id"]),
            event_type,
            attributes,
        )

    async def _async_receipt(self, unique_id: str) -> None:
        try:
            await self.account.async_send_receipt(unique_id)
        except (BlinkAuthError, ClientError, TimeoutError) as err:
            _LOGGER.debug("Blink receipt failed: %s", type(err).__name__)
