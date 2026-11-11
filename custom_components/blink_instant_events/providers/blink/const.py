"""Constants only the Blink provider uses."""

from __future__ import annotations

from firebase_messaging import FcmRegisterConfig

# Public Firebase client identifiers of the Blink Android app (not user secrets).
FCM_CONFIG = FcmRegisterConfig(
    project_id="blink-6c1ea",
    app_id="1:351117235468:android:14fb3ef63df2a7c2",
    api_key="AIzaSyBcgwrpglzCZJPUNNeQQnpBbJAFlas_XAI",
    messaging_sender_id="351117235468",
)
APP_VERSION = "60.0 (29834864)"
CLIENT_NAME = "Home Assistant Instant Events"
