# Changelog

## 0.7.3

- First tagged release: HACS shows version numbers and release notes instead of commit ids.
- Shorter README with a **Limitations** section; entities, actions, recording, Raspberry Pi, Docker,
  security and troubleshooting moved to [docs/reference.md](docs/reference.md).

## 0.7.2

- **Fix: Blink live view (and the live look after motion) failed** with "Blink's live-view server failed
  certificate verification: self-signed certificate". Blink's live-view servers present Blink's own
  self-signed certificate (`*.immedia-semi.com`, valid until 2039), the same on every server, addressed by
  IP, so the public-CA check added in 0.6.1 could never pass. Live view now trusts exactly that certificate
  and nothing else: still verified, unlike blinkpy, which doesn't check at all. Tested against Blink's
  servers; a different self-signed certificate is refused.
- The discovered add-on card says **Camera Vision** instead of `blink_instant_events`.

## 0.7.1

- **Fix: Blink accounts failed to load in 0.7.0** with `TypeError: 'method' object is not subscriptable`.
  Moving the FCM constants dropped Blink's Firebase project from the push client's arguments, so the
  stored credentials and the save callback each landed one place off. The call now names every argument,
  and a test builds the real push client from exactly what setup passes.

## 0.7.0 — local cameras, one pipeline for every camera

**Renamed: Local Camera Vision** (was Blink Instant Events; the add-on is **Camera Vision**, was Blink Vision). Only the names you see change: the domain `blink_instant_events`, entity ids, actions, the HTTP API and the add-on slug stay, so nothing needs setting up again.

**Local cameras, no account or cloud.** Add an **RTSP camera** (address and optional login, checked by
reading one frame), or **a camera already in Home Assistant** with its motion sensor (how ONVIF cameras
come in, through Home Assistant's ONVIF integration). Nothing they do contacts Blink, Amazon or Google.

- RTSP: one managed ffmpeg per camera (reconnects with backoff, terminated on unload, frames dropped
  rather than queued while busy), motion measured on the stream, configurable fps, width, keyframes-only
  decoding, sensitivity, frames per detection and cooldown. Camera entity with live view through Home
  Assistant's stream; motion and stream-connection sensors; event entity (`motion`, `detection`,
  `recording`).
- Recording (RTSP, optional): event clips with time before and after, or continuous; copied, not
  re-encoded; kept for N days and at most N GB; pauses under 2 GB free.
- RTSP addresses are validated (rtsp/rtsps, real host) and ffmpeg may only open RTSP protocols; logins are
  stored apart from the address and scrubbed from ffmpeg's messages, logs and diagnostics.

**One smart-detection pipeline** (`ai/`) for Blink and local cameras:

- New per-camera sensors, Blink included: person, known person, unknown person, uncertain identity,
  vehicle, pet, last detection.
- A name counts as known only when confirmed (two frames, or one strong match); weaker matches are
  "possibly …" (`uncertain`) and never known; an unknown face makes the event `identity: unknown`.
- Detection events add `identity`, `uncertain`, `matches`, `confidence`, `provider`, `source`,
  `timestamp`, `detection_id`. Signed image links last 24 h (were 2 days).
- Actions: `enroll_face` and `delete_face` (admins only; from a camera or an allowed file) and
  `process_snapshot` (analyze any camera now, returns the detection). Device trigger **Recording saved**.
- Face pictures in the people API are admin-only.

**Camera Vision 0.2.0** (add-on): token on every call but `/health`, handed to Home Assistant through
Supervisor discovery (or `BLINK_VISION_TOKEN` for Docker); known / uncertain / unknown bands; enrolment
refuses no face, two faces, small, unclear or blurry faces and look-alikes of someone else (409, `force`),
skips duplicates, keeps at most 20 photos; thresholds are add-on options; `benchmark.py` times each stage.

**Blink:** live view verifies Blink's TLS certificate (0.6.1). The code moved to `providers/blink/`.
The integration now holds several entries (at most one Blink account). See
[docs/upgrade-0.7.md](docs/upgrade-0.7.md) and [docs/blink-local-feasibility.md](docs/blink-local-feasibility.md).

## 0.6.1

- **Live view checks Blink's server certificate.** blinkpy opens the live-view connection with
  certificate and hostname checks turned off, so anyone on the network path could have impersonated
  Blink's server, read the video and taken the live-view token. The connection now uses Home
  Assistant's default TLS context (system CAs, hostname or IP checked). A server that fails the check
  ends the live view with "Blink's live-view server failed certificate verification: <reason>";
  there is no insecure fallback.
- **Push no longer drops every minute or two.** firebase-messaging 0.4.5 decodes a push's crypto-key and
  salt without adding base64 padding; a push carrying them unpadded raised "Incorrect padding", the push
  client shut down, reconnected and was handed the same push again, so the Push connection sensor (and any
  alert on it) flipped off and on all day. They're now padded (and read up to any ";"), and a push that still
  can't be read is logged and skipped instead of stopping the client.
- Reloading or unloading no longer fails when the push client never started (Blink or DNS unreachable at
  setup): firebase_messaging's stop() raised on a client that had not started, which left the entry in
  "failed unload" until Home Assistant restarted.
- Blink Mini 2 (`hawk`): its light can be switched in the live-view controls, over the live view as
  in the Blink app. Outside a live view the controls say it works only there; no switch entity.
- Blink Vision 0.1.1: stops cleanly (it ignored SIGTERM as PID 1, so a stop ended in "error").
- **Smart detection without a Blink subscription**: option **Look with live view after motion**
  reads 8 seconds of live view after each push when there are no clips. A look that sees nobody
  says "Gone before the camera looked" rather than "Nothing found".
- Push events carry Blink's own `detection_type`, `detections`, `title` and `message` when Blink sends
  them.
- **Pushes now arrive.** Blink sends pushes only to clients of its Android app, and the push token
  had been registered on blinkpy's iOS-client session, so no push ever came. Pushes now use an
  Android-client session of their own, set up with **Configure → Sign in for instant events**. A
  repair asks for it until it's done; when it expires, the repair returns (it no longer asks you
  to sign in to the whole integration again).
- `cv_motion` pushes (Outdoor 4) count as motion.
- The push sign-in trades Blink's code together with the hardware id it was issued to, and logs
  Blink's reason when it refuses one.
- An icon: Home Assistant shows it for the integration (from `brand/`), and the add-on store for
  Blink Vision.
- Smart detection with the new **Blink Vision** add-on (in this repository): each new clip is
  read locally for faces (taught by name), people, vehicles and animals. Results fire as a
  `detection` event (with a signed link to the frame, boxes drawn), show in
  `image.<camera>_last_detection`, and label clips in the clips API. People API to teach and
  forget faces. A motion push starts looking for its clip right away.
- `blink_instant_events.show_on_tv`: a camera's live view on a Cast TV, e.g. when the doorbell
  rings; finds the Cast entity of a TV added through another integration.
- The push reconnect now waits out its backoff before re-pairing with Blink too (it retried every
  minute while Blink was unreachable).

- When ffmpeg stops early, the live view error now carries its exit code and its last lines.
- Tests: the fake Blink camera tolerates the client hanging up mid-send; the system ffmpeg is used
  when there is one.
- Repo: `CODEOWNERS`, `SECURITY.md` (private vulnerability reporting), Dependabot for GitHub
  Actions, a read-only workflow token, and push CI on `main` only.

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
