# Blink Instant Events

Instant Blink motion and doorbell-press events in Home Assistant.

Home Assistant's Blink integrations poll Blink's cloud, so motion shows up minutes late and
doorbell presses don't show up at all. The Blink phone app is instant because Blink pushes events
through Firebase Cloud Messaging (FCM). This integration signs in to Blink as its own app client,
registers an FCM token, and keeps a push connection open inside Home Assistant — no Docker, no
polling, no clips required.

> **Unofficial.** Not affiliated with Blink, Amazon, Google or Home Assistant. It relies on
> reverse-engineered behaviour of the Blink app and can break whenever Blink changes it.

## Requirements

- Home Assistant 2026.10 or newer
- A Blink account (email + password + the PIN Blink texts/emails you)

## Install

1. HACS → ⋮ → **Custom repositories** → add this repository's URL, category **Integration**.
2. Download **Blink Instant Events**, restart Home Assistant.
3. **Settings → Devices & services → Add integration → Blink Instant Events**.
4. Enter your Blink email and password, then the PIN Blink sends. The password is used once and
   not stored; if Blink later revokes the session, Home Assistant asks you to sign in again.

## What you get

| Entity | Description |
| --- | --- |
| `event.<camera>_blink_event` | One per camera. Event types: `motion` for every camera, `doorbell_press` for doorbells, and Blink's raw type (e.g. `camera_offline`) for anything else. |
| `binary_sensor.blink_instant_events_push_connection` | Diagnostic: on while the push connection is up and registered with Blink. |

Event attributes: `camera`, `camera_id`, `network_id`, `blink_event_type`, plus `created_at`,
`category` and any thumbnail/clip id Blink includes.

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

## How it works

1. Registers an FCM identity in Blink's Firebase project with
   [firebase-messaging](https://github.com/sdb9696/firebase-messaging).
2. Signs in with [blinkpy](https://github.com/fronzbot/blinkpy) and attaches the FCM token to that
   Blink session (`clients/{id}/update`), as the phone app does.
3. Keeps the Google MCS connection open, reconnects with backoff, re-registers every 12 hours, and
   acknowledges pushes that ask for a receipt.

Protocol details: [docs/protocol.md](docs/protocol.md).

## Privacy

Tokens are stored in the config entry and never logged. Diagnostics redact email, tokens, account
and client ids, FCM credentials and serial numbers.

## Risks

- Blink can change its push registration, app-version checks or Firebase project at any time.
- This adds a signed-in client to your Blink account; Blink may email you about the new sign-in.
- Automated access may conflict with Blink's terms of service. Use at your own risk.

## Credits

- [CoreyHayward/ha-blink-live-events](https://github.com/CoreyHayward/ha-blink-live-events) (MIT) —
  the protocol research this integration builds on.
- [firebase-messaging](https://github.com/sdb9696/firebase-messaging) (MIT) — FCM registration and
  MCS client, also used by Home Assistant's Ring integration.
- [blinkpy](https://github.com/fronzbot/blinkpy) (MIT) — Blink sign-in.

## License

MIT
