<img src="custom_components/blink_instant_events/brand/icon@2x.png" alt="" width="112" align="right">

# Local Camera Vision

Camera alerts for Home Assistant that know who's at the door. When someone walks up, you get a
notification with the picture saying whether it's someone you know, someone it doesn't recognize,
a car or the cat. The looking happens on your own hardware, not in someone else's cloud.

It works with two kinds of cameras:

- **RTSP and ONVIF cameras**, or any camera already in Home Assistant. No account, no subscription,
  and nothing leaves your network.
- **Blink cameras.** Instant motion and doorbell alerts, arm/disarm, snapshots, clips and live view.
  These still go through Blink's cloud, the same as the Blink app.

This used to be *Blink Instant Events*. If you had that installed, just update; your setup carries over.

> Unofficial. Not affiliated with Blink, Amazon, Google or Home Assistant.

## Install

You need Home Assistant 2026.10 or newer.

1. Open it in the Marketplace (HACS), download **Local Camera Vision** and restart Home Assistant.

   [![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rohanjariwala03&repository=ha-local-camera-vision&category=integration)

   Or add `https://github.com/rohanjariwala03/ha-local-camera-vision` as a custom repository (type
   **Integration**) by hand.
2. For face and person detection, add the app repository, then install and start **Camera Vision**.
   Home Assistant finds it on its own and offers it under Settings → Devices & services → **Discovered**.

   [![Open your Home Assistant instance and add this app repository.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Frohanjariwala03%2Fha-local-camera-vision)
3. Add the integration and pick a camera:

   [![Open your Home Assistant instance and start setting up Local Camera Vision.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=blink_instant_events)

   - **RTSP camera** if you have its stream address, like `rtsp://192.168.1.20:554/stream2`. Use the
     sub-stream if the camera has one: it's plenty for detection and much lighter on a Raspberry Pi.
   - **A camera already in Home Assistant** for ONVIF cameras (add them with Home Assistant's ONVIF
     integration first), Reolink, UniFi Protect or Generic Camera. You pick the camera and its motion sensor.
   - **Blink account** with your Blink email, password and the PIN Blink sends. For instant alerts,
     open **Configure → Sign in for instant events** afterwards.

Running Home Assistant in Docker without add-ons? [Run Camera Vision as a container](docs/reference.md#docker).

## Teach it a face

Stand in view of a camera and run this from Developer tools → Actions (for Blink, press the camera's
**Refresh snapshot** first), or point `file` at a photo in `/media`:

```yaml
action: blink_instant_events.enroll_face
data:
  name: Alex
  camera: camera.front_door
```

A few photos from different angles and light help. It never learns a face on its own; strangers stay strangers.

## Get a notification

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.front_door_unknown_person
    to: "on"
actions:
  - action: notify.mobile_app_phone
    data:
      title: Someone's at the door
      message: "{{ state_attr('sensor.front_door_last_detection', 'summary') }}"
      data:
        image: "{{ state_attr('image.front_door_last_detection', 'entity_picture') }}"
```

Each camera also gets `person`, `known_person`, `uncertain_identity`, `vehicle` and `pet` sensors. Home
Assistant may add the area to the name (`camera.cameras_outdoor`), so check your entity ids first. The
[reference](docs/reference.md) lists every entity, action and option.

## What runs where

| | Local cameras | Blink cameras |
| --- | --- | --- |
| Motion, snapshots, live view | your network | Blink's cloud |
| Recordings | your Home Assistant media folder | Blink's clips, fetched from Blink |
| Face, person, vehicle and pet detection | your network | your network, on frames from Blink |
| Face database | the add-on, in your backups | same |

## Limitations

Worth knowing before you rely on it:

- **Recognition is a good guess, not proof.** Don't use it to unlock doors or disarm alarms. Faces
  smaller than about 32 pixels (a camera mounted high, someone far away) only come out as "a person".
- **Blink can't go local.** Blink cameras have no local stream; I checked a Sync Module 2 on my own
  network and it offers nothing to connect to ([details](docs/blink-local-feasibility.md)). If Blink's
  cloud is down, Blink cameras stop updating; local cameras keep going.
- **Blink support is unofficial.** It copies what the Blink app does and can break whenever Blink
  changes something. Automated access may also be against Blink's terms. One Blink account per Home Assistant.
- **Blink without a subscription is slower.** With no clips to read, it opens live view after motion,
  which starts about 5 seconds late. Someone walking past quickly can be gone by then. Every look also
  wakes the camera and costs battery.
- **Few real cameras tested so far.** RTSP has been tested against a synthetic stream, not yet against
  real camera brands. Raspberry Pi 5 speed hasn't been measured yet. Please open an issue with
  what works and what doesn't.
- **No packages, no accelerators.** The detection model has no "package" class, and Coral and Hailo
  aren't supported yet. Everything runs on the CPU.
- **ONVIF is only as good as the camera.** Motion comes from Home Assistant's ONVIF integration, and not
  every camera sends ONVIF motion events. If yours doesn't, add it as an RTSP camera instead.
- **Recordings are RTSP only and have no audio.** Many cameras send audio MP4 can't hold.
- **Cameras limit connections.** This uses one stream per camera, plus one while someone watches live
  view. Some cameras only allow two or three.

## Releases

Every version is on the [Releases page](https://github.com/rohanjariwala03/ha-local-camera-vision/releases)
with what changed, and HACS shows the same notes when there's an update. Older history is in the
[changelog](CHANGELOG.md).

## More

- [Reference](docs/reference.md): entities, actions, recording, Raspberry Pi tips, Docker, security, troubleshooting
- [Upgrading to 0.7](docs/upgrade-0.7.md) · [HTTP API for apps](docs/app-api.md) · [Moving from Blink Live View Proxy](docs/migration.md) · [Protocol notes](docs/protocol.md)

MIT licensed. See [NOTICE](NOTICE).
