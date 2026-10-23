# Changelog

## Unreleased

- Live view no longer drops the camera's first frames when they arrive before ffmpeg connects
  (it then had to wait for the next keyframe); ffmpeg's own error is shown when it stops early.

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
