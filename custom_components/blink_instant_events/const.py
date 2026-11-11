"""Constants for Blink Instant Events."""

from __future__ import annotations

from aiohttp import ClientError

DOMAIN = "blink_instant_events"

CONF_PROVIDER = "provider"
PROVIDER_BLINK = "blink"  # Blink cameras through Blink's cloud
PROVIDER_RTSP = "rtsp"  # an RTSP stream on the local network
PROVIDER_CAMERA = "camera"  # a camera another Home Assistant integration provides (ONVIF, Generic, ...)
PROVIDER_VISION = "vision"  # where Blink Vision runs, for those not found by themselves

# Local cameras
CONF_CAMERA = "camera"
CONF_MOTION = "motion"
CONF_FPS = "fps"
CONF_WIDTH = "width"
CONF_KEYFRAMES_ONLY = "keyframes_only"
CONF_SENSITIVITY = "sensitivity"  # % of the picture that must change to count as motion
CONF_FRAMES = "frames"
CONF_COOLDOWN = "cooldown"
CONF_RECORD = "record"
CONF_PRE = "pre"
CONF_POST = "post"
CONF_KEEP_DAYS = "keep_days"
CONF_MAX_GB = "max_gb"

CONF_AUTH = "auth"
CONF_FCM = "fcm"
CONF_PUSH_AUTH = "push_auth"  # the Android-client session Blink pushes to
CONF_SNAPSHOT_AFTER_MOTION = "snapshot_after_motion"
CONF_VISION_URL = "vision_url"
CONF_VISION_TOKEN = "vision_token"
CONF_LIVE_LOOK = "live_look"  # no clips (no Blink subscription): read a few seconds of live view after a push

# What blinkpy and Blink's odd answers raise; reported as 502 (or a failed update) rather than escaping as 500.
BLINK_ERRORS = (ClientError, RuntimeError, TimeoutError, KeyError, ValueError, TypeError, OSError)


EVENT_MOTION = "motion"
EVENT_DOORBELL = "doorbell_press"
EVENT_DETECTION = "detection"  # Blink Vision looked at the clip
EVENT_RECORDING = "recording"  # a local camera's event clip was saved
MOTION_TYPES = {"cloud_motion", "cam2sm_motion", "pir", "cv_motion"}
DOORBELL_TYPES = {"button_press"}

SIGNAL_EVENT = f"{DOMAIN}_event_{{}}_{{}}"
SIGNAL_CONNECTION = f"{DOMAIN}_connection_{{}}"
SIGNAL_DETECTION = f"{DOMAIN}_detection_{{}}"
SIGNAL_MOTION = f"{DOMAIN}_motion_{{}}"  # a local camera's motion state changed
SIGNAL_STREAM = f"{DOMAIN}_stream_{{}}"  # an RTSP camera's stream came or went

POLL_SECONDS = 300
MAX_POLL_SECONDS = 3600
PUSH_REFRESH_DELAY = 30  # let Blink finish the clip and thumbnail first
MOTION_RESET_SECONDS = 30
AUTO_SNAPSHOT_MIN_SECONDS = 300  # at most one automatic snapshot per camera per 5 minutes
