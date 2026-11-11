# Blink without the cloud: feasibility report

Question: do Blink Outdoor cameras and the Sync Module 2 expose any local video or event interface that
Home Assistant could use instead of Blink's cloud? Status: research only. Nothing in the integration
depends on the answers below, and nothing speculative is implemented.

Scope and rules: documented behaviour, this repository's own code and traffic, blinkpy, published
community research, and passive or read-only checks on one home network's own Sync Module 2 (results
below). No authentication bypass, no firmware modification, nothing that could disturb the cameras.

## 1. Confirmed

| Finding | Evidence |
| --- | --- |
| **Live view comes from Blink's servers, not the camera.** Blink's REST API answers a live-view request with an `immis://<Blink server>:443/…` address; the integration connects there over TLS and Blink relays the camera's video. | `providers/blink/liveview.py` (`request_liveview`, `ImmisStream`); blinkpy `livestream.py`. |
| **Older families (XT, White) get `rtsps://` addresses, also on Blink's servers.** This is cloud-relayed RTSP over TLS, not a local RTSP server, and community reports say even that address is hard to play. | `request_liveview` rejects `rtsp*` answers; [blinkpy issue](https://lightrun.com/answers/fronzbot-blinkpy-get_liveview-returns-a-immis-protocol-link). |
| **Motion and doorbell events come from Blink's cloud through Google FCM.** No event reaches Home Assistant from the camera or the Sync Module directly. | `providers/blink/listener.py`, `docs/protocol.md`. |
| **Sync Module 2 USB clips are reached through the cloud.** The documented flow: ask Blink's REST API for a manifest of the USB drive, ask it to upload a clip, download that clip from Blink. | blinkpy `api.py` (`request_local_storage_manifest`, `request_local_storage_clip`); `providers/blink/clips.py`. |
| **Cameras keep two links: Wi-Fi to your router (video, cloud) and a sub-GHz radio to the Sync Module (wake-up, arming).** blinkpy reports both signal strengths (`wifi_strength` and `lfr`); the Sync Module 2 board carries a Silicon Labs Si4455 sub-GHz transceiver. | blinkpy `camera.py` (`sync_signal_strength = signals["lfr"]`); [luekker/blink-sync-module-2](https://github.com/luekker/blink-sync-module-2). |
| **Sync Module 2 hardware:** Atheros AR9331 (MIPS, 400 MHz) SoC; TCP 53 and 80 open in community scans; SSH and Telnet refused; a UART on test pads with a ~0.4 s console window nobody has caught reliably. | [luekker/blink-sync-module-2](https://github.com/luekker/blink-sync-module-2), [brainonfire.net](https://www.brainonfire.net/blog/2023/10/21/reversing-blink/). |
| **The Sync Module's HTTP server is for setup and status.** Terse HTTP/1.0 on port 80: status/log text, and setup endpoints (`/api/version`, `/api/get_fw_version`, `/api/set/key`, SSID setting, firmware update). Most requests are AES-CBC encrypted with a session key from the app's key exchange; it is used while the phone is joined to the module's own `BLINK-…` access point during onboarding. No video, snapshot or event endpoint has been found. | Same two sources. |

## 2. Unknown

- ~~Whether ports 53 and 80 are open on the home LAN after setup~~: **they are not** on the module checked
  (below); the published scans were of the module's own setup network.
- The sub-GHz protocol between cameras and the Sync Module (only "reverse the Si4455 protocol" as a goal
  in the hardware notes). It is almost certainly a low-rate wake/arm/status link, not video.
- Whether the Sync Module or a camera ever streams video on the LAN (e.g. camera → module while writing
  to USB). No source shows it; the camera's Wi-Fi uploads go to Blink.
- The firmware itself: not dumped or analyzed in any public source.

## 3. What works without Blink's cloud

Nothing that would be useful in Home Assistant:

- No local video, snapshot or clip interface is documented or observed.
- No local event interface: motion and doorbell events need Blink's cloud and FCM.
- The USB drive's clips can only be fetched through Blink's API. Taking the drive out and reading it on
  a computer is possible but manual (and Blink's file format would need its own research).
- If Blink's cloud is unreachable, the integration's Blink cameras stop updating; local cameras carry on.

## 4. Hardware limits

- Battery cameras sleep. They wake on PIR motion or a command delivered over the sub-GHz link, record or
  stream, then sleep again. Any "always-on local stream" would drain the battery in days, even if an
  interface existed.
- The Sync Module 2 is a small MIPS router-class SoC; it stores to USB and relays commands. Nothing points
  to it being able to serve live video to several clients.
- Video from the camera is H.264 over TLS to Blink. Intercepting it would mean defeating TLS, which is out
  of scope and would be an attack on the device, not an integration.

## 5. Is direct local video realistic?

**No, not with current evidence.** Every confirmed path for Blink video goes through Blink's servers, the
only local interface found is an encrypted setup API, and the battery design argues against a local stream.
The realistic local-first options are the ones this integration now offers: put RTSP/ONVIF cameras where
you need local video and fast detection, and keep Blink cameras for what they do (with smart detection
running locally on the clips or live looks Blink delivers).

## Checks on a home network (2026-11-10)

One Sync Module 2 in normal operation (armed network, three cameras), from a computer on the same LAN.
Passive or read-only; no logins, no requests to any open service.

| Check | Result |
| --- | --- |
| Find it | The router's DHCP names it `blink-sync-module`; a second Blink device (`Blink-Device`, a camera or the doorbell) was also on the LAN. Both answer ping. |
| mDNS, 3 minutes | No Blink service. Types seen on the network: AirPlay, HomeKit, Home Assistant, Matter, Spotify Connect, Apple pairing. |
| SSDP (one standard M-SEARCH, then listening) | Only the router answered. Nothing from Blink devices. |
| TCP connect scan, all 65 535 ports of the Sync Module | **No open port.** Ports 53 and 80 (open in the published setup-mode scans) answer with a refusal. |
| `GET /`, `/api/version` | Not attempted: nothing listens. |
| Traffic destinations during motion and live view | Not possible from a computer on a switched network; needs router or mirrored-port logs. From this integration's own traffic, live video comes from the Blink-hosted `immis://` server named in each live-view answer. |

So on the home LAN the Sync Module 2 offers nothing to connect to: no local API, no discovery, no
stream. Its setup API exists only on its own access point during onboarding. This confirms the
conclusion above. Not checked: UDP services, the camera on the LAN, and other firmware versions.

If a local video or event interface ever turns up, it gets its own experimental module, separate from
the production providers, and only after it is shown to work.
