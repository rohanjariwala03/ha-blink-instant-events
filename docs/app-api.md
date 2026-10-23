# App API

Everything Blink Instant Events exposes for the iPhone app: entities, attributes and HTTP endpoints.
Shapes match Blink Live View Proxy wherever the proxy had them, so switching is mostly a path change
(`/api/blink_liveview_proxy/` → `/api/blink_instant_events/`). Differences are called out.

`<camera>` below is the camera's device name as an entity id (`front_door`, `outdoor`,
`outdoor_4_gl39`); `<slug>` is the same text in URLs and attributes, derived from the Blink camera
name. They match unless you rename a device in Home Assistant.

## Entities

### Per camera

| Entity | State | Attributes |
| --- | --- | --- |
| `camera.<camera>` | snapshot camera (latest Blink thumbnail) | `snapshot_of` (= slug), `snapshot_id` (changes with the picture), `snapshot_taken` (ISO time Blink took it, or null), `last_record`, `entity_picture`, `access_token` |
| `camera.<camera>_live_view` | live-view facts; picture = snapshot | `proxy_slug` (= slug), `blink_camera_id`, `network_id`, `camera_type` (`doorbell` / `default` / `mini`), `product_type` (e.g. `tulip`, `sedona`), `ptt_supported` |
| `binary_sensor.<camera>_motion` | `on` for 30 s after each motion **push** | `last_record`: the later of the last motion push and the last clip Blink listed |
| `binary_sensor.<camera>_battery` | `on` = low | `battery_state` (`ok`/`low`), `battery_level` |
| `sensor.<camera>_battery_voltage` | volts | — |
| `sensor.<camera>_temperature` | °C (display precision 0) | — |
| `sensor.<camera>_wifi_signal` | dBm, or 0–5 bars on models that report bars (no unit then) | — |
| `switch.<camera>_motion_detection` | motion detection on/off | — |
| `button.<camera>_refresh_snapshot` | press: camera takes a new picture (wakes it) | — |
| `event.<camera>_blink_event` | instant event | `event_type` (`motion`, `doorbell_press`, or Blink's raw type), `camera`, `camera_id`, `network_id`, `blink_event_type`, `created_at`, … |

Sensors a camera doesn't report (the doorbell's temperature, for one) are not created.
`camera_type` on the live camera is how to tell the doorbell apart (`doorbell`).

### Per Sync Module network

| Entity | State | Attributes |
| --- | --- | --- |
| `alarm_control_panel.blink_<network>` (`blink_home`) | `armed_away` / `disarmed`, and transient `arming` / `disarming` while the command runs | `status` (`online`/`offline`), `changed_by`, `code_format`, `code_arm_required` |
| `binary_sensor.blink_<network>_connection` | `on` while Blink can reach the module | — |

Arm with `alarm_control_panel.alarm_arm_away`, disarm with `alarm_control_panel.alarm_disarm`.
An offline module keeps its last arm state; check `status` or the connection sensor.

### Account

| Entity | State | Attributes |
| --- | --- | --- |
| `sensor.blink_instant_events_last_blink_refresh` | time of the last successful poll | `state` (`starting`/`ok`/`backoff`/`auth_failed`), `interval` (s), `failures`, `polls`, `last_error` |
| `binary_sensor.blink_instant_events_push_connection` | `on` while push is connected | — |

Show "Blink signed out" when `state` is `auth_failed`. The fix is Home Assistant's normal reauth:
Settings → Devices & services shows Blink Instant Events with a **Re-authenticate** prompt; sign in
and enter the PIN. Everything recovers on its own after it. There is no add-on to restart.
Polling is every 5 minutes (`interval` doubles up to 3600 s while Blink fails) plus once 30 s after
each push; motion itself no longer waits for a poll.

## HTTP

Base: `http://<ha>:8123`. "Bearer" means `Authorization: Bearer <HA long-lived token>`.
"Token" means `?token=<camera token>` from the token endpoint; bearer works there too.
Errors are plain text with the HTTP status. A bad or expired camera token is **403** (never 401, so
Home Assistant does not count it as a failed login and ban the phone's IP).

### Clips (bearer)

`GET /api/blink_instant_events/clips?source=both|local|cloud&hours=720&limit=100&pages=10[&camera=<slug>]`

```json
{
  "count": 1,
  "source": "both",
  "hours": 720,
  "clips": [
    {
      "id": "7bc082072d76759f6e7e5f41",
      "source": "cloud",
      "slug": "front_door",
      "camera_name": "Front Door",
      "created_at": "2026-10-22T18:03:11+00:00",
      "size": 1234567,
      "download_url": "/api/blink_instant_events/clips/7bc0…f41.mp4?hours=720.0&pages=10&limit=100&source=cloud&camera=front_door",
      "thumbnail_url": "/api/blink_instant_events/clips/7bc0…f41.jpg?…",
      "cached": false
    }
  ]
}
```

Limits: `hours` 0.1–720, `pages` 1–10 (cloud pages of ~25), `limit` 1–100. Newest first.
`local` clips need a USB drive in the Sync Module; `cloud` clips need a Blink subscription.

`GET <download_url>` → `video/mp4`; `GET <thumbnail_url>` → `image/jpeg` (first frame). The first
request fetches the clip from Blink (several seconds; one at a time), later ones come from disk and
answer byte ranges. `cached: true` in the listing means the thumbnail is already on disk.

### Live view, under `/api/blink_instant_events/cameras/<slug>/`

| Method + path | Auth | Answer |
| --- | --- | --- |
| `GET` or `POST token` | bearer | `{"token": "...", "expires_in": 600, "session": "..."}`. The token is scoped to this camera and renewed on every use. The app may keep generating its own `session` id. |
| `GET hls/index.m3u8?token=&session=` | token | HLS playlist (`application/vnd.apple.mpegurl`). The first fetch wakes the camera and waits for video (up to 30 s). Segment lines already carry `?token=`. 502 with a reason if Blink refuses or the camera sends nothing. |
| `GET hls/segment_NNNNN.ts?token=` | token | `video/mp2t` |
| `POST stop?token=` | token | `{"stopped": true, "slug": "..."}`. Frees the camera now; otherwise it stops 15 s after the last playlist/segment request. |
| `GET ptt?token=&session=` (WebSocket) | token | Push-to-talk, see below. 400 if `ptt_supported` is false, 409 if that session has no live view running. |
| `GET controls?token=&session=` | token | Controls state, see below. |
| `POST controls?token=&session=` | token | Body: any of `{"night_vision": "auto"|"on"|"off", "flood_light": bool, "volume": 1-8, "brightness": 1-10, "light_settings": {…}}`. Answers the new state plus `rejected`, `busy`, `pending`, `deferred`. 400 for a control the camera lacks. |
| `GET last-liveview` | token or bearer | `{"available": true, "slug": "...", "ended_at": "2026-10-22T18:10:00+00:00", "bytes": 1234}` or `{"available": false, "slug": "..."}` |
| `GET last-liveview.mp4` | token or bearer | `video/mp4` of the camera's last finished live view. 404 if none yet. |

One Blink session per camera, shared by everyone watching that camera (a second viewer joins the
running stream; `stop` ends it for both). Different cameras stream at the same time.

**Push-to-talk** rides the live view opened with the same `session`. Send text
`{"type":"start","sampleRate":48000}`, then binary frames of signed 16-bit little-endian mono PCM
at that rate, then `{"type":"stop"}`. The server answers `{"type":"started"}`,
`{"type":"listening","frames":1}` once audio reaches the camera, `{"type":"stopped"}`, or
`{"type":"error","message":"..."}`. This is exactly what `modules/mic-stream` sends.

**Controls** state:

```json
{
  "slug": "outdoor", "name": "Outdoor", "product_type": "sedona",
  "capabilities": {"flood_light": false, "night_vision": true, "brightness": false,
                   "temperature": true, "volume": false, "light_settings": false},
  "flood_light": null, "night_vision": "auto", "brightness": null, "volume": null,
  "temperature_f": 68, "temperature": 20, "temperature_unit": "°C",
  "light_settings": null, "deferred": []
}
```

`temperature` is in Home Assistant's unit. After a POST: `rejected` (Blink refused), `busy`
(camera busy, try later), `pending` (accepted, not reflected yet), `deferred` (will be written when
the live view ends; the next GET carries `deferred_result`). The flood light goes over the live view
itself while one is open.

### Not carried over from the proxy

`/cameras/<slug>/mpegts`, `/player`, `/clips/viewer`, `/snapshot-refresh` (use
`button.<camera>_refresh_snapshot`), `/last-liveview.ts`, the auth panel and dashboard routes.
Cameras Blink streams over RTSP (XT, White) are not supported for live view.
