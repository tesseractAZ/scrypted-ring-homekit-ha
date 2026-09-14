# Operations guide — running the single-Scrypted-engine camera stack

Everything that runs *after* the [migration](migration-runbook.md) is done: making live view instant, getting motion and doorbell presses back into Home Assistant, watching for silently‑dead cameras, announcing the doorbell, keeping it all healthy, and what the monitoring itself costs (§9). Addresses/tokens/credentials are placeholders — substitute your own.

Conventions used below:
- `<HA_HOST_IP>` — the IP where HA (and the Scrypted add‑on) is reachable on your LAN.
- `<SCRYPTED>` — `https://<HA_HOST_IP>:10443` (admin) / `http://<HA_HOST_IP>:11080` (insecure).
- `<cam>` — a camera's short name; `<cid>` — its Scrypted device id; `<token>` — its per‑stream token.

---

## 1. Instant live view (and why prebuffer is not how you get it)

By default a Ring live view is **on‑demand**: opening it makes Scrypted negotiate a fresh stream with Ring's cloud, so first frame lands in ~3 s (and the HLS fallback path can be ~10 s).

**Prebuffer** keeps a warm rolling stream so live view opens in **~0.1 s**. **But the cost is severe and easy to miss: Ring progressively stops delivering motion events for a persistently‑streamed camera — wired or battery.** The cloud treats the camera as "in live view" and suppresses its event pushes, decaying over roughly a week-plus, camera by camera, while every pull path (streams, snapshots, probes) stays green — so HomeKit/HKSV and the MQTT sensors silently go blind. A one‑ or two‑day "motion still works" verification passes during the decay window and proves nothing. Ring's periodic Snapshot Capture also pauses on a streamed camera. **Default to prebuffer OFF; consider it only for a camera whose motion events you genuinely don't need.** One honest caveat: the suppression mechanism was **measured under a standard (non-24/7) Ring plan**. Under a plan with 24/7 continuous recording the camera is *always* streaming to Ring's cloud, which may change how additional client sessions are treated — unknown until tested. If you ever re-test, do it on ONE low-value camera with a per-camera motion dead-man watching it (see `cam_motion.py`), and hold the verdict for at least a week: the original suppression took ~10 days to fully manifest and short verifications pass falsely.

**If you accept that trade for a specific camera:**

1. In Scrypted, on each wired camera, set the Rebroadcast/prebuffer mixin's **Prebuffered Streams** to include the stream you serve (e.g. the WebRTC/RTSP stream).
2. **Reload the prebuffer plugin.** The buffer loop does **not** start on a live setting change — the setting looks like a no‑op until the plugin is reloaded (`plugins.reload('@scrypted/prebuffer-mixin')` via the client API, or restart the plugin from the UI).
3. Verify: a WebRTC/`camera.stream` request now returns in ~0.1 s instead of ~3 s.

**Cost:** roughly a few % CPU per continuous stream and a low‑bitrate substream's worth of bandwidth each. On a modern quad‑core host, all cameras prebuffered is typically <10% CPU. Confirm the streams **stay up** (no reconnect churn) — a soak that watches the online/​stream state for 20–30 min is the check.

**One camera may refuse to warm.** A camera whose encoder emits **keyframes on‑demand** (some models emit H.264 High profile with sparse keyframes) will log `Unable to find sync frame in rtsp prebuffer` — the buffer is warm but has no decodable frame, so it waits for the next keyframe and stays at ~3 s. This is a hardware/GOP property, not tunable from Scrypted; leave that one camera on‑demand (a continuous stream that never warms just wastes a session).

**Snapshots ride the prebuffer when it exists.** The snapshot plugin's "Snapshots From Prebuffer" defaults to using the warm stream — so after disabling prebuffer, cameras can throw snapshot **500s** until that setting is explicitly set to Disabled (falling back to Ring's stored snapshot / live capture). Expect ~10 s live-capture snapshots for the first minutes after the switch, until Ring's Snapshot Capture cache (§4) resumes on the no-longer-streamed camera.

---

## 2. go2rtc WebRTC survives with a reload-on-restart automation

HA's built‑in **go2rtc** serves the Generic Cameras' WebRTC live view, and the browser negotiates WebRTC first (falling back to HLS). But **an HA restart can leave go2rtc unable to serve the Generic‑Camera sources** — every `camera/webrtc/offer` returns `go2rtc_webrtc_offer_failed` even though the stream source is a perfectly valid RTSP URL. Live view then silently falls back to the slow HLS path.

**Fix (immediate):** reload the go2rtc config entry:

```
POST /api/config/config_entries/entry/<go2rtc_config_entry_id>/reload
```

(the equivalent WS `config_entries/reload` command is not available; use the REST endpoint). Within seconds all cameras answer WebRTC again at ~0.1 s warm.

**Fix (durable):** add an automation so it self‑heals after every restart. Trigger on HA `start`, wait a couple of minutes for Scrypted's rebroadcast to warm, then reload the go2rtc entry:

```yaml
alias: go2rtc - reload after HA start (restore camera WebRTC)
mode: single
trigger:
  - platform: homeassistant
    event: start
action:
  - delay: "00:02:00"
  - service: homeassistant.reload_config_entry
    data:
      entry_id: "<go2rtc_config_entry_id>"
```

> There is also an upstream `go2rtc async_close_session` `KeyError` that spams tracebacks under rapid WebRTC session churn (e.g. many offers in quick succession). It is benign for normal one‑view‑at‑a‑time use; the real fix is an HA core version that includes the upstream patch.

---

## 3. Motion + doorbell press → Home Assistant (MQTT)

When Scrypted becomes the sole Ring client, motion and doorbell events reach **HomeKit** but stop reaching HA (HA no longer runs the Ring integration). Restore them with Scrypted's **MQTT** plugin publishing to your broker via **Home Assistant MQTT discovery** — HA then materializes `binary_sensor`s automatically.

Do **not** use the reverse‑direction "Home Assistant" Scrypted plugin — that *imports HA entities into Scrypted*, the wrong way.

**Setup:**

1. Install `@scrypted/mqtt`. Point its **External Broker** at your MQTT broker (`mqtt://<HA_HOST_IP>:1883`) with a broker username/password (`<mqtt_user>`/`<mqtt_pass>`).
2. Apply the MQTT **mixin** to each camera (the mixin is stored in the device's `mixins` array — note it does **not** add a `mixin:@scrypted/mqtt` string to the `interfaces` list, so don't mistake its absence there for "not applied").
3. Scrypted publishes retained HA‑discovery config under `homeassistant/binary_sensor/<prefix>-<cid>/…`. HA auto‑creates:
   - `binary_sensor.<cam>_motion` (MotionSensor)
   - `binary_sensor.<cam>_online` (availability)
   - `binary_sensor.<doorbell>_...` (the doorbell's BinarySensor / press)

**Polish (optional):** the discovered entities come in with generic names and no `device_class`. Set `device_class: motion` / `connectivity` and rename via the HA entity registry. Because the plugin publishes **on change, non‑retained**, freshly discovered entities read `unknown` until the first event — publish a retained baseline (`false` for motion, `true` for online) so they show real state immediately.

**Verify it end‑to‑end** by watching a real motion event flow broker → HA and land on the sensor with the same timestamp; a state *transition* is the only proof (a static snapshot isn't).

**The event path can die while everything else stays green.** Motion/doorbell events ride a cloud **push subscription** inside the camera-source plugin; snapshots, live view, and health probes are all **pull** paths. The subscription can wedge silently — every stream keeps working, every snapshot probe passes, the MQTT broker and HA integration are healthy, and yet no motion event flows (HomeKit/HKSV stops getting motion too, so no clips record). Three operational consequences:

- **The discovery `_online` sensors cannot catch this** — they're seeded retained values that stay `on` with the bridge dead. Don't read 9/9 online as "events work."
- **Detection needs a motion-shaped dead-man**: alert when the *whole fleet* has reported zero motion over a rolling window, checked hourly and around the clock (see the shipped `cameras_motion_stale_alert`). Fixed twice-daily checks let a wedge run half a day. **Make it per-camera as well as fleet-wide**: a fleet-wide check only fires when *every* camera is silent, so one camera whose motion path dies stays invisible while its neighbours keep the fleet looking healthy — that blind spot hid a dead camera for a week here. Source the timestamps from the **recorder**, not entity `last_changed`, which resets on every restart and would mask staleness for the length of the window (see the shipped `cam_motion.py`).
- **A cloud PLAN change is a system event — treat it like a maintenance window.** Changing the camera subscription tier can reset the cloud clip-history epoch (queries return zero for *everything* older than the transition — an outage/reset, not evidence of missing recordings), alter snapshot cadence, and re-arm or alter motion settings server-side. After any plan change, re-verify every monitor's watched signals and re-measure baselines before trusting alerts or concluding anything from cloud-API results. Also note the clips API pages: expect a fixed cap per query (20 observed) served latest-first — count by windowed pagination, never one call.
- **Fix**: reload the camera-source plugin (rebuilds the subscription). If a fresh motion event still doesn't appear in the engine's own log, restart the engine outright — then prove recovery with a real end-to-end event, not with sensor timestamps (an HA restart refreshes `last_changed` on retained sensors, which looks deceptively like activity).

---

## 4. Snapshot freshness (Ring Snapshot Capture)

Enable **Snapshot Capture** in the Ring app (e.g. a 15‑second interval) on wired cameras. The *effective* refresh cadence is plan-dependent: measured ~15 s under a standard plan and ~11 s under a 24/7-recording plan with the identical frame-hash method — re-measure after any plan change rather than assuming. Ring then stores a fresh periodic snapshot that Scrypted serves quickly from cache, so dashboard tiles stay current and the webhook `takePicture` returns in ~0.1–0.5 s instead of forcing a slow live capture.

You can tell it's working by sampling a camera's snapshot hash over time: a camera on Snapshot Capture serves a **fresh cached frame on a periodic beat** (and returns fast), whereas a camera doing on‑demand live captures is slow and its image changes on every call. Enabling Snapshot Capture fleet‑wide also tends to *smooth out* the occasional slow‑capture stalls, because there's always a warm cached frame to serve.

---

## 5. Self-probing health watchdog

**Do not trust `/api/camera_proxy`** for health — it returns HTTP 200 with a **stale cached JPEG** when the upstream camera is dead, so it cannot detect failure. Probe the **honest** source: the Scrypted webhook snapshot endpoint on the insecure port.

A small `command_line` sensor runs a script that fetches every camera's `http://<HA_HOST_IP>:11080/endpoint/@scrypted/webhook/public/<cid>/<token>/takePicture` concurrently and classifies each:

- **down** — non‑200 / tiny body for **N consecutive** probes (tolerate a single transient miss).
- **frozen** — the **byte‑identical** JPEG returned for several consecutive probes (a healthy camera returns a *distinct* frame every probe, so an identical hash is a reliable stuck‑source signal). This catches a camera that returns a stale‑but‑valid 200 — the failure a proxy‑based check is blind to.
- **fleet‑stale** — *multiple* cameras byte‑identical to their previous probe in the same cycle. Per‑camera freeze needs several consecutive probes to be trustworthy, but a healthy camera re‑frames on every probe at this cadence — so several cameras simultaneously stale is near‑impossible unless the snapshot pipeline is wedged fleet‑wide. That makes it the *fastest* page tier (~5 min sustained) with a near‑zero false‑positive rate. Two validity caveats: it only holds at the production probe cadence (probes seconds apart legitimately read identical cloud‑cached frames), and the camera cloud's snapshot‑capture interval must stay well below the probe interval.
- **slow** — `takePicture` latency above a threshold for a couple of consecutive probes.

It emits one JSON object; the `command_line` sensor exposes `healthy` (0–N) plus `down_count` / `frozen_count` / `slow_count` and a `detail` map. A `template` `binary_sensor` trips on any of them.

**Tuning that matters (learned the hard way):**
- The probe fires all cameras **concurrently**, so a healthy camera can transiently spike to several seconds from contention — set the "slow" threshold *above* that contention band (near the request timeout), or it false‑flags healthy cameras.
- **Separate what pages from what's merely visible.** A camera actually *down* should alert fast (minutes). *slow/frozen* blips are usually transient stream jitter that self‑recovers in a few‑to‑tens of minutes — alert on those only if **sustained** past that window, or you'll page several times a night on nothing. Concretely: two alert triggers — `down_count>0` for ~8 min, and `(frozen_count>0 or slow_count>0)` for ~30 min. Recovery should silently dismiss, not post a "recovered" message per blip.

**Early‑warning for a systemic motion failure:** a companion automation flags **zero motion across *all* cameras**, checked hourly, 24/7, against a flat bar (9 h on this fleet). One quiet camera is normal; a silent fleet means the motion pipeline (Ring → Scrypted → MQTT → HA) is down. Fit the bar by simulating the checks the automation actually makes — not from gap *durations*, which is not what an hourly sampler ever sees — and avoid a day/night split: it classifies a gap by its start hour while the automation picks a bar at check time, so the two disagree whenever a gap crosses the boundary.

---

## 6. Diagnosing prebuffer flapping (stream-layer churn the watchdog can't see)

A prebuffered camera can silently **flap** — its rebroadcast session keeps dying and restarting (`timeout waiting for data, killing parser session rtsp` → `restarting prebuffer session in 5 seconds`, or `rtsp read loop exited`) — while the §5 watchdog reports it **healthy the whole time**. That's not a watchdog bug; it's a sampling gap. Each restart self‑heals in ~5 s, and the watchdog snapshot‑probes on a ~120 s beat, so the chance any given probe lands inside a restart is small (≈ restart_seconds ÷ probe_interval). A camera restarting its stream **dozens of times an hour** can still pass every snapshot probe, because between restarts `takePicture` returns a valid frame. The probe layer answers *"can I get a still right now?"* — it does **not** answer *"is the live stream stable?"*

**Measure the real rate at the stream layer — count restarts in the Scrypted log.** For each camera, count lines matching `\[<Camera>\].*restarting prebuffer` over a known window; that's the honest flap rate. Two traps make this deceptively hard:

- **Measure the window from real timestamps; fall back to the probe clock only if you must.** The supervisor log API's `?verbose=true` form prefixes each line with a real timestamp, so take the span from the first and last of those (what `cam_flap.py` does). On a non-verbose log the `takePicture` lines can serve as a clock, but the rate must include **every** prober: `window_minutes ≈ takePicture_count ÷ (camera_count × PROBERS ÷ probe_interval_minutes)`. With nine cameras and two probers every 120 s that is 9 `takePicture` per minute, not 4.5 (§9.2), and other snapshot clients such as dashboards inflate the count further, which is why the timestamp span is the reliable measure.
- **The supervisor add‑on‑log API returns the *oldest* retained entries by default, not the newest.** Fetching `GET /api/hassio/addons/<slug>/logs` with header `Range: entries=:0:N` returns a **frozen head slice** (the oldest N lines) — it does *not* advance between calls, so you can be analyzing hours‑old data while believing it's live. Confirm by fetching twice a couple of minutes apart: if the response is byte‑identical, you're on the frozen head. Use **`Range: entries=:-N:N`** (negative skip) to get the genuine live tail. This single trap can completely invert a conclusion — always verify your window is actually recent.

**Root cause is usually below Scrypted.** Ring cameras stream *through Ring's cloud relay*, so `timeout waiting for data` means that relayed feed starved — which points at (a) the camera's own **Wi‑Fi uplink** (weak 2.4 GHz, RF interference — a garage with a door opener / EV charger / metal shelving is a classic dead zone), (b) the camera **needlessly roaming** between mesh APs (Wi‑Fi "client steering" / band steering bouncing a *stationary* camera), or (c) Ring‑side relay flakiness. It is **not** something Scrypted config fixes.

**How to tell which, and what to do:**
- Change **one** network variable at a time and re‑measure the per‑camera rate before/after — the log often retains enough history to compare a pre‑change window against a post‑change one directly. Watch for a per‑camera *split* in the response: one camera improving while another doesn't under the same change narrows the diagnosis (roamer vs weak‑link). But hold any "this setting fixed it" conclusion until it survives several days across varying conditions — a single before/after window, especially one that includes a reboot, over‑credits the change.
- For a camera a network change **doesn't** help, check its actual **signal strength / AP association** (mesh app + the Ring app's Wi‑Fi reading). Strong signal ⇒ the cause is upstream (Ring relay), so stop changing the network.
- **Scrypted‑side mitigations for a stubborn flapper** (accept them only after the network is ruled out): point that camera's prebuffer at a **lower‑bitrate substream** (less data to starve), or **disable prebuffer on just that one camera** — you lose its instant live view (~3 s on‑demand) but stop the churn. The real cost of the churn isn't the ~5 s live‑view blips; it's that a motion event landing on a restart can **truncate that camera's HKSV clip** — so fix your *highest‑value* camera (the doorbell) before a low‑value one. (The prebuffer *window length* is not among the levers: current prebuffer‑mixin hardcodes ~10 s with no duration setting.)

**Make the flap rate a first‑class sensor.** Rather than re‑running this log analysis by hand, deploy the flap monitor in [`ha/`](../ha/): a command_line sensor (`ha/scripts/cam_flap.py`, 10‑min scan) parses the add‑on log tail over a fixed time window (`WINDOW_MIN`, 6 h here) measured from the log's own `?verbose=true` timestamps, and publishes per‑camera **HKSV recording errors/hr** — worst camera as the state (graph it and diurnal patterns become visible), the full per‑camera table plus stream faults, undecryptable pushes, probe shortfall and door‑contact coverage as attributes — plus alert/recovery/dead‑man automations. Design points that came out of adversarial review, worth keeping in any re‑implementation:

- **Bound the rate window by TIME, not by line count.** The same 60k‑line tail spanned anywhere from 106 to 727 minutes here, so identical error counts published rates differing ~7×. Clip to a fixed `WINDOW_MIN` before counting.
- **No timestamps ⇒ no rate.** If the tail carries no verbose timestamps, or the measured span is under `MIN_SPAN_MIN`, fail to an explicit error state rather than publishing anything.
- **Per‑camera thresholds.** One known‑chronic camera shouldn't force fleet‑wide desensitization; give it its own raised threshold and keep the default tight for the healthy ones.
- **A dead‑man automation for the monitor itself.** Every error path above lands in a visible "monitor down" page after an hour — an advisory that dies silently is worse than none, because it converts "unmonitored" into "assumed healthy."
- **A log‑parsing monitor dies silently when the log stops saying that.** A counter keyed to one log string reads zero forever once the underlying behaviour changes — and zero is indistinguishable from healthy. That happened here: a prebuffer‑restart counter kept reporting "no faults" for nine days after prebuffer was disabled fleet‑wide made its string impossible. Pin any log‑derived metric to a string you have *just* confirmed still occurs, re‑confirm it whenever you change the thing it watches, and prefer counting the **harm** (a failed recording) over a particular **mechanism** (a restart) — mechanisms get configured away.
- **Measure the window, don't infer it.** The supervisor log API's `?verbose=true` form prefixes real timestamps; deriving the span from those removes a whole class of error. Inferring it from probe cadence ran ~11% long here, because the dashboard's own snapshot pulls inflated the count the clock was based on.
- **Per‑camera asymmetry is usually the whole story — but compare against *expected*, not the fleet median.** One camera behaving unlike the other eight is the signal worth chasing. A median is skewed by unevenly distributed dashboard traffic, which can make a perfectly‑probed camera look starved; comparing each camera against the count you *expect* from the probe interval avoids that false positive.


---

## 7. Doorbell announcements

With the doorbell press restored as a `binary_sensor` (§3), announce it. Trigger on the doorbell sensor → `on`, then speak on your media players. Two robustness notes:

- **Route the announcement so one speaker can't silence the rest.** Put a primary/priority speaker on its **own** action branch in `parallel:` with the group, each `continue_on_error: true`, so a failure of one path doesn't abort the others — and they play simultaneously (no stagger, no double‑announce).
- **Always fire a notification too**, so even if audio fails the doorbell is never fully silent.

Example shape:

```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.<doorbell>_doorbell
    to: "on"
action:
  - parallel:
      - continue_on_error: true          # the group
        service: media_player.play_media
        target: { entity_id: [ <speaker_a>, <speaker_b>, ... ] }
        data: { media_content_id: "media-source://tts/<tts_engine>?message=Someone is at the front door.", media_content_type: music, announce: true }
      - continue_on_error: true          # a priority speaker, isolated
        service: tts.speak
        target: { entity_id: <tts_engine> }
        data: { media_player_entity_id: <priority_speaker>, message: "Someone is at the front door." }
  - service: persistent_notification.create
    data: { notification_id: doorbell, title: "🔔 Front door", message: "Doorbell pressed." }
```

> If your speakers are all fronted by one integration (e.g. a media server), a full outage of that integration silences audio everywhere — the notification is your floor. For true audio resilience, keep one speaker on an independent integration.

---

## 8. Backups & housekeeping

- HA auto‑creates a backup before every core/add‑on update. With a **time‑based** retention (`days`) these accumulate without bound against an event‑driven trigger. Switch to **count‑based** retention (`copies: N`) so it self‑trims:
  ```
  WS: {"type":"backup/config/update","retention":{"copies":20,"days":null}}
  ```
  Automatic retention prunes only *automatic* backups; a **manual** full backup is exempt, so keep one manual full backup as a stable restore point.
- Trigger a full backup with `POST /backups/new/full`. Note the supervisor API returns an empty `unknown_error` on this call when it exceeds the proxy timeout — the backup keeps running; poll `/jobs/info` (`backup_manager_full_backup`) and `/backups` for completion rather than trusting the immediate response.

---

## 9. Performance, load, and resource cost

Four `command_line` sensors run the monitoring stack on three poll intervals (120 s, 600 s and 1800 s). Two of them probe the same nine snapshot endpoints on the same 120 s interval. On the reference host every script finishes well inside its `command_timeout` (§9.3). This section writes the load down so that it is *known*. A change that doubles it then becomes visible, and the constants that depend on it, `PROBERS` above all, have a stated reason.

Figures are labelled as follows:
- **(derived)**: arithmetic from the shipped constants, or from measured figures quoted beside it.
- **(measured on the reference fleet, `<date>`)**: taken on the nine-camera reference installation (Raspberry Pi 5, 8 GB, Home Assistant 2026.9.2, recorder schema 53). "Reference host" means that machine. Unless stated otherwise, host timings were taken in its SSH add-on container, whose Python (3.14.7) is not the interpreter the sensors run under.
- **(measured on a laptop, `<date>`)**: a development machine (Apple M5, CPython 3.14) running the shipped code against copies of reference-fleet data.
- **(measure on your box)**: depends on hardware, snapshot size or recorder contents. A command is given where one is safe to run.

### 9.1 Poll rate and process churn

| Sensor | `scan_interval` | `command_timeout` | Polls/day (derived) | What one completed run does |
|---|---|---|---|---|
| `sensor.camera_flap_rate` | 600 s | 90 s | 144 | Fetches the last 60,000 lines of the engine add-on log from the Supervisor (`timeout=45`), parses them, judges door trips, then atomically rewrites `/config/.cam_flap_door_state.json` (a rolling 7-day trip record) |
| `sensor.camera_health` | 120 s | 30 s | 720 | 9 concurrent `takePicture` fetches (`max_workers=9`) and a SHA-256 of each body, then rewrites `/config/.cam_health_state.json` |
| `sensor.camera_motion_stale` | 1800 s | 60 s | 48 | 3 read-only recorder queries and the corroboration maths; reads the vision and door state files, then rewrites `/config/.cam_motion_state.json` |
| `sensor.camera_visual_activity` | 120 s | 60 s | 720 | 9 concurrent `takePicture` fetches (`max_workers=len(CAMS)`), then a PIL decode, thumbnail and block-diff of each frame, one at a time in the main loop; then rewrites `/config/.cam_vision_state.json` |

**1,632 script launches per day** (derived: 720 + 720 + 144 + 48). Each poll runs the configured `python3 /config/scripts/…` command as a new process. There is no long-lived worker, so every run pays interpreter start-up and its imports again. That includes `cam_vision.py`'s `PIL` import, 720 times a day.

The polls/day column is a ceiling, not a guarantee. In current Home Assistant core source (`command_line` sensor, read 2026-09-14), each sensor runs once when it is added at Home Assistant start and then on its interval. A scheduled update is skipped, with a warning, while the same sensor's previous update is still in progress.

**Start-up cost.** Measured on the reference host, 2026-09-14, as best of 10 runs including process creation:
- A bare interpreter start took about 17 ms wall-clock.
- A start that imports `json`, `urllib.request` and `sqlite3` took about 80 ms.

At 1,632 launches that is about 28 s/day of bare start-up. If every launch paid the import figure, it would be about 2.2 min/day, not counting PIL (derived). The SSH add-on's interpreter has no PIL, and import cost depends on the interpreter and its bytecode cache. Measure from a shell where `python3` is the interpreter the sensors use (measure on your box):

```sh
python3 -m timeit -n 1 -r 20 -s "import subprocess" \
  "subprocess.run(['python3', '-c', 'import json, urllib.request, sqlite3, PIL.Image'])"
```

**State-file writes.** Every completed run of each script rewrites its state file. On the reference fleet, 2026-09-14, `.cam_vision_state.json` measured 43,965 bytes and `.cam_health_state.json` measured 1,160 bytes. At 720 rewrites a day each, that is about 32 MB/day and 0.8 MB/day written (derived). `.cam_flap_door_state.json` is rewritten on every flap poll that gets past the log fetch and the timestamp checks, whether or not a new trip was judged. Its size is covered in §9.6.

### 9.2 Snapshot probe load and the two-prober doubling

**`cam_health.py` and `cam_vision.py` probe the identical nine webhook `takePicture` URLs on the identical 120 s interval.** On the reference fleet the two deployed URL sets hash identically (checked 2026-09-14). The two scripts share nothing: each downloads every JPEG in full, `cam_health.py` hashes it and discards it, and `cam_vision.py` decodes it.

The shipped code does not share the fetch and does not offset the two schedules. This is a known open issue, not a tuned choice (§9.9). There is a trade-off: each monitor has its own dead-man (`camera_health_monitor_down`, `camera_vision_monitor_down`). One shared fetch would halve the probe load but tie the two monitors' failures together.

**Load** (derived):
- Per camera: `PROBERS × 60 ÷ scan_interval` = 2 × 60 ÷ 120 = **1.0 request/min**, which is 60/h or 1,440/day.
- Fleet of nine: **9 requests/min = 540/h = 12,960/day**, twice what one prober would generate.

**Measurements that agree with the derivation:**

- **Code comment.** The comment on `PROBERS` in `ha/scripts/cam_flap.py` records two figures measured 2026-08-30:
  - 34,807 `takePicture` requests in 62.7 h. That is 555/h, 2.06× a single prober and 2.8 % above the derived 540/h.
  - `probe_counts` of 362 against an expected count of 180. The formula gives 180 for a 360-minute window when the prober factor is left out. With `PROBERS = 2` the same window expects 360 (derived).
- **Engine log.** 51.4 h, 2026-09-12 17:54 to 09-14 21:17 UTC (measured on the reference fleet): 29,572 `takePicture` requests, or 575/h. Of those, 533/h arrived in the regular 120 s probe bursts. The rest came in four irregular bursts of 130–1,712 requests outside that cadence, from other snapshot clients such as dashboards (not attributed further).
- **Live attributes.** 2026-09-14 21:37 UTC (measured on the reference fleet): `span_min` 338, and `probe_counts` 338–339 per camera. The expected count is 338 ÷ 2 × 2 = 338, so the shortfall bar at that span is 0.75 × 338 ≈ 254 (derived).

**How the detector uses it** (`main()` in `cam_flap.py`). It computes `expected = span_min ÷ PROBE_INTERVAL_MIN × PROBERS`. A camera goes into `probe_shortfall` when its count is below `PROBE_SHORTFALL` (0.75) × `expected`, and only when `expected` ≥ 10. `expected` is internal and is not published as an attribute.

Consequences:

- **`PROBERS` is load-bearing.**
  - **If set to 1 while two sensors probe:** the bar drops to 0.75 × half the real rate, about 37 % of it (derived). A camera would have to lose about 63 % of its probes to be flagged, instead of 25 %. A prober that stops completely drops every camera to about 50 %, which is still above that bar, so it goes unflagged.
  - **At the shipped `PROBERS = 2`:** a prober that stops puts all nine cameras at about 50 % of expected. That is below the 75 % bar, so all nine appear in `probe_shortfall`.
  - **Not a paging path:** `probe_shortfall` has no automation of its own and shows up only in the sensor's attributes and `summary`. A stopped prober is paged by that monitor's own dead-man.
- **Manual log-window arithmetic needs the same factor.** When `takePicture` lines are used as a clock, as in §6: `window_minutes ≈ takePicture_count ÷ (camera_count × PROBERS ÷ probe_interval_minutes)`. On this fleet that is 9 per minute, not 4.5. Leaving out `PROBERS` doubles the computed window. Non-probe snapshot traffic inflates the count further (575/h against 540/h in the 51.4 h log above), so a window measured from the verbose timestamps is more reliable.
- **The two probers run in phase, including after every restart.** The shipped configuration does not offset them, and on the reference fleet they fire together:
  - **Recorder, 14 days to 2026-09-14.** Ten Home Assistant restart periods fell in that span; the two shorter than 7 minutes were too short to measure. In the other eight, `sensor.camera_visual_activity`'s `last_updated` trailed `sensor.camera_health`'s by a median of 0.06 s (90th percentile ≤ 0.3 s). This held even though the absolute position within the 120 s cycle changed at each restart (measured on the reference fleet).
  - **Engine log above.** 1,516 of 1,527 probe bursts were exactly 18 requests, two per camera, with a median spread of 0.19 s and a 120 s period. After the one Home Assistant restart inside that log, 874 of 874 bursts were (measured on the reference fleet).

  So in normal operation the engine gets **18** near-simultaneous `takePicture` requests every cycle. It is not a chance alignment, and restarting Home Assistant does not change it. What causes the alignment was not established.
- **The slow tier was calibrated before the second prober existed.** `SLOW_SECS = 9.0`, `SLOW_AFTER = 2` and the 4–7 s concurrent-probe contention band quoted in `cam_health.py` date from the initial release (2026-07-22). `cam_vision.py` was added on 2026-08-11, so that band describes 9-way concurrency, not the current 18-way. Measured under current conditions, over 7 days to 2026-09-14 on the reference fleet (`detail` records a latency only when it is ≥ 1 s):
  - 80 of 1,577 recorded `sensor.camera_health` updates had at least one camera at ≥ 1 s, 207 camera-samples in all.
  - The median of those samples was 1.8 s. 6 fell between 4 and 7 s, and 31 were between 9 and 10.2 s.
  - 17 updates carried `slow_count` > 0.

  Nobody has measured whether offsetting the two schedules would shorten that tail.
- **Checking phase on your own box.** Count `takePicture` lines per 120 s burst in the add-on's verbose log: 18 per burst for nine cameras means in phase, and two separate bursts of 9 means offset. Alternatively, compare the two sensors' `last_updated` modulo 120 s. `last_updated` marks when a run finished and moves only when the payload changes, so use a moment when both have updated recently.

### 9.3 Per-script runtime, and what dominates each

All timeouts below come from the shipped scripts and packages. Host figures were measured on the reference host on 2026-09-14 (SSH add-on container, Python 3.14.7, SQLite 3.53.4). Neither `cam_flap.py` nor `cam_motion.py` was run on the host, because both write state files; their parts were measured separately. Laptop figures are from an Apple M5 with Python 3.14.5.

**None of the four scripts has a wall-clock ceiling of its own.**
- Three of them rely on `urllib` socket timeouts. Python applies those to each blocking socket operation (the connect, each read), not to the whole request. A peer that never answers is cut off at the timeout. A peer that answers slowly but steadily is not.
- The fourth, `cam_motion.py`'s `QUERY_TIMEOUT_S`, is a SQLite lock wait and does not limit how long a statement runs.

So the headroom ratios below cover the common failure, a peer that does not answer at all. They are not a guarantee.

| Script | `command_timeout` | Own timeout (code) | What dominates a run | Headroom |
|---|---|---|---|---|
| `cam_health.py` | 30 s | `TIMEOUT = 15` per socket operation; 9 fetches in parallel (`max_workers=9`) | The nine webhook fetches. In one healthy sample every camera answered in under 1 s: all nine `detail` entries read `ok`, which the script prints only below 1 s (measured on the reference fleet, 2026-09-14). | **2.0×** against a fetch that never answers (derived: 30 ÷ 15) |
| `cam_vision.py` | 60 s | `TIMEOUT = 12` per socket operation; fetches in parallel (`max_workers=len(CAMS)`) | First the fetches, then nine PIL decodes run **one after another** in the main loop. After that come `block_diffs` (3.0 ms for all nine) and the state-file rewrite (0.8 ms JSON round-trip of the ~44 KB state), both measured on the reference host. | 5.0× before decode and the PIL import (derived: 60 ÷ 12). Decode time not measured (measure on your box). |
| `cam_flap.py` | 90 s | `timeout=45` per socket operation on the Supervisor log fetch | The fetch: 8.41–8.45 s for 60,000 lines (5.6 MB) across four samples. Everything after it takes about 0.14 s. Both measured on the reference host. The 0.14 s excludes the door-record rewrite, which was not run there (~2 ms on a laptop). | **2.0×** against the socket timeout (derived: 90 ÷ 45); ~10× against the measured run (derived: 90 ÷ ~8.7 s) |
| `cam_motion.py` | 60 s | `QUERY_TIMEOUT_S = 20`, passed to `sqlite3.connect(timeout=…)`. This is a lock wait, not a limit on statement time. | Three recorder statements, 0.125–0.127 s together (§9.5). The largest is the all-entity 24 h gap query, 0.092–0.099 s. The co-change loop took about 2 ms, with 1–28 vision-log entries per camera that day. All measured on the reference host. | No ceiling of its own. About 0.13 s was measured, so headroom was ample that day (derived). See §9.5 for the query plans and §9.8 for the loop's worst case. |

Interpreter start-up is small against every timeout: about 17 ms bare and 80 ms with the standard-library imports (§9.1). `cam_vision.py` also imports PIL, which was not measured (measure on your box).

**What an overrun does.** At `command_timeout`, Home Assistant's `command_line` integration stops waiting, logs `Timeout for command: …`, and gets no output. The sensor's state becomes `unknown` and its JSON attributes are cleared (Home Assistant `command_line` source, checked 2026-09-14). A single overrun does not send an alert. The monitor-down automations wait 30 min (health, vision) or 60 min (flap, motion) before firing, and the next successful run cancels the wait.

To time a script where the sensors actually run, use `time python3 /config/scripts/<script>.py > /dev/null`. A manual run is a real poll: it advances the same streak counters and rewrites the same state files as a scheduled run. Time it once, not in a loop. For `cam_motion.py`, use the read-only snippets in §9.5 and §9.8 instead.

Two results worth stating, so that optimisation effort goes to the right place:

- **The log parse is not where `cam_flap.py` spends its time.** Measured on the reference host against a live 60,000-line payload (2026-09-14):
  - `decode().splitlines()`: 14 ms.
  - The per-line scan: 125 ms. It runs both name regexes, the probe accounting, and the recording-error, push and five stream-fault substring checks.
  - `judge_doors`: 0.2 ms.

  On a laptop, the full post-fetch path of the deploy copy takes about 37 ms per 60,000-line slice of a redacted engine log (measured: three slices, median of seven runs). That includes about 2 ms for `update_rolling`'s door-record read and atomic rewrite. Against an 8.4 s fetch, the parse is under 2 % of the run (derived). The runtime is the Supervisor producing 60,000 journal lines, and a faster parser would not change that. The `WINDOW_MIN` trim only runs when those lines span more than 360 min. It shortens the scan that follows it: when forced on a laptop, the trimmed run was faster than the untrimmed one (measured).
- **`block_diffs` is not the cost in `cam_vision.py` either.** It does 48 blocks × 30 px = 1,440 inner iterations per camera (derived). That takes 0.67 ms for all nine cameras with the shipped function on a laptop, and 3.0 ms with a line-for-line copy on the reference host (measured). It only runs when a same-mode baseline exists that is no older than `BASELINE_MAX_AGE_S` and the frame bytes differ.

  The cost that has *not* been measured is the PIL path, run one frame at a time for nine frames: `Image.open`, `thumbnail((96, 60))`, then an HSV and an L conversion. `thumbnail()` calls JPEG `draft()` with its default `reducing_gap` of 2.0, which makes a 192×120 request. `draft()` then decodes at 1/2, 1/4 or 1/8 scale, but only when the frame is at least 384×240 (Pillow documentation and source; size derived). Whether that applies depends on your snapshot resolution, so time the decode on your box before changing the resize.

### 9.4 Memory

Each poll is a fresh process, so every figure here is a short-lived per-poll peak, not steady-state usage.

- **`cam_flap.py` has the largest measured peak.**
  - **Peak size.** `raw.decode("utf-8", "replace").splitlines()` briefly holds three things at once: the raw bytes, the decoded string and the 60,000-element list. `del raw` follows. Peak Python-heap allocation was **3.51–3.53× the payload** (tracemalloc). A 5.6 MB live payload peaked at 19.8 MB on the reference host (2026-09-14), and 5.5 MB redacted slices gave the same ratio on a laptop.
  - **After the split.** Once `del raw` runs, the list alone is about 1.5× the payload (8.5 MB). Nothing later in `main()` raised the peak.
  - **RSS and frequency.** Process RSS rose about 20 MB over an import-only baseline (24 → 44 MB, measured on a laptop). This happens 144 times a day.
  - **Why the ratio holds.** The payload is pure ASCII (0 non-ASCII bytes, measured). CPython stores a string at 2 or 4 bytes per character once it contains any character above U+00FF. That includes the U+FFFD that `"replace"` inserts for invalid UTF-8. So a single such character in the log widens the decoded string and adds one to three payload-sized copies to the peak (derived).
  - **The trim.** The `WINDOW_MIN = 360` trim builds a second list of references to the same strings (8 bytes per kept line on a 64-bit build, derived). The first list is released when `lines` is rebound. The trim did not run on 2026-09-14, because the 60,000 lines spanned only 338–348 min (measured on the reference fleet).
  - **The door record is small.** The live payload held 2 door openings and 10 motion lines (measured). On every poll that reaches it, `update_rolling` loads and atomically rewrites `/config/.cam_flap_door_state.json`, which keeps only the last `DOOR_ROLLING_D = 7` days of judged trips (sizes in §9.6).
- **`cam_vision.py` keeps all nine JPEG bodies in `results` for the whole of `main()`.** The list is never released, so the bodies are still in memory while the state file is written.
  - **Compared with `cam_health.py`.** `cam_health.py` hashes each body inside its worker and returns only the digest, so each body is freed when its probe returns. With nine fetches in flight, both scripts can briefly hold nine bodies; the difference is how long they are kept, not how many.
  - **Unmeasured parts.** Snapshot size depends on the camera, and so do the PIL import and the decode buffers (measure on your box).
  - **The state file.** It is loaded and rewritten as a dict: 43,965 bytes on the reference fleet, of which **17,397 bytes (40 %) are the per-camera `frame` key** (measured, 2026-09-14). On 9 of 9 cameras that key held a copy of `frames[mode][0]`, the current mode's baseline. Nothing reads it in normal operation. `cam_motion.py` reads only `probe_ts` and `log`. `cam_vision.py`'s legacy migration reads `frame` only when `frames` is empty or missing.
  - **Known open issue in the shipped code.** The duplicate costs about 12.5 MB/day of redundant writes (derived: 17.4 KB × 720).
- **`cam_motion.py`** holds the 30-day motion `on` rows as (timestamp, name) tuples: about 1,450 rows on the reference fleet (measured, 2026-09-14). The count depends on motion activity and on recorder retention (§9.5). It also loads the ~44 KB vision state, the door record and its latch file. All small.
- **`cam_health.py`** holds nine digests and a 1,160-byte state file (measured). Negligible.

Measure the log payload yourself, since it is the figure that varies most with how chatty the engine is. Run this from a shell with `$SUPERVISOR_TOKEN` in its environment and Supervisor API access. The snippet prints only sizes and timing, because the add-on log can contain credentials; never print its content.

```sh
python3 - <<'PY'
import os, time, urllib.request
r = urllib.request.Request(
    "http://supervisor/addons/<scrypted_addon_slug>/logs?verbose=true",
    headers={"Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"],
             "Range": "entries=:-60000:60000"})
t = time.monotonic()
b = urllib.request.urlopen(r, timeout=45).read()
t = time.monotonic() - t
n = max(b.count(b"\n"), 1)
print(len(b), "bytes", n, "lines", len(b) // n, "B/line", round(t, 1), "s fetch",
      "->", round(len(b) * 144 / 1e9, 2), "GB/day at 144 fetches")
PY
```

On the reference fleet, 60,000 lines came to 5.62–5.64 MB (93.7–93.9 B/line) and took 8.41–8.45 s to fetch across four samples (measured, 2026-09-14). That works out to about 0.81 GB/day (derived: 5.63 MB × 144) and about 20 min/day of fetch time (derived: 8.4 s × 144). The data never leaves the host, but the Supervisor does real work to produce it every 10 minutes.

`WINDOW_MIN = 360` trims *after* the fetch. According to the in-code note, the 60,000-line span has ranged from about 106 to about 727 min (measured on the reference fleet; date not recorded). At 727 min, about half of each fetch is thrown away as soon as it arrives (derived: 1 − 360 ÷ 727). On 2026-09-14 the span was 338–348 min, so nothing was thrown away (measured). The request asks for a fixed number of lines (`Range: entries=:-60000:60000`), so it costs the same even when less time is needed. Whether a time-bounded request could replace it has not been evaluated.

### 9.5 The three SQL queries

`cam_motion.py` opens the recorder database read-only (`file:/config/home-assistant_v2.db?mode=ro`) once per poll (`scan_interval: 1800`). It runs three `SELECT` statements on one connection, in the order below.

**Measurement setup** (measured on the reference fleet, 2026-09-14):
- Raspberry Pi 5, Home Assistant 2026.9.2, recorder schema 53.
- A 764 MB database in WAL mode, with `sqlite_stat1` statistics present (about 1.8 M `states` rows).
- 58.4 days of history held.
- The plans were identical in two tools: the SQLite 3.53.4 command-line shell (`.timer on`), and Python's `sqlite3` module inside the Home Assistant container (SQLite 3.53.2). The Python run bound parameters the same way the script does. Its `gap_sql` result matched the live sensor's `host_gap_min`.
- The page cache was not controlled, and the database was in use the whole time.

| Statement | Scope | Observed plan | Rows | Time |
|---|---|---|---|---|
| `sql`: latest `on` per camera | The nine `binary_sensor.<cam>_motion` entities. **No time bound.** | `SEARCH m USING COVERING INDEX ix_states_meta_entity_id (entity_id=?)`, then `SEARCH s USING INDEX ix_states_metadata_id_last_updated_ts (metadata_id=?)`. `state` is not in that index, so every retained row of the nine entities is fetched to test `state = 'on'`. `GROUP BY` runs in `entity_id` order and needs no temp B-tree. | 10,906 examined, 4,983 `on`, 9 returned | 0.023–0.027 s |
| `ev_sql`: motion `on` rows for corroboration | The same nine entities, newer than `now − CORROBORATE_LOOKBACK_D` (30 d) | The same two searches, with the time range inside the index (`metadata_id=? AND last_updated_ts>?`). Then `USE TEMP B-TREE FOR ORDER BY` merges the nine entities by time. | 1,446 returned | 0.009–0.010 s |
| `gap_sql`: largest recorder hole across all entities | **Every entity**, newer than `now − HOST_GAP_LOOKBACK_H` (24 h) | `SEARCH states USING COVERING INDEX ix_states_last_updated_ts (last_updated_ts>?)`, inside two co-routines. The index already returns rows in `last_updated_ts` order, so the `LAG() OVER (ORDER BY last_updated_ts)` window needs no sort. | 76,066 examined (317 entities), 1 returned | 0.092–0.099 s |

All three statements together take **0.125–0.127 s** over three consecutive in-container runs (measured on the reference fleet, 2026-09-14). That is about 0.2 % of `command_timeout: 60` (derived). The code comment "Measured cost ~0.1 s" for `gap_sql` agrees with this. The figure depends on the instance's write volume over 24 hours, so it will differ on other installs.

How each statement scales (derived):

- **`sql` reads the full retained history of the nine motion entities.** It has no time bound, so its cost grows with retention, up to the `purge_keep_days` limit. The reference fleet writes about 187 rows/day for these entities. After a year that would be about 68,000 rows, roughly 6× the 2026-09-14 read, or about 0.15 s if cost stays linear. That is no timeout risk, but it is the only one of the three statements whose cost keeps growing. This is a known open issue in the shipped code (§9.9).
- **`ev_sql`** is limited by its 30-day window. Its cost scales with motion activity.
- **`gap_sql`** is limited by its 24-hour window. Its cost scales with the write rate of **every** entity in the instance, not with retention.

**The standalone index is part of the current recorder schema.** `ix_states_last_updated_ts` is declared on `States.last_updated_ts` (`index=True`). The schema-31 migration creates it, and no later migration drops it (checked in the Home Assistant 2026.9.2 source). A recorder at schema 31 or newer therefore has it, unless someone removed it by hand. The script does not check for it.

Without that index, the planner behaved as follows (observed on the reference recorder, 2026-09-14):

- **Forced onto the composite index** (`INDEXED BY ix_states_metadata_id_last_updated_ts`): the planner chose a skip-scan, `SEARCH states USING COVERING INDEX ix_states_metadata_id_last_updated_ts (ANY(metadata_id) AND last_updated_ts>?)`, followed by `USE TEMP B-TREE FOR ORDER BY`. It took 0.147–0.157 s. Skip-scan depends on the planner's statistics, so don't expect it on a database without `sqlite_stat1`.
- **No usable index** (`NOT INDEXED`): the plan is `SCAN states` plus `USE TEMP B-TREE FOR ORDER BY`, meaning the whole table is read and sorted on every poll. This plan was not timed on the live recorder because it reads the entire table. If you ever see this plan, measure it on your box.

Check the plans and timings read-only. **Do not** time `python3 /config/scripts/cam_motion.py` by hand: every run rewrites its proof-latch file, `/config/.cam_motion_state.json`. The snippet below runs the same three statements with the same bound parameters and writes nothing. Its statements matched the shipped `main()` exactly on 2026-09-14. Run it from any shell where `/config` is readable and Python 3 has `sqlite3`:

```sh
python3 - <<'PY'
import ast, re, sqlite3, time
src = open("/config/scripts/cam_motion.py").read()
cams = ast.literal_eval(re.search(r"^CAMS = (\[.*?\])", src, re.S | re.M).group(1))
ents = ["binary_sensor.%s_motion" % c for c in cams]
ph = ",".join("?" * len(ents))
# Copied from main() in cam_motion.py - re-copy if the script changes.
sql = ("SELECT m.entity_id, MAX(s.last_updated_ts) "
       "FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id "
       "WHERE m.entity_id IN (%s) AND s.state = 'on' "
       "GROUP BY m.entity_id" % ph)
ev_sql = ("SELECT s.last_updated_ts, m.entity_id "
          "FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id "
          "WHERE m.entity_id IN (%s) AND s.state = 'on' AND s.last_updated_ts > ? "
          "ORDER BY s.last_updated_ts" % ph)
gap_sql = ("SELECT MAX(gap) FROM (SELECT last_updated_ts - "
           "COALESCE(LAG(last_updated_ts) OVER (ORDER BY last_updated_ts), ?) "
           "AS gap FROM states WHERE last_updated_ts > ?)")
c = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True)
now = time.time()
for name, q, p in (("sql", sql, ents),
                   ("ev_sql", ev_sql, ents + [now - 30 * 86400]),
                   ("gap_sql", gap_sql, [now - 24 * 3600] * 2)):
    plan = " | ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + q, p))
    t = time.perf_counter()
    n = len(c.execute(q, p).fetchall())
    print("%-7s %.3f s %6d rows  %s" % (name, time.perf_counter() - t, n, plan))
PY
```

What to expect in the output:
- **`sql` and `ev_sql`:** `ix_states_meta_entity_id`, then `ix_states_metadata_id_last_updated_ts`.
- **`gap_sql`:** `SEARCH states USING COVERING INDEX ix_states_last_updated_ts`. If `gap_sql` shows `ANY(metadata_id)` or a temp B-tree instead, the standalone index is not being used. If it shows `SCAN states`, the whole table is read on every poll.
- The Python that runs the snippet may use a different SQLite version from the one inside the Home Assistant container.

Two related traps:

- **`QUERY_TIMEOUT_S = 20` limits how long the script waits for a lock. It does not limit how long a query runs.**
  - The value is passed to `sqlite3.connect(timeout=…)`, which sets how long to wait on a locked database. A development machine showed the difference (SQLite 3.53.1, 2026-09-14): a connection with `timeout=0.2` finished a 0.56 s `SELECT` without error, while a connection with `timeout=0.5` that hit an exclusive lock raised `database is locked` after 0.55 s.
  - Nothing else in `cam_motion.py` limits its run time: there is no progress handler and no alarm. `command_timeout: 60` is therefore the only ceiling, for the SQL and the corroboration maths alike. The other three scripts at least set network timeouts on their fetches (15 s, 12 s, 45 s).
  - **This is a known open issue in the shipped code and is not fixed.** With the plans above, the SQL uses about 0.13 s of the 60 s.
  - **If a run exceeds the limit** (Home Assistant 2026.9.2 source):
    - The `command_line` integration logs `Timeout for command: python3 /config/scripts/cam_motion.py` and discards the output. Nothing in that code path stops the script, so an over-limit run can still be running when the next poll starts (derived).
    - The sensor goes `unknown` with its attributes cleared, and the template `binary_sensor.camera_motion_stale` reads `unavailable`.
  - `camera_motion_monitor_down` fires only once that state has lasted 60 minutes. At the 1800 s poll, that means about three over-limit runs in a row (derived).
  - The alert reads as a dead monitor, not a slow query. The log line is what tells the two apart.
  - A lock wait longer than 20 s behaves differently: the script reports it in its `error` attribute, and the same automation's error trigger catches that after 60 minutes.
- **`CORROBORATE_LOOKBACK_D = 30` assumes at least 30 days of recorder history.**
  - Home Assistant's default `purge_keep_days` is 10 (`recorder/__init__.py`, 2026.9.2).
  - The script reads neither the recorder setting nor the age of its oldest row. On a default install, `ev_sql` silently returns about 10 days of rows. The "cannot be tested" verdict does state how many days of history before the camera's last event the fit actually used, but nothing warns that retention is shorter than the window. **This is a known open issue in the shipped code.**
  - The cluster and co-fire counts checked by `CORROBORATE_MIN_PRE` (20) and `CORROBORATE_MIN_HITS` (8) scale roughly with the window. A 10-day history therefore supplies about a third of what the 30-day fit expects (derived). Whether a given partner camera then falls below a gate depends on its event rate (measure on your box).
  - The reference fleet is configured with `purge_keep_days: 548` and held 58.4 days of history, so its 30-day window is fully populated (measured on the reference fleet, 2026-09-14).
  - If you rely on the corroboration verdict, set `recorder: purge_keep_days:` to at least 30. At a steady write rate, that gives about 3× the default's row count and database size (derived). It also makes `sql`'s unbounded history read about 3× longer (derived).

### 9.6 Recorder growth: the heartbeat, `ages_min`, and the vision attributes

The recorder writes a new `states` row for an entity only when its state **or a published attribute** changes. A poll that reproduces the previous payload byte for byte adds no row. Growth therefore depends on how often each payload *changes*, not on the poll rate. `sensor.camera_health` polls 720 times a day but writes about a third as many rows.

**How the figures were taken.** Row counts are for one 24 h window ending 2026-09-14, with no Home Assistant restart inside it (measured on the reference fleet, 2026-09-14). The daily range over the preceding 7 days is in parentheses. "Attribute JSON" is the combined size of the distinct `state_attributes` rows those `states` rows reference. The table lists the largest writer first.

| Entity | Polls/day (derived) | Rows in 24 h (measured) | Attribute JSON in 24 h (measured) | What changes, per 24 h (measured) |
|---|---|---|---|---|
| `binary_sensor.camera_monitor_stalled` | template | **1,869** (1,861–1,942 on each full day since it was added) | 121 distinct rows, 21 KB | only `ages_min`; the state did not change all day |
| `sensor.camera_visual_activity` | 720 | **720** (719–721) | 720 rows, 1.09 MB | `max_norm_diff` and `hours_since_visual` on every poll; `updated_at` 144, `changes_24h` 88, `summary` 87, `probe_age_min` 2 |
| `sensor.camera_health` | 720 | **237** (225–236 since the heartbeat; 11-day average 141 before it) | 204 rows, 97 KB | `updated_at` 143, `stale_count` 105, `detail` 13. **123 rows differ from the previous one only in `updated_at`** |
| `sensor.camera_flap_rate` | 144 | **144** (144–145 since the heartbeat; 101–120 before) | 144 rows, 235 KB | `updated_at` every poll, `probe_counts` 110, `span_min` 62; 30 rows change `updated_at` only |
| `binary_sensor.camera_vision_monitor_problem` | template | **87** (82–132) | 60 rows, 10 KB | `summary`, which mirrors the vision sensor's |
| `sensor.camera_motion_stale` | 48 | **48** (48–50) | 48 rows, 80 KB | `hours_since`, `oldest_hours`, `verdicts`, `visual_hours`, `summary` on every poll |
| `binary_sensor.camera_motion_stale` | template | **48** (48–54) | 48 rows, 7 KB | `summary` |
| `binary_sensor.cameras_problem` | template | **5** (2–62) | 4 rows, <1 KB | state and `summary`, only when a fault appears or clears |
| `binary_sensor.cameras_flapping` | template | **0** (0–4) | none | state only |
| `binary_sensor.camera_door_coverage_problem` | template | not yet recorded (new) | none | state and `failing`; changes only when the failing set changes (derived) |

**Total: 3,158 rows and 1.54 MB of attribute JSON per day** (measured on the reference fleet, 2026-09-14). At Home Assistant's default `purge_keep_days: 10`, that keeps about 31,600 rows and 15 MB of attribute JSON (derived). Scale it by your own retention. The per-row `states` and index overhead is not included (measure on your box).

Three writers matter:

- **The heartbeat adds about 150 rows/day across the four sensors.** Each script sets `HEARTBEAT_BUCKET_S = 600` and publishes `updated_at` as the poll time rounded down to that bucket. A running monitor therefore writes at least one row per 10-minute bucket. The ceiling is 144 extra rows/day per sensor; in practice the extra is the number of buckets in which nothing else changed. Measured on the reference fleet, 2026-09-14:
  - **`camera_health`: 123 heartbeat-only rows.** Its payload otherwise stays unchanged for long stretches. That is roughly one extra row per bucket, not the "stays ~144" the script headers state.
  - **`camera_flap_rate`: 30.** Its poll interval equals the bucket, so every poll now writes a row: 144/day, up from 101–120/day before the heartbeat.
  - **`camera_visual_activity` and `camera_motion_stale`: none.** Their payloads change on every poll anyway.

- **`ages_min` writes most of the rows (known open issue in the shipped template, `ha/packages/cam_health.yaml`).**
  - **What it is.** The attribute publishes four whole-minute ages computed from `now()`.
  - **Why it writes so often.** The rendered string changes whenever one of the four sensors updates: 1,148 of its rows land within 1.5 s of a row from one of them, one per row those sensors write. It changes again at minute boundaries, which accounts for the other ~720 rows.
  - **Where the cost falls.** Attribute rows are deduplicated and the same age strings recur, so 1,869 rows reference only 121 distinct attribute rows (21 KB). The cost is `states` rows and their index entries: 59 % of the stack's rows, about 1.4 % of its attribute bytes (measured on the reference fleet, 2026-09-14).
  - **Current status.** The entity is not excluded from the recorder in the shipped configuration or on the reference fleet. Nothing reads its history: the alert automation uses a live state trigger with a `for:` hold, and the notification reads the current attribute.
  - **Fix on your box.** Exclude the entity from the recorder. This loses history for this one entity only; automations and notification text are unaffected, and the four sensors' own rows still record their `last_updated`. Recorder configuration takes effect after a restart:

  ```yaml
  recorder:
    exclude:
      entities:
        - binary_sensor.camera_monitor_stalled
  ```

  - **Side effect of excluding it.** About 1,870 rows/day drop out of the window that `cam_motion.py`'s all-entity gap query scans on every run (§9.5). That query's host-gap resolution does not change, because the vision sensor alone still writes a row about every 120 s (derived). The alternative is to publish coarser ages from the template, which trades diagnostic resolution for fewer rows.

- **The vision sensor writes most of the bytes (known open issue in the shipped code, `ha/scripts/cam_vision.py` / `ha/packages/cam_vision.yaml`).**
  - **Why.** Two attributes change on every poll: `max_norm_diff` (each camera's peak diff, threshold, mode and luma, to 0.1) and `hours_since_visual` (each camera, to 0.1 h). The sensor therefore writes a new ~1.5 KB attribute row on all 720 polls: 1.09 MB/day, 71 % of the stack's attribute bytes (measured on the reference fleet, 2026-09-14).
  - **Who reads them.** No shipped automation, template or script reads either attribute. `cam_motion.py` reads the vision state file, not the entity.
  - **Without them.** Replaying the same 24 h without those two attributes gives 225 rows and about 0.18 MB (derived from the measured rows).

**The door-coverage attributes make the flap sensor's rows larger.** `door_rolling`, `door_deferred` and `door_coverage_failing` are in its attribute allowlist, and the 24 h figures above predate them. `door_rolling` alone is about 0.6 KB of compact JSON for four door-covering cameras (measured on the prepared door record, 2026-09-14). This sensor writes a row on every poll, so the attributes add roughly 90 KB/day of attribute JSON (derived; measure on your box).

**Disk writes outside the recorder.** Each script below rewrites a state file under `/config` on its own schedule:

| File | Written by | Size | Writes/day | Bytes/day |
|---|---|---|---|---|
| `.cam_flap_door_state.json` | `cam_flap.py` | 3,886 B for 23 judged trips over 2.2 days (measured on the prepared door record, 2026-09-14). Grows by ~130 B per judged trip until the 7-day horizon | up to 144: every poll that gets past the log fetch and the timestamp checks. Written to a temp file, then renamed | ~0.56 MB at that size; ~1.5 MB with a full 7-day record at the same trip rate (derived) |
| `.cam_health_state.json` | `cam_health.py` | 1,160 B (measured on the reference fleet, 2026-09-14) | 720 | ~0.8 MB (derived) |
| `.cam_motion_state.json` | `cam_motion.py` | 14 B (measured on the reference fleet, 2026-09-14) | 48 | negligible |
| `.cam_vision_state.json` | `cam_vision.py` | 43,965 B (measured on the reference fleet, 2026-09-14) | 720 | ~32 MB (derived) |

In total that is about 33–34 MB/day written to `/config` (derived). This is worth knowing on SD-card storage.

### 9.7 Freshness bars vs poll intervals

`binary_sensor.camera_monitor_stalled` turns on when a sensor's `last_updated` is older than that sensor's bar. A bar must sit above the longest time a *running* monitor can go without writing a row. With the heartbeat, that time depends on the poll interval P and the bucket B (`HEARTBEAT_BUCKET_S`, 600 s in all four scripts) (derived):

- **P ≥ B:** every poll lands in a new bucket, so the longest interval is P.
- **P < B:** only the first poll in each bucket is forced to write. The interval is B in steady running, and less than B + P when the poll phase shifts against the bucket boundaries, which are aligned to the Unix epoch.

| Sensor | `scan_interval` | Longest interval while running (derived) | Longest interval since the heartbeat (measured on the reference fleet, 2026-09-11 → 2026-09-14, ~71.5 h) | Bar | Margin, bar ÷ derived interval |
|---|---|---|---|---|---|
| `camera_flap_rate` | 600 s | ~600 s, plus variation in how long a run takes | 616 s | 2700 s | 4.5× |
| `camera_health` | 120 s | < 720 s | 601 s | 2700 s | 3.75× |
| `camera_motion_stale` | 1800 s | 1800 s | 1801 s | 5400 s | 3.0× |
| `camera_visual_activity` | 120 s | < 720 s. The payload changes every poll, so ~120 s in practice | 220 s | 2700 s | 3.75× |

**Why a bare age bar could not work without the heartbeat.** The reference fleet's recorder covers 11 days before the heartbeat existed (2026-08-31 → 2026-09-11). Over that period:
- `camera_health` went up to 7 h 04 m without a new row.
- `camera_flap_rate` went up to 5 h 00 m without one.
- A 45-minute bar would have been crossed 94 times on the first and 24 times on the second.
- The vision sensor wrote a row at least every 199 s throughout, so the host and the recorder were up (measured on the reference fleet).

**Detection latency (derived).** A monitor that stops publishing pages about bar + 1 min + 15 min after its last row:
- The template re-evaluates at least once a minute because it reads `now()`.
- The alert automation then requires the entity to stay `on` for 15 minutes.

That is about 61 min for the three monitors with a 45-minute bar and about 106 min for the motion monitor. The recovery automation likewise waits for 15 minutes of `off`.

**Reading `ages_min`.** It renders as `camera_health=<n> camera_visual_activity=<n> camera_flap_rate=<n> camera_motion_stale=<n>`, in whole minutes. A healthy fast monitor reads up to 10, or about 12 in the worst phase case. The motion monitor legitimately reads up to 30. The alert body's "can never exceed ~10 min" holds for the three fast monitors only.

**If you change `scan_interval` or `HEARTBEAT_BUCKET_S`:**
- `HEARTBEAT_BUCKET_S` is defined separately in each of the four scripts. Change all four together.
- The four bars are literals that appear twice in the template: once in the state and once in the `stalled` attribute. Change both.
- Re-derive the table above. A bucket or poll interval raised close to a bar makes the dead-man fire on a healthy monitor, starting with the one whose payload can otherwise sit unchanged for hours (`camera_health`).
- Raising a bar never hides a dead monitor. It only delays the page by the amount the bar was raised.

### 9.8 Scaling: what breaks when you add cameras

- **Each cycle's snapshot burst is `cameras × PROBERS` requests, and that is normal.** The two 120 s probers fire in phase, and restarts do not change that (§9.2). With nine cameras the engine gets **18** near-simultaneous `takePicture` requests per cycle, not 9. Adding cameras or a third prober makes the burst bigger in proportion, so re-check `slow` counts after such a change before blaming individual cameras.
- **Probe volume grows in proportion to `cameras × PROBERS`.** The rate is `cameras × PROBERS × 60 ÷ scan_interval` requests per minute: 9 req/min, or 540/h, as shipped (derived; 575/h measured including other snapshot clients, §9.2). `cam_flap.py` computes each camera's expected count from the same factors (§9.2). What you must change depends on what you add:
  - **A camera:** add it to every script's camera list, including `DEVICE_IDS` in `cam_flap.py`. The probe accounting in `cam_flap.py` skips a `takePicture` line whose device id is not in `DEVICE_IDS`. The new camera then never gets a `probe_counts` entry and can never appear in `probe_shortfall`. `PROBERS` stays the same.
  - **A prober, or a different probe `scan_interval`:** update `PROBERS` or `PROBE_INTERVAL_MIN` in the same change. The shortfall bar is `PROBE_SHORTFALL = 0.75` of the expected count. If `PROBERS = 2` while three sensors actually probe, the bar sits at 50 % of the real rate instead of 75 % (derived).
- **`cam_health.py` fixes its thread pool at nine. This is a known open issue in the shipped code.** `cam_health.py` uses `ThreadPoolExecutor(max_workers=9)`; `cam_vision.py` uses `max_workers=len(CAMS)`. `TIMEOUT = 15` and `command_timeout: 30` (`ha/packages/cam_health.yaml`) only allow for one round of fetches.
  - **What goes wrong with a tenth camera:** if the first nine fetches hang until the timeout, the tenth starts at about 15 s and ends at about 30 s, which is exactly `command_timeout` (derived).
  - **The timeout is not a hard cap:** `TIMEOUT` is urllib's per-socket-operation timeout, so a fetch that trickles bytes can run past 15 s even when the pool is big enough.
  - **What the user sees:** when `command_timeout` expires, Home Assistant publishes no output for that poll. The sensor goes `unknown` with its attributes cleared, and after 30 minutes of that `camera_health_monitor_down` pages. The alert looks like a monitor failure at exactly the moment cameras are hanging.
  - **Before adding cameras:** set the pool to `len(CAMS)`, or raise `command_timeout` above `ceil(cameras ÷ 9) × TIMEOUT` with some headroom.
- **Other fixed counts assume nine cameras** (derived):
  - `FLEET_STALE_MIN = 5` and `FLEET_MISS_MIN = 5` in `cam_health.py` are commented as a majority, but they are counts, not fractions. With twelve cameras, five is no longer a majority, so the fleet-level indicators fire on a smaller share of the fleet.
  - The all-down summary in `cam_health.py` is the fixed text `"ALL 9 cameras OFFLINE"`.
  - `CO_CHANGE_MIN = 3` in `cam_motion.py` is also a count. With more cameras, more of one camera's scene changes happen within ±180 s of changes on two other cameras and get discarded as global. Fewer localized changes survive.
- **`cam_motion.py`'s co-change rejection loop is quadratic. This is a known open issue in the shipped code.** The loop reads the per-camera visual-change logs that `cam_vision.py` writes and trims.
  - **Log size:** each log gets at most one entry per 120 s poll and keeps `KEEP_HOURS = 48`, so it holds at most about 1,440 entries (`L ≤ ~1,440`).
  - **Cost:** for every entry, the loop scans every other camera's whole log with `any()`. That is up to `cameras × (cameras − 1) × L²` comparisons: 149 M for nine cameras at `L = 1,440`, and 1.83× that for twelve cameras (derived).
  - **Worst case:** at `L = 1,440`, with every camera changing on every sample (possible), the loop took 7.6–7.7 s on the reference host (synthetic logs, measured 2026-09-14). The same case took 1.5–2.3 s on a laptop. The theoretical limit, where no changes coincide (which cannot happen at that density), took about twice as long on the laptop, 3.1–4.7 s (measured on a laptop, 2026-09-14). Halving `L` quarters the cost (derived).
  - **Cost on 2026-09-14:** the reference fleet's logs held 1–28 entries per camera, and the loop took about 2 ms on the reference host (measured on the reference fleet, 2026-09-14). The cost grows with visually busy cameras. The loop has no time limit of its own and shares `cam_motion.py`'s 60 s `command_timeout` with the recorder queries.
  - **Possible fix:** the logs are already sorted before the loop, so a `bisect` window test gives identical output. On a laptop it was 206× faster in the possible worst case and 492× faster at the theoretical limit, with identical output on the live state as well (measured on a laptop, 2026-09-14).

  To measure your own logs, do not run the script by hand: that is a real poll and rewrites its proof-latch file. This snippet only reads:

  ```sh
  python3 - <<'PY'
  import json, time
  vs = json.load(open("/config/.cam_vision_state.json"))
  logs = {c: sorted(v["log"]) for c, v in vs.items() if isinstance(v, dict) and "log" in v}
  print("entries per camera:", sorted(len(l) for l in logs.values()))
  t = time.perf_counter()
  for cam, mine in logs.items():
      for ts in mine:
          sum(1 for c2, o in logs.items() if c2 != cam and any(abs(t2 - ts) <= 180.0 for t2 in o))
  print("co-change loop: %.4f s" % (time.perf_counter() - t))
  PY
  ```

  Run it anywhere that has both `/config` and `python3` (measure on your box).
- **State-file writes grow with cameras and with door traffic.**
  - **Vision state:** `cam_vision.py` rewrites `/config/.cam_vision_state.json` on every poll: 43,965 B for nine cameras, about 4.9 KB per camera, of which about 1.9 KB is the duplicate `frame` key (§9.4; per-camera figures derived). Both grow in proportion to the camera count.
  - **Door record:** `cam_flap.py` rewrites `/config/.cam_flap_door_state.json` atomically on every poll that reaches `update_rolling`, up to 144 times a day (§9.6). Its size depends on door traffic, not camera count, and is capped by the 7-day `DOOR_ROLLING_D` retention. The every-poll rewrite also keeps the file's modification time fresh, and `cam_motion.py` relies on that: it ignores a record older than `DOOR_STATE_MAX_AGE_MIN = 45` minutes. A write-only-on-change change would have to keep that freshness signal.
  - **Door matching:** `judge_doors` compares every door opening with every motion line. The busiest 60,000-line window of a 51.4 h engine log held 7 door openings and 23 motion lines (measured on the reference fleet, 2026-09-14), so this cost is negligible.

### 9.9 Known open efficiency issues

None of these is fixed in the shipped code. Figures are the ones established above.

1. **Duplicate snapshot probing** (`cam_health.py`, `cam_vision.py`; §9.2). Both download the same nine full JPEGs every 120 s: 12,960 `takePicture` requests a day where one shared fetch would need 6,480 (derived).
2. **Probers not offset** (`ha/packages/cam_health.yaml`, `ha/packages/cam_vision.yaml`; §9.2). The two schedules run in phase after every restart, so the engine receives 18 near-simultaneous requests each cycle instead of two waves of 9.
3. **Fixed pool size** (`cam_health.py`; §9.8). `ThreadPoolExecutor(max_workers=9)` is a literal. A tenth camera queues behind the pool, and the worst case becomes 2 × `TIMEOUT` = 30 s, exactly `command_timeout` (derived).
4. **No wall-clock deadline on fetches** (`cam_health.py`, `cam_vision.py`, `cam_flap.py`; §9.3). The `urlopen` timeouts apply per socket operation. A slow-but-steady response is not cut off at the timeout and can run past `command_timeout`.
5. **No runtime bound in `cam_motion.py`** (§9.5). `QUERY_TIMEOUT_S` is only a SQLite lock wait. No progress handler or other statement bound exists, so `command_timeout: 60` is the only ceiling, and Home Assistant does not stop an over-limit run. The measured total was about 0.13 s, so the risk is latent on the reference fleet.
6. **Quadratic co-change loop** (`cam_motion.py`; §9.8). Each entry rescans every other camera's full vision log. That was about 2 ms on 2026-09-14, but 7.6–7.7 s on the reference host at the reachable 1,440-entry cap. A `bisect` window test over the already-sorted logs gives identical output.
7. **Unbounded `sql` history read** (`cam_motion.py`; §9.5). Every poll fetches every retained row of the nine motion entities to compute nine maxima. On 2026-09-14 that was 10,906 rows in 0.023–0.027 s; after a year of retention it would be about 6× that (derived).
8. **Full vision-state rewrite with a duplicate key** (`cam_vision.py`; §9.4, §9.6). The ~44 KB state file is rewritten in place, not atomically, on every 120 s run: about 32 MB/day (derived). 40 % of it is the per-camera `frame` key, which duplicates `frames[mode][0]`: about 12.5 MB/day (derived).
9. **Door record rewritten on every poll** (`cam_flap.py`; §9.8, minor). The whole record is rewritten even when no new trip was judged, about 0.56 MB/day at the measured size (derived). Any fix must keep the file's modification time fresh for `cam_motion.py`.
10. **`ages_min` recorder rows** (`ha/packages/cam_health.yaml`; §9.6). The `now()`-derived attribute on `binary_sensor.camera_monitor_stalled` wrote 1,869 rows in 24 h, 59 % of the stack's 3,158. The entity is not recorder-excluded, and nothing reads its history.
11. **Unread per-poll vision attributes** (`cam_vision.py`, `ha/packages/cam_vision.yaml`; §9.6). `max_norm_diff` and `hours_since_visual` change on every poll and nothing reads them. They force 720 rows and 1.09 MB/day of attribute JSON, 71 % of the stack's attribute bytes.

### 9.10 Quick performance checklist

1. **Probe load.** In `sensor.camera_flap_rate`, each `probe_counts` value should be close to `span_min ÷ PROBE_INTERVAL_MIN × PROBERS`. As shipped, that equals `span_min`; it only reaches 360 when the log slice fills `WINDOW_MIN`. On the reference fleet, `span_min` was 338 and every camera's count was 338–339 (measured on the reference fleet, 2026-09-14). Counts well above the expected value mean extra snapshot pulls, for example from dashboards. A camera below 75 % of the expected value appears in `probe_shortfall`.
2. **Runtime.**
   - Three scripts are limited only by network timeouts, which apply per socket operation: `cam_health.py` 15 s, `cam_vision.py` 12 s, `cam_flap.py` 45 s.
   - `cam_motion.py` has no limit on its own work. `QUERY_TIMEOUT_S = 20` is sqlite3's lock-wait timeout, not a statement timeout. No progress handler is installed, and the co-change loop (§9.8) has no CPU limit. Both are known open issues (§9.9).
   - Running a script by hand is a real poll: it updates that script's state (probe streaks, vision baselines, the proof latch, the door record). Use the read-only checks in item 3 and §9.8 instead.
3. **`gap_sql` query plan.** `EXPLAIN QUERY PLAN` (§9.5) should show `SEARCH states USING … INDEX ix_states_last_updated_ts`, not `SCAN states`. On the reference fleet it shows `SEARCH states USING COVERING INDEX ix_states_last_updated_ts (last_updated_ts>?)` and takes 0.092–0.099 s over about 76,000 rows in its 24 h window (measured on the reference fleet, 2026-09-14).
4. **Recorder rows.** The reference fleet's monitoring entities wrote 3,158 rows in 24 h, 1,869 of them from `binary_sensor.camera_monitor_stalled` (per-entity figures in §9.6; measured on the reference fleet, 2026-09-14, before `binary_sensor.camera_door_coverage_problem` existed). If your count is much higher, look for a `now()`-based or finely rounded value added to an attribute. This query is read-only; run it with `sqlite3 -readonly 'file:/config/home-assistant_v2.db?mode=ro'`:

   ```sql
   SELECT m.entity_id, COUNT(*) AS rows_24h
   FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
   WHERE m.entity_id IN ('sensor.camera_health', 'sensor.camera_visual_activity',
       'sensor.camera_flap_rate', 'sensor.camera_motion_stale',
       'binary_sensor.cameras_problem', 'binary_sensor.camera_vision_monitor_problem',
       'binary_sensor.camera_motion_stale', 'binary_sensor.cameras_flapping',
       'binary_sensor.camera_monitor_stalled', 'binary_sensor.camera_door_coverage_problem')
     AND s.last_updated_ts > CAST(strftime('%s', 'now') AS REAL) - 86400
   GROUP BY m.entity_id;
   ```
5. **Prober alignment.** The two 120 s sensors should write within a fraction of a second of each other on every cycle (median 0.06 s on the reference fleet, §9.2). This is normal and survives restarts, so the engine's burst each cycle is `cameras × PROBERS` (§9.8). Alignment is not a fault. Re-check `slow` counts after adding a camera or a prober.
6. **Retention vs corroboration.** `CORROBORATE_LOOKBACK_D = 30` in `cam_motion.py` reads 30 days of motion rows. Home Assistant's default `purge_keep_days` is 10, which silently cuts that window to 10 days, and the script does not check (§9.5). Check your `recorder:` block. The reference fleet is configured for 548 days, held 58.4, and is not affected (measured on the reference fleet, 2026-09-14).

---

## Quick health checklist

1. **Snapshots** — every camera's webhook `takePicture` returns a fresh JPEG fast; the watchdog reads all healthy.
2. **Live view** — wired cams answer WebRTC in ~0.1 s (warm); the on‑demand cam(s) ~3 s.
3. **Motion/doorbell** — a real motion event and a doorbell press both land on their HA `binary_sensor`s.
4. **HomeKit** — each camera once in Apple Home; HKSV recording within your iCloud+ tier.
5. **Single client** — the Ring app's Control Center lists only the Scrypted device.
6. **go2rtc** — after any restart, WebRTC still answers (the reload automation ran).
