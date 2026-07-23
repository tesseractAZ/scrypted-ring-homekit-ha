#!/usr/bin/env python3
"""Camera prebuffer flap-rate monitor.

Reads the Scrypted add-on's rolling log via the Supervisor API and reports
per-camera "restarting prebuffer" rates over the retained tail (~1h).

Time normalization: the cam_health watchdog probes all 9 cameras every 120s,
so its takePicture requests appear in this same log at a fixed rate.
That cadence is used as the window clock — no log timestamps needed.

Numerator and denominator MUST cover the same region: restarts are counted
only between the first and last takePicture line, and a freshness guard
fails hard if the newest slice of the tail has no probes (cam_health dead =
no clock = no rate; reporting anything else inflates rates unboundedly).

NOTE: the Range header MUST use a negative skip (entries=:-N:N) to get the
NEWEST tail. entries=:0:N returns the OLDEST retained slice (frozen data).
"""
import json
import os
import sys
import urllib.request

ADDON = "<scrypted_addon_slug>"
LINES = 75000        # ~1h of tail at current log density
TP_PER_MIN = 4.5     # cam_health cadence: camera_count / probe_interval_min (default 9 / 2)
MIN_TP = 20          # below this the span estimate is unusable
FRESH_LINES = 5000   # newest slice (~4 min) must contain a probe, else stalled
ALERT_HR = 30.0      # default per-camera alert threshold, restarts/hr
MIN_EVENTS = 5       # ignore tiny absolute counts on short spans

# Cameras with a known-chronic baseline get a raised threshold so the
# standing condition never pages while a real escalation still does.
# e.g.  "<chronic_cam>": 40.0,
ALERT_HR_OVERRIDES = {}

CAMS = {
    "<Scrypted Camera Name 1>": "<cam_1>",
    "<Scrypted Camera Name 2>": "<cam_2>",
    "<Scrypted Camera Name 3>": "<cam_3>",
    "<Scrypted Camera Name 4>": "<cam_4>",
    "<Scrypted Camera Name 5>": "<cam_5>",
    "<Scrypted Camera Name 6>": "<cam_6>",
    "<Scrypted Camera Name 7>": "<cam_7>",
    "<Scrypted Camera Name 8>": "<cam_8>",
    "<Scrypted Camera Name 9>": "<cam_9>",
}


def emit(payload):
    base = {
        "span_min": None,
        "rates": None,
        "counts": None,
        "flapping": [],
        "flap_count": 0,
        "worst": None,
        "worst_rate": -1,
        "summary": "",
        "error": None,
    }
    base.update(payload)
    print(json.dumps(base))
    sys.exit(0)


def fail(msg):
    emit({"summary": "error: " + msg, "error": msg})


def main():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        fail("SUPERVISOR_TOKEN unavailable")

    req = urllib.request.Request(
        "http://supervisor/addons/%s/logs" % ADDON,
        headers={
            "Authorization": "Bearer " + token,
            "Range": "entries=:-%d:%d" % (LINES, LINES),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - report any fetch failure as state
        fail("log fetch: %s" % exc)
    log = raw.decode("utf-8", "replace")
    del raw

    lines = log.splitlines()
    del log
    tp_idx = [i for i, ln in enumerate(lines) if "takePicture" in ln]
    tp = len(tp_idx)
    if tp < MIN_TP:
        fail("span unavailable (takePicture=%d; is cam_health running?)" % tp)
    if len(lines) - 1 - tp_idx[-1] > FRESH_LINES:
        fail("cam_health stalled - no probe in newest %d lines" % FRESH_LINES)

    # Clip to the probe-covered region so numerator and denominator agree.
    window = lines[tp_idx[0]:tp_idx[-1] + 1]
    span_min = (tp - 1) / TP_PER_MIN
    if span_min <= 0:
        fail("degenerate span (takePicture=%d)" % tp)

    counts = {name: 0 for name in CAMS.values()}
    for line in window:
        if "restarting prebuffer" not in line:
            continue
        for label, name in CAMS.items():
            if ("[%s]" % label) in line:
                counts[name] += 1
                break

    rates = {n: round(c / span_min * 60, 1) for n, c in counts.items()}
    worst = max(rates, key=rates.get)
    flapping = sorted(
        n for n, r in rates.items()
        if r >= ALERT_HR_OVERRIDES.get(n, ALERT_HR) and counts[n] >= MIN_EVENTS
    )
    summary = "worst %s %.1f/hr over %.0fm; %d cam(s) over threshold" % (
        worst, rates[worst], span_min, len(flapping),
    )
    emit({
        "span_min": round(span_min, 1),
        "rates": rates,
        "counts": counts,
        "flapping": flapping,
        "flap_count": len(flapping),
        "worst": worst,
        "worst_rate": rates[worst],
        "summary": summary,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - the sensor contract is JSON-always
        fail("unhandled: %s" % exc)
