#!/usr/bin/env python3
"""Visual-activity monitor — a continuous, automated walk test.

Compares each camera's snapshot against its previous frame and records when
the SCENE visibly changed. Combined with the motion-event monitor this
discriminates, with no human present, between:
  - vacancy: no motion events AND no visual change  -> area genuinely quiet
  - fault:   visual changes accumulating WITHOUT motion events -> the
             detection/event path is broken

Method (deliberately boring): grayscale, downscale to 48x30, split into an
8x6 block grid, mean |difference| per block against the stored previous
frame, then subtract the MEDIAN block difference (removes uniform lighting
shifts). A "visual change" needs >= MIN_BLOCKS hot blocks (localized, like a
person) but <= MAX_FRACTION of the grid (a scene-wide delta is sun/clouds/
exposure, not motion). Each camera is classified into one of THREE exposure
regimes every sample (see SAT_IR / LUMA_DARK below) and keeps a SEPARATE
baseline frame and noise estimate per regime, so a regime flip costs nothing:
the frame is compared against the last frame in the SAME regime rather than
across two different exposures. Nothing is reset on a flip.

Honest limitations: samples every ~2 min, so brief walk-throughs can fall
between frames — absence of visual change over hours is strong evidence of
vacancy, a single missed transit is not disproof. Outdoor scenes change
constantly (vegetation, vehicles), so visual activity WITHOUT events is only
meaningful on interior/controlled views; the motion monitor applies it as an
annotation, not a pager, until per-camera baselines are tuned.

Emits ONE JSON object on stdout, always, exit 0 (command_line contract).
State: /config/.cam_vision_state.json (per-cam baseline frames, noise EMAs and
their last-comparison stamps, recent body hashes, change log).
"""
import base64
import hashlib
import io
import json
import math
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# A command_line sensor's stderr is written into the HA core log; at this
# script's 2-minute cadence a single library warning becomes hundreds of log
# lines a day, burying real signal. Fix the deprecated calls AND suppress
# warnings defensively - this process's stderr is a shared log, not a console.
import warnings
warnings.simplefilter("ignore")

try:
    from PIL import Image, ImageStat
    PIL_OK = True
except Exception:
    PIL_OK = False

HOST = "<HA_HOST_IP>:11080"
CAMS = [
    ("<cam_1>", "<device_id_1>", "<webhook_token>"),
    ("<cam_2>", "<device_id_2>", "<webhook_token>"),
    ("<cam_3>", "<device_id_3>", "<webhook_token>"),
    ("<cam_4>", "<device_id_4>", "<webhook_token>"),
    ("<cam_5>", "<device_id_5>", "<webhook_token>"),
    ("<cam_6>", "<device_id_6>", "<webhook_token>"),
    ("<cam_7>", "<device_id_7>", "<webhook_token>"),
    ("<cam_8>", "<device_id_8>", "<webhook_token>"),
    ("<cam_9>", "<device_id_9>", "<webhook_token>"),
]

STATE = "/config/.cam_vision_state.json"
RESIZE = (48, 30)
GRID_X, GRID_Y = 8, 6          # 48 blocks of 6x5 px
# Adaptive threshold: each camera learns its OWN noise level SEPARATELY for
# day and IR frames (day and night are big differences - IR noise, dawn/dusk
# lighting sweep, headlights, insects near the IR illuminator). The threshold
# is NOISE_K x that camera's current-mode noise EMA, clamped to a floor/cap.
THRESH_FLOOR = 12.0            # never more sensitive than this
THRESH_CAP = 60.0              # never less sensitive than this
NOISE_K = 5.0                  # threshold = K x learned noise for this cam+mode
SETTLE_SAMPLES = 2             # comparisons to skip when a mode has no usable
                               # baseline yet (auto-exposure hunts for a frame or
                               # two). NOT armed on a bare mode flip any more -
                               # see BASELINE_MAX_AGE_S.
BASELINE_MAX_AGE_S = 900.0     # PER-MODE BASELINE FRAMES. Previously one baseline
                               # was kept per camera, so every day<->IR flip
                               # compared across two different exposures and had
                               # to be blanked by the settle guard. On a camera
                               # whose IR illuminator cycles ~60s that blanked
                               # ~57% of its samples - it was effectively excluded
                               # from the monitor. Keeping one baseline PER MODE
                               # lets a flip cost nothing: the frame is compared
                               # against the last frame in the SAME mode. A
                               # baseline older than this is discarded rather than
                               # compared, since the scene has moved on.
# NOISE RELAXATION ACROSS ABSENCE. The noise EMA learns only from non-event
# samples, which assumes most samples of a regime are of an EMPTY scene. That
# holds for a regime seen around the clock and fails for one seen only when
# someone is there: a garage whose lights are on only during a visit is in
# "day" exposure almost exclusively while occupied. Its non-event samples are
# then a person who lit fewer than MIN_BLOCKS blocks, each one grows the EMA (up
# to the 15% cap), and the bar climbs past what a person produces - after which
# EVERY occupied frame is a non-event and the climb feeds itself. Measured on
# <cam_8>/day: the bar sat at the 12.0 floor through 2026-09-26 03:19Z, then
# climbed visit by visit while visits still drew visual changes (15.6 on 09-26
# 22:49Z, 21.7 on 09-27, 25.6 on 09-29 13:59Z). 09-29 23:25Z was the first visit
# with NO visual change (a 24.1 peak against 23.2, one block), and 4 visits
# later, 10-01 03:29Z, the bar sat at the 60 cap. Its 9 motion clusters from
# 09-29 23:00Z drew 1 visual change. The same climb reached 36.0 on 09-20 and
# came back only because the light was once left on with nobody in view for
# ~30 min (09-21 01:01-01:28Z). The rule had no way back: only a QUIET frame in
# that regime lowers the EMA, and such a regime almost never shows one. A
# cached-snapshot outage pinned <cam_6>/dark at 60 the same way (two cached
# JPEGs alternating - see CACHED SNAPSHOTS), and it then stayed at 60 with no
# data at all.
# A noise estimate is only as current as the frames it came from. After more
# than NOISE_RELAX_GRACE_S without a comparison in a regime (the same horizon as
# its baseline frame, so a regime compared every poll never relaxes and, below
# NOISE_MAX, its output is byte-identical to the rule without relaxation), the
# EMA relaxes toward NOISE_PRIOR - the level at which THRESH_FLOOR binds - with
# time constant NOISE_RELAX_TAU_S, and only downward. From the cap, the first
# comparison after an absence of 0.5 / 1 / 2 / 4 / 8.5 h sees a bar of
# 54.4 / 45.0 / 32.0 / 19.4 / 12.8 (<cam_8>/day visits are a median 8.5 h
# apart). The relaxed value is persisted BEFORE the event decision (below).
# Replayed over 2026-08-28..10-02 (35 days, 9 cameras, every regime, with
# NOISE_MAX and the body-hash ring): <cam_8>/day stays within 12.0-13.8
# through the episode above, its 9 clusters go from 1 visual change to ~8
# expected, and the camera's replayed recall from 0.67 to 0.94. Everywhere else
# the change adds ~22 expected visual changes in 35 days (+1.2% of the fleet's
# ~1,890): ~8 with motion on the SAME camera within 6 min, and ~14 with none
# (~0.4/day) - about two thirds of those in the first 30 min after a regime
# re-entry and half within an hour of sunrise, i.e. day and IR slots that now
# start each morning near the floor instead of at the value frozen at dusk.
# (Counting only events with no motion on ANY camera within 15 min calls ~5 of
# them unexplained; the fleet nearly always has motion somewhere, so that
# filter undercounts - the own-camera figure is the one to quote.)
# Residuals, by construction: a regime compared every poll can still ratchet
# WITHIN one long occupied stretch (about a dozen consecutive one-block
# non-events take the floor to the cap), a revisit 0.5-2 h after a ratcheted
# visit starts at 54-32, and nothing here notices a view that goes deaf just as
# its motion stops.
NOISE_PRIOR = THRESH_FLOOR / NOISE_K   # 2.4
NOISE_RELAX_GRACE_S = BASELINE_MAX_AGE_S
NOISE_RELAX_TAU_S = 7200.0
# Above THRESH_CAP/NOISE_K the threshold is pinned at the cap, so a larger EMA
# changes nothing except how long the way back takes: the ratcheted slots above
# were stored at 16.89 and 18.26, not 12. Clamping costs +0.5 expected changes
# in the same 35-day replay. EVERY stored mode is clamped when the state loads,
# not only the mode being compared: a slot not seen since it ratcheted
# (<cam_6>/dark, last compared 09-28) would otherwise keep its over-cap
# value in the state file until its regime happens to return.
NOISE_MAX = THRESH_CAP / NOISE_K       # 12.0
NOISE_DEFAULT = 4.0            # a regime's EMA before its first non-event sample
MIN_BLOCKS = 2                 # localized change needs at least this many hot blocks
MAX_FRACTION = 0.7             # more than this fraction hot = global change, ignore
# EXPOSURE-REGIME classification. Neither saturation nor luma alone is right:
#   * A true IR-illuminated night frame is GRAYSCALE and BRIGHT (sat 0.00,
#     luma ~95-124). Saturation identifies it correctly; luma alone calls it
#     "day" and then blends real daylight and IR-night into one noise estimate,
#     which collapses the daytime threshold (measured: one camera's day EMA fell
#     9.55 -> 1.19 overnight, i.e. its daytime bar dropped ~48 -> the floor).
#   * A camera whose IR illuminator is OFF gives a near-black COLOUR frame whose
#     mean saturation is meaningless - S=(max-min)/max is unstable as max->0, so
#     it reads ~92. Saturation alone calls that "day"; only luma identifies it.
# So test BOTH, in the order that matches the physics: grayscale first (the
# illuminator is on), then darkness (the illuminator is off), else daylight.
# These are three genuinely different EXPOSURE REGIMES, and because each keeps
# its own baseline frame and its own noise estimate, switching between them is
# free - there is no need for hysteresis to suppress the switching itself.
SAT_IR = 10.0                  # mean saturation below this = grayscale = IR on
LUMA_DARK = 40.0               # ...else mean luma below this = unlit/near-black
KEEP_HOURS = 48.0
TIMEOUT = 12
# HEARTBEAT. Home Assistant rewrites last_updated only when the state or an
# attribute CHANGES, and a healthy fleet emits a byte-identical payload for hours
# - so a sensor whose update loop has STOPPED is indistinguishable from one that
# is simply steady, and every dead-man in this stack triggers on
# unavailable/unknown/-1 without ever looking at age. Measured 2026-09-11:
# sensor.camera_health had gone 37 minutes without writing a recorder row while
# perfectly healthy. Publishing a bucketed clock makes staleness observable;
# bucketing costs one extra row per bucket rather than one per poll (this sensor
# wrote ~139 rows/day against 720 polls, and stays ~144 with the heartbeat).
# Consumed by binary_sensor.camera_monitor_stalled.
HEARTBEAT_BUCKET_S = 600
PROBE_BLIND_MIN = 15.0   # a camera not successfully probed in this long is
                         # reported as BLIND rather than quiet. cam_motion.py
                         # reads the same per-camera stamp to refuse a verdict.
# CACHED SNAPSHOTS. When a camera does not answer a snapshot request in time,
# the Scrypted webhook still returns HTTP 200 - with the LAST image it cached.
# A real sensor never produces two byte-identical JPEGs (noise alone differs),
# so an identical body is not a picture of a still scene: it is the same old
# picture. Treating it as a sample made a camera that had stopped answering read
# as "no visual change - consistent with a quiet area" for days (measured
# 2026-09-28: a camera proven broken by corroboration, cached for ~3 days, got
# exactly that reassuring line). A cached body is now not analysed, does not
# refresh the per-mode baselines, and does not move `fresh_ts` - the time of the
# last genuinely NEW frame. `probe_ts` still moves, because the fetch did work:
# it measures reachability, which drives the monitor's own `blind` dead-man, and
# a camera-side stall must not page as "vision monitor down" (the snapshot
# monitor already pages it as a stale snapshot). cam_motion.py reads fresh_ts
# and withholds its visual verdict for a camera with no new frame in
# PROBE_BLIND_MIN.
# The cache can hold MORE THAN ONE image. In <cam_6>'s 2026-09-26..09-28
# outage it served two JPEGs that ALTERNATED: all 36 comparisons in 49 h read
# d=18.7, luma 35.4 / 36.4 by turns. Checked against the PREVIOUS body alone,
# each switch passes as a new frame: fresh_ts moves (so the downstream CACHED
# guard lapses for 15 min after every switch) and the pair is compared as if it
# were scene change. Under the old noise rule those comparisons silently grew
# the regime's EMA to the cap; with noise relaxation they would have become ~7
# visual changes that never happened. A body is therefore cached when it
# matches ANY of the last BODY_RING_N DISTINCT bodies this camera sent. Order is
# most-recently-seen last, and a cached hit moves its hash to the end, so an
# image the cache keeps serving is never aged out by real frames in between. A
# real sensor never repeats a JPEG and a cached outage serves the same few
# images, so the ring does not churn; it costs ~0.4 KB of state per camera.
BODY_RING_N = 8


def emit(payload):
    base = {
        "hours_since_visual": None, "changes_24h": None, "ir_mode": None,
        "max_norm_diff": None, "active_count": None,
        "probe_age_min": None, "blind": None, "blind_count": None,
        "summary": "", "error": None, "updated_at": None,
    }
    base.update(payload)
    print(json.dumps(base))
    sys.exit(0)


def fail(msg):
    emit({"summary": "error: " + msg, "error": msg})


def fetch(cam):
    name, cid, token = cam
    url = "http://%s/endpoint/@scrypted/webhook/public/%s/%s/takePicture" % (HOST, cid, token)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=TIMEOUT) as r:
            return name, r.read() if r.status == 200 else None
    except Exception:
        return name, None


def analyze(body):
    """Return (luma_bytes_1440, mean_saturation, mean_luma) or None."""
    img = Image.open(io.BytesIO(body))
    img.thumbnail((96, 60))
    sat = ImageStat.Stat(img.convert("HSV")).mean[1]
    grey = img.convert("L")
    lum = ImageStat.Stat(grey).mean[0]
    return grey.resize(RESIZE).tobytes(), sat, lum


def block_diffs(a, b):
    """48 per-block mean |a-b| values over two 48x30 luma buffers."""
    w, h = RESIZE
    bw, bh = w // GRID_X, h // GRID_Y
    out = []
    for gy in range(GRID_Y):
        for gx in range(GRID_X):
            total = 0
            for y in range(gy * bh, (gy + 1) * bh):
                row = y * w
                for x in range(gx * bw, (gx + 1) * bw):
                    total += abs(a[row + x] - b[row + x])
            out.append(total / (bw * bh))
    return out


def relaxed_noise(ema, last_ts, now):
    """The noise EMA as of `now` for a regime last compared at `last_ts`.

    Unchanged within NOISE_RELAX_GRACE_S of the last comparison, and never raised:
    an EMA already at or below NOISE_PRIOR is returned as is. A missing stamp
    means "unknown age" and also returns the EMA unchanged (main() seeds stamps
    for state written before they existed)."""
    if last_ts is None or ema <= NOISE_PRIOR:
        return ema
    gap = now - float(last_ts) - NOISE_RELAX_GRACE_S
    if gap <= 0:
        return ema
    return NOISE_PRIOR + (ema - NOISE_PRIOR) * math.exp(-gap / NOISE_RELAX_TAU_S)


def _finite(v):
    """float(v) if it is a finite number, else None (bad JSON is data, not a crash)."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def load_noise(raw):
    """Stored per-regime EMAs, EVERY one clamped to NOISE_MAX. Returns
    (noise, dropped). A value that is not a finite, non-negative number is
    dropped and named: before, it reached float() at the regime's next
    comparison and failed the whole sensor, every poll, for every camera."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["noise"]
    noise, dropped = {}, []
    for m, v in raw.items():
        f = _finite(v)
        if f is None or f < 0:
            dropped.append("noise[%s]" % m)
            continue
        noise[m] = min(f, NOISE_MAX)
    return noise, dropped


def load_noise_ts(raw, noise, frames, now):
    """{mode: time of the last comparison in that regime}, and the names of any
    malformed entries dropped. State written before the stamp existed is seeded
    from the regime's baseline-frame stamp, else as absent for the frame-pruning
    horizon (a regime with no surviving frame has not been compared for at
    least that long). Non-finite stamps are dropped: an infinite one would
    otherwise make its regime's gap -inf, i.e. never relax."""
    noise_ts, dropped = {}, []
    if raw is not None and not isinstance(raw, dict):
        dropped.append("noise_ts")
        raw = None
    for m, v in (raw or {}).items():
        f = _finite(v)
        if f is None:
            dropped.append("noise_ts[%s]" % m)
            continue
        noise_ts[m] = f
    frames = frames if isinstance(frames, dict) else {}
    for m in noise:
        if m not in noise_ts:
            try:
                f = _finite(frames[m][1])
            except Exception:  # noqa: BLE001 - no usable frame stamp
                f = None
            noise_ts[m] = f if f is not None else now - BASELINE_MAX_AGE_S * 4
    return noise_ts, dropped


def load_ring(prev):
    """The last BODY_RING_N distinct body hashes, most recently seen last, and
    the names of any malformed entries dropped. State written by the
    single-previous-hash schema has only `body_hash`, which seeds the ring."""
    raw = prev.get("body_hashes")
    ring, dropped = [], []
    if isinstance(raw, list):
        for h in raw:
            if isinstance(h, str) and h:
                if h in ring:
                    ring.remove(h)
                ring.append(h)
            else:
                dropped.append("body_hashes[]")
    elif raw is not None:
        dropped.append("body_hashes")
    last = prev.get("body_hash")
    if isinstance(last, str) and last and last not in ring:
        ring.append(last)
    return ring[-BODY_RING_N:], dropped


def main():
    if not PIL_OK:
        fail("PIL unavailable in this python environment")
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {}

    now = time.time()
    with ThreadPoolExecutor(max_workers=len(CAMS)) as ex:
        results = list(ex.map(fetch, CAMS))

    hours, changes, irs, maxdiff = {}, {}, {}, {}
    day_events = ir_events = dark_events = 0
    # The noise relaxation, the EMA clamp and the body-hash ring are refinements
    # of a working monitor, so none of them may take the sensor down: each runs
    # under its own try, falls back to the rule it replaced, and says so in the
    # summary. Malformed state values they drop are named there too (once - the
    # repaired state is written back).
    repaired, faults = [], []
    for name, body in results:
        prev = st.get(name, {})
        log = [t for t in prev.get("log", []) if now - t < KEEP_HOURS * 3600]
        events = [e for e in prev.get("events", []) if now - e[0] < KEEP_HOURS * 3600]
        try:
            noise, dropped = load_noise(prev.get("noise"))
            noise_ts, dropped_ts = load_noise_ts(prev.get("noise_ts"), noise, prev.get("frames"), now)
            dropped += dropped_ts
        except Exception as exc:  # noqa: BLE001 - see above
            try:
                noise = dict(prev.get("noise") or {})   # as loaded before the clamp
            except Exception:  # noqa: BLE001
                noise = {}
            noise_ts, dropped = {}, []                  # no stamps = no relaxation
            faults.append("%s state load %s" % (name, type(exc).__name__))
        try:
            ring, dropped_ring = load_ring(prev)
            dropped += dropped_ring
        except Exception as exc:  # noqa: BLE001 - back to the single-previous-hash check
            ring = [prev["body_hash"]] if isinstance(prev.get("body_hash"), str) else []
            faults.append("%s hash ring %s" % (name, type(exc).__name__))
        if dropped:
            repaired.append("%s %s" % (name, "/".join(dropped)))
        settle = int(prev.get("settle", 0))
        entry = {"log": log, "events": events, "noise": noise, "noise_ts": noise_ts,
                 "body_hashes": ring}
        # Carry the previous successful-probe stamp forward by default; it is
        # refreshed ONLY on a sample this camera was actually seen in. The state
        # file's mtime says the MONITOR ran, not that this camera was reachable,
        # so a per-camera stamp is the only thing that lets a reader tell "we
        # looked and the scene was quiet" from "we could not look at all".
        if prev.get("probe_ts") is not None:
            entry["probe_ts"] = prev["probe_ts"]
        for k in ("fresh_ts", "body_hash"):
            if prev.get(k) is not None:
                entry[k] = prev[k]
        body_hash = hashlib.sha1(body).hexdigest() if body else None
        if body and body_hash in ring:
            # Byte-identical to one of the last BODY_RING_N distinct samples: the
            # webhook's cached image. Reachable, so probe_ts moves; nothing else
            # does (see CACHED SNAPSHOTS) - only the hash moves to most-recent.
            entry.update({k: prev[k] for k in ("frame", "frames", "ir", "settle") if k in prev})
            entry["probe_ts"] = now
            entry["cached_n"] = int(prev.get("cached_n", 0)) + 1
            ring.remove(body_hash)
            ring.append(body_hash)
            body = None
            cached_sample = True
        else:
            cached_sample = False
        if body:
            try:
                luma, sat, lum = analyze(body)
            except Exception:
                luma, sat, lum = None, None, None
            if luma is not None:
                if sat < SAT_IR:
                    mode = "ir"        # grayscale: IR illuminator on
                elif lum < LUMA_DARK:
                    mode = "dark"      # colour but near-black: illuminator off
                else:
                    mode = "day"       # lit scene
                is_ir = mode != "day"
                # frames = {mode: [b64_luma, ts]} - one baseline per exposure mode.
                frames = dict(prev.get("frames") or {})
                if not frames and prev.get("frame"):
                    # migrate the old single-baseline schema into this mode's slot
                    # Stamp the migrated legacy frame as ALREADY EXPIRED: its true
                    # age and its capture mode are both unknown (no "frame_ts" key
                    # was ever written), so comparing against it would be a
                    # cross-mode comparison of unknown age. Seed the slot instead
                    # and let the next poll produce the first honest comparison.
                    frames[mode] = [prev["frame"], now - BASELINE_MAX_AGE_S - 1.0]
                base = frames.get(mode)
                old = None
                if base and (now - float(base[1])) <= BASELINE_MAX_AGE_S:
                    old = base[0]
                # With like-for-like (same-mode) comparison there is nothing to
                # settle: a flip no longer costs a comparison. If this mode has no
                # usable baseline we simply seed it and compare on the next poll.
                # `settle` is only drained here so state written by the previous
                # schema finishes cleanly.
                if settle > 0:
                    settle -= 1
                elif old is not None:
                    old_b = base64.b64decode(old)
                    if old_b != luma:
                        bd = block_diffs(luma, old_b)
                        med = sorted(bd)[len(bd) // 2]
                        norm = [d - med for d in bd]
                        peak = max(norm)
                        ema = min(float(noise.get(mode, NOISE_DEFAULT)), NOISE_MAX)
                        try:
                            ema = relaxed_noise(ema, noise_ts.get(mode), now)
                            # Persist the relaxed value BEFORE the event decision:
                            # the stamp moves to now on every comparison, so an
                            # event sample that left the stored EMA un-relaxed
                            # would restore the old bar on the very next poll.
                            noise[mode] = round(ema, 2)
                        except Exception as exc:  # noqa: BLE001 - un-relaxed bar
                            faults.append("%s relax %s" % (name, type(exc).__name__))
                        noise_ts[mode] = now
                        thr = min(THRESH_CAP, max(THRESH_FLOOR, NOISE_K * ema))
                        hot = sum(1 for d in norm if d > thr)
                        maxdiff[name] = {"d": round(peak, 1), "thr": round(thr, 1), "m": mode,
                                         "lum": round(lum, 1)}
                        if MIN_BLOCKS <= hot <= int(MAX_FRACTION * len(bd)):
                            log.append(now)
                            events.append([now, mode])
                        else:
                            # quiet sample = this camera+mode's live noise estimate.
                            # Growth is capped at 15%/sample: a single above-threshold
                            # peak with only ONE hot block (too localized to count as
                            # an event) used to ratchet the bar 40-80% in one step -
                            # the detector training itself to ignore exactly the
                            # excursions it exists to catch. Decay stays uncapped.
                            ema_new = 0.9 * ema + 0.1 * max(peak, 0.0)
                            noise[mode] = round(min(ema_new, max(ema * 1.15, 0.5), NOISE_MAX), 2)
                frames[mode] = [base64.b64encode(luma).decode(), now]
                # drop any mode baseline that has aged out, so state cannot grow
                frames = {m: v for m, v in frames.items()
                          if (now - float(v[1])) <= BASELINE_MAX_AGE_S * 4}
                entry["frames"] = frames
                entry["frame"] = frames[mode][0]   # back-compat for readers
                entry["probe_ts"] = now            # this camera WAS seen
                entry["fresh_ts"] = now            # ...and sent a NEW frame
                entry["body_hash"] = body_hash     # newest fresh body (back-compat)
                ring.append(body_hash)             # not in the ring: it was not cached
                del ring[:-BODY_RING_N]
                entry["cached_n"] = 0
                entry["ir"] = is_ir
                entry["settle"] = settle
                entry["noise"] = noise
                entry["noise_ts"] = noise_ts
            else:
                entry.update({k: prev[k] for k in ("frame", "frames", "ir", "settle") if k in prev})
        elif not cached_sample:
            # A failed FETCH must carry the per-mode baselines forward exactly as
            # the failed-ANALYZE branch above does. Dropping "frames" here silently
            # reverted the camera to the legacy single-baseline path, which then
            # seeded the surviving frame into whichever mode the NEXT frame
            # happened to be - i.e. it restored the cross-exposure comparison this
            # release exists to remove, on every probe miss.
            entry.update({k: prev[k] for k in ("frame", "frames", "ir", "settle") if k in prev})
        st[name] = entry
        day_events += sum(1 for e in events if now - e[0] < 24 * 3600 and e[1] == "day")
        ir_events += sum(1 for e in events if now - e[0] < 24 * 3600 and e[1] == "ir")
        # "dark" was added as a third regime without extending this tally, so
        # its events were counted in changes_24h yet invisible in the summary
        # line - measured 2026-08-30 as summary 84 against changes_24h 87.
        dark_events += sum(1 for e in events if now - e[0] < 24 * 3600 and e[1] == "dark")
        last = max(log) if log else None
        hours[name] = round((now - last) / 3600.0, 1) if last else None
        changes[name] = len([t for t in log if now - t < 24 * 3600])
        irs[name] = entry.get("ir")

    tmp = "%s.tmp.%d" % (STATE, os.getpid())
    try:
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, STATE)
    except Exception:  # noqa: BLE001 - losing state must not fail the sensor
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass

    cached = sorted(
        n for n in [c[0] for c in CAMS]
        if st.get(n, {}).get("fresh_ts") is not None
        and (now - float(st[n]["fresh_ts"])) / 60.0 > PROBE_BLIND_MIN)
    active = sum(1 for v in hours.values() if v is not None and v < 24)
    # A camera serving cached frames is not "quiet" - it cannot be seen at all.
    quiet = sorted(n for n, v in hours.items() if (v is None or v >= 24) and n not in cached)
    summary = "%d/%d cams visually active <24h (day %d / ir %d%s events)" % (
        active, len(CAMS), day_events, ir_events,
        " / dark %d" % dark_events if dark_events else "")
    if quiet:
        # "No visual change DETECTED", not "quiet": on the busiest outdoor views
        # frame differencing catches only a few percent of real activity
        # (cam_motion.py measures this per camera as vision recall).
        summary += "; no visual change detected: " + ",".join(quiet)
    probe_age_min = {}
    for name in [c[0] for c in CAMS]:
        pts = st.get(name, {}).get("probe_ts")
        probe_age_min[name] = round((now - float(pts)) / 60.0, 1) if pts else None
    blind = sorted(n for n, v in probe_age_min.items()
                   if v is None or v > PROBE_BLIND_MIN)
    if blind:
        summary += "; NOT SEEN >%.0fm: %s" % (PROBE_BLIND_MIN, ",".join(blind))
    cached = [n for n in cached if n not in blind]
    if cached:
        summary += "; SNAPSHOTS CACHED (no new frame >%.0fm): %s" % (
            PROBE_BLIND_MIN, ",".join(cached))
    for label, items in (("state repaired, malformed values dropped", repaired),
                         ("FAULT, fell back to the previous rule", faults)):
        if items:
            summary += "; %s: %s%s" % (label, ", ".join(items[:6]),
                                       " (+%d more)" % (len(items) - 6) if len(items) > 6 else "")
    emit({
        "hours_since_visual": hours, "changes_24h": changes, "ir_mode": irs,
        "max_norm_diff": maxdiff, "active_count": active,
        "probe_age_min": probe_age_min, "blind": blind, "blind_count": len(blind),
        "summary": summary,
        "updated_at": int(time.time() // HEARTBEAT_BUCKET_S) * HEARTBEAT_BUCKET_S,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - JSON-always contract
        fail("unhandled: %s" % exc)
