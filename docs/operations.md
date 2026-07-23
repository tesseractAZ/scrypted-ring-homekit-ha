# Operations guide — running the single-Scrypted-engine camera stack

Everything that runs *after* the [migration](migration-runbook.md) is done: making live view instant, getting motion and doorbell presses back into Home Assistant, watching for silently‑dead cameras, announcing the doorbell, and keeping it all healthy. Addresses/tokens/credentials are placeholders — substitute your own.

Conventions used below:
- `<HA_HOST_IP>` — the IP where HA (and the Scrypted add‑on) is reachable on your LAN.
- `<SCRYPTED>` — `https://<HA_HOST_IP>:10443` (admin) / `http://<HA_HOST_IP>:11080` (insecure).
- `<cam>` — a camera's short name; `<cid>` — its Scrypted device id; `<token>` — its per‑stream token.

---

## 1. Instant live view (prebuffer, wired cameras only)

By default a Ring live view is **on‑demand**: opening it makes Scrypted negotiate a fresh stream with Ring's cloud, so first frame lands in ~3 s (and the HLS fallback path can be ~10 s).

**Prebuffer** keeps a warm rolling stream so live view opens in **~0.1 s**. The catch is well known: on **battery** cameras a continuous stream suppresses motion events and drains the battery — never enable it there. On **wired/hardline** cameras neither applies, so prebuffer is safe and is the single biggest live‑view win.

**Enable it (wired cams):**

1. In Scrypted, on each wired camera, set the Rebroadcast/prebuffer mixin's **Prebuffered Streams** to include the stream you serve (e.g. the WebRTC/RTSP stream).
2. **Reload the prebuffer plugin.** The buffer loop does **not** start on a live setting change — the setting looks like a no‑op until the plugin is reloaded (`plugins.reload('@scrypted/prebuffer-mixin')` via the client API, or restart the plugin from the UI).
3. Verify: a WebRTC/`camera.stream` request now returns in ~0.1 s instead of ~3 s.

**Cost:** roughly a few % CPU per continuous stream and a low‑bitrate substream's worth of bandwidth each. On a modern quad‑core host, all cameras prebuffered is typically <10% CPU. Confirm the streams **stay up** (no reconnect churn) — a soak that watches the online/​stream state for 20–30 min is the check.

**One camera may refuse to warm.** A camera whose encoder emits **keyframes on‑demand** (some models emit H.264 High profile with sparse keyframes) will log `Unable to find sync frame in rtsp prebuffer` — the buffer is warm but has no decodable frame, so it waits for the next keyframe and stays at ~3 s. This is a hardware/GOP property, not tunable from Scrypted; leave that one camera on‑demand (a continuous stream that never warms just wastes a session).

**Snapshots vs live view are different paths.** Prebuffer warms *live view*. Snapshots come from the webhook `takePicture` endpoint and are governed by Ring's Snapshot Capture setting (§4), independent of prebuffer.

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
- **Detection needs a motion-shaped dead-man**: alert when the *whole fleet* has reported zero motion over a rolling window during active hours (see the shipped `cameras_motion_stale_alert`). Fixed twice-daily checks let a wedge run half a day.
- **Fix**: reload the camera-source plugin (rebuilds the subscription). If a fresh motion event still doesn't appear in the engine's own log, restart the engine outright — then prove recovery with a real end-to-end event, not with sensor timestamps (an HA restart refreshes `last_changed` on retained sensors, which looks deceptively like activity).

---

## 4. Snapshot freshness (Ring Snapshot Capture)

Enable **Snapshot Capture** in the Ring app (e.g. a 15‑second interval) on wired cameras. Ring then stores a fresh periodic snapshot that Scrypted serves quickly from cache, so dashboard tiles stay current and the webhook `takePicture` returns in ~0.1–0.5 s instead of forcing a slow live capture.

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

**Early‑warning for a systemic motion failure:** a companion automation flags **zero motion across *all* cameras during daytime** (e.g. checked mid‑afternoon and evening — no motion on any camera since morning). One quiet camera is normal; the whole fleet silent all day means the motion pipeline (Ring → Scrypted → MQTT → HA) is down.

---

## 6. Diagnosing prebuffer flapping (stream-layer churn the watchdog can't see)

A prebuffered camera can silently **flap** — its rebroadcast session keeps dying and restarting (`timeout waiting for data, killing parser session rtsp` → `restarting prebuffer session in 5 seconds`, or `rtsp read loop exited`) — while the §5 watchdog reports it **healthy the whole time**. That's not a watchdog bug; it's a sampling gap. Each restart self‑heals in ~5 s, and the watchdog snapshot‑probes on a ~120 s beat, so the chance any given probe lands inside a restart is small (≈ restart_seconds ÷ probe_interval). A camera restarting its stream **dozens of times an hour** can still pass every snapshot probe, because between restarts `takePicture` returns a valid frame. The probe layer answers *"can I get a still right now?"* — it does **not** answer *"is the live stream stable?"*

**Measure the real rate at the stream layer — count restarts in the Scrypted log.** For each camera, count lines matching `\[<Camera>\].*restarting prebuffer` over a known window; that's the honest flap rate. Two traps make this deceptively hard:

- **The add‑on log has no per‑line timestamps** for these events. Use a *known‑periodic* line as a clock: the §5 watchdog fires `takePicture` on every camera on a fixed interval, so `takePicture` appears at a steady rate. Then `window_minutes ≈ takePicture_count ÷ (camera_count ÷ probe_interval_minutes)` (e.g. probing every 120 s ⇒ `camera_count ÷ 2` `takePicture` per minute).
- **The supervisor add‑on‑log API returns the *oldest* retained entries by default, not the newest.** Fetching `GET /api/hassio/addons/<slug>/logs` with header `Range: entries=:0:N` returns a **frozen head slice** (the oldest N lines) — it does *not* advance between calls, so you can be analyzing hours‑old data while believing it's live. Confirm by fetching twice a couple of minutes apart: if the response is byte‑identical, you're on the frozen head. Use **`Range: entries=:-N:N`** (negative skip) to get the genuine live tail. This single trap can completely invert a conclusion — always verify your window is actually recent.

**Root cause is usually below Scrypted.** Ring cameras stream *through Ring's cloud relay*, so `timeout waiting for data` means that relayed feed starved — which points at (a) the camera's own **Wi‑Fi uplink** (weak 2.4 GHz, RF interference — a garage with a door opener / EV charger / metal shelving is a classic dead zone), (b) the camera **needlessly roaming** between mesh APs (Wi‑Fi "client steering" / band steering bouncing a *stationary* camera), or (c) Ring‑side relay flakiness. It is **not** something Scrypted config fixes.

**How to tell which, and what to do:**
- Change **one** network variable at a time and re‑measure the per‑camera rate before/after — the log often retains enough history to compare a pre‑change window against a post‑change one directly. Watch for a per‑camera *split* in the response: one camera improving while another doesn't under the same change narrows the diagnosis (roamer vs weak‑link). But hold any "this setting fixed it" conclusion until it survives several days across varying conditions — a single before/after window, especially one that includes a reboot, over‑credits the change.
- For a camera a network change **doesn't** help, check its actual **signal strength / AP association** (mesh app + the Ring app's Wi‑Fi reading). Strong signal ⇒ the cause is upstream (Ring relay), so stop changing the network.
- **Scrypted‑side mitigations for a stubborn flapper** (accept them only after the network is ruled out): point that camera's prebuffer at a **lower‑bitrate substream** (less data to starve), or **disable prebuffer on just that one camera** — you lose its instant live view (~3 s on‑demand) but stop the churn. The real cost of the churn isn't the ~5 s live‑view blips; it's that a motion event landing on a restart can **truncate that camera's HKSV clip** — so fix your *highest‑value* camera (the doorbell) before a low‑value one. (The prebuffer *window length* is not among the levers: current prebuffer‑mixin hardcodes ~10 s with no duration setting.)

**Make the flap rate a first‑class sensor.** Rather than re‑running this log analysis by hand, deploy the flap monitor in [`ha/`](../ha/): a command_line sensor (`ha/scripts/cam_flap.py`, 10‑min scan) parses the add‑on log tail, normalizes time by the §5 watchdog's probe cadence, and publishes per‑camera restarts/hr — worst camera as the state (graph it and diurnal patterns become visible), full per‑camera table as attributes — plus alert/recovery/dead‑man automations. Design points that came out of adversarial review, worth keeping in any re‑implementation:

- **Numerator and denominator must cover the same log region.** Count restarts only between the first and last probe line. If the watchdog dies, probe lines stop while restart lines keep accumulating — an unclipped count silently inflates every camera's rate until a known‑chronic camera crosses the page threshold: a false page, from the monitoring itself.
- **No clock ⇒ no rate.** If the newest slice of the tail contains no probe lines, fail to an explicit error state rather than reporting anything.
- **Per‑camera thresholds.** One known‑chronic camera shouldn't force fleet‑wide desensitization; give it its own raised threshold and keep the default tight for the healthy ones.
- **A dead‑man automation for the monitor itself.** Every error path above lands in a visible "monitor down" page after an hour — an advisory that dies silently is worse than none, because it converts "unmonitored" into "assumed healthy."


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

## Quick health checklist

1. **Snapshots** — every camera's webhook `takePicture` returns a fresh JPEG fast; the watchdog reads all healthy.
2. **Live view** — wired cams answer WebRTC in ~0.1 s (warm); the on‑demand cam(s) ~3 s.
3. **Motion/doorbell** — a real motion event and a doorbell press both land on their HA `binary_sensor`s.
4. **HomeKit** — each camera once in Apple Home; HKSV recording within your iCloud+ tier.
5. **Single client** — the Ring app's Control Center lists only the Scrypted device.
6. **go2rtc** — after any restart, WebRTC still answers (the reload automation ran).
