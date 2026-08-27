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
exposure, not motion). IR/day-night transitions are detected via mean
saturation and reset the baseline instead of comparing across modes.

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
    ("<cam_1>", "<device_id>", "<webhook_token>"),
    ("<cam_2>", "<device_id>", "<webhook_token>"),
    ("<cam_3>", "<device_id>", "<webhook_token>"),
    ("<cam_4>", "<device_id>", "<webhook_token>"),
    ("<cam_5>", "<device_id>", "<webhook_token>"),
    ("<cam_6>", "<device_id>", "<webhook_token>"),
    ("<cam_7>", "<device_id>", "<webhook_token>"),
    ("<cam_8>", "<device_id>", "<webhook_token>"),
    ("<cam_9>", "<device_id>", "<webhook_token>"),
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
SETTLE_SAMPLES = 2             # legacy; drained only, no longer armed on a flip
BASELINE_MAX_AGE_S = 900.0     # PER-MODE BASELINE FRAMES. Previously ONE baseline
                               # was kept per camera, so every day<->IR flip
                               # compared across two different exposures and had to
                               # be blanked by the settle guard. On a camera whose
                               # IR illuminator cycles every ~60s that blanked most
                               # of its samples - it was effectively excluded from
                               # the monitor (measured: 0 of 11 polls compared,
                               # vs 9 of 11 after this change). One baseline PER
                               # MODE makes a flip free: the frame is compared with
                               # the last frame in the SAME mode. A baseline older
                               # than this is discarded rather than compared.
MIN_BLOCKS = 2                 # localized change needs at least this many hot blocks
MAX_FRACTION = 0.7             # more than this fraction hot = global change, ignore
# IR/day classification is keyed to mean LUMA, not saturation. Mean HSV
# saturation is mathematically unstable at low luma - S=(max-min)/max as max->0 -
# so a near-black frame can read sat~90 while a bright IR-illuminated frame reads
# 0.00. A saturation threshold (and any hysteresis band over it) therefore cannot
# separate the two populations: measured on a camera whose IR illuminator cycles
# ~60s, mode flips ran at an identical rate before AND after a saturation
# hysteresis band was added. Luma separates the same two cleanly.
LUMA_IR_ENTER = 60.0           # mean luma below this = dark scene -> IR/night
LUMA_IR_EXIT = 90.0            # ...above this = lit scene -> day. Between the two
                               # the PREVIOUS mode holds, so a scene sitting near
                               # the boundary cannot dither.
SAT_IR_FALLBACK = 10.0         # only used to seed the very first sample
KEEP_HOURS = 48.0
TIMEOUT = 12


def emit(payload):
    base = {
        "hours_since_visual": None, "changes_24h": None, "ir_mode": None,
        "max_norm_diff": None, "active_count": None, "summary": "", "error": None,
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
    day_events = ir_events = 0
    for name, body in results:
        prev = st.get(name, {})
        log = [t for t in prev.get("log", []) if now - t < KEEP_HOURS * 3600]
        events = [e for e in prev.get("events", []) if now - e[0] < KEEP_HOURS * 3600]
        noise = dict(prev.get("noise", {}))
        settle = int(prev.get("settle", 0))
        entry = {"log": log, "events": events, "noise": noise}
        if body:
            try:
                luma, sat, lum = analyze(body)
            except Exception:
                luma, sat, lum = None, None, None
            if luma is not None:
                prev_ir = prev.get("ir")
                if lum < LUMA_IR_ENTER:
                    is_ir = True
                elif lum > LUMA_IR_EXIT:
                    is_ir = False
                elif prev_ir is not None:
                    is_ir = prev_ir           # inside the band: hold previous mode
                else:
                    is_ir = sat < SAT_IR_FALLBACK   # first ever sample
                mode = "ir" if is_ir else "day"
                # frames = {mode: [b64_luma, ts]} - one baseline per exposure mode.
                frames = dict(prev.get("frames") or {})
                if not frames and prev.get("frame"):
                    # Migrate the legacy single baseline as ALREADY EXPIRED: its
                    # true age and capture mode are both unknown, so comparing
                    # against it would be a cross-mode comparison of unknown age.
                    frames[mode] = [prev["frame"], now - BASELINE_MAX_AGE_S - 1.0]
                base = frames.get(mode)
                old = None
                if base and (now - float(base[1])) <= BASELINE_MAX_AGE_S:
                    old = base[0]
                # With like-for-like comparison there is nothing to settle: a flip
                # no longer costs a comparison. `settle` is only drained so state
                # written by the previous schema finishes cleanly.
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
                frames = {m: v for m, v in frames.items()
                          if (now - float(v[1])) <= BASELINE_MAX_AGE_S * 4}
                entry["frames"] = frames
                entry["frame"] = frames[mode][0]   # back-compat for readers
                entry["ir"] = is_ir
                entry["settle"] = settle
                entry["noise"] = noise
            else:
                entry.update({k: prev[k] for k in ("frame", "frames", "ir", "settle") if k in prev})
        else:
            # A failed FETCH must carry the per-mode baselines forward exactly as
            # the failed-ANALYZE branch does; dropping "frames" here silently
            # reverted the camera to the legacy single-baseline path on every miss.
            entry.update({k: prev[k] for k in ("frame", "frames", "ir", "settle") if k in prev})
        st[name] = entry
        day_events += sum(1 for e in events if now - e[0] < 24 * 3600 and e[1] == "day")
        ir_events += sum(1 for e in events if now - e[0] < 24 * 3600 and e[1] == "ir")
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
    summary = "%d/%d cams visually active <24h (day %d / ir %d events)" % (
        active, len(CAMS), day_events, ir_events)
    if quiet:
        summary += "; visually quiet: " + ",".join(quiet)
    emit({
        "hours_since_visual": hours, "changes_24h": changes, "ir_mode": irs,
        "max_norm_diff": maxdiff, "active_count": active, "summary": summary,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - JSON-always contract
        fail("unhandled: %s" % exc)
