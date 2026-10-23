# Cut-over from Blink Live View Proxy

A checklist for replacing the proxy add-on (`a54c2c78_blink_liveview_proxy`) and the
`blink_liveview_proxy` integration with Blink Instant Events. Do it once both have run side by side
and agree (see the per-phase tests in the release notes).

`<slug>` is `front_door`, `outdoor`, `outdoor_4_gl39`.

## 1. What replaces what

### Entities

| Proxy | Blink Instant Events |
| --- | --- |
| `camera.blink_proxy_<slug>` (snapshot) | `camera.<slug>` |
| `camera.blink_live_<slug>` (live) | `camera.<slug>_live_view` |
| `binary_sensor.blink_proxy_<slug>_motion` (poll, minutes late) | `binary_sensor.<slug>_motion` (push, instant, on 30 s) |
| `binary_sensor.blink_proxy_<slug>_battery` | `binary_sensor.<slug>_battery` |
| `sensor.blink_proxy_<slug>_temperature` (°F) | `sensor.<slug>_temperature` (°C) |
| `sensor.blink_proxy_<slug>_wifi_signal` | `sensor.<slug>_wifi_signal` |
| `sensor.blink_proxy_<slug>_battery_voltage` | `sensor.<slug>_battery_voltage` |
| `switch.blink_proxy_<slug>_motion_detection` | `switch.<slug>_motion_detection` |
| `button.blink_proxy_<slug>_refresh_snapshot` | `button.<slug>_refresh_snapshot` |
| `alarm_control_panel.blink_proxy_home` | `alarm_control_panel.blink_home` |
| `binary_sensor.blink_proxy_home_connection` | `binary_sensor.blink_home_connection` |
| `sensor.blink_liveview_proxy_last_blink_refresh` | `sensor.blink_instant_events_last_blink_refresh` |
| `binary_sensor.blink_liveview_proxy` (add-on health) | none needed (no add-on); `binary_sensor.blink_instant_events_push_connection` covers the push link |
| — | `event.<slug>_blink_event` (motion, `doorbell_press`) — already in use |

Attributes keep their names (`snapshot_of`, `snapshot_id`, `snapshot_taken`, `last_record`,
`proxy_slug`, `blink_camera_id`, `network_id`, `camera_type`, `product_type`, `ptt_supported`,
`battery_state`, `battery_level`, `status`, and the poll sensor's `state`, `interval`, `failures`,
`polls`, `last_error`). New: the alarm panel shows `arming`/`disarming` while a command runs.
Temperature is now °C natively; templates that assumed °F need a look.

### Endpoints

| Proxy | Blink Instant Events |
| --- | --- |
| `/api/blink_liveview_proxy/cameras/<slug>/token` | `/api/blink_instant_events/cameras/<slug>/token` (also returns `session`) |
| `…/hls/index.m3u8`, `…/hls/<segment>` | same paths under `/api/blink_instant_events/cameras/<slug>/` |
| `…/stop`, `…/ptt`, `…/controls`, `…/last-liveview`, `…/last-liveview.mp4` | same paths under `/api/blink_instant_events/cameras/<slug>/` |
| `/api/blink_liveview_proxy/clips`, `/clips/<id>.mp4`, `/clips/<id>.jpg` | `/api/blink_instant_events/clips…` (bearer) |
| `…/mpegts`, `…/player`, `/clips/viewer`, `…/snapshot-refresh`, `…/last-liveview.ts`, auth panel | dropped (unused by the app; snapshot refresh is the button) |

Details: [app-api.md](app-api.md).

## 2. What the app must change (~/Projects/smart-home)

- `src/components/live-view.tsx` `cameraBase`: `/api/blink_liveview_proxy/cameras/` →
  `/api/blink_instant_events/cameras/`.
- `src/components/clips.tsx`: `/api/blink_liveview_proxy/clips` → `/api/blink_instant_events/clips`.
- `src/automations.ts`:
  - `BLINK_POLL` → `sensor.blink_instant_events_last_blink_refresh` (unless you keep the old id, §3).
  - `restartBlinkProxy` (Supervisor add-on restart) → remove. Recovery is Home Assistant's
    re-authenticate prompt; nothing needs restarting.
  - `BLINK_SIGNED_OUT` text: point to Settings → Devices & services → Blink Instant Events →
    Re-authenticate, not the proxy's Authentication panel.
  - `blinkDelay` and "motion arrives on the proxy's next poll" copy: motion is now instant (push).
  - The `switch.blink_proxy_` prefix checks (also in `src/ha.tsx`, the watch and widget targets):
    the `_motion_detection` suffix check already catches the new switches; drop the prefix or keep
    it if you keep the old ids.
- Entity discovery by attribute (`snapshot_of`, `proxy_slug`) and by `<object id>_motion` /
  `_battery` / … siblings keeps working with either naming.
- Optional: doorbell presses now arrive as `event.front_door_blink_event` with
  `event_type: doorbell_press`, so the doorbell alert no longer has to be its motion.
- Comments and README text that name the proxy.

## 3. Removing the proxy without breaking automations

Automations, scripts, dashboards and the app's own `smarthome_alert_*` automations reference the
proxy's entity ids. Home Assistant only lets one entity hold an id, so the safe order is: delete the
proxy's entities first, then give Blink Instant Events' entities the freed ids. Nothing has to be
edited and nothing stays broken for more than a few minutes.

1. **Check.** Both integrations show the same arm state, motion switch states, battery and sync
   connection; live view, talk and clips work through `/api/blink_instant_events/…`.
2. **Find device-based references.** Settings → Automations: search for automations using
   *device* triggers/conditions/actions on a "Blink …" proxy device (these use device ids, which a
   rename does not carry over). Rewrite those few as entity-based or point them at the new device.
   Entity-id references need nothing.
3. **Back up.** Settings → System → Backups → Create backup.
4. **Delete the proxy integration.** Settings → Devices & services → Blink Live View Proxy → ⋮ →
   Delete. Its entities leave the registry, so their ids become free. Automations referencing them
   go "unavailable" until step 5.
5. **Take over the ids.** For each row in the entity table above: open the new entity →
   ⚙ Settings → *Entity ID* → type the proxy id (e.g. `binary_sensor.blink_proxy_outdoor_motion`)
   → Update. Do the motion sensors, `alarm_control_panel.blink_proxy_home`, the
   `camera.blink_proxy_<slug>` snapshot cameras and `sensor.blink_liveview_proxy_last_blink_refresh`
   first: those are what your automations use. If you'd rather adopt the new ids, skip this step and
   edit the automations instead (search and replace using the table).
6. **Uninstall the add-on.** Settings → Add-ons → Blink Live View Proxy → Uninstall. Then HACS →
   Blink Live View Proxy → ⋮ → Remove, and remove its add-on repository (Settings → Add-ons → Add-on
   store → ⋮ → Repositories) if nothing else uses it.
7. **Restart Home Assistant** and check: Settings → Repairs is empty, the automations' traces show
   the new entities firing, the app shows cameras and the alarm.
8. **Blink account.** The proxy's sign-in remains a client on your Blink account. It does no harm;
   remove it in the Blink app if it lists signed-in devices.

Roll back by restoring the backup from step 3.
