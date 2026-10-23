# Blink Instant Events

Blink cameras in Home Assistant with instant events: motion and doorbell presses arrive the moment
Blink pushes them, plus arm/disarm, motion switches, battery and climate sensors, snapshots, clips,
live view with two-way talk, and camera controls — one Blink sign-in, no add-on, no Docker.

Home Assistant's Blink integrations poll Blink's cloud, so motion shows up minutes late and
doorbell presses don't show up at all. The Blink phone app is instant because Blink pushes events
through Firebase Cloud Messaging (FCM). This integration signs in to Blink as its own app client,
registers an FCM token, and keeps a push connection open inside Home Assistant. The same session
polls Blink every 5 minutes for everything else.

> **Unofficial.** Not affiliated with Blink, Amazon, Google or Home Assistant. It relies on
> reverse-engineered behaviour of the Blink app and can break whenever Blink changes it.

## Requirements

- Home Assistant 2026.10 or newer (its `ffmpeg`, used for live view and clip thumbnails, is built in)
- A Blink account (email + password + the PIN Blink texts/emails you); one account per Home Assistant

## Install

1. HACS → ⋮ → **Custom repositories** → add this repository's URL, category **Integration**.
2. Download **Blink Instant Events**, restart Home Assistant.
3. **Settings → Devices & services → Add integration → Blink Instant Events**.
4. Enter your Blink email and password, then the PIN Blink sends. The password is used once and
   not stored; if Blink later revokes the session, Home Assistant asks you to sign in again.

## What you get

Per camera (`<camera>` = its name, e.g. `front_door`):

| Entity | Description |
| --- | --- |
| `event.<camera>_blink_event` | Instant events: `motion`, `doorbell_press` (doorbells), or Blink's raw type (e.g. `camera_offline`). |
| `binary_sensor.<camera>_motion` | On for 30 s after each motion push. `last_record` attribute. |
| `camera.<camera>` | Latest Blink snapshot. |
| `camera.<camera>_live_view` | Live-view details for apps (the stream is the HTTP API below). |
| `button.<camera>_refresh_snapshot` | Take a new snapshot (wakes the camera). |
| `switch.<camera>_motion_detection` | The camera's motion detection. |
| `binary_sensor.<camera>_battery`, `sensor.<camera>_battery_voltage`, `sensor.<camera>_temperature`, `sensor.<camera>_wifi_signal` | Battery and health, where the model reports them. |

Per Sync Module network: `alarm_control_panel.blink_<network>` (arm away/disarm) and
`binary_sensor.blink_<network>_connection`. Account: `sensor.blink_instant_events_last_blink_refresh`
(poll health, `state` = `ok`/`backoff`/`auth_failed`) and
`binary_sensor.blink_instant_events_push_connection`.

The automation editor offers device triggers **Motion detected** and **Doorbell pressed**.

```yaml
triggers:
  - trigger: state
    entity_id: event.front_door_blink_event
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.front_door_blink_event
    attribute: event_type
    state: doorbell_press
actions:
  - action: notify.mobile_app_phone
    data:
      message: Someone is at the front door
```

Motion pushes only arrive when the Blink app itself would notify you: the system is armed, the
camera's motion detection is on, and notifications are enabled.

**HTTP API** (for apps; HA bearer auth or a short-lived camera token): clips list and download,
HLS live view, push-to-talk WebSocket, camera controls (night vision, flood light, volume) and the
last live view as MP4. See [docs/app-api.md](docs/app-api.md). Moving from Blink Live View Proxy:
[docs/migration.md](docs/migration.md).

**Option:** *Take a snapshot after each motion* (Configure on the integration). Off by default.

## Smart detection (optional): who and what is in each clip

Install the **Blink Vision** add-on from this repository (Settings › Add-ons › Add-on store › ⋮ ›
Repositories › add `https://github.com/rohanjariwala03/ha-blink-instant-events`) and start it. The
integration finds it by itself (or set *Blink Vision URL* under Configure if it runs elsewhere).

Every new clip is then read on your own hardware: frames one second apart go through YuNet and
SFace (faces) and YOLOX-S (people, vehicles, animals) from the OpenCV Model Zoo. Nothing leaves
your network. A push starts looking for the clip at once (Blink usually lists it 15–40 s after the
motion); without pushes, the next poll that lists a new clip does.

- `event.<camera>_blink_event` fires `detection` with `summary` ("Rohan and an unknown person",
  "A vehicle", …), `people`, `unknown_faces`, `labels` (`person`/`vehicle`/`animal`), `known_only`
  (only faces you taught it) and `image`, a signed link to the frame with boxes drawn (for
  notifications; valid 2 days).
- `image.<camera>_last_detection`: that frame for the newest clip, with the same attributes.
- Clips listed by the HTTP API carry their `detection`.
- Teach faces with `POST /api/blink_instant_events/people/<name>` (a JPEG with that face; admins
  only; several photos from different angles help). See [docs/app-api.md](docs/app-api.md).

It analyzes clips from after it started (plus 15 minutes); results are kept 7 days. A face needs
to be at least ~32 px tall to be told apart, so a camera mounted high mostly reports "a person".

## Show a camera on the TV

`blink_instant_events.show_on_tv` plays a camera's live view on a Cast TV or Chromecast for
`duration` seconds (default 60), by default only if the TV is on. Point it at the TV's own entity
and it uses the Cast entity on the same TV. For example, when the doorbell rings:

```yaml
triggers:
  - trigger: state
    entity_id: event.front_door_blink_event
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.front_door_blink_event
    attribute: event_type
    state: doorbell_press
actions:
  - action: blink_instant_events.show_on_tv
    data:
      camera: camera.front_door
      media_player: media_player.living_room_tv
```

## Battery

Cameras are never woken on a timer. They wake for: live view (for as long as it runs; it stops on
request or 15 s after the player stops asking), a snapshot you request, the optional snapshot after
motion (at most once per camera every 5 minutes, about 30 s after the motion push), and camera
control changes. Polling every 5 minutes reads Blink's cloud, not the cameras.

## Privacy

Tokens are stored in the config entry and never logged; live-view tokens and stream addresses are
not logged either. Diagnostics redact email, tokens, account and client ids, FCM credentials,
serial numbers and media URLs. Clips and the last live view of each camera are kept under
`.cache/blink_instant_events/` (clips capped at 256 MB, oldest first), which Home Assistant backups
skip.

## Risks

- Blink can change its push registration, app-version checks, live-view protocol or Firebase project
  at any time; any of the features can stop working without notice.
- Live view connects to Blink's stream server the way blinkpy does, without verifying its TLS
  certificate.
- Live view and snapshots cost camera battery.
- This adds a signed-in client to your Blink account; Blink may email you about the new sign-in.
- Automated access may conflict with Blink's terms of service. Use at your own risk.

## Credits

- [Teethree89/ha-blink-live-view-proxy](https://github.com/Teethree89/ha-blink-live-view-proxy) (MIT) —
  live view, talk, camera controls, clips and device-state code ported from it.
- [CoreyHayward/ha-blink-live-events](https://github.com/CoreyHayward/ha-blink-live-events) (MIT) —
  the push protocol research this integration builds on.
- [blinkpy](https://github.com/fronzbot/blinkpy) (MIT) — Blink sign-in, devices and livestream.
- [OpenCV Model Zoo](https://github.com/opencv/opencv_zoo) — YuNet (MIT), SFace (Apache-2.0) and
  YOLOX-S (Apache-2.0) models, downloaded when the Blink Vision add-on is built.
- [firebase-messaging](https://github.com/sdb9696/firebase-messaging) (MIT) — FCM registration and
  MCS client, also used by Home Assistant's Ring integration.

See [NOTICE](NOTICE). Protocol notes: [docs/protocol.md](docs/protocol.md).

## License

MIT
