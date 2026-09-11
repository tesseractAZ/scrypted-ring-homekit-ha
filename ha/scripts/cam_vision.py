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
State: /config/.cam_vision_state.json (per-cam baseline frame + change log).
"""
import base64
import io
import json
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
    ("<cam_1>", "28", "<webhook_token>"),
    ("<cam_2>", "29", "<webhook_token>"),
    ("<cam_3>", "30", "<webhook_token>"),
    ("<cam_4>", "31", "<webhook_token>"),
    ("<cam_5>", "34", "<webhook_token>"),
    ("<cam_6>", "38", "<webhook_token>"),
    ("<cam_7>", "41", "<webhook_token>"),
    ("<cam_8>", "44", "<webhook_token>"),
    ("<cam_9>", "47", "<webhook_token>"),
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
HEARTBEAT_BUCKET_S = 600  # see the emit heartbeat note below
PROBE_BLIND_MIN = 15.0   # a camera not successfully probed in this long is
                         # reported as BLIND rather than quiet. cam_motion.py
                         # reads the same per-camera stamp to refuse a verdict.


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
    for name, body in results:
        prev = st.get(name, {})
        log = [t for t in prev.get("log", []) if now - t < KEEP_HOURS * 3600]
        events = [e for e in prev.get("events", []) if now - e[0] < KEEP_HOURS * 3600]
        noise = dict(prev.get("noise", {}))
        settle = int(prev.get("settle", 0))
        entry = {"log": log, "events": events, "noise": noise}
        # Carry the previous successful-probe stamp forward by default; it is
        # refreshed ONLY on a sample this camera was actually seen in. The state
        # file's mtime says the MONITOR ran, not that this camera was reachable,
        # so a per-camera stamp is the only thing that lets a reader tell "we
        # looked and the scene was quiet" from "we could not look at all".
        if prev.get("probe_ts") is not None:
            entry["probe_ts"] = prev["probe_ts"]
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
                        ema = float(noise.get(mode, 4.0))
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
                            noise[mode] = round(min(ema_new, max(ema * 1.15, 0.5)), 2)
                frames[mode] = [base64.b64encode(luma).decode(), now]
                # drop any mode baseline that has aged out, so state cannot grow
                frames = {m: v for m, v in frames.items()
                          if (now - float(v[1])) <= BASELINE_MAX_AGE_S * 4}
                entry["frames"] = frames
                entry["frame"] = frames[mode][0]   # back-compat for readers
                entry["probe_ts"] = now            # this camera WAS seen
                entry["ir"] = is_ir
                entry["settle"] = settle
                entry["noise"] = noise
            else:
                entry.update({k: prev[k] for k in ("frame", "frames", "ir", "settle") if k in prev})
        else:
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

    try:
        json.dump(st, open(STATE, "w"))
    except Exception:
        pass

    active = sum(1 for v in hours.values() if v is not None and v < 24)
    quiet = sorted(n for n, v in hours.items() if v is None or v >= 24)
    summary = "%d/%d cams visually active <24h (day %d / ir %d%s events)" % (
        active, len(CAMS), day_events, ir_events,
        " / dark %d" % dark_events if dark_events else "")
    if quiet:
        summary += "; visually quiet: " + ",".join(quiet)
    probe_age_min = {}
    for name in [c[0] for c in CAMS]:
        pts = st.get(name, {}).get("probe_ts")
        probe_age_min[name] = round((now - float(pts)) / 60.0, 1) if pts else None
    blind = sorted(n for n, v in probe_age_min.items()
                   if v is None or v > PROBE_BLIND_MIN)
    if blind:
        summary += "; NOT SEEN >%.0fm: %s" % (PROBE_BLIND_MIN, ",".join(blind))
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
