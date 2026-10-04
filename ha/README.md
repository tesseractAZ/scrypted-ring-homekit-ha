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
| `scripts/cam_vision.py` | `/config/scripts/cam_vision.py` | the frame-differencing visual-activity sensor |
| `scripts/partial_loss.py` | `/config/scripts/partial_loss.py` (beside `cam_motion.py`) | imported by `cam_motion.py` (the partial-loss test) |
| `scripts/cam_logarchive.py` | `/config/scripts/cam_logarchive.py` | the hourly engine-log archive sensor |
| `packages/cam_health.yaml`, `packages/cam_flap.yaml`, `packages/cam_motion.yaml`, `packages/cam_vision.yaml`, `packages/cam_logarchive.yaml` | `/config/packages/` | `homeassistant: packages: !include_dir_named packages` |
| `automations/*.json` | HA **storage** automations (not files) | `POST /api/config/automation/config/<id>` |

## Deploy order

1. **Fill placeholders.** Five of the six scripts carry them (`partial_loss.py`
   has none) — grep each for `<`.
   `scripts/cam_health.py` and `scripts/cam_vision.py` each need the HA host IP
   plus every camera's Scrypted device id and webhook token (how to obtain them:
   [`docs/migration-runbook.md`](../docs/migration-runbook.md)); they probe the
   same nine endpoints, so the two lists must match.
   `scripts/cam_motion.py` needs the nine camera entity stems (it queries
   `binary_sensor.<stem>_motion`) and, optionally, per-camera staleness
   overrides. (There is deliberately no recorder-outage exclusion knob — see
   the corroboration section for why one can only ever delete real data.)
   Its `WITNESS` map (optional, ships empty) names the light-switch scene
   entities of a room whose camera has no co-firing partner, and
   `PARTIAL_LOSS_ACK` (ships empty) acknowledges a deliberate partial loss.
   `scripts/cam_logarchive.py` needs only `<scrypted_addon_slug>`.
   `automations/camera_room_walk_test.json` needs the same switch scene
   entities as `WITNESS` (`event.<witness_switch_scene_1>`/`_2`) and that
   room camera's `binary_sensor.<stem>_motion`; skip it when `WITNESS` is empty.
   `scripts/cam_flap.py` needs: `<scrypted_addon_slug>` (visible in the add-on's
   URL in the HA UI, e.g. `xxxxxxxx_scrypted`); its `CAMS` dict keys must
   byte-match the camera names Scrypted prints in log brackets (e.g. `[Kitchen Cam]`);
   `PROBE_INTERVAL_MIN` = the scan_interval of ONE probing sensor in minutes,
   and `PROBERS` = how many sensors probe those endpoints on that interval
   (2 as shipped: `cam_health.yaml` and `cam_vision.yaml` both run
   `scan_interval: 120` against the identical URLs). Getting `PROBERS` wrong
   halves the expected-probe denominator and silently detunes the
   probe-shortfall detector by that factor; and
   `ALERT_HR_OVERRIDES` ships empty — add a raised threshold per known-chronic
   camera if you have one. `automations/front_door_doorbell_announce.json`
   needs your speaker and TTS entity ids.
   `automations/go2rtc_reload_on_start.json` needs the go2rtc config-entry id —
   find it with `GET /api/config/config_entries/entry` (filter `domain: go2rtc`).
   Every fault alert (except the card-only `camera_log_archive_event`) also
   carries a `notify.<your_mobile_app_target>` action —
   replace it with your own push target, or delete those steps if you only want
   the in-UI cards. Leaving them unreplaced makes the automation error at run
   time. Note the push steps are deliberately gated so that a re-assert (which
   exists only to restore a card a restart erased) does not re-notify: only
   genuinely new information reaches the device. One gate is subtler than it
   looks: `camera_motion_dead_alert` watches the proven-broken set with a template
   trigger, and a template trigger does NOT fire from an initially-true state - the
   same arming trap `numeric_state` has here - so it also pushes on BOOT whenever a
   camera is already proven broken. Without that clause a proof raised before the
   automation loaded would never page, and since the cards are in-memory, the
   restart that wiped the card would be the very event guaranteeing the silence.
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
   CI cannot check that for you. Its allowlist-parity step compares the repo's
   script with the repo's package, so a detector deployed live but never synced
   passes CI while this directory silently lacks it — and a later rebuild from
   here deletes it. Diff your sanitized deployed files against this directory
   before calling a change done.
3. **Copy** the script and package file to `/config/` (SSH add-on or Samba).
   Ensure `configuration.yaml` includes the `packages:` directive above.
4. **Restart HA fully.** The `command_line` integration only loads on a full
   restart — `reload_all` leaves the sensor `unavailable`. Once it is loaded, a
   changed package (a new sensor or new `json_attributes`) applies with
   `command_line.reload`; reload it BEFORE `template.reload`, so the template
   sensors (including the two delayed dead-man binaries) first render against
   the new attributes, and check every camera `binary_sensor` is off just
   before `template.reload` (an `on` one re-fires its onset automation).
5. **Create the automations** — for each JSON file:
   **Prerequisite:** the motion-stale pair and the doorbell announce presuppose
   the MQTT motion/doorbell `binary_sensor`s from
   [`docs/operations.md`](../docs/operations.md) §3 — deploy those three only
   after MQTT discovery is live, and replace their camera entity lists with
   your own (on an install where the sensors don't exist yet, the stale-motion
   check reads as "no motion" and false-alarms at the next hourly check).
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

- `camera_health_alert` — pages on a **sustained** fault only, four tiers:
  camera down 8 min (fast); degraded (frozen/slow) 30 min — transient stream
  blips self-heal in under ~22 min and paging on them is pure noise;
  fleet-stale 5 min (fastest) — multiple cameras returning byte-identical
  frames simultaneously means a fleet-level snapshot-pipeline wedge; and
  fleet-miss 5 min — every camera failing the same probe cycle, which is the
  snapshot path being down rather than any camera being down.
  Re-asserts hourly while the problem binary has been on 30+ min (the longest
  tier's window, so a re-assert can never page a blip the tiers absorb):
  persistent notifications are in-memory, so a restart wipes the page with the
  fault still latched and an edge trigger can never re-fire.
- `camera_health_recovered` — dismisses the alert after **35 min** clear.
  Dismiss-only: no "recovered" notification (avoids churn). The gate must sit
  above the longest raise tier: at 5 min it was 6× *shorter* than the 30-min
  degraded tier, so any fault whose quiet runs exceeded 5 min re-raised and
  re-cleared once per oscillation — one 7.3 h fault produced 4 card appearances
  and 6 dismissals, with the card absent for 143 min during which the fault was
  actually active 81% of the time. 35 min also clears the freeze detector's own
  10-min re-arm window (`FREEZE_PROBES × scan_interval`), so "recovered" can no
  longer be asserted from an interval the detector is blind in.
- `camera_health_monitor_down` / `_recovered` — dead-man for the watchdog
  ITSELF. The alert's template triggers all read `state_attr(...)|int(0)`, so a
  dead sensor — or the script's own error path, which publishes `healthy: -1` —
  evaluates as "no fault" on every trigger: without this, the primary liveness
  monitor can die and be reported as a clean fleet. The recovered half
  dismisses the card once the sensor is back (dismiss-only).
- `camera_motion_monitor_down` / `_recovered` — the same dead-man for the
  motion-staleness monitor. Covers BOTH death modes: command-level death
  (state `unavailable`/`unknown`) and the script's fail() path, which emits
  valid JSON with exit 0 — the sensor stays numeric but its `error` attribute
  goes non-null, so a state trigger alone would miss the likeliest death (a
  recorder-DB failure).
- `camera_health_heartbeat` — daily proactive status card (plus a boot trigger, so
  a restart cannot erase the only proactive positive signal for the rest of the
  day), sourced from ALL FOUR monitors — snapshot probes, motion staleness, the
  visual monitor and stream faults — with an honest "not reporting" fallback per
  line, and it leads with any proven event-path failure. "Probes OK" is the deliberate
  wording: the probe measures the snapshot path, not motion-event delivery,
  and a card that says "healthy" from one monitor while another is latched is
  a false all-clear.
- `go2rtc_reload_on_start` — reloads the go2rtc config entry 2 min after HA
  starts, healing the WebRTC live-view regression every restart causes.
- `front_door_doorbell_announce` — doorbell press → parallel TTS announce on
  two independent speaker paths (`continue_on_error` on both, so one path's
  failure can't silence the other) + a persistent notification.
- `cameras_motion_stale_alert` / `_clear` — fleet-wide dead-man's switch,
  checked hourly, **24/7**, against a **flat 9-hour** bar. If zero cameras report motion
  across that window the motion pipeline itself is down (a single quiet camera is
  normal; a silent fleet is not). It used to carry a hard 10:00–20:00 time
  condition, so a total pipeline outage starting at 20:01 raised nothing until
  10:11 the next morning — up to **14 h of unmonitored fleet, every night**.

  Fit the bar by **simulating the checks the automation actually makes**, not by
  looking at gap durations. A day/night split was tried first and was unsound:
  the fit classified a gap by its *start* hour while the automation picks a bar at
  *check* time, so a gap beginning overnight under the loose bar got judged
  against the tight one once the check crossed the boundary. And gap *duration* is
  not what an hourly sampler sees — over 36 clean days here the largest elapsed
  ever presented to a check was **7.89 h**, even though several gaps ran past 8 h.
  A flat 9 h bar gives zero false pages with 1.11 h of headroom, and removes the
  boundary class of bug entirely. Worst-case detection latency is the bar plus one
  check interval.

  One more trap in the same condition: `hours_since` is measured as of the
  sample instant, so the true elapsed *now* is `hours_since + sample_age`. The
  original subtracted it, making the test read `true − 2×age` — so the effective
  bar drifted with the sensor's refresh phase, which resets on every restart
  (measured 10.15 h to 10.97 h against a nominal 10 h). Fit that window to your own fleet's
  occupied data, not to intuition — on this one, 456 inter-event gaps over ten
  occupied days gave p50 0.04 h, p95 2.41 h, p99 6.45 h and a max of 8.46 h, so
  a 4-hour threshold fired on 4.0% of in-window checks (about one page every
  2.5 days) and every firing observed resolved itself with no intervention.
  Note two structural limits: it tests the **minimum** staleness across the
  fleet, so any one healthy camera suppresses it entirely — it can only ever
  catch a *total* pipeline outage, never a single camera — and the active-hours
  bar applies around the clock. Per-camera failure is
  `camera_motion_dead_alert`'s job. Catches the silent event-listener
  wedge described in `docs/operations.md` §3. The condition reads the
  staleness sensor's **recorder-derived** `hours_since` map, NOT entity
  `last_changed`: last_changed resets on every restart (blinding the check
  for up to 4 h), and a motion sensor stuck `on` reads as *fresh motion
  forever* — a stuck sensor once pinned the fleet clock to "now" for ~35
  consecutive hourly checks, mathematically disabling the dead-man while it
  claimed to be watching.

The stream-fault monitor also records **door contacts**. The Ring door/contact
sensors reach the engine but are not published to Home Assistant — no
`binary_sensor` exists for any of them, so the recorder had never seen one and
no monitor could use them. They are the only signal in the stack that is
*independent of the camera event path*: a door physically opened, whatever the
cameras did or did not report. `cam_flap.py` now publishes `door_openings`,
`doors` (per door), and `door_orphans` — openings with no camera motion within
`DOOR_MOTION_WINDOW_S` anywhere on the fleet. `door_orphan_times` carries
`[timestamp, door]` **pairs**, not bare timestamps: the per-door counts live in a
separate attribute that has no times, the reporting window rolls every few hours,
and the add-on log that could rejoin them retains only ~2.3 days — so an orphan
recorded without its door becomes permanently unattributable, destroying exactly
the history the feature exists to accumulate. Note also that summing
`door_orphans` across recorder samples **double-counts heavily**: the monitor
reports over a rolling multi-hour window at a much shorter cadence, so one event
appears in dozens of consecutive samples. Deduplicate via `door_orphan_times`.

**Per-camera door coverage.** The first release deliberately did not adjudicate
individual cameras: the add-on log retains only ~2.3 days, far too little to fit a
per-camera expectation. Two later additions made a per-camera test possible without
fitting any rate. `DOOR_CAMERA` in `cam_flap.py` maps each door contact to the camera
that covers it. That is owner knowledge — it ships empty, as a commented template, and a wrong
pair manufactures accusations against a healthy camera — and it
turns "did this door's own camera fire?" into a direct question. Repeat openings of
one door within `DOOR_TRIP_S` count as one **trip**. A trip is **saw** if the
covering camera fired within `DOOR_MOTION_WINDOW_S`, a **corroborated miss** if it
did not while some other camera did, and **unseen** if no camera fired at all.
An unseen trip is not a miss, but it is still a trip the covering camera did not
see, so it counts toward the trip minimum and the seen fraction; at the working
covering cameras on the reference fleet, none of 19 trips went unseen. `door_missed_by_cam`,
`door_miss_times` and `door_cover_stats` report the current window.

**Judge only what the slice can see.** The log slice is bounded by the 60,000-line
fetch far more often than by time, and the log arrives in probe-sized bursts, so an
opening near either edge can have its evidence outside the slice. Unguarded, that
manufactured verdicts at both ends: of the first ten distinct misses the detector
recorded, three were single-sample artifacts. Twice the covering camera had fired
seconds *before* the door (46 s and 8 s) and that line had already scrolled out; once
the door was judged 11 s after it opened, before the camera's line 72 s later
existed. The same gap produced false orphans. An opening is now judged only when its
whole ±`DOOR_MOTION_WINDOW_S` window lies inside the slice, and a trip only when
`DOOR_TRIP_S` of look-back does too, because whether an opening *starts* a trip
depends on the opening before it. Everything else is counted in `door_deferred`.
Deferral loses nothing: overlapping samples judge each event later with full context.
Replaying 2.2 days of engine log through sliding windows at ten sampling phases gave
zero wrong verdicts and zero dropped trips with the guard, against 71 wrong verdicts
and 11 false orphans (summed over the ten phases) without it.

**A rolling record, and its own alert.** A 6-hour slice can never show a pattern, so
every judged trip is kept in `/config/.cam_flap_door_state.json` for 7 days and
published per camera as `door_rolling`: `trips`, `saw`, `missed`, `unseen`, and
`days` — how much history the record really spans, never a flat 7 that a fresh
record has not earned. A camera is listed in `door_coverage_failing` when it has at
least 4 judged trips, saw no more than 20 % of them, and at least 2 were corroborated
misses. Over the same replayed log, the covering cameras that work saw 8 of 9, 9 of 9
and 1 of 1 of their trips; the failing one saw 0 of 4.

**Two refinements (2026-09-30).** The fraction rule cannot see an outage while
healthy days remain in the 7-day window: a camera that had seen 15 trips and then
went dark sat at 15/29 (52 %) through 11 corroborated misses over 3.5 days and never
failed. A camera therefore also fails after **4 corroborated misses in a row since
it last saw its door** (`door_rolling.run`). Replaying 2026-09-14..30, the longest
run at a working camera was 2 and the two real outages ran 6 and 11; the rule would
have flagged that 3.5-day outage about a day in, two days before the staleness
alert. Second, a miss is only as good as its corroboration: a camera that fires in
half of all evening minutes corroborates nothing. `cam_motion.py` publishes each
camera's chance of firing inside a ±180 s window for every hour of the day (last 7
days, counting only days the camera fired at all), and a miss counts only if at
least one corroborating camera's chance at that hour is at most 20 % to extend that
run. Otherwise it is recorded as **weak** (`door_rolling.weak`): it neither extends
nor breaks a run, but it still counts in the fraction rule exactly as before, so the
new rules can only add detections. Each miss is classified once, when fresh rates
first exist, and the class is stored with the trip so the record cannot flap. Measured: the three door-side cameras peak at 7–10 %, the
doorbell at 20 %, the busiest camera at 54 %. If the rates are missing or more than
3 hours old, every miss counts as before — the rule can only remove evidence, so it
is never applied blind.

This needs its own trigger because **motion staleness cannot carry it**. A camera
that still fires every day or two never goes stale — each stray event resets its
clock — so it can miss every person at its own door and never page.
`binary_sensor.camera_door_coverage_problem` and `camera_door_coverage_alert` watch
the failing set directly. The push is gated on freshness rather than on trigger type:
`cam_flap.py` stamps `failing_since_ts` at the poll where a camera enters the set,
and the alert pushes only while that stamp is under 15 minutes old **and** newer than
the alert's own previous run (`this.attributes.last_triggered`, which Home Assistant
restores across restarts). The script only runs while HA runs, so an onset during a
restart is stamped by the first poll after it and still pages, whichever of the boot
or attribute trigger sees it first. A failure that was already paged re-posts or
updates its card on a restart, a set change or the 6-hourly re-assert without
re-notifying. `cam_motion.py` also appends failing cameras to its own `summary`, because
that summary is the only text the staleness push carries.

The fleet-level orphan rate still ships **without an alert threshold**: its expected
value is ~0 (measured 1 of 26 openings over 2.3 days), but 26 openings is far too thin
to fit a bar, and inventing one from noise is a mistake this project has made before.

`stream_errors` is collected and published but deliberately **not wired to an
alert**, and that is a measured decision rather than an oversight. Across 4,520
samples the distribution is bimodal — p50 0, p95 2.16/h, p99 15.6/h — so a bar in
the valley around 6/h is easy to pick and would have fired on four episode-days in
55. The problem is that none of those episodes cost anything. On the largest
(peak 25.6/h, 108 raw errors, ~15 h) motion delivery was completely normal, no
camera went down, and the recording-error rate — the metric that *is* wired — was
flat zero. The one episode that involved real trouble had already paged through
`camera_flap_alert` and `camera_health_alert`. High `stream_errors` does correlate
with recording errors (32% of samples vs 11%), but recording errors are already
the alert metric, so wiring this would add duplicate and false pages without
adding detection. Keep it as context on the card; revisit only if an outage ever
shows up here first.

- `camera_flap_alert` / `camera_flap_recovered` / `camera_flap_down` — the
  stream-fault monitor (see `docs/operations.md` §6): pages on a sustained
  per-camera **recording-error** rate, dismisses on recovery, and pages
  separately if the monitor itself sits in an error state for an hour
  (dead-man's switch - covers the watchdog dying too, since that starves
  the monitor's clock). Also carries a tripwire on UNDECRYPTABLE cloud push
  messages (`push_undecryptable`). The push receiver either drops a message it
  cannot decrypt and logs one line (`Message dropped as it could not be
  decrypted: <reason>`, e.g. a missing crypto-key) or rethrows the error as a
  Node dump whose header and `code:` line both name it (e.g.
  `ERR_CRYPTO_ECDH_INVALID_PUBLIC_KEY`), never both for one message; the monitor
  counts both shapes, once per failure (a dump's later lines, a nested cause, or
  a window opening inside a dump are not counted again). These are **not**
  dropped motion events: across
  14 such failures, 30 of 30 engine-side motion detections still reached HA in the
  same second, because the failure sits in the push receiver rather than in the
  signalling session that actually delivers motion. Do **not** re-authenticate the
  camera-source plugin on this signal alone — confirm a real motion-delivery gap
  first. The rate is also confounded by push volume, which tracks motion, so
  normalise before calling a trend.
  Error-coded HKSV closes (`closed_with_error`, unchanged) are also split by HAP
  close reason (HAP-NodeJS `HDSProtocolSpecificErrorReason`): `closed_cancelled`
  (3, how a clip ends: HAP closes 12 s after the fragment generator stops),
  `closed_timeout` (6, the home hub stopped waiting for data) and `closed_other`;
  the three sum to `closed_with_error`. `video_stalls` counts mid-session video
  stalls once per on-demand session: a code-6 close at least 10 s after the
  camera's last sent fragment, or an `unspecified size` / `exit code: 234` ffmpeg
  line tagged with the camera while its session is up (the same lines from a
  snapshot racing a healthy teardown are not counted); `video_stall_times` lists
  the latest 20 episodes and the summary names stalls only when present. Stalls
  are informational and do not page: each stalled recording also ends in a
  `motion recording error`, which already is the paging metric.
- `camera_door_coverage_alert` / `_recovered` — per-camera door coverage (see the
  door-contact section above): pages when a camera keeps failing to report motion
  at its own door while other cameras see the activity. The push is gated on the
  `failing_since_ts` freshness stamp, so a restart neither drops nor repeats the
  page; the card is dismissed once the failing set has been empty for 60 minutes.
- `camera_motion_partial_loss_alert` / `_recovered` — a camera that still fires but
  has dropped out of most of the motion its usual partners keep recording (see
  the partial-loss section). The card gives per-camera evidence, the cameras that
  could not have been convicted, the scope, and the ack stamp. For each flagged
  camera it also lists, in local time, the camera's own detections and its
  co-fires with the partner whose count the card shows over the last 48 h, and
  states that a walk test at the door or a door opening does not clear the flag:
  a partial loss can spare close approaches or some hours of the day, so a
  detected walk proves only that spot at that hour; the outer parts of the view
  that the partner cameras record are walked instead, at hours with no detection
  listed. The push carries a one-line version (the local hours the camera was
  still seen at, or how many hours when there are more than eight). The push goes out
  on a set change or boot only for a flag whose `since` is under an hour old AND
  newer than the automation's own `last_triggered` (the door-coverage pattern),
  so a flag entered by the startup poll still pages and a reload or timeout
  re-adding a paged flag does not. The card is dismissed after 30 min off.
- `camera_motion_partial_loss_down` / `_up` — the partial-loss test's own
  dead-man. The 60-min hold lives in the delayed template binary
  `binary_sensor.camera_motion_partial_loss_test_down`, not in a template
  trigger: a template trigger is not armed when its condition is already true
  at attach (restart, reload, deploy order), while a template binary restores
  its state and starts its delay from its first render. One push on the
  binary's off->on edge; boot and an hourly re-assert restore the card. The
  binary's `reason` attribute states the current reading: `test running` while
  the test runs, the test's own error or the missing-attribute text while it
  fails, and `no reading` while the staleness sensor itself is unavailable.
- `camera_log_archive_down` / `_down_recovered` — the engine-log archive failed
  two runs in a row (`binary_sensor.camera_log_archive_down`, delay_on 75 min,
  same pattern), or has seen no engine line for 48 h. One push on the edge: the
  journal holds ~37 h, the deadline for a fix. Cleared by the first run that
  archived.
- `camera_log_archive_event` / `_event_cleared` — a gap (lines lost), resync
  (lines may repeat) or withheld line. Card only, restored at boot and every
  6 h, written only from a payload that carries the counters; cleared by
  `input_button.camera_log_archive_acknowledge` or after 35 days, never by a good
  run.
- `camera_room_walk_test` — a deliberate walk test for a camera with no
  co-firing partner (see the switch-notes section): double-tap the room's
  switch, walk in, and get PASS or FAILED pushed within 5 minutes.
- `camera_monitor_stalled` / `_recovered` — **freshness** dead-man for all five
  monitors (the engine-log archive is the fifth), and the one that closes the largest hole in this design. Every other
  dead-man here triggers on `unavailable`/`unknown`/`-1`, and none of them looks
  at *age*. Home Assistant rewrites `last_updated` only when the state or an
  attribute **changes**, and a healthy fleet emits a byte-identical payload for
  hours — so a monitor whose update loop has stopped sits pinned at its last
  reading indefinitely: no bad state, no recorder row, no page, nothing. Measured
  here before the fix, the probe monitor had gone **37 minutes** without writing a
  row while perfectly healthy, and legitimate steady periods reach 1 h 56 m — which
  is why a naive age bar false-fires. The fix is a bucketed `updated_at` published
  by each script, so `last_updated` advances at least once per bucket whenever the
  loop actually runs, at roughly one extra recorder row per bucket rather than one
  per poll. Bars are per sensor because the poll intervals differ 30× (3 h for the
  hourly engine-log archive).
- `camera_vision_monitor_down` / `_recovered` — dead-man for the visual monitor.
  It was the only one of the four without one, which mattered because its most
  likely failure is not a crash but going **blind while still running**:
  `cam_vision.py` rewrites its state file every cycle regardless of whether any
  given camera answered, and `cam_motion.py` used that file's mtime as its only
  freshness gate — so a camera nobody could actually see read downstream as a
  camera that was quiet, the reassuring direction. `cam_vision.py` now stamps a
  per-camera `probe_ts` on successful samples only, publishes `probe_age_min`
  and `blind`, and `cam_motion.py` refuses a verdict for any camera it could not
  see. A frame can also be *reachable but not new*: when a camera does not answer
  a snapshot request in time, the webhook returns HTTP 200 with its last cached
  image. A real sensor never produces two byte-identical JPEGs, so
  `cam_vision.py` treats a body identical to **any of the last 8 distinct
  bodies** from that camera as the same old picture (the cache can hold more
  than one image: one outage served two JPEGs that alternated for two days,
  which a check against the previous body alone passed as a new frame every
  poll; the ring is ordered by when each hash was last seen, so an image the
  cache keeps serving never ages out). Such a body is not analysed, does not refresh the baselines, and does not move `fresh_ts`, the
  time of the last genuinely new frame. `probe_ts` still moves, so a
  camera-side stall does not page as a dead vision monitor, and `cam_motion.py`
  withholds its visual verdict once a camera has sent no new frame for 15 min.
  Before this, a camera serving a cached image for days read as "no visual
  change - consistent with a quiet area". Do not rely on the stream-fault monitor to notice the visual monitor
  dying: both scripts probe the same endpoints, so `PROBERS` must be set to 2 or
  cam_vision stopping merely halves the probe count to a level still above the
  shortfall bar.
- `camera_host_down_alert` / `_recovered` — reports an outage of the HOST, after
  the fact. Every monitor here is a process inside the thing it monitors, so a
  host-down event raises nothing at all while it is happening: the monitor
  dead-men can only fire while HA is up, and every gate is ≥5 min while a cold
  boot restores service in ~2 min. A real mains cut took one fleet dark for
  57 minutes and produced no camera signal whatsoever. `cam_motion.py` publishes
  `host_gap_min` — the largest hole spanning **every** entity in the recorder over
  24 h, which is only explicable by the host being down. Note the trigger is a
  template plus a half-hourly sweep, **not** `numeric_state`: numeric_state only
  fires from an *armed* state, and after a cold boot neither ordering arms it
  (attach-before-first-poll raises on the missing attribute and never arms;
  first-poll-before-attach is already above the threshold and never arms), so it
  would have been structurally incapable of reporting the very outage it exists
  for.
- `camera_motion_dead_alert` / `_recovered` — per-camera motion staleness:
  pages when **one** camera has produced no motion EVENTS beyond its window
  (default 72 h; naturally-quiet or rarely-visited cameras take longer
  per-camera overrides in `cam_motion.py`) while the rest of the fleet is
  active. Read the page as an observation, not a fault verdict: zero events
  can mean a genuinely unvisited area, disabled/zoned-out motion detection in
  the camera app, or a dead event-push path — a walk-test discriminates, and
  under a 24/7-recording plan the camera records continuously regardless.
  When a stale camera's verdict carries a switch note (see the switch-notes
  section), the push ends with how to run that camera's walk test, because the
  push is the only part of the page that reliably reaches a phone; the card's
  corroboration header says the check reads recorder rows only.
  The push opens with what caused it. `New: <cameras>.` names the cameras that a
  stale-set change added. It is the same set difference the push gate computes from
  the trigger's old and new stale lists. When that change fires as the motion
  monitor recovers from a gap (the previous reading was unavailable, unknown,
  missing its stale list or carrying an error: a command_line reload, a timeout or
  a script error), the push opens with `Motion monitor back after a gap - stale:
  <cameras>.` instead, because the gate then counts every camera still stale as
  added. `HA restarted.` marks the boot push. A proven push gets no extra lead:
  its existing `PROVEN event-path failure: <cameras>.` line already leads it. It
  opens with `Motion monitor back after a gap.` when the proof re-fires as the
  monitor recovers. The fleet summary and any walk-test hint follow unchanged.
  Without this lead, a page for a newly stale camera opened with a summary that
  still listed a camera paged earlier, and it read like a repeat of that page.
  The staleness page carries TWO automated discriminators, printed strongest
  first. **Cross-camera corroboration** is the decisive one: cameras that share
  a sight-line co-fire at a stable rate, and that rate is a real test of one
  camera's event path, computable from motion rows already in the recorder — no
  walk test, no new hardware, no physical access. For a stale camera it measures
  how often each partner's motion clusters historically contained the target,
  then counts how often they have since it went quiet; zero out of enough
  opportunities is a proof. On this fleet it identified a genuinely broken
  camera at P = 2e-08 (a partner that co-fired in 42.6% of its clusters
  historically produced 32 clusters with zero co-fires afterwards, while a
  different partner's share of the same clusters rose to 100% — so the scene was
  demonstrably *more* active, not quiet). Crucially it also reports its own
  power: a camera with no high-rate partner (a spatially isolated view, an
  interior room) returns "cannot be tested" — worded as no evidence either way,
  because the same result appears when a camera that fired only intermittently
  before its last event has diluted every partner's rate, or when the 30-day fetch
  has slid past its working period (the verdict states how many days the fit really
  used, and names partners excluded only for too little history) — and a partner too sparse to reach
  significance returns "inconclusive" with the p-value it could have reached.
  It never converts weak evidence into an all-clear.

  Two guards matter more than they look, because p is exponentially sensitive to
  a rate that is only ESTIMATED. The test uses the **Wilson 95% lower bound** of
  the historical rate, not the point estimate - plugging in the point estimate
  asserts the rate is known exactly, which lets a thin sample manufacture a
  confident verdict. It also requires at least `CORROBORATE_MIN_HITS` actual
  historical co-fires, not merely enough clusters. Both were added after a rate
  fitted from **4 co-fires in 25 clusters** came within about a day of stamping
  "EVENT PATH BROKEN (proof)" on a camera - and would then have self-retracted two
  days later, when the sliding lookback dropped that partner below
  `CORROBORATE_MIN_PRE`. A verdict decided by window alignment rather than by the
  camera is worse than no verdict. Under the bound a 26/61 fit barely moves
  (0.426 -> 0.310, still decisive) while a 4/25 fit collapses (0.160 -> 0.064) and
  can no longer support a proof.

  Note there is deliberately **no exclusion list for recorder outages**. A recorder
  blackout is *defined* by having no rows, so excluding one removes nothing - the
  mechanism can only ever delete real data. A hard-coded span whose epochs were
  four days off its own comment silently discarded 39.6% of the motion history on
  this fleet while the window it named held exactly one row. If a gap ever does
  need masking, mask it where it is measurable - as a gap in the data - not as a
  constant no test can see.

  A proof is also LATCHED once established. The fit window is bounded relative
  to *now* while a stale camera's cut is fixed at its last event, so the
  historical half of the evidence shrinks by a day per day and a proof
  eventually starves itself — measured here, a camera proven broken on 44
  co-fires was down to 11 and about three days from falling under
  `CORROBORATE_MIN_HITS` while being just as broken. Losing a true positive to
  calendar arithmetic is worse than the false positive that floor exists to
  prevent. Widening the window is the obvious fix and the wrong one: anchoring
  it at `[cut-30d, cut]` fits the partner rate *worse* (mean absolute error
  0.450 vs 0.269 against the actual post-period rate, over the cameras alive
  across the cut), and since p = (1-rate_lb)^post, overstating the rate makes a
  proof cheaper — on one camera the zero-hit clusters needed to reach p<1e-3
  fell from 61 to 9. Latching costs nothing statistically: a verdict that
  already passed every gate is a fact about a moment, not a claim to re-derive
  hourly. The latch is keyed to the camera's cut timestamp, so the instant it
  produces any event its cut moves and the latch stops matching — it clears
  itself, with no reset path to get wrong, and it can only ever preserve a
  verdict, never create one.

  One structural blind spot is reported rather than papered over. Cameras that
  share a path co-fire at 85–95% with *each other* and only a few percent with
  anything else, so when a whole group goes dark together, every member's only
  high-power partners are the other silent members — and the `MIN_POST` filter
  removes exactly the cameras that could adjudicate, leaving some 3%-correlated
  survivor that cannot. The verdict says so explicitly ("this camera's
  co-firing partners are SILENT TOO") instead of the misleading "no partner
  shares enough of this view", because a group going dark together is a
  *stronger* signal than one quiet camera, not a weaker one. That claim is
  gated on partners having produced literally nothing over at least
  `CLIQUE_MIN_SPAN_S`: treating "fewer than MIN_POST clusters" as silence would
  print a group-outage verdict for a healthy fleet whenever the post window is
  merely short.

  Second, and weaker:
  `cam_vision.py`
  compares each camera's snapshots over time (block-based frame differencing,
  lighting-normalized, IR-aware) and the alert states whether the scene has
  visibly changed without events (detection/event path suspect) or not changed
  at all (genuinely quiet area). The discriminator counts only **localized**
  changes — a change that fires on three or more cameras within three minutes is
  a lighting transition, a cloud shadow, or the first frame after a restart
  (which has no valid prior frame and so registers on every camera at once), and
  says nothing about any one camera — and it requires several of them. Testing
  "is the vision log non-empty" is not a discriminator at all: an outdoor scene
  guarantees entries via sun, shadow and IR transitions, so every stale outdoor
  camera reads "suspect" regardless of its true state - no human walk test required, which matters
  when nobody is at the property for weeks.
  How much of a view's activity frame differencing can see differs by an order of
  magnitude between cameras. Measured over 14 days as the share of each camera's
  own motion clusters matched by a vision change within ±6 minutes: 79 %, 53 % and
  33 % on three quiet door-side views, but 8 %, 6 %, 4 % and 3 % on the four busiest
  outdoor views — one of which, while broken, was described as "consistent with a
  quiet area". `cam_motion.py` therefore records this **vision recall** per camera
  (the last 40 settled motion clusters, persisted so a camera that has gone silent
  is judged by what the detector saw while it worked). "Consistent with a quiet
  area" is printed only when recall is at least 25 % of at least 10 clusters, with
  the figures; below that, or with fewer than 10 clusters measured, the verdict is
  withheld. Motion from a period when the visual monitor could not see the camera
  is never scored against it. The visual monitor's own summary says "no visual change
  detected", not "quiet", for the same reason. The fleet-wide
  dead-man above only fires when *every* camera goes quiet, so a single dead
  camera is invisible to it. Reads the recorder rather than entity
  `last_changed`, which resets on restart and would otherwise mask staleness.
  The alert fires on the onset edge, on any change to the **stale set** (so an
  additional camera crossing its threshold raises genuinely new information
  rather than silently rewriting a card you have already read), on HA start, and
  on a slow 2-hourly re-assert. It used to re-assert *hourly* off a bare clock,
  which produced 260 identical recreations against 3 real state transitions in
  26 days and trained the reader to ignore the channel. Its condition reads the
  recorder-derived `stale_count` rather than the template entity's
  `last_changed`, so a restart no longer disarms it for the first hour — a restart otherwise wipes the notification with the binary still
  latched (the edge can never re-fire), and a second camera crossing its
  threshold while latched would otherwise never page; recreating the same
  notification_id is idempotent, so the re-assert adds no churn.
  Pushes go out only on genuinely new information: a camera ADDED to the stale
  set, a new proof, or a restart while a proof stands. The 1-hour onset trigger
  no longer pushes - every transition that turns the binary on is also a
  stale-set change that `setchange` already pushed, so the first camera to go
  stale used to page twice an hour apart - and a camera LEAVING the set updates
  the card without re-paging the cameras still on it. Fit each per-camera
  override to the longest silence that camera shows when nothing is wrong (here,
  an unused interior room reached 211 h), not to a round number.

Every fault alert writes a `persistent_notification` (the HA notification
centre) **and**, except the card-only `camera_log_archive_event`, sends a push
via `notify.<your_mobile_app_target>`. Both matter:
persistent notifications are in-memory, so a restart erases every card with the
fault still latched — which is why each alert also carries a re-assert trigger —
and they are only visible to someone with the HA UI open, which is no use to an
operator who is away from the property. The push steps are gated to fire on new
information only, never on a re-assert.

## Partial motion loss (`cam_motion.py` + `partial_loss.py`)

Staleness and the co-firing proof judge only a camera that has gone completely
silent. A camera that still fires a few times a day resets its staleness clock
with every stray event, so a camera that has lost most of its detection (a
narrowed motion zone, a sensitivity or schedule change, a partial obstruction)
is invisible to both. `partial_loss.py` closes that gap: a pure function called
on every 30-minute poll of `cam_motion.py`, for every camera that is not stale.
It ships next to `cam_motion.py`; if it is missing or raises, `partial_loss` is
published as `null` and the test's own dead-man pages.

**Statistic.** Motion ON rows of all cameras are chained into clusters with the
co-firing test's 5-minute linkage. For a target camera T and a partner P:
`n_B`, `k_B` are P's clusters in the reference window and how many also contain
T; `lb` is the Wilson 95 % lower bound of `k_B/n_B`; `n_R`, `k_R` are the same
counts over the last 48 h; the tail is `P(X <= k_R)`, `X ~ Binomial(n_R, lb)`.
The rate is conditional on P firing, so a quiet week does not move it - only T
dropping out of scenes P still records does. The reference window is the latest
27 days of usable time ending where the recent window starts (searched back at
most 60 days); T's own stale silences and every all-entity recorder hole longer
than 5 minutes are excluded as unobserved time.

**Conviction.** A poll is a hit when some tested partner has tail x m < 1e-6
(m = eligible partners, a Bonferroni factor), `k_R/n_R <= 0.33 x lb`, and, when
the last 7 days of the reference hold at least 10 partner clusters,
`k_R/n_R <= 0.5 x` that week's rate; T's own cluster rate has fallen to 67 % of
its reference or less; and no other tested partner vetoes. A partner vetoes when
it has at least 3 recent clusters and its co-fires would be improbable if T were
really down to `0.33 x lb`: `P(X >= k_R | n_R, 0.33 x lb) < 0.01`. A partner is
eligible with `n_B >= 20`, `k_B >= 8` and `lb >= 0.15` (the co-firing test's own
floors, passed in from `cam_motion.py`, never restated), and tested when it also
has at least 5 recent clusters, fires at no more than 2x its reference cluster
rate, and has not come back from its own stale silence within the last 48 h. A
flag is raised when hits have run continuously for 30 minutes of wall time (a
forced poll seconds after a scheduled one cannot satisfy it). The veto and the
2x bar were set against a partner that starts firing on junk: Poisson junk
injected into the real history at 1.0-2.5x a partner's cluster rate (288 runs)
latched a false flag in 21 runs under an earlier power-floor veto and a 3x bar,
and in 0 of 288 with the likelihood veto and the 2x bar.

**Power is published, and no flag is not an all-clear.** Each camera's entry in
`partial_detail.cams` carries its status: `testable` when even a total loss
could convict it this poll, `underpowered` when tested but not even zero
co-fires could, `untestable` when no partner is tested, `stale` when staleness
owns it. `min_loss` is the smallest loss the most sensitive partner would
convict at its expected count, and `partial_detail.cannot_convict` lists the
cameras that could not have been convicted this poll. Measured by removing
whole visit clusters from the real history (6 cameras x 4 healthy onsets),
losses of 100 / 90 / 80 / 67 / 50 % were caught in 18 / 17 / 14 / 6 / 4 of 24
cases (staleness pages the missed total losses at 72 h), a day-only loss in 8 of
24 and a night-only loss in 1 of 24, typically 40-50 h after onset. Slow
declines are not detected. The same wording is published in
`partial_detail.scope` and on the card.

**Clearing is evidence-based.** While a camera is flagged, the span that
convicted it is excluded from its reference, so the reference stays what it was
at entry. The flag clears only when the entry partner, judged against its frozen entry
bound (another tested partner against its own bound when the entry partner
cannot re-test), shows `k_R/n_R >= 0.5 x lb` **and** that many co-fires would
be improbable if the loss were still present (`P(X >= k_R | n_R, 0.33 x lb) <
0.01`), continuously for 6 hours. There is no time-based exit: a continued 90 %
loss replayed 40 days forward stays flagged (an earlier form that let the loss
become its own reference cleared it after about 17 days). A flagged camera that
goes stale leaves the set (staleness and its proof outrank this test) but its
reference stays **dormant**: the flagged span stays out of its baseline, so a
loss still present when it returns re-enters with the **same** `since` (no
second page; an acknowledgement still matches), while recovery evidence held
6 hours forgets the reference. Holding the flag itself through the silence
would instead re-flag every camera back from a total outage for 40-46 h until a
partner could re-test it, which a replay of the two real outages showed.

**Evidence times.** For a camera that is flagged or in a hit run - and only
then, so the payload stays bounded - `partial_detail.cams.<camera>` carries
`own_recent` (the start of each of the camera's own motion clusters in the 48 h
window) and `cofire_recent` (the camera's own first detection in each cluster of
`cofire_partner` that contained it, so every co-fire time is also one of its own
detection times), as epoch seconds, newest last, at most 24 each, with the full
counts in `own_recent_n` and `cofire_recent_n`. `cofire_partner` is the partner
whose counts the detail reports. On this fleet a camera convicted at roughly
90 % loss still fired only on close approaches (a delivery at the door, door
openings), all inside one afternoon window, so a doorstep walk test or a door
trip passed under the loss. The times are published as facts, with no cause
inferred: a time-of-day schedule and a zone, range or sensitivity restriction
fitted that case equally well.

A deliberate, permanent change (a narrowed zone) is silenced per flag in
`cam_motion.py`: `PARTIAL_LOSS_ACK = {"<camera>": <ack stamp>}`, where the ack
stamp is printed on the card. The acknowledgement is keyed to that flag, so a
later, separate flag pages again; one matching no current flag is listed in
`partial_detail.ack_unmatched`.

**Recorder holes.** Every all-entity gap longer than 5 minutes in the ~62-day
lookback is passed to the test. Scanning the whole lookback costs about 5 s on
a Raspberry Pi 5 (4 M rows), so the gap list is cached in the motion state file
and each poll scans only rows written since the previous scan; a missing or
unusable cache costs one full scan. If the scan fails the test still runs and
`partial_detail.gap_error` says so.

**Surfaces.** `partial_loss` lists the flagged cameras minus acknowledged ones,
and is `null` (never `[]`) whenever the test did not run.
`binary_sensor.camera_motion_partial_loss` is on while it is non-empty and
unavailable - never off - when it is `null`. The recorder stores no attributes at all for a state whose
attributes exceed 16 KB, so the evidence lists are trimmed (oldest first, counts
kept) whenever the whole payload would pass 12,000 bytes, and
`partial_detail.evidence_cap` then records the cap (absent when nothing was
trimmed). Replayed every 6 hours over 56 days of recorder history, the whole
attribute set peaked at 8.1 KB and nothing was trimmed; nine cameras flagged at
the full cap would come to 14.4 KB untrimmed.

**Measured over the recorder** (3,459 polls over 76 days, every real recorder
hole masked): a camera that kept firing but dropped to 3 co-fires in 77 of its
best partner's clusters (against 225/667) was flagged 48 h after onset. That
catch rested on a favourable reference (33.7 %, lb 0.30): the same camera losing
everything from either of two earlier dates would not have been flagged before
going stale. A 116 h outage was flagged 27 h before staleness, and a 38 h
silence that never paged at all was flagged and cleared on recovery. No flag
entered in a healthy period; one flag raised in the fortnight after a
fleet-wide plan change (references fitted on a different activity regime) held
three days into the following healthy period and cleared on evidence.

## Vision recall recency

The 25 % recall floor over the last 40 outcomes answers "could frame
differencing see this view?" with hits from up to 40 clusters ago, so a detector
that has just gone deaf keeps clearing it. A trailing run of misses is
therefore judged against the camera's own rate before the run: `P = (1 -
p_lb)^run`, where `p_lb` is the Wilson lower bound of the hit rate before the
run, with the hit that ended the run left out. The quiet verdict is withheld
when `run >= 4` and `P < 0.01` (the verdict states P and says the silence cannot
be read as a quiet area), or when `run >= 8` regardless (the verdict states
neither P nor a cause: a run can also be the camera firing on things frame
differencing rightly ignores). On records rebuilt from the recorder, a camera
whose vision threshold had ratcheted to its cap was withheld from its 4th miss
where the floor alone needed about 30; on healthy history the rule withheld
0 of 962 and 0 of 1,017 polls the floor allowed on two cameras and 15 of 428 on
the one whose recall sits at the floor. Deafness that begins as motion stops
leaves no clusters to score.

## Noise relaxation (visual monitor)

Each camera's threshold is set per exposure regime at 5x a learned noise EMA,
clamped to [12, 60]. The EMA learns only from samples that are not visual
changes, which assumes most samples of a regime show an empty scene. That fails
for a regime seen only while someone is present: an indoor view lit only during
visits is in day exposure almost exclusively while occupied, so its non-event
samples are a person who lit one block, each one grows the EMA, and the bar
climbs past what a person produces. Measured on one such view: the bar went from the floor to the cap over five
days, and once it was there 9 of the camera's motion clusters drew 1 visual
change.

`cam_vision.py` therefore stamps each regime's last comparison. Once a regime
has gone more than 15 min without one, its EMA relaxes toward 2.4 (where the
12 floor binds) with a 2 h time constant, downward only, and the relaxed value
is stored before the event decision so an event sample cannot restore the old
bar on the next poll. From the cap, the first comparison after 0.5 / 1 / 2 / 4 /
8.5 h of absence is judged at 54 / 45 / 32 / 19 / 13. A regime compared every
poll never relaxes, and below the clamp its output is byte-identical to the rule
without relaxation. The stored EMA is also clamped at 12 when the state loads
(above that the threshold is already at the cap). Replayed over 35 days, the
ratcheting view stays at 12.0-13.8 and its recall rises from 0.67 to 0.94;
elsewhere the change adds ~22 visual changes (+1.2 % of ~1,890), about 8 with
motion on the same camera within 6 min and about 14 without (roughly 0.4 a day; about two thirds of those within 30 min of a regime
returning, about half within an hour of sunrise). Malformed state
values are dropped and named in the summary, and a fault in any of these
refinements falls back to the earlier rule and is named there too. The state
file is written atomically. A regime compared every poll can still ratchet
within one long occupied stretch.

**Unlit grayscale frames.** A grayscale frame is filed as "ir" on the assumption
that the illuminator lit it. One camera occasionally delivers a single mono frame
with the illuminator off: nearly black (mean luma 15.6-45.8, 0.13-0.53 of the lit
picture). Compared against the lit IR baseline it logged a visual change, and,
stored as the new baseline, made the next lit frame log a second one: 27 times
in 36 days (54 changes), none with motion on the camera within 6 minutes.
`cam_vision.py` therefore skips a grayscale frame whose mean luma is below 55
(`LUMA_IR_MIN`) AND below 0.6 (`UNLIT_RATIO`) of an IR baseline stored in the
last 15 minutes: it is not compared, not stored as the baseline, leaves the noise
estimate alone, still counts as a fresh frame, and the summary says "unlit
grayscale frame not compared" on that poll. Both conditions are needed: the bar
alone would permanently stop comparisons for a camera whose genuine night picture
settled below 55 (an ageing lamp, wet ground), and would drop a real exposure dip;
the ratio alone would skip a lit frame right after lights go off (64.0, 0.52 of
the picture before it). With no fresh IR baseline nothing is skipped, so a lasting
dim scene costs at most 15 minutes of comparisons. Replayed over the 36 days
(227,038 comparisons), the rule removes all 54 changes and loses no change with
motion on the camera; skipping any same-regime comparison whose luma jumps by more
than T (T = 15-40) lost real changes at every T, and testing luma before saturation
missed the 45.8 frame. A shallower family of single-frame dips on the same camera
stays above the bar and is not addressed.

**Single-frame luma spikes.** The same camera also delivers, about once a night,
one IR frame much brighter than the frames either side (luma 86 -> 98 -> 86, or
86 -> 184 -> 86) with no motion near. The spike is compared and logs a change -
right once - but was then stored as the baseline, so the next, normal frame
logged a second change against it. Luma cannot tell such a spike from a real
one-frame event (room lights on for one frame during a visit), so the spike is
always compared and always logs. When an IR frame logs a change and its luma
differs from its baseline by more than 8 (`SPIKE_LUMA`), `cam_vision.py` keeps
that baseline for one more IR frame (surviving failed, cached, unlit and dark
polls; a lit colour frame drops it). If the next IR frame also logs a change,
reads within 3 (`SPIKE_RETURN`) of the kept frame and shows no change against
it, the picture is back: the spike's change is withdrawn and the return's kept,
so the pair counts once, and the summary says "one-frame luma spike counted
once" with the luma values and the test result ("... both kept" when the return
differs). Every frame is still compared against the same frame and stored as
before, so a lasting change is never held back and a change can only be
withdrawn, never added. The withdrawn stamp moves to a "merged" list in the
state file: `cam_motion.py` counts a camera's own changes from the log (a false
pair alone no longer reads "detection/event path suspect" on a stale camera) but
scores vision recall and cross-camera co-change on log + merged, so a merge never
costs a motion cluster its recall hit. Replayed over 36.9 days, 89 of 135
motion-free pairs become one change, no change with motion is lost and no motion
cluster loses its hit. The rule is limited to IR (in colour a one-frame spike is
mostly a light switched on and off during a visit).

## A camera with no co-firing partner: switch notes and the walk test

An interior room's camera has no partner that shares 15 % of its view, so it is
judged on duration alone (a per-camera window). Silence alone cannot tell an
empty room from a dead camera. When that camera is stale, its stale verdict
carries a note listing every physical press of the room's light switch since
its last motion event (`WITNESS` in `cam_motion.py`). Z-Wave Central Scene
events are sent only for a paddle pressed by hand, so these are a clean record
of someone at the switch: a row is a press when its timestamp lies -5 to +120 s
from the time it records (restart and re-interview rewrites repeat an old time
and are dropped). Each press shows whether a companion camera fired within
2 minutes, and the note says that a switch box outside the camera's view makes a
press with no motion no proof of a fault. Presses never make the camera stale or
broken. Each visit's outcome is logged in the motion state file (newest 50) to
build the calibration a press-based rule would need.

`camera_room_walk_test` turns a press into a deliberate test: double-tap
either paddle (`KeyPressed2x`), then walk into the room. It posts a "running"
card, waits up to 5 minutes for the camera's motion sensor (motion already being
reported within 30 s before the tap counts), and pushes PASS with the reaction
time or FAILED with what to check in the Ring app. Only a fresh double-tap
starts it: a state restored after a restart or an event older than 2 minutes is
ignored. A restart during the 5 minutes aborts the test without a result. A
double-tap that is not recognised (not reported by the switch, or dropped by
those checks) starts no test, so the stale verdict's note says: if no "walk
test running" card appears within a few seconds, the double-tap was not
recognised - walking in is enough on its own.

## Engine log archive (`cam_logarchive.py`)

The Scrypted engine log is the only record of door trips, per-camera recording
errors and session teardown, and the systemd journal retains only about
37-38 hours of it (591,537 lines over 38.1 h when measured), shrinking in
whole-file steps, so a review run more than ~37 h after the previous one loses
the difference. `cam_logarchive.py` copies every new engine line, hourly, into a
**redacted** archive of one gzip file per UTC day under
`/config/cam_engine.log.d/` (0700; files 0600), kept for 35 days (~1.4 MiB/day
measured). The directory name matches the Supervisor's core-backup exclusion
`*.log.*`, so the archive never enters a backup; a core restore empties
`/config` and deletes it, and the next run backfills what the journal still
holds. The archive is personal data - camera and door names with times show
when people come and go - and must not be attached to issues or shared.

**Resume.** Every run that writes ends with a marker line
`#~camlog~ CURSOR seq=<n> ts=<UTC> c=<journal cursor>` in the newest day file.
The next run requests `Range: entries=<cursor>:0:<N>` from the Supervisor log
API: skip 0 returns the anchor entry first, and `X-First-Cursor` must equal the
stored cursor, which proves the anchor is still in the journal (skip 1 on a
vacuumed cursor silently drops the journal's head line). The commit cursor must
match the systemd cursor shape and the API must return the very line archived
as that entry; otherwise the run fails and writes nothing.

**Run outcomes** (the sensor state):

| State | Meaning |
|---|---|
| `ok` | everything new archived |
| `catchup` | stopped at the 60 s work budget; the next run continues |
| `deferred` | Home Assistant started < 5 min ago (an archive > 24 h behind runs anyway with a 20 s budget) |
| `busy` | the previous run still holds the lock |
| `gap` | the cursor's entry is gone and the journal head is newer: that span exists nowhere; a `#~camlog~ GAP` line records it |
| `resync` | the cursor could not be verified for any other reason, or a corrupt archive file was set aside; the fetched window is archived again after a `#~camlog~ RESYNC` line - lines may repeat, none are missing |
| `withheld` | a line still matched the residual scan after redaction and was written as `<WITHHELD residual=...>` |
| `clock` | no new engine line for 48 h+ (the engine is stopped or hung, its log no longer reaches the journal, or the clock jumped); retention is paused |
| `error` | the run failed and wrote nothing - including a journal line format the parser does not recognise, so a Supervisor format change cannot silently archive nothing |
| `unknown` | `command_timeout` passed; Home Assistant 2026.9.4 publishes `unknown` with attributes `{}` and does not kill the process |

**Redaction, before anything is written.** The `<UTC timestamp> <host>
<ident>[<pid>]: ` prefix is kept (the log readers anchor on it). Rules on the
message, in order: JWTs and e-mail addresses; IPv6 including `::ffff:a.b.c.d`
before IPv4 (classified, never kept); credentials (`Authorization`/`Cookie`,
bearer/basic tokens, ICE `ice-pwd`/`ice-ufrag`/`usernameFragment`, quoted
passphrases, password/username/token/secret/api-key assignments, a credential
word before an opaque run); RTSP session ids; DTLS fingerprints and MACs; Ring ids by key (pseudonyms for camera, ding, cell and session ids, below), UUIDs, HomeKit codes, coordinates; then runs of 16+ hex and
bare runs of 7+ digits. An independent **residual scan** runs over the whole
archived line and withholds rather than writes anything that still looks like
an identifier: JWT prefixes, dotted quads (also URL-encoded), IPv6 shapes,
credential assignments with escaped quotes or `%3A`/`%3D` separators,
`credential`/`api-key`/`signature` keys, URL-encoded e-mail, alternative MAC
forms, HomeKit setup URIs, a quoted mixed-class value under `auth`/`key`, a quoted value under any key
ending in `id` or `_key` that still holds a run of eight or more letters, digits
and dashes mixing letters and digits, a raw segment after `mode/location/`,
`locations/` or `accounts/` in a URL path, any text but the placeholder after
`for location` in either Ring location error, and
any keyless mixed-case alphanumeric run of 24+ characters. Base64 blobs (for
example an SDP `sprop-parameter-sets`) are therefore withheld by design.
Authentication prose such as "Refresh token is not valid" survives. Measured on
the whole live journal (574,598 entries): 0 lines withheld, 0 residual hits;
5,531 distinct secret values extracted independently from the raw lines, none
present in the archive.

**Pseudonyms.** The Ring identifiers that a review needs in order to tie
messages together are replaced by keyed pseudonyms instead of a constant
placeholder: `doorbot_id`/`device_id` become `<DEV:xxxxxxxx>` (one per camera),
`ding_id` `<DING:…>`, `cell_id` `<CELL:…>`, and the UUID-valued
`dialog_id`/`session_id` `<SES:…>` (dialog and media session share the class,
so an equal value stays visibly equal). A token is HMAC-SHA256 over the field
class and the value, under a 32-byte key, written in base 20 with the letters
`g`-`z`: 8 letters for cameras, 12 for the rest. That alphabet has no hex
letter, no digit and no capital, so a token can never trip the residual scan,
which keeps its full strictness for raw values; at 12 letters the chance of
any collision among 100,000 values is about 1e-6. Account-level ids (location,
user, account), hardware ids, serials and MACs stay the constant `<RID>`, and
other UUIDs (SDP stream ids) stay `<UUID>`. A ding's `created_at` and
`requested_at` (epoch milliseconds) become the signed offset from the line's
own UTC stamp, for example `<T:-1.722s>`, so the latency from Ring to the
engine can be measured without keeping an absolute number; anything else
there stays `<NUM>`. The key lives in `.pseudonym.key` in the archive
directory (0600, never logged, outside every backup like the archive). It is a
dotfile so that a `*` copy of the directory leaves it behind: pull day files by
name and never copy the key off the host, because with the key the camera ids
can be recovered from their tokens by brute force. A run that finds no key, or
a key of the wrong size, generates a new one and saves it only after its lines
are on disk. When the archive already has day files, that run writes
`#~camlog~ REKEY …` at the head of every gzip member it appends (a fresh
archive starts with `START` instead), so each day file shows where its tokens
change key: tokens on the two sides of a REKEY line are not comparable. A key
lost after it was in use also raises a `pseudonym key lost` warning. Any other
error reading the key fails the run before anything is written or repaired. A
dry run always uses a throwaway key and saves nothing. Measured on
the whole retained journal (about 600,000 entries): one token per camera that
held a session, an exact one-to-one match with the camera names over every
`sdp` answer, every signalling block attributable to a camera (previously
about 19 % could not be), 0 lines withheld. Ring ids are recognised by key in the `key: value`, JSON, escaped-JSON and
URL-encoded forms, and as the `id` of a `location`, `device`, `doorbot` or
`ding` object, whether the object is written on one line or its `id` follows
on a line of its own inside the same dump. The location id is also recognised
where the Ring client library writes it outside a key: as the path segment
after `mode/location/`, `locations/` or `accounts/` in the URL of a failed or
retried request, and in its two errors that name a location (`... found for
location <name> - <id>`, `Could not find a security panel for location <name>
- <id>`), where the name goes too. An id inside an array, as a map key, in
other prose or as a bare string is out of reach of every rule (a numeric id or
a UUID there still meets the number and UUID rules).

**Push notifications.** A doorbell press makes the engine log the Ring push
notification as an object dump (`<camera> onDoorbellPressed { ... }`). Its
location id (as `group_key` and as `data.location.id`) becomes `<RID>`; its
`data.device.id`, the camera's own id, becomes the same `<DEV:…>` token as the
camera's `doorbot_id`, so the press is attributable (a `device` or `doorbot`
object whose id is not a Ring id of six or more digits gets `<RID>`, so a
non-Ring device never reads as a camera); the ding id at the head of
`server_correlation_id` becomes the ding's `<DING:…>` token, which ties the
press to its live session, and its suffix becomes `<RID>` whether quoted or
not, after `|` or `%7C`; `triggered_at`, `sent_at` and the snapshot
`timestamp` become offsets like the ding stamps. The other copies of the camera
id in the dump (the channel suffix, `referring_item_id`, the snapshot id) stay
`<NUM>`. As a general rule, any quoted value under a key ending in `id` or
`_key` (or `Key`) has every run of eight or more letters, digits, dashes and
underscores that mixes letters and digits replaced by `<RID>`; stream and track
ids, counters and codec names are digits only, UUIDs or shorter and are not
touched. The residual scan withholds any such run that is still there, so a
variant under a quoted `..id` or `.._key` key that no rule knows is withheld
instead of archived; an id outside a quoted key (prose, an array element, a map
key, a URL path other than the Ring paths above) is still out of reach. A rule
fix applies to new lines only: day files written before it keep what they hold
until they are re-redacted.

**Request-dump collapse.** The two probing monitors fetch the same `takePicture`
webhooks every 120 s, and the engine logs each request as a 15-line object
dump. Each dump that matches a strict grammar is replaced by its url line and a
note of what was removed (about half the lines; gzip size -33 %); anything else
is archived verbatim. `cam_flap.py` counts probes by device id
(`public/<id>/<token>/takePicture`, the token lower-case hex in the live journal or
`<HEX>` in the archive), so its parser gives the same `probe_counts` on both. A
request whose token is neither, such as an unfilled `<webhook_token>`
placeholder, is not a probe: the engine logs the url before rejecting it with
401, so such a prober stays visible in `probe_shortfall`.

**Retention and repair.** A day file is deleted only when it is older than both
`today - 34 d` and `newest archived day - 34 d`, is not among the newest 35
files, was not written this run and was not modified within 35 days; with the
clock more than 2 days past the newest archived line nothing is deleted. A
half-written gzip tail (a killed run) is truncated before the next append. An
undecodable member with intact members after it is on-disk corruption, not a
tail: the whole file is moved aside as `*.corrupt-<epoch>` (outside the day-file
namespace, so retention never deletes it - remove it by hand) and the run
reports `resync`.

**Entities and alerts.** `sensor.camera_engine_log_archive` (`scan_interval`
3600, `command_timeout` 600, 44 attributes); `binary_sensor.camera_log_archive_problem`
(the current run); `binary_sensor.camera_log_archive_down` (a failed run -
`error`, `unknown`, `busy` or `clock` - held 75 min, i.e. confirmed by the next
run); `binary_sensor.camera_log_archive_events` (a gap, resync or withheld event
younger than 35 days and newer than the last press of
`input_button.camera_log_archive_acknowledge`). The archive is also the fifth
member of `camera_monitor_stalled`, with a 3 h bar.

**Measured** in the core container: an hourly increment of 15,000 entries takes
about 2 s; the whole journal about 85-89 s, so a first backfill takes two to
three runs. `cam_logarchive.py --dry-run` fetches, collapses, redacts and scans, writes
nothing, and adds a pseudonym census to its note (counts only: tokens per
class, cameras, sdp pairs, whether the token-to-camera map is one-to-one,
attributable blocks, offset medians, push-notification dumps and how many carry
their camera's token); `--scan FILE...` reports per-file line,
withheld, residual and marker counts plus the same census, one per REKEY
segment, where a malformed token counts as a bad token. A line that makes the
redaction itself fail is withheld (`residual=redact_error`) instead of stopping
the run. `rss_mb` is the script's own peak resident set (`VmHWM`;
`getrusage().ru_maxrss` carried Home Assistant Core's high-water mark across the
exec). It does not track the work a run does: each run decompresses every member
of the three newest day files to find its resume marker, so the largest member
among them sets the peak. Measured on the first 21 runs (2026-10-03) it read
73-83 MB while the initial backfill member (13.6 MB of text) was among them, and
it is expected to fall once that file ages out of the newest three.

**Limits.** Redaction is pattern-based: a new identifier shape that has no key,
is shorter than the opaque and digit floors, and is not a JWT, IP, UUID or
colon-hex passes both scans - a `withheld` event or a nonzero `residual_run` is
the signal to add a rule. A run interrupted between two gzip members (only a run
that crosses midnight writes two) repeats its first member's lines on the next
run, which reports `resync`. Whether the journal persists across a host reboot
is unverified; if it does not, the lines from the last run to the reboot are lost
and reported as a `gap`.
