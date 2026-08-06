#!/usr/bin/env python3
"""Camera stream-fault monitor (v2).

v1 counted "restarting prebuffer" — a string that stopped occurring entirely
once prebuffer was disabled fleet-wide, leaving the sensor pinned at 0.0 and
structurally incapable of firing. It reported "no faults" when it meant
"not measuring". This version counts signals that actually occur now:

  RECORDING FAILURES (per camera, the user-facing harm — a lost/truncated
  HKSV clip):   "[Cam] motion recording error ..."
                "[Cam] motion recording closed (error code: N)"
  STREAM FAULTS (fleet-wide; these lines carry no camera bracket):
                "timeout waiting for data, killing parser session"
                "rebroadcast error", "rtsp read loop exited",
                "camera_rsp_timeout", "camera_unexpected_close"

Window clock: the log API's ?verbose=true form prefixes real timestamps, so
the span is measured, not inferred. (v1 estimated it from probe cadence and
ran ~11% long because HA's own dashboard snapshot pulls inflated the count.)

Probe shortfall: every camera should appear in the snapshot-probe stream about
equally often. A camera materially below the fleet median is quietly failing to
complete probes — a signal no other monitor carries. (The count includes HA's
own dashboard pulls as well as watchdog probes: the user-agent that would
separate them sits ~13 lines away in a block that interleaves under concurrent
probing, so per-line attribution is unreliable. The relative comparison across
cameras is what matters. Compare against EXPECTED CYCLES (span / probe
interval), NOT the fleet median: the median is inflated by dashboard pulls that
are unevenly distributed, which would make a correctly-probed camera look
short.)

NOTE: Range MUST use a negative skip (entries=:-N:N) for the live tail;
entries=:0:N returns the OLDEST frozen slice.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

ADDON = "<scrypted_addon_slug>"
LINES = 60000
MIN_SPAN_MIN = 10.0     # below this the rates are too noisy to publish
ALERT_HR = 2.0          # per-camera recording failures/hour -> flagged
MIN_EVENTS = 3          # ...and at least this many absolute failures
PROBE_INTERVAL_MIN = 2.0  # cam_health scan_interval; expected probes = span / this
PROBE_SHORTFALL = 0.75    # camera probed < this fraction of EXPECTED cycles

ALERT_HR_OVERRIDES = {}  # e.g. "<chronic_cam>": 6.0

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
DEVICE_IDS = {  # scrypted device id -> short name, for probe accounting
    "<device_id_1>": "<cam_1>",
    "<device_id_2>": "<cam_2>",
    "<device_id_3>": "<cam_3>",
    "<device_id_4>": "<cam_4>",
    "<device_id_5>": "<cam_5>",
    "<device_id_6>": "<cam_6>",
    "<device_id_7>": "<cam_7>",
    "<device_id_8>": "<cam_8>",
    "<device_id_9>": "<cam_9>",
}

PUSH_DECRYPT = "ERR_CRYPTO_ECDH_INVALID_PUBLIC_KEY"  # each hit = one dropped Ring push

STREAM_FAULTS = (
    "timeout waiting for data, killing parser session",
    "rebroadcast error",
    "rtsp read loop exited",
    "camera_rsp_timeout",
    "camera_unexpected_close",
)
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ ")
PROBE_RE = re.compile(r"public/(\d+)/[a-f0-9]+/takePicture")


def emit(payload):
    base = {
        "span_min": None, "rates": None, "counts": None,
        "recording_errors": None, "closed_with_error": None, "stream_errors": None,
        "push_drops": None, "push_drop_rate": None,
        "probe_counts": None, "probe_shortfall": [],
        "flapping": [], "flap_count": 0,
        "worst": None, "worst_rate": -1, "summary": "", "error": None,
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
        "http://supervisor/addons/%s/logs?verbose=true" % ADDON,
        headers={"Authorization": "Bearer " + token,
                 "Range": "entries=:-%d:%d" % (LINES, LINES)},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001
        fail("log fetch: %s" % exc)
    lines = raw.decode("utf-8", "replace").splitlines()
    del raw

    # --- real wall-clock span from the verbose timestamps ---
    stamps = []
    for ln in lines:
        m = TS_RE.match(ln)
        if m:
            stamps.append(m.group(1))
            break
    for ln in reversed(lines):
        m = TS_RE.match(ln)
        if m:
            stamps.append(m.group(1))
            break
    if len(stamps) < 2:
        fail("no verbose timestamps in log (is ?verbose=true supported?)")
    try:
        t0 = datetime.strptime(stamps[0], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(stamps[1], "%Y-%m-%d %H:%M:%S")
    except Exception as exc:  # noqa: BLE001
        fail("timestamp parse: %s" % exc)
    span_min = (t1 - t0).total_seconds() / 60.0
    if span_min < MIN_SPAN_MIN:
        fail("span too short (%.1f min) - log just rotated?" % span_min)

    rec = {n: 0 for n in CAMS.values()}
    closed_err = {n: 0 for n in CAMS.values()}
    probes = {n: 0 for n in DEVICE_IDS.values()}
    stream_errors = 0
    push_drops = 0

    for ln in lines:
        if "takePicture" in ln:
            m = PROBE_RE.search(ln)
            if m and m.group(1) in DEVICE_IDS:
                probes[DEVICE_IDS[m.group(1)]] += 1
        if "motion recording error" in ln or "motion recording closed (error code:" in ln:
            for label, name in CAMS.items():
                if ("[%s]" % label) in ln:
                    if "motion recording error" in ln:
                        rec[name] += 1
                    else:
                        closed_err[name] += 1
                    break
        elif PUSH_DECRYPT in ln:
            push_drops += 1
        elif any(f in ln for f in STREAM_FAULTS):
            stream_errors += 1

    # Alert metric = hard recording errors only. Error-coded closes are
    # reported as context but excluded: code 3 also covers a benign
    # max-duration cancel, so counting them over-flags healthy cameras.
    fails = dict(rec)
    rates = {n: round(v / span_min * 60, 1) for n, v in fails.items()}
    worst = max(rates, key=rates.get)
    flapping = sorted(
        n for n, r in rates.items()
        if r >= ALERT_HR_OVERRIDES.get(n, ALERT_HR) and fails[n] >= MIN_EVENTS
    )

    # A camera whose watchdog probes are quietly not completing. Measured
    # against expected cycles, because per-camera totals also contain HA
    # dashboard pulls which are NOT evenly spread across cameras.
    expected = span_min / PROBE_INTERVAL_MIN
    shortfall = sorted(n for n, v in probes.items()
                       if expected >= 10 and v < expected * PROBE_SHORTFALL)

    push_drop_rate = round(push_drops / span_min * 60, 1)
    summary = "worst %s %.1f/hr over %.0fm; %d cam(s) over threshold" % (
        worst, rates[worst], span_min, len(flapping))
    if push_drop_rate >= 1.0:
        summary += "; push drops %.1f/hr" % push_drop_rate
    if shortfall:
        summary += "; probe shortfall: " + ",".join(shortfall)

    emit({
        "span_min": round(span_min, 1),
        "rates": rates, "counts": fails,
        "recording_errors": rec, "closed_with_error": closed_err,
        "stream_errors": stream_errors,
        "push_drops": push_drops, "push_drop_rate": push_drop_rate,
        "probe_counts": probes, "probe_shortfall": shortfall,
        "flapping": flapping, "flap_count": len(flapping),
        "worst": worst, "worst_rate": rates[worst], "summary": summary,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - JSON-always contract
        fail("unhandled: %s" % exc)
