# Ring → Scrypted → HomeKit + Home Assistant: a single-session camera engine with a statistical observability stack

One [Scrypted](https://github.com/koush/scrypted) session is the only Ring cloud client on the automation side. It serves every camera to Apple HomeKit (with HomeKit Secure Video) and to Home Assistant. Around it runs a purpose-built monitoring and log-forensics stack:

- **six Python scripts:** five monitors plus the partial-loss detector module that `cam_motion.py` imports, ≈7,200 lines, standard library plus Pillow;
- **34 Home Assistant automations;**
- **5 command-line sensors.**

Together they detect the failures a camera system usually hides: a dead camera behind a cached snapshot, an event path that has gone silent while every probe stays green, streams that flap without missing a snapshot, and a camera that slowly stops seeing people at its own door.

> **Why the monitoring exists.** Standard health checks pass while these systems are broken.
> - A failed snapshot is served as HTTP 200 with the last cached JPEG.
> - On a standard (non-24/7) Ring plan, Ring stops delivering motion events for a camera that is persistently streamed. The loss builds over a week or more (about 10 days to fully manifest), while streams, snapshots and probes all stay healthy.
> - A stream can restart dozens of times an hour and still pass every snapshot probe.
>
> Most of the monitors here were added after a failure like these went unnoticed by everything else, and each measures its failure directly, with statistics where a fixed threshold would mislead. The engine-log archive exists because the host journal keeps only about 37 hours of the log those faults are judged from.

The repository holds the deployable monitors, packages and automations as sanitized, placeholder-ready files ([`ha/`](ha/)), a zero-downtime migration runbook, and an operations reference that includes a measured cost model of the monitoring itself.

---

## What it does

### Single Ring session, two ecosystems
- **One cloud client for automation.** Scrypted holds the only Ring login on the automation side. HomeKit and Home Assistant read from Scrypted locally and open no Ring sessions of their own, so they cannot contend for the account. The Ring mobile app remains a separate client.
- **HomeKit Secure Video** per camera in accessory mode, plus Home Assistant *Generic Camera* entities on Scrypted's local snapshot endpoint and RTSP rebroadcast.
- **Events and announcements:** motion and doorbell presses arrive over MQTT discovery, and doorbell announcements run on error-isolated parallel branches.

### Snapshot health: `cam_health.py`
- **Probes the real source, not Home Assistant's camera proxy,** which answers HTTP 200 with a stale cached frame when a camera is dead. All cameras are fetched concurrently every 120 s straight from Scrypted's webhook, each with its status, size, SHA-256 and latency.
- **Four per-camera tiers.**
  - *Down:* 2 consecutive hard failures.
  - *Pending:* a first miss. It is never counted as healthy, so a total probe blackout cannot publish "all OK".
  - *Stale snapshot:* 6 byte-identical frames in a row, which is how a timed-out snapshot served from cache gives itself away.
  - *Slow:* 2 consecutive probes over 9 s, a bar set above the 4–7 s contention band of concurrent probing.
- **Fleet indicators in one cycle.** FLEET-STALE and FLEET-MISS are raised when at least 5 of the 9 cameras repeat their previous frame or fail in a single probe cycle, without waiting for per-camera streaks. They page once that has held for 5 minutes. A tier already raised when Home Assistant starts cannot fire its own trigger, and after every host reboot FLEET-MISS is raised then, because the first probe runs before the Supervisor starts the engine add-on; a start-up check pages any such tier that is still raised after 8 minutes (30 for slow or stale snapshots).

### Visual activity: `cam_vision.py`
- **Block-grid frame differencing.** Each frame becomes a 48×30 luma thumbnail compared in an 8×6 block grid after median lighting normalisation. A change counts only when it is *localized*: at least 2 hot blocks and no more than 70 % of the frame, so a global lighting shift is not motion.
- **Exposure-aware baselines.** Every frame is classified into one of three regimes (day, infrared, dark), and each regime keeps its own baseline, so a day/IR switch is never read as scene change.
- **Learned per-camera thresholds.**
  - The change bar is 5× a per-camera, per-regime noise estimate (EMA), clamped to 12–60.
  - Only non-event samples feed the estimate, and its growth is capped at 15 % per sample. A single sub-threshold excursion cannot ratchet it upward, though about a dozen one-block excursions in one occupied stretch still can.
  - Across long absences the bar decays back toward its prior. In a 35-day, nine-camera replay, that decay raised the recall of the one view whose bar had ratcheted from 0.67 to 0.94, at a cost of about 22 extra visual changes fleet-wide (+1.2 %).
- **Artifact rejection.**
  - *Cached frames:* a frame matching any of the last 8 distinct body hashes is a cached image and is never analysed.
  - *Unlit frames:* an unlit-grayscale guard catches lamp-off frames.
  - *IR spikes:* a one-frame IR luma spike and its return are merged into one event.
- **One fetch, two analysers.** Vision analyses the frames the health probe just fetched, shared atomically through tmpfs. That halves the snapshot requests to Scrypted, and with them the ~25-line request dump Scrypted logs for each one. If the health probe stops, vision fetches its own frames.

### Motion statistics and corroboration: `cam_motion.py`, `partial_loss.py`
- **Recorder-derived staleness.** Each camera's motion history is read from Home Assistant's recorder, read-only, against a per-camera window (72 h by default). A camera is flagged only while at least one other camera has fired in the last 24 h.
- **Cross-camera corroboration: proof without a walk test.**
  - Motion events are grouped into clusters linked within 300 s, and each camera's historical co-firing rate with each partner is fitted.
  - A partner qualifies when it co-fires at least 15 % of the time, with at least 20 clusters before the silence and 8 since.
  - When those partners keep firing and the quiet camera's co-fires stop, the camera is shown broken at α = 10⁻³ (Wilson interval). The proof latches and clears itself on the camera's next real event.
- **Partial-loss detection.**
  - A camera can keep firing but drop out of most of the activity its partners record. A binomial tail test with Bonferroni correction (α = 10⁻⁶ per camera-poll) looks for this on a 48-hour window against a 27-day baseline, guarded by a likelihood veto.
  - In replay it caught 14–18 of 24 losses of 80–100 %, typically 40–50 h after onset. Losses below about 75 %, day-only or night-only losses, and slow declines are mostly missed. The test publishes its own statistical power, so "no flag" is never mistaken for an all-clear.
  - A flag clears only on recovery evidence held for 6 h, never because time has passed. A camera that goes fully silent passes to the staleness check, and its flag is kept dormant.
- **Chance and coverage models.**
  - Per-camera, per-hour background firing rates over 7 days estimate how often motion coincides by chance.
  - An incremental index of recorder gaps across all entities (over 5 min, ~62-day lookback) keeps unobserved time out of the partial-loss test. The 72 h staleness clock does not exclude it.
  - A host outage is reported after the fact from those same recorder holes. Each outage is paged once, on the first poll that sees it, so a second outage inside the same 24 h is paged too.
- **Fleet-wide motion drop.** A fault upstream of every camera (the Ring plugin's push receiver, the engine, MQTT) silences the whole fleet at once. The last 1.5 h of fleet motion is compared with the same clock window on each of the previous 14 days. With zero onsets at two polls 30 min apart, at an hour when 12 of those days logged 7 or more, the alert pages and stays latched until motion returns. In replay it paged the 2026-10-28 push-decrypt outage 2.1 h after the last onset, where the 9 h fleet-silence check never fired. Over 26 days it produced one false page at 27 of 30 poll phases (none at two, two at one), and it is armed about 40–43 % of the time, mostly by day and in the evening.
- **Vision cross-checks.** Frame differencing annotates each stale camera's verdict without changing the flag. Two or more localized scene changes with no motion event read "detection/event path suspect". A quiet scene is called consistent with vacancy only when the camera's measured vision recall supports it. That recall is scored with a trailing-miss run test.

### Stream and log forensics: `cam_flap.py`
- **HKSV recording-failure rate** per camera, parsed from the last 60,000 lines of the Scrypted engine journal and trimmed to at most 6 hours. A camera is flagged at 2 or more failures per hour with at least 3 failures in the window, and pages once that has held for 20 minutes.
- **HAP close-code split.** Error-coded HomeKit closes are split by reason code into hub TIMEOUT, CANCELLED and every other code, and the three always sum to the lumped count. Measured timing showed what each code means:
  - every CANCELLED landed 12.0 s after the clip's last fragment, a normal clip end;
  - every TIMEOUT landed 16–17 s after, a stream that stopped.

  So an error-coded close is not read as a fault on its own.
- **Mid-session video stalls:** a hub TIMEOUT close after 10 s or more without a fragment, or an ffmpeg failure while the session is up. Each session counts once, and stalls stay out of the paging metric.
- **A push-decrypt tripwire,** counted once per failure. Each failure can be a lost motion event, because Ring motion arrives only as these pushes (see [Operations §3](docs/operations.md#3-motion--doorbell-press--home-assistant-mqtt)).
- **Probe-shortfall accounting,** which flags a camera whose logged snapshot requests fall below 75 % of the expected probe cycles.

### Door coverage: `cam_flap.py`
- **Physical ground truth.** Ring door contacts, read from the engine log, are matched within 180 s against the camera paired with each door in `DOOR_CAMERA`. The operator fills in that mapping, since it ships empty; a door without a pair is not judged.
- **Four verdicts per trip:** *saw*, *missed*, *weak* (motion that a per-hour background model says could be chance) or *unseen*.
- **A failing rule grounded in probability.** Verdicts are kept for a rolling 7 days, and two tests decide failure:
  - *Fraction test:* a camera that sees 79 % of its trips has a 0.2 % chance of seeing none of 4.
  - *Run test:* 4 strong misses in a row. This was set by replay: the longest run at a working camera was 2, and two real outages ran 6 and 11.

  So a camera that still fires often enough never to look stale is caught when it stops seeing people at its own door.

### Engine-log archive: `cam_logarchive.py`
- **Cursor-verified incremental capture** of the Scrypted journal every hour.
  - Seven in-band markers (CURSOR, GAP, RESYNC, START, SILENCE, REKEY, STAMPS) and explicit GAP/RESYNC semantics mean a gap is recorded as a gap and no lost minute is silent.
  - Storage is crash-safe gzip members, kept for 35 days, with a clock guard and a size cap.
- **Redaction before anything is written.**
  - *30 ordered rules run first.* Measured on 593,141 live lines, they removed 7,200 JWTs, 41,091 IPv4 addresses, 9,047 IPv6 addresses, 5,004 ICE credential lines and 3,336 DTLS fingerprints, among others.
  - *Then a separate 21-pattern residual scan* withholds any line that still matches one of its identifier patterns, so a missed variant of a known shape fails closed. An identifier in a shape no pattern covers (an array element, a map key, free prose) is documented as out of reach.
- **Keyed HMAC-SHA256 pseudonyms** in four classes keep cameras, dings, media cells and sessions correlatable across lines without exposing a single id. Ding timestamps become offsets from the line's own time. A pseudonym census, run on demand, checks that camera tokens map one-to-one onto cameras; on 2026-10-03 it found 7 tokens for the 7 cameras that held a session.
- **Engineering under load.**
  - *Probe dumps collapsed:* repetitive probe request dumps are folded down. Measured while two monitors still probed every camera, that cut the archive by about a third.
  - *Time budget:* each run stays inside a 60 s work budget.
  - *Regex safety:* regexes found to go quadratic on hostile lines were rewritten.
  - *Timestamps:* every run normalises all three Supervisor timestamp formats to UTC, failing closed when the format cannot be determined.

### Alerting discipline
- **A second fault pages too.** Tier alerts fire on the edge of a fleet count, so a camera that joined a tier while another was already latched used to move no edge. The health, stream-churn and vision monitors now publish the cameras that joined a tier at least one hold after its episode began, and each newly listed camera is pushed ("Also down: …").
- **Pushes carry new information.** There are 36 automations with 82 triggers, and 16 of them can push. No re-assert ever pushes; pushes are gated on an onset, a set difference or a freshness stamp. There are three deliberate exceptions:
  - the fleet-silence alert pages every hour and at boot while the whole fleet stays silent past 9 h;
  - the per-camera dead-man pages again at boot while a broken-camera proof stands;
  - the camera-health and stream-churn alerts and the health, motion, flap and vision dead-men push once per start when a fault already present at the start outlasts its hold, since a fault present when the triggers arm cannot fire them.
- **Survives restarts.** Persistent notifications do not survive a Home Assistant restart, so 14 staggered re-asserts and boot triggers restore every open fault card.
- **Every monitor has a dead-man.**
  - Each monitor pages when it errors, goes unavailable or stops updating, with freshness tracked through bucketed heartbeats and per-sensor age bars from 45 minutes to 3 hours.
  - A failure already present at start-up still pages: the archive and partial-loss dead-men hold in delayed template binaries, and the health, motion, flap and vision dead-men run a start-up check that waits up to their own hold (30, 65, 60 and 30 minutes).
  - Template sensors fail closed, and every script emits the same attribute set on success and on error.

### Engineering and supply chain
- **Contract-checked in CI.**
  - All 137 published sensor attributes are checked against each package's allowlist in both directions.
  - Every automation parses, and each one that carries an `id` is checked against its filename.
  - A public-repo hygiene sweep runs generic patterns plus instance patterns loaded from a CI secret, in a job that isolates that secret.
- **Hardened pipeline.**
  - CodeQL (`security-extended`) on Ubuntu 26.04.
  - SHA-pinned actions, the CI's one third-party dependency (PyYAML) pinned by version and hash, and Dependabot.
  - A protected `main` that blocks force-pushes and deletion.
  - A threat model and security controls documented in [`SECURITY.md`](SECURITY.md).

---

## How it fits together

```
                         ┌──────────────────────────────┐
   Ring cloud  ──────────►          SCRYPTED             │  (the ONE Ring client)
   (single session)      │  @scrypted/ring              │
                         │  ├─ @scrypted/homekit ────────┼──► Apple Home + HKSV (iCloud)
                         │  ├─ @scrypted/prebuffer-mixin ─┼──► RTSP rebroadcast  ┐
                         │  ├─ @scrypted/webhook ────────┼──► HTTP snapshot URL  │
                         │  └─ @scrypted/mqtt ───────────┼──► MQTT (motion/ding) │
                         └──────────────┬───────────────┘                       │
                                        │ engine journal (Supervisor log API)    │
                                        ▼                                       ▼
   ┌──────────────────────────────────────────────────────────────────────────────────┐
   │                                HOME ASSISTANT                                     │
   │  Generic Camera (still = webhook, stream = RTSP) → go2rtc WebRTC/HLS              │
   │  MQTT → binary_sensor.<cam>_motion / _doorbell → recorder                         │
   │                                                                                   │
   │  MONITORS (command_line)                                                          │
   │   cam_health  ── webhook snapshots ──┐ shared frames (tmpfs)                       │
   │   cam_vision  ───────────────────────┘ frame differencing                         │
   │   cam_motion + partial_loss ── recorder history: staleness, corroboration, loss   │
   │   cam_flap   ── engine journal: HKSV failures, close codes, stalls, doors         │
   │   cam_logarchive ── engine journal → redacted, pseudonymised 35-day archive       │
   │        │                                                                          │
   │        ▼  sensors + fail-closed template binaries                                 │
   │   36 automations: gated pushes · cards · re-asserts · dead-men · walk test        │
   └──────────────────────────────────────────────────────────────────────────────────┘
```

**Scrypted owns the Ring session, and everything downstream is a local reader.** Because Scrypted is its own Control Center device with its own persisted login, removing Home Assistant's Ring integration does not log Scrypted out. That is what makes the cutover safe and reversible.

---

## Requirements

- **Home Assistant** with the built‑in go2rtc (2024.11+), ideally HAOS/Supervised so Scrypted can run as an add‑on.
- **Scrypted** (add‑on or standalone) with the Ring, HomeKit, Rebroadcast (prebuffer‑mixin), Webhook, and MQTT plugins.
- **For the monitors in `ha/`:** Scrypted as an add-on on HAOS/Supervised (`cam_flap.py` and `cam_logarchive.py` read its log through the Supervisor API) and the default SQLite recorder (`cam_motion.py` opens `/config/home-assistant_v2.db` read-only). Other installs need those reads adapted.
- **An MQTT broker** reachable by both Scrypted and HA (e.g. the Mosquitto add‑on) — for motion and doorbell events.
- **Apple HKSV** (optional): a Home Hub (Apple TV / HomePod) on the **same subnet** as Scrypted, plus an iCloud+ plan sized to your camera count.
- A **Ring account with 2FA** — you log Scrypted in interactively once.

---

## Getting started

Read in order:

1. **[`docs/migration-runbook.md`](docs/migration-runbook.md)** — the cutover from a stock‑Ring setup to the single‑Scrypted engine, with **zero camera downtime** and a rollback checkpoint at every stage.
2. **[`docs/operations.md`](docs/operations.md)** — everything that runs afterward: live view (and why prebuffer is not the way to make it instant), motion/doorbell into HA, the health watchdog, doorbell announcements, snapshot freshness, diagnosing stream flapping, backups, and (§9) what the monitoring stack itself costs in probe load, runtime, memory, and recorder and disk writes.
3. **[`ha/`](ha/)** — the deployable artifacts themselves: the five monitor scripts (plus `partial_loss.py`, which `cam_motion.py` imports), their package YAML, and every automation as sanitized, placeholder-ready files. Rebuilding on fresh hardware starts here.

---

## Hard-won lessons

The condensed version of what actually bites in practice. Full detail lives in the docs.

**Streaming & live view**
- **Pin the RTSP rebroadcast port.** Left blank it's randomized on every restart, which silently breaks the HA stream URL.
- **Prebuffer suppresses Ring motion events on ANY camera — wired included — and the suppression is *progressive*.** Ring's side treats a persistently‑streamed camera as "in live view" and stops pushing its events: delivery decays over roughly a week-plus per camera (staggered, camera-by-camera — on a mixed fleet the one non-prebuffered camera is the last one still eventing), while streams, snapshots, and health probes all stay perfectly green. A short verification (a day or two of "motion still works") passes and is still wrong — this exact trap was fallen into and published here before being reversed. Battery drain is the battery-specific cost; event suppression is universal. Ring's periodic Snapshot Capture also pauses on a streamed camera, so after disabling prebuffer expect ~10 s snapshots for a few minutes until the stored-snapshot cache refreshes. Keep prebuffer OFF wherever motion or HKSV matters.
- **One camera may refuse to warm** (`Unable to find sync frame`) if its encoder emits keyframes on demand. That's a hardware/GOP trait, not tunable — leave that camera on‑demand.
- **The prebuffer window length is not a lever.** Current prebuffer‑mixin hardcodes ~10 s (`prebufferDurationMs` constant) — there is no duration setting, and writing a legacy storage key is a silent no‑op. Read the running version's `getSettings()` before writing any setting; vanished options don't error, they just stop mattering. (HKSV only uses a few seconds of pre‑roll, so the cap costs little.)
- **An HA restart can break go2rtc WebRTC** for Generic Cameras until go2rtc is reloaded. Automate the reload on HA start so live view self‑heals.

**Health monitoring**
- **Probe the real snapshot source, not the HA camera proxy.** The proxy returns a *stale cached* frame on failure, so it can't tell a dead camera from a live one.
- **A snapshot‑based watchdog is blind to stream flapping.** A camera can restart its stream dozens of times an hour and still pass every periodic snapshot probe. Measure stream stability at the *log* layer, not the snapshot layer — and the root cause is usually Wi‑Fi (weak signal, interference, or mesh‑roaming behavior) or Ring's cloud relay, not Scrypted.

**HomeKit & MQTT**
- **HomeKit cameras pair in accessory mode** — one QR per camera. Don't scan the HomeKit *bridge* QR for cameras.
- **De‑conflict the HA HomeKit bridge** so the same camera isn't exposed by both HA and Scrypted.
- **Bring motion/doorbell back with Scrypted's MQTT plugin** (publishing via HA MQTT discovery) — *not* the reverse‑direction "Home Assistant" plugin, which imports HA entities into Scrypted, the wrong way.

**Operating it**
- **A Ring plan change is a system event.** Upgrading the subscription tier reset the cloud clip-history epoch (making clip queries return zero for everything older — easily misread as "the API broke"), changed the snapshot refresh cadence, and revived a camera's server-side motion detection that no local fix had touched. Re-verify monitors and re-measure baselines after any plan change.
- **The supervisor add‑on‑log API returns the *oldest* entries by default.** `Range: entries=:0:N` is a frozen head slice that never advances; use `entries=:-N:N` for the live tail, or you'll analyze hours‑old data and reach the wrong conclusion.
- **Use the insecure HTTP snapshot endpoint** so HA doesn't reject Scrypted's self‑signed certificate.
- **Home Assistant stores backups locally by default.** That protects you against a bad update, not against a dead SD card — arrange an off‑site copy, or a hardware failure takes your only rebuild image with it.

---

## Security & privacy

Before adapting it:

- Every address, port‑path token, device id, pairing code, and credential in these docs is a **placeholder** (`<HA_HOST_IP>`, `<device_id>`, `<token>`, `<mqtt_user>`, …). Never commit real tokens, Ring credentials, HomeKit pairing codes, or rebroadcast path tokens.
- Deploying `ha/` creates `/config/cam_engine.log.d/`, a redacted 35-day archive of the engine log. It is still personal data (camera and door names with times show when people come and go): it is kept out of backups by design, must not be attached to issues, and its `.pseudonym.key` must never leave the host.
- The Scrypted↔Ring session is bound to your account — treat the Scrypted admin login and its stored Ring refresh token as secrets.
- The local snapshot/RTSP endpoints are unauthenticated on the LAN by design (they carry per‑stream tokens). Keep them off untrusted networks and reach them remotely over a VPN, not a port‑forward. Over IPv6 no port‑forward is needed to expose them: the Scrypted add‑on runs on the host network, its server listens on every interface, and its startup banner prints a Remote URL on the host's global IPv6 address, so confirm the router drops unsolicited inbound IPv6 to the host (at least ports `10443`, `11080` and the pinned rebroadcast port). See [`SECURITY.md`](SECURITY.md).

---

## Contributing & security

Issues and PRs are welcome. Report suspected security issues via [GitHub Security Advisories](../../security/advisories/new) - see [`SECURITY.md`](SECURITY.md).

## License / disclaimer

Licensed under the **MIT License** — Copyright © 2026 Eric Paschal. See [`LICENSE`](LICENSE) for the full text.

Not affiliated with or endorsed by Ring, Amazon, Apple, or the Scrypted project. Ring, HomeKit, HKSV, and Home Assistant behaviors change across versions — treat every version‑specific note as "verify on your box."
