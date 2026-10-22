"""Constants for Blink Instant Events."""

from __future__ import annotations

from firebase_messaging import FcmRegisterConfig

DOMAIN = "blink_instant_events"

CONF_AUTH = "auth"
CONF_FCM = "fcm"

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
