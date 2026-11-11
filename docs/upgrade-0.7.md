# Upgrading to 0.7 (local cameras and the shared pipeline)

Nothing to do for a working Blink setup: entities, entity ids, events, actions and the HTTP API keep their
names. What changes, and what to check:

## Update the Blink Vision add-on with the integration

Blink Vision 0.2 requires a token on every call except `/health`. Update both, then:

- **Home Assistant OS**: the add-on announces itself on start; accept **Blink Vision** under
  **Settings → Devices & services → Discovered**. Until you do, smart detection is off (the add-on refuses
  calls without the token).
- **Add-on elsewhere (Docker)**: set `BLINK_VISION_TOKEN`, then either add **Blink Instant Events → Blink
  Vision address**, or put the token in the Blink account's **Configure → Settings → Blink Vision token**
  next to the address you already had.

Integration 0.7 also works with Blink Vision 0.1 (no token) in the meantime.

## What's new on Blink cameras

New entities per camera: `binary_sensor.<camera>_person`, `_known_person`, `_unknown_person`,
`_uncertain_identity`, `_vehicle`, `_pet` and `sensor.<camera>_last_detection`. They are additive.

## Behaviour changes

- **Known means confirmed.** A name now counts only after matching in two frames, or once with a strong
  match (similarity ≥ 0.5). Weaker matches are reported in `uncertain` ("Possibly Rohan") and make
  `known_only` false. An automation that stays quiet on `known_only` may now notify a little more often;
  that is intended.
- **Detection events** keep every previous attribute and add `identity`, `uncertain`, `matches`,
  `confidence`, `provider`, `source`, `timestamp` and `detection_id` (`clip_id` stays, with the same value).
- **Signed image links** in detection events last 24 hours (were 2 days), since they are stored in history.
- **Face pictures** (`GET /api/blink_instant_events/people/<name>.jpg`) are admin-only, like teaching and
  forgetting. Listing names is unchanged.
- **Live view** verifies Blink's TLS certificate. If Blink's live-view server ever fails verification,
  live view (and the live look after motion) stops with "failed certificate verification" instead of
  connecting insecurely; please report it.
- **More than one entry.** The integration now holds a Blink account (at most one) plus any number of
  local cameras and a Blink Vision address. Old entries are Blink accounts; no migration step runs.

## For developers

The Blink code moved to `custom_components/blink_instant_events/providers/blink/`; the shared detection
code is in `ai/`; local cameras are `providers/rtsp`, `providers/camera` and `providers/local`. Patch paths
in tests changed accordingly (e.g. `providers.blink.liveview`).
