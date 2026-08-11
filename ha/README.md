# Deployable Home Assistant artifacts

The exact scripts, package config, and automations described in
[`docs/operations.md`](../docs/operations.md), ready to copy onto a fresh HA
instance. Every instance-specific value is a `<placeholder>` — fill them all
before deploying (grep for `<` to find them).

## Layout → where it goes

| Repo path | Deploy target | Loaded by |
|---|---|---|
| `scripts/cam_health.py` | `/config/scripts/cam_health.py` | the command_line sensor below |
| `scripts/cam_flap.py` | `/config/scripts/cam_flap.py` | the stream-fault command_line sensor |
| `scripts/cam_motion.py` | `/config/scripts/cam_motion.py` | the per-camera motion-staleness sensor |
| `packages/cam_health.yaml`, `packages/cam_flap.yaml`, `packages/cam_motion.yaml` | `/config/packages/` | `homeassistant: packages: !include_dir_named packages` |
| `automations/*.json` | HA **storage** automations (not files) | `POST /api/config/automation/config/<id>` |

## Deploy order

1. **Fill placeholders.** `scripts/cam_health.py` needs the HA host IP plus each
   camera's Scrypted device id and webhook token (how to obtain them:
   [`docs/migration-runbook.md`](../docs/migration-runbook.md)).
   `scripts/cam_flap.py` needs: `<scrypted_addon_slug>` (visible in the add-on's
   URL in the HA UI, e.g. `xxxxxxxx_scrypted`); its `CAMS` dict keys must
   byte-match the camera names Scrypted prints in log brackets (`[Front Door]`);
   `TP_PER_MIN` = camera_count ÷ probe_interval_minutes (the default 4.5 assumes
   9 cameras probed every 120 s — recompute if either differs); and
   `ALERT_HR_OVERRIDES` ships empty — add a raised threshold per known-chronic
   camera if you have one. `automations/front_door_doorbell_announce.json`
   needs your speaker and TTS entity ids.
   `automations/go2rtc_reload_on_start.json` needs the go2rtc config-entry id —
   find it with `GET /api/config/config_entries/entry` (filter `domain: go2rtc`).
2. **Snapshot first.** Take a full backup immediately before deploying, so a
   bad change is a restore rather than a reconstruction. Note that manual
   backups are usually **exempt** from the supervisor's automatic-backup
   retention, so prune old pre-change snapshots yourself or they accumulate.
   The mirror-image hazard is worth planning for too: **restoring a backup
   silently reverts every change made after that backup was taken.** Storage
   automations are the easy thing to lose this way, because nothing warns you
   that an automation's config moved backwards. After any restore, diff the
   live automations against these files and re-apply the delta — which is the
   practical reason to keep this directory in sync with the running system.
3. **Copy** the script and package file to `/config/` (SSH add-on or Samba).
   Ensure `configuration.yaml` includes the `packages:` directive above.
4. **Restart HA fully.** The `command_line` integration only loads on a full
   restart — `reload_all` leaves the sensor `unavailable`.
5. **Create the automations** — for each JSON file:
   **Prerequisite:** the motion-stale pair and the doorbell announce presuppose
   the MQTT motion/doorbell `binary_sensor`s from
   [`docs/operations.md`](../docs/operations.md) §3 — deploy those three only
   after MQTT discovery is live, and replace their camera entity lists with
   your own (on an install where the sensors don't exist yet, the stale-motion
   check reads as "no motion" and false-alarms at the next 15:00/18:00 check).
   For each JSON file:
   `POST /api/config/automation/config/<filename-without-extension>` with the
   file body. They take effect immediately, no restart.
6. **API calls**: all REST endpoints above are `http://<HA_HOST_IP>:8123/...`
   with an HA long-lived access token (`Authorization: Bearer <token>`).
   Note the flap monitor requires an HAOS/Supervised install — it reads the
   add-on log through the Supervisor API using the `SUPERVISOR_TOKEN` available
   inside the Core container; on Container/Core installs, adapt the fetch.
7. **Verify**: `sensor.camera_health` reads the camera count (all healthy),
   `binary_sensor.cameras_problem` is `off`, and a test notification fires when
   a camera URL is deliberately broken.

## What each automation does

- `camera_health_alert` — pages on a **sustained** fault only, three tiers:
  camera down 8 min (fast); degraded (frozen/slow) 30 min — transient stream
  blips self-heal in under ~22 min and paging on them is pure noise; and
  fleet-stale 5 min (fastest) — multiple cameras returning byte-identical
  frames simultaneously means a fleet-level snapshot-pipeline wedge.
- `camera_health_recovered` — dismisses the alert after 5 min clear.
  Dismiss-only: no "recovered" notification (avoids churn).
- `go2rtc_reload_on_start` — reloads the go2rtc config entry 2 min after HA
  starts, healing the WebRTC live-view regression every restart causes.
- `front_door_doorbell_announce` — doorbell press → parallel TTS announce on
  two independent speaker paths (`continue_on_error` on both, so one path's
  failure can't silence the other) + a persistent notification.
- `cameras_motion_stale_alert` / `_clear` — fleet-wide dead-man's switch,
  checked hourly during active hours: if **zero** cameras report motion over
  a rolling 4-hour window, the motion pipeline itself is down (a single quiet
  camera is normal; a silent fleet is not). Catches the silent event-listener
  wedge described in `docs/operations.md` §3.

- `camera_flap_alert` / `camera_flap_recovered` / `camera_flap_down` — the
  stream-fault monitor (see `docs/operations.md` §6): pages on a sustained
  per-camera **recording-error** rate, dismisses on recovery, and pages
  separately if the monitor itself sits in an error state for an hour
  (dead-man's switch - covers the watchdog dying too, since that starves
  the monitor's clock). Also carries a tripwire on cloud push-decryption
  failures - each one is a dropped motion push; a sustained climb means the
  event transport is degrading and the camera-source plugin needs re-auth.
- `camera_motion_dead_alert` / `_recovered` — per-camera motion staleness:
  pages when **one** camera has produced no motion EVENTS beyond its window
  (default 72 h; naturally-quiet or rarely-visited cameras take longer
  per-camera overrides in `cam_motion.py`) while the rest of the fleet is
  active. Read the page as an observation, not a fault verdict: zero events
  can mean a genuinely unvisited area, disabled/zoned-out motion detection in
  the camera app, or a dead event-push path — a walk-test discriminates, and
  under a 24/7-recording plan the camera records continuously regardless. The fleet-wide
  dead-man above only fires when *every* camera goes quiet, so a single dead
  camera is invisible to it. Reads the recorder rather than entity
  `last_changed`, which resets on restart and would otherwise mask staleness.
  The alert fires on the onset edge AND re-asserts hourly while the condition
  persists — a restart otherwise wipes the notification with the binary still
  latched (the edge can never re-fire), and a second camera crossing its
  threshold while latched would otherwise never page; recreating the same
  notification_id is idempotent, so the re-assert adds no churn.

As shipped, alerts use `persistent_notification` (HA notification center) —
swap in your `notify.*` service of choice (e.g. mobile push) in the JSON.
