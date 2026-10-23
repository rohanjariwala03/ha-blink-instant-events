# Changelog

## 0.5.0 — camera settings, offline cameras, clips in the Media panel

- Camera settings as entities, read from Blink's cloud every 30 minutes and right after a change
  (reading never wakes a camera), written through the same code as the live view's settings
  sheet: `select.<camera>_night_vision` (auto/on/off) on every camera, and on the models that
  have them `switch.<camera>_flood_light`, `number.<camera>_flood_light_brightness` and
  `number.<camera>_speaker_volume`. A change Blink calls busy raises an error; one made during a
  live view is held and written when it ends.
- `binary_sensor.<camera>_connection`: whether Blink can reach the camera. While Blink calls a
  camera offline, its other entities are unavailable instead of showing stale values.
- Clips in Home Assistant's Media panel (Media → Blink clips): a folder per camera and one for all
  of them, the last 7 days of cloud and Sync Module clips, newest first, with thumbnails, played
  in place through the clips API.
- Tests: the arming test no longer flakes (its listener ran on a worker thread).

## 0.4.1 — faster, steadier live view

- Live view no longer loses the start of the stream: video Blink sent before ffmpeg had connected
  was dropped, and with it the opening keyframe, so ffmpeg waited for the next one (~4 s) or failed
  with "Invalid data found when processing input". It is now held until ffmpeg connects.
- ffmpeg is told the input is MPEG-TS instead of guessing it.
- The HLS video is re-encoded (libx264, veryfast, zero latency) with a keyframe every second, so
  every 1 s segment starts with one (`EXT-X-INDEPENDENT-SEGMENTS`). Blink sends a keyframe only
  every ~4 s, and a player can only start at one: copying kept phones 5-10 s behind and slow to
  start. About 0.9 Mbit/s; the saved last live view stays Blink's original video. Without libx264
  the video is copied and cut by time as before.
- Debug logging times each live view start (Blink's answer, first video, first keyframe, first
  segment) and logs ffmpeg's own steps.
- Fixed the connection sensor reading "off" for the first minute after a restart.

## 0.4.0 — live view and two-way talk

Ported from Blink Live View Proxy into the integration: no add-on needed.

- Live view over HLS, under `/api/blink_instant_events/cameras/<slug>/`: `token` (bearer auth,
  10-minute camera token + session id), `hls/index.m3u8` (first fetch starts the camera),
  `hls/segment_NNNNN.ts`, `stop`. One Blink session per camera, shared by its viewers; cameras
  stream independently. Idle sessions end after 15 s without a request.
- Push-to-talk WebSocket `ptt` on the viewer's session (s16le mono PCM in, 16 kHz AAC to the
  camera). Starts ~5 s sooner than the proxy: ffmpeg no longer probes the microphone input.
- Camera controls `controls` (GET/POST): night vision, flood light, speaker volume, temperature in
  Home Assistant's unit; `rejected`/`busy`/`pending`/`deferred` for changes that did not land, with
  busy ones written after the live view ends.
- `last-liveview` (`{available, ended_at}`) and `last-liveview.mp4`: the last session of each
  camera, kept under `.cache/`.
- Live camera entity per camera (`camera.<name>_live_view`) with `proxy_slug`, `blink_camera_id`,
  `network_id`, `camera_type`, `product_type`, `ptt_supported`.
- Token renewal survives a cancelled request (an app hanging up can't lose the rotated token),
  and so does stopping a live view. Blink errors answer 502/503 (503 also starts reauth), not 500.
- One Blink account per Home Assistant (`single_config_entry`).
- Not ported: Blink's RTSP families (XT, White), the MPEG-TS route, the browser player and panel.

## 0.3.0 — snapshots and clips

- Snapshot camera per camera (`camera.<name>`): the latest Blink thumbnail, with `snapshot_of`
  (the API slug), `snapshot_id`, `snapshot_taken`, `last_record`.
- `button.<name>_refresh_snapshot`: asks the camera for a new picture (wakes it).
- Option **Take a snapshot after each motion** (off by default): one picture about 30 s after a
  motion push, at most once per camera every 5 minutes. Counts are in diagnostics.
- Clips API with Home Assistant bearer auth: `GET /api/blink_instant_events/clips` lists cloud and
  Sync Module (USB) clips; `/clips/<id>.mp4` and `/clips/<id>.jpg` fetch a clip once from Blink,
  cache it under `.cache/` (256 MB, oldest first, not in backups) and serve it with byte ranges.
- Depends on Home Assistant's `ffmpeg` integration (for clip thumbnails).

## 0.2.0 — device state and control

One Blink session now serves both push and polling: the integration loads your cameras through
blinkpy and polls every 5 minutes (backing off to an hour while Blink fails), plus once 30 s after
a push and right after every command.

- Alarm panel per Sync Module network (`alarm_control_panel.blink_<network>`), with a transient
  `arming`/`disarming` state until Blink confirms; attribute `status`.
- Motion detection switch per camera.
- Battery binary sensor (attributes `battery_state`, `battery_level`), battery voltage,
  temperature (°C) and Wi-Fi signal (dBm, or bars on models that report bars) sensors.
- Sync Module connectivity binary sensor.
- `sensor.blink_instant_events_last_blink_refresh`: last successful poll, with `state`
  (`ok`/`backoff`/`auth_failed`), `interval`, `failures`, `polls`, `last_error`.
- Motion binary sensor per camera, driven by push: on for 30 s per motion push; `last_record`.
- Token renewal is serialised and every rotated token is stored, so the session survives restarts.

## 0.1.0

- Instant motion and doorbell events over Blink's FCM push.
