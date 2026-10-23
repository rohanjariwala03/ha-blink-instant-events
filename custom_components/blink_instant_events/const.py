"""Constants for Blink Instant Events."""

from __future__ import annotations

from aiohttp import ClientError
from firebase_messaging import FcmRegisterConfig

DOMAIN = "blink_instant_events"

CONF_AUTH = "auth"
CONF_FCM = "fcm"
CONF_SNAPSHOT_AFTER_MOTION = "snapshot_after_motion"

# What blinkpy and Blink's odd answers raise; reported as 502 (or a failed update) rather than escaping as 500.
BLINK_ERRORS = (ClientError, RuntimeError, TimeoutError, KeyError, ValueError, TypeError, OSError)

# Public Firebase client identifiers of the Blink Android app (not user secrets).
FCM_CONFIG = FcmRegisterConfig(
    project_id="blink-6c1ea",
    app_id="1:351117235468:android:14fb3ef63df2a7c2",
    api_key="AIzaSyBcgwrpglzCZJPUNNeQQnpBbJAFlas_XAI",
    messaging_sender_id="351117235468",
)
APP_VERSION = "60.0 (29834864)"
CLIENT_NAME = "Home Assistant Instant Events"

EVENT_MOTION = "motion"
EVENT_DOORBELL = "doorbell_press"
MOTION_TYPES = {"cloud_motion", "cam2sm_motion", "pir"}
DOORBELL_TYPES = {"button_press"}

SIGNAL_EVENT = f"{DOMAIN}_event_{{}}_{{}}"
SIGNAL_CONNECTION = f"{DOMAIN}_connection_{{}}"

POLL_SECONDS = 300
MAX_POLL_SECONDS = 3600
PUSH_REFRESH_DELAY = 30  # let Blink finish the clip and thumbnail first
MOTION_RESET_SECONDS = 30
AUTO_SNAPSHOT_MIN_SECONDS = 300  # at most one automatic snapshot per camera per 5 minutes
