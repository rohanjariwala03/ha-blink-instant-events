# Reference

Everything the [README](../README.md) leaves out: setup details, every entity and action, recording,
Raspberry Pi and Docker notes, security and troubleshooting.

## An RTSP camera (no account anywhere)

Choose **RTSP camera**: a name, the stream address (e.g. `rtsp://192.168.1.20:554/stream2`), and its
login if it has one. One frame is read to check the address and login before it is saved; the login is
stored apart from the address and never logged.

Use the camera's **sub-stream** if it has one (often `/stream2`, `…/102`, `subtype=1`): detection needs
little resolution and a Raspberry Pi decodes it far faster. **Configure** sets how it is read
(frames per second, frame width, *decode keyframes only* for the lowest CPU), motion sensitivity,
frames per detection, the cooldown between detections, and recording.

## An ONVIF camera

Add it with Home Assistant's own **ONVIF** integration first: it discovers ONVIF cameras on your network,
reads the device information and stream profiles, picks the RTSP address, and turns the camera's motion
events into a binary sensor (where the camera supports ONVIF events; not all do). Then add **A camera
already in Home Assistant** here, and pick that camera and its motion sensor. Generic Camera, Reolink,
UniFi Protect and other camera integrations work the same way.

If an ONVIF camera has no motion events, add its RTSP address as an **RTSP camera** instead: motion is
then measured on the stream itself.

## A Blink account

Choose **Blink account (cloud)**, enter your Blink email and password, then the PIN Blink sends. The
password is used once and not stored; if Blink later revokes the session, Home Assistant asks you to
sign in again. For instant events: **Configure → Sign in for instant events**. Blink pushes only to its
Android app, so this signs in once more, as that app. Use Blink's own page, on a computer rather than a
phone with the Blink app. When the sign-in finishes, paste the address it ends on
(`https://applinks.blink.com/signin/callback?…`) into the form. Until you do, a repair reminds you, and
motion only shows up from polling.

## Entities

Every camera, local or Blink (`<camera>` = its name, e.g. `front_door`). Home Assistant may put the
area first: a camera named Outdoor in the area Cameras becomes `camera.cameras_outdoor`. Check
**Settings → Entities** for the real ids before writing automations.

On every camera:

| Entity | Description |
| --- | --- |
| `binary_sensor.<camera>_person` | On for 60 s after a detection with a person in it. |
| `binary_sensor.<camera>_known_person` | Someone you taught it was recognized (confirmed in 2 frames, or 1 strong match). |
| `binary_sensor.<camera>_unknown_person` | An unknown face, or a person whose face couldn't be matched (from behind counts). |
| `binary_sensor.<camera>_uncertain_identity` | A face that only possibly matches someone; never counted as known. |
| `binary_sensor.<camera>_vehicle`, `binary_sensor.<camera>_pet` | Cars, trucks, buses, motorcycles, bicycles; cats and dogs. |
| `sensor.<camera>_last_detection` | "Rohan and a vehicle", with `people`, `uncertain`, `unknown_faces`, `labels`, `identity`, `matches` (similarity per name), `confidence`, `known_only`. |
| `image.<camera>_last_detection` | The best frame of that detection, boxes drawn. With several faces to teach, they are numbered. |
| `calendar.<camera>_visitors` | Who came when: every detection with a person, on Home Assistant's calendar (the last 7 days). |

Local cameras also have `event.<camera>_event` (`motion`, `detection`, `recording`) and, for RTSP,
`camera.<camera>` (live view through Home Assistant), `binary_sensor.<camera>_motion` and
`binary_sensor.<camera>_stream` (connected; `last_error`, `restarts`, `dropped_frames`).

Blink cameras also have:

| Entity | Description |
| --- | --- |
| `event.<camera>_blink_event` | `motion`, `doorbell_press` (doorbells), `detection`, or Blink's raw type (e.g. `camera_offline`). |
| `binary_sensor.<camera>_motion` | On for 30 s after each motion push. `last_record` attribute. |
| `camera.<camera>` | Latest Blink snapshot. |
| `camera.<camera>_live_view` | Live view as a Home Assistant stream: the dashboard, HomeKit Bridge, go2rtc and Frigate can play it. Opening it wakes the camera, like live view in the Blink app. Also carries live-view details for apps. |
| `button.<camera>_refresh_snapshot` | Take a new snapshot (wakes the camera). |
| `switch.<camera>_motion_detection` | The camera's motion detection. |
| `binary_sensor.<camera>_battery`, `sensor.<camera>_battery_voltage`, `sensor.<camera>_temperature`, `sensor.<camera>_wifi_signal` | Battery and health, where the model reports them. |

Per Sync Module network: `alarm_control_panel.blink_<network>` and `binary_sensor.blink_<network>_connection`.
Account: `sensor.blink_instant_events_last_blink_refresh` and `binary_sensor.blink_instant_events_push_connection`.
The automation editor offers device triggers **Motion detected**, **Doorbell pressed** and **Detection**.

Every `detection` event carries `summary`, `people`, `uncertain`, `unknown_faces`, `labels`, `identity`
(`known`/`uncertain`/`unknown`/none), `matches`, `confidence`, `known_only`, `faces_to_teach` (unknown or
uncertain faces `enroll_face` can learn from it), `detection_id`, `doorbell` and `at_the_door` (below),
`provider`, `source` (`local`/`cloud`), `timestamp`, and `image`: a signed link to the annotated frame,
valid 24 hours, for notifications.

## Actions

| Action | What it does |
| --- | --- |
| `blink_instant_events.enroll_face` | Admins only. Teach a face from a camera's current picture (`camera`), a photo file (`file`, inside `allowlist_external_dirs`, e.g. `/media`), or a past detection (`detection`: its `detection_id`; `face`: which one, by the number on its picture, 1 by default). Detections keep their faces for 7 days. One face, close (≥ 80 px), sharp and facing the camera; a face that already matches someone else is refused unless `force: true`; the same photo twice is not stored twice. |
| `blink_instant_events.not_this_person` | Admins only. "That wasn't Alex": the face a detection (`detection`) matched to `name` was someone else. Camera Vision keeps that face as *not Alex*, so it and faces like it stop matching Alex; Alex's own photos are kept. Needs Camera Vision 0.3.0. |
| `blink_instant_events.set_zone` | Admins only. Where detections count on a camera (see [Detection zones](#detection-zones)). |
| `blink_instant_events.delete_face` | Admins only. Forget every photo of a person. |
| `blink_instant_events.process_snapshot` | Analyze any camera's picture now and get the detection back (`response_variable`). |
| `blink_instant_events.show_on_tv` | Blink: play a camera's live view on a Cast TV for `duration` seconds. |

Unknown faces are never learned by themselves. Several photos from different angles and lighting help.

## Blueprints

Ready-made automations; import one, pick your camera and phone, save.

| Blueprint | What it does | |
| --- | --- | --- |
| Who's at the door | After a doorbell press: "Alex is at the door" on your phone with the picture, and on speakers if you pick some. **This is…** and **Not Alex** buttons, as below. | [![Import this blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Frohanjariwala03%2Fha-local-camera-vision%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flocal_camera_vision%2Fwho_is_at_the_door.yaml) |
| Person alert with picture | A person on a camera, quiet when only family. A face it doesn't know gets a **This is…** button: type a name to teach it. When it names someone, **Not Alex** tells it that face wasn't Alex. | [![Import this blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Frohanjariwala03%2Fha-local-camera-vision%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flocal_camera_vision%2Fperson_alert.yaml) |
| Blink doorbell on the TV | A doorbell press plays the live view on a Cast TV that is on. | [![Import this blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Frohanjariwala03%2Fha-local-camera-vision%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flocal_camera_vision%2Fdoorbell_on_tv.yaml) |

The phone is notified through `notify.mobile_app_<phone>`, the name the Home Assistant app registered.
Anyone who can answer the phone's notifications can teach a face this way, so pick your own phone.

## More automations

Quiet when only people you know were seen (recognition is a likelihood, so keep it for convenience,
not for unlocking doors):

```yaml
triggers:
  - trigger: state
    entity_id: event.porch_event
    not_from: unavailable
conditions:
  - "{{ trigger.to_state.attributes.event_type == 'detection' }}"
  - "{{ not trigger.to_state.attributes.known_only }}"
  - "{{ 'person' in trigger.to_state.attributes.labels }}"
actions:
  - action: notify.mobile_app_phone
    data:
      message: "{{ trigger.to_state.attributes.summary }}"
      data:
        image: "{{ trigger.to_state.attributes.image }}"
```

Blink doorbell on the TV:

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

## Recording (RTSP cameras)

Off by default. **Event clips** keeps a rolling buffer of 2-second segments in Home Assistant's cache and,
`post` seconds after motion starts, joins the segments from `pre` seconds before it into one MP4 under
the media folder (Media › My media › `blink_instant_events/<camera>/<date>/`); the `recording` event names
it. **Continuous** keeps one-minute segments there instead. Video is copied, never re-encoded, so it costs
disk, not CPU; audio is not recorded (many cameras send G.711, which MP4 can't carry).

Recordings older than *keep for* are deleted, the oldest go first above *at most … GB*, and recording
pauses while the disk has under 2 GB free and resumes by itself. Blink's own clips are separate: they
are listed and fetched from Blink (HTTP API, Media panel) and are never mixed with local recordings.

## Archiving Blink clips

Off by default. **Configure → Settings → Archive new clips** copies each new Blink clip to the media
folder (Media › My media › `blink_instant_events/blink/<camera>/<date>/`, named `HHMMSS-cloud.mp4` or
`HHMMSS-local.mp4`) and deletes them after *Keep archived clips for* days. Clips from the Sync Module's USB
drive work without a Blink subscription. It checks once an hour and copies one clip at a time; while a live
view is open it waits for the next hour, because the Sync Module has to upload each USB clip first and a
busy module makes the Blink app say "system busy". Under 2 GB free it stops copying until there is room.

## Detection zones

By default the whole picture counts. To ignore the street or a neighbour's drive, give a camera a zone: a
polygon with at least 3 corners, each `[x, y]` in percent of the picture from its top left. People,
vehicles and animals count where they stand (the bottom middle of their box), faces by their middle.

```yaml
action: blink_instant_events.set_zone
data:
  camera: camera.front_door   # or any entity of the camera's device
  zone: [[0, 40], [100, 40], [100, 100], [0, 100]]   # the lower 60 % of the picture
```

The zone is outlined in cyan on every detection picture after that, so check `image.<camera>_last_detection`
and adjust. Run it without `zone` to go back to the whole picture. Zones don't change when Blink records or
alerts; they only change what the detection reports. Asking for a picture by hand (`process_snapshot`)
ignores them.

## Smart detection on Blink cameras

Every new Blink clip is read the same way: frames one second apart through the add-on. A push starts
looking for the clip at once (Blink usually lists it 15–40 s after the motion). **No Blink subscription
(no clips)?** Turn on **Configure → Settings → Look with live view after motion**: each push then opens
8 seconds of live view and that is read instead. It starts about 5 seconds after the motion (the camera
has to wake and Blink has to set up the stream), so someone passing quickly can be gone; software can't
remove that delay. Each look wakes the camera, much like recording a clip.

**Doorbell presses** always look with 4 seconds of live view, with or without a subscription, and don't
count against the daily limit: the doorbell is awake anyway, and whoever rang is standing still, close,
facing it. If the doorbell is already on a screen (`show_on_tv`, a dashboard), that live view is read
instead of starting another. The detection has `doorbell: true` and `at_the_door`, a sentence for a
speaker that is never surer than the detection: "Alex is at the door", "It might be Alex at the door",
"Someone you don't know is at the door", "Someone rang the doorbell". It arrives about 15 seconds after
the press. With a subscription, the press's clip is analyzed again later as an ordinary detection.

## Raspberry Pi 5

Home Assistant OS on a Pi 5 (8 GB, SSD) runs everything: the integration in Home Assistant, Camera Vision as
an add-on. What matters for speed:

- **Use sub-streams** for RTSP cameras and leave *frame width* at 640. The Pi 5 has no hardware H.264
  decoder, so decoding is on the CPU; a 640×360 sub-stream at 2 fps costs little, a 4K main stream a lot.
  *Decode keyframes only* cuts it further.
  On an Intel or AMD mini PC, turn on *Hardware decoding* (per camera, under Configure) to decode on the
  graphics chip; ffmpeg falls back to the CPU when it can't use it.
- **Detection runs only after motion**, on up to 10 frames, one camera at a time; frames that arrive while
  the Pi is busy are dropped, not queued. Identical frames are analyzed once.
- **Measure before buying an accelerator.** In the add-on:
  `docker exec -it addon_<repo>_blink_vision python /benchmark.py /media/snapshot.jpg 10`
  prints milliseconds per stage. On an Apple M-series Mac (8 cores) one frame takes about 136 ms
  (faces 18 ms, objects 121 ms); a Pi 5 has not been measured yet and will be slower. Multiply by the
  frames per detection to get the delay before a notification.
- **Detection accelerators** are not supported yet. Hailo (AI HAT+) is the realistic option for a Pi 5, if the
  benchmark shows the CPU is too slow; Coral's Python library is unmaintained. None is required.

## Docker

The integration runs inside Home Assistant either way. Without the Supervisor, run Camera Vision as a
container on the same machine or network:

```sh
docker build -t blink-vision blink_vision
docker run -d --name blink-vision --restart unless-stopped \
  -p 127.0.0.1:8000:8000 -v blink-vision:/data \
  -e BLINK_VISION_TOKEN="$(openssl rand -base64 32)" blink-vision
```

Then **Add integration → Local Camera Vision → Camera Vision address**: `http://<host>:8000` and the token.
Publish the port only where Home Assistant can reach it: `127.0.0.1` works when Home Assistant runs
with host networking on the same machine (the usual setup); otherwise bind your LAN address and firewall
it. Never expose it to the internet. Thresholds can be set in `/data/options.json` (same keys as the add-on options).

## Security

- **Credentials** live in Home Assistant's config entries (Blink tokens, camera logins, the Vision token),
  are never logged, and are redacted from diagnostics; ffmpeg's messages are scrubbed of camera passwords.
  ffmpeg receives the camera address with its login as an argument, which other processes inside the
  Home Assistant container could read.
- **RTSP addresses** must be `rtsp://` or `rtsps://` with a valid host; ffmpeg may only open RTSP's own
  protocols, never files or other URL schemes.
- **TLS**: Blink's live-view servers present Blink's own self-signed certificate. blinkpy turns checking off;
  this integration pins that certificate and trusts nothing else for live view, so an impostor server is refused.
- **Camera Vision** answers only with its token (except `/health`), has no published ports as an add-on,
  keeps faces in its own `/data` (mode 0700, included in Home Assistant backups — encrypt your backups),
  and only admins can teach, forget or see enrolled faces.
- Live-view players use short-lived per-camera tokens; detection pictures use signed links that expire.
- Nothing here exposes a camera stream outside Home Assistant. Don't port-forward Home Assistant
  without its own authentication and TLS.

## Troubleshooting

- **RTSP camera stays disconnected**: `binary_sensor.<camera>_stream` → `last_error` shows ffmpeg's reason
  (password removed). Open the same address in VLC to rule out the camera; try its other stream path.
- **"Invalid authentication" while adding**: the camera answered 401; check the login, and that the
  camera allows RTSP for that user.
- **No detections**: the add-on must be running and set up (Discovered, or the Vision address entry);
  diagnostics show `smart_detection`. A 401 in the log means the token changed: set it up again.
- **Motion too often / too rarely** (RTSP): raise or lower *motion sensitivity*; trees, rain and the
  camera's day/night switch move many pixels.
- **A vehicle that never shows up**: a vehicle where one stood in the camera's last detection counts as
  parked and is left out. After a restart the first detection still reports it once.
- **Faces "unclear"**: Camera Vision found a face too blurred to read. Lower `min_detect_sharpness` in the
  add-on options if sharp faces come out unclear; raise it if blurred ones come out unknown.
- **Faces never known**: faces under ~32 px can't be told apart (a high-mounted camera mostly says
  "a person"); teach more photos, or lower `same_person` in the add-on options a little.
- **Blink**: see the poll sensor's `last_error`; motion pushes only arrive when the Blink app would
  notify you (armed, motion detection on, notifications on).

## Tests

`pytest` runs the integration's tests, including local cameras end to end against a synthetic RTSP
camera (ffmpeg encoding a moving test picture, served over RTSP) while Blink, its push client and every
host but the mocked Camera Vision are forbidden: stream, motion, detection, sensors, saved clip,
reconnection, a wrong login, the ONVIF-style camera path and the actions. `pytest blink_vision` tests the
add-on (needs `opencv-python-headless` and `numpy`).

