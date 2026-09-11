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
| `packages/cam_health.yaml`, `packages/cam_flap.yaml`, `packages/cam_motion.yaml`, `packages/cam_vision.yaml` | `/config/packages/` | `homeassistant: packages: !include_dir_named packages` |
| `automations/*.json` | HA **storage** automations (not files) | `POST /api/config/automation/config/<id>` |

## Deploy order

1. **Fill placeholders.** ALL FOUR scripts carry them — grep each for `<`.
   `scripts/cam_health.py` and `scripts/cam_vision.py` each need the HA host IP
   plus every camera's Scrypted device id and webhook token (how to obtain them:
   [`docs/migration-runbook.md`](../docs/migration-runbook.md)); they probe the
   same nine endpoints, so the two lists must match.
   `scripts/cam_motion.py` needs the nine camera entity stems (it queries
   `binary_sensor.<stem>_motion`) and, optionally, per-camera staleness
   overrides and `EXCLUDE_SPANS` for any period your recorder was not writing.
   `scripts/cam_flap.py` needs: `<scrypted_addon_slug>` (visible in the add-on's
   URL in the HA UI, e.g. `xxxxxxxx_scrypted`); its `CAMS` dict keys must
   byte-match the camera names Scrypted prints in log brackets (`[Front Door]`);
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
   Every fault alert also carries a `notify.<your_mobile_app_target>` action —
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
  blips self-heal in under ~22 min and paging on them is pure noise; and
  fleet-stale 5 min (fastest) — multiple cameras returning byte-identical
  frames simultaneously means a fleet-level snapshot-pipeline wedge.
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
- `camera_health_heartbeat` — daily proactive status card, sourced from ALL
  THREE monitors (snapshot probes, motion staleness, stream faults) with an
  honest "not reporting" fallback per line. "Probes OK" is the deliberate
  wording: the probe measures the snapshot path, not motion-event delivery,
  and a card that says "healthy" from one monitor while another is latched is
  a false all-clear.
- `go2rtc_reload_on_start` — reloads the go2rtc config entry 2 min after HA
  starts, healing the WebRTC live-view regression every restart causes.
- `front_door_doorbell_announce` — doorbell press → parallel TTS announce on
  two independent speaker paths (`continue_on_error` on both, so one path's
  failure can't silence the other) + a persistent notification.
- `cameras_motion_stale_alert` / `_clear` — fleet-wide dead-man's switch,
  checked hourly during active hours: if **zero** cameras report motion over
  a rolling 6-hour window, the motion pipeline itself is down (a single quiet
  camera is normal; a silent fleet is not). Fit that window to your own fleet's
  occupied data, not to intuition — on this one, 456 inter-event gaps over ten
  occupied days gave p50 0.04 h, p95 2.41 h, p99 6.45 h and a max of 8.46 h, so
  a 4-hour threshold fired on 4.0% of in-window checks (about one page every
  2.5 days) and every firing observed resolved itself with no intervention.
  Note two structural limits: it tests the **minimum** staleness across the
  fleet, so any one healthy camera suppresses it entirely — it can only ever
  catch a *total* pipeline outage, never a single camera — and the active-hours
  gate leaves it un-evaluable for the rest of the day. Per-camera failure is
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
`DOOR_MOTION_WINDOW_S` anywhere on the fleet.

Two deliberate limits. It does **not** adjudicate individual cameras: fitting a
per-camera expectation needs history that does not exist, since the add-on log
retains only ~2.3 days, and on this fleet the one plausible camera/door pairing
is already explained by that camera's ~13% historical co-fire rate with its own
neighbours — zero hits in ten openings is the *expected* outcome there, not a
fault. And it ships **without an alert threshold**: the orphan rate needs no
baseline in principle (its expected value is ~0, measured 1 of 26 openings over
2.3 days) but 26 openings is far too thin to fit a bar, and inventing one from
noise is a mistake this project has made before. The value now is that the
recorder starts accumulating durable door history — which is what makes both a
future threshold and a future door-based corroboration partner possible, and
which the 2.3-day log rotation otherwise made impossible.

- `camera_flap_alert` / `camera_flap_recovered` / `camera_flap_down` — the
  stream-fault monitor (see `docs/operations.md` §6): pages on a sustained
  per-camera **recording-error** rate, dismisses on recovery, and pages
  separately if the monitor itself sits in an error state for an hour
  (dead-man's switch - covers the watchdog dying too, since that starves
  the monitor's clock). Also carries a tripwire on cloud push-decryption
  failures - each one is a dropped motion push; a sustained climb means the
  event transport is degrading and the camera-source plugin needs re-auth.
- `camera_vision_monitor_down` / `_recovered` — dead-man for the visual monitor.
  It was the only one of the four without one, which mattered because its most
  likely failure is not a crash but going **blind while still running**:
  `cam_vision.py` rewrites its state file every cycle regardless of whether any
  given camera answered, and `cam_motion.py` used that file's mtime as its only
  freshness gate — so a camera nobody could actually see read downstream as a
  camera that was quiet, the reassuring direction. `cam_vision.py` now stamps a
  per-camera `probe_ts` on successful samples only, publishes `probe_age_min`
  and `blind`, and `cam_motion.py` refuses a verdict for any camera it could not
  see. Do not rely on the stream-fault monitor to notice the visual monitor
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
  interior room) returns "cannot be tested", and a partner too sparse to reach
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
  when nobody is at the property for weeks. The fleet-wide
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

Every fault alert writes a `persistent_notification` (the HA notification
centre) **and** sends a push via `notify.<your_mobile_app_target>`. Both matter:
persistent notifications are in-memory, so a restart erases every card with the
fault still latched — which is why each alert also carries a re-assert trigger —
and they are only visible to someone with the HA UI open, which is no use to an
operator who is away from the property. The push steps are gated to fire on new
information only, never on a re-assert.
