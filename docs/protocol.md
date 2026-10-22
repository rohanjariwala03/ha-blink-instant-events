# Blink push protocol

Unofficial, reverse-engineered notes. Blink can change any of this without notice.
Learned from [CoreyHayward/ha-blink-live-events](https://github.com/CoreyHayward/ha-blink-live-events)
(MIT) and re-verified here against a live account on 2026-10-22.

## 1. FCM identity

The Blink Android app receives pushes through Firebase Cloud Messaging. Its public
Firebase client identifiers (from the APK, as bundled by the prior art):

| Field | Value |
| --- | --- |
| Project id | `blink-6c1ea` |
| Sender id | `351117235468` |
| App id | `1:351117235468:android:14fb3ef63df2a7c2` |
| API key | `AIzaSyBcgwrpglzCZJPUNNeQQnpBbJAFlas_XAI` |
| Android package / cert SHA1 | `com.immediasemi.android.blink` / `65902c52c99a37502bdd4f342db2180c4c73b578` |

Two registration paths both succeed against this project (verified 2026-10-22):

- **Web push** (what `firebase-messaging` 0.4.5 does): GCM check-in as Chrome, `c2dm/register3`,
  Firebase Installations create, then `fcmregistrations.googleapis.com/v1/projects/blink-6c1ea/registrations`.
  Messages arrive encrypted (RFC 8291) and the library decrypts them. **Used by the integration.**
- **Android**: Firebase Installations create with `X-Android-Package` / `X-Android-Cert` headers,
  GCM check-in as `DEVICE_ANDROID_OS`, `c2dm/register3` with `app`, `cert`, `sender=351117235468`,
  `X-gms_app_id`, `X-Goog-Firebase-Installations-Auth`. Messages arrive as plain `app_data`. Kept as
  the fallback if Blink stops delivering to web tokens.

Both connect to MCS (`mtalk.google.com:5228`) and stay connected.

## 2. Blink session

Sign-in uses blinkpy 0.25.9's OAuth v2 + PKCE flow (`client_id=ios`): email + password, then the
SMS/email PIN. Tokens refresh with:

```
POST https://api.oauth.blink.com/oauth/token
grant_type=refresh_token&refresh_token=…&client_id=ios&scope=client&hardware_id=<uuid>
```

`GET https://rest-{tier}.immedia-semi.com/api/v1/users/tier_info` gives `tier` and `account_id`.

## 3. Associating the FCM token with Blink

```
GET  https://rest-{tier}.immedia-semi.com/api/v3/accounts/{account_id}/homescreen
     Authorization: Bearer <access token>
     → response header Client-Id: <this session's Blink client id>
       body: cameras[], doorbells[], owls[] with id, name, serial, type, network_id

POST https://rest-{tier}.immedia-semi.com/clients/{client_id}/update
     Authorization: Bearer <access token>
     Client-Id: <client id>
     {"app_version": "60.0 (29834864)", "os_version": "14",
      "client_name": "Home Assistant Instant Events", "notification_key": "<FCM token>"}
     → 200 {"message":"Client successfully updated","code":241}   (verified 2026-10-22, web token, iOS session)
```

blinkpy's own `client_id` is empty after the OAuth v2 login; the `Client-Id` response header is the
one that works.

## 4. Push payloads

Per the prior art (validated there on a Video Doorbell `lotus`, Mini Pan-Tilt `owl`, Outdoor 3
`catalina`): the JSON body may be nested as a string under `data`, `payload`, `message` or
`notification`.

| Event | `category` | `event_type` |
| --- | --- | --- |
| Doorbell press | `media` | `button_press` |
| Doorbell / Pan-Tilt PIR motion | `media` | `pir` |
| Outdoor 3 motion | `motion` | `cam2sm_motion` |
| Other motion | `motion` | `cloud_motion` |

Other fields seen: `camera_name`, `created_at`, `account_id`, `tier`, `unique_id`, `return_receipt`.

If `return_receipt` is truthy, Blink expects:

```
POST https://rest-{tier}.immedia-semi.com/api/v2/notification
{"transaction_id": "<unique_id>", "status": "RECEIVED"}
```

### Still to confirm on this account

- [ ] A push actually arrives on the web token with an iOS-client session.
- [ ] Outdoor 4 (`sedona`) motion payload: category / event_type / camera id field.
- [ ] Front Door (`tulip`) doorbell press and PIR payloads.
- [ ] Which camera identifier the payload carries (`camera_id` vs name only).

Redacted samples go here once captured.
