#!/usr/bin/env python3
"""Camera stream-fault monitor (v2).

v1 counted "restarting prebuffer" — a string that stopped occurring entirely
once prebuffer was disabled fleet-wide, leaving the sensor pinned at 0.0 and
structurally incapable of firing. It reported "no faults" when it meant
"not measuring". This version counts signals that actually occur now:

  RECORDING FAILURES (per camera, the user-facing harm — a lost/truncated
  HKSV clip):   "[Cam] motion recording error ..."
                "[Cam] motion recording closed (error code: N)"
                (RESTORED 2026-08-27. It had been retired on the claim that this
                build "never emits" the parenthetical - that claim was FALSE:
                25 such lines appeared in a single 23h window, concentrated on
                one camera whose HKSV clips were arriving effectively empty. A
                hard-zeroed counter reads as "checked, none found", which is the
                exact anti-pattern the v2 rewrite existed to remove. It stays OUT
                of the alert metric - code 3 also covers a benign max-duration
                cancel - but it is counted, published, and surfaced in the
                summary so a camera failing this way is visible.)
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
from datetime import datetime, timedelta

ADDON = "<scrypted_addon_slug>"
LINES = 60000
WINDOW_MIN = 360.0      # rate window is bounded by TIME, not by LINES: the 60k-line
                        # span was observed anywhere from ~106 to ~727 min depending
                        # on engine chattiness (~85%% of it is our own probe traffic),
                        # so identical error counts published rates differing ~7x.
                        # Lines older than this are dropped before counting.
MIN_SPAN_MIN = 10.0     # below this the rates are too noisy to publish
ALERT_HR = 2.0          # per-camera recording failures/hour -> flagged
MIN_EVENTS = 3          # ...and at least this many absolute failures
PROBE_INTERVAL_MIN = 2.0  # scan_interval of ONE probing sensor, in minutes
PROBERS = 2               # ...and this many sensors probe the SAME nine webhook
                          # endpoints on that interval: cam_health.yaml and
                          # cam_vision.yaml both run scan_interval 120 against
                          # the identical takePicture URLs. Measured 2026-08-30:
                          # 34,807 takePicture requests in 62.7 h = 2.06x a
                          # single prober, and probe_counts read 362 against an
                          # `expected` of 180. Omitting this factor halved the
                          # denominator, so the shortfall bar sat at 37% of the
                          # real rate: a camera had to lose 63% of its probes to
                          # trip it rather than the intended 25%, and cam_vision
                          # dying outright (probes halve to ~181) stayed ABOVE
                          # the bar - which is exactly the failure this signal is
                          # relied on to catch, since cam_vision has no dead-man.
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
    "<device_id_1>": "<cam_1>", "29": "<cam_2>", "30": "<cam_3>",
    "<device_id_4>": "<cam_4>", "34": "<cam_5>", "38": "<cam_6>",
    "<device_id_7>": "<cam_7>", "44": "<cam_8>", "47": "<cam_9>",
}

# UNDECRYPTABLE PUSH MESSAGES. Deliberately NOT called "dropped pushes": that
# causal claim was measured and REFUTED - across 14 of these failures, 30 of 30
# Scrypted-side motion detections reached HA at the same second, zero misses.
# The stack sits in the FCM push receiver, which is separate from the RMS
# signalling session that actually delivers motion here. Do NOT re-authenticate
# the Ring plugin on this signal alone; gate any lost-event claim on an actual
# motion-delivery gap. Rate is also confounded by push VOLUME (which tracks
# motion), so normalise before calling a trend.
PUSH_DECRYPT = "ERR_CRYPTO_ECDH_INVALID_PUBLIC_KEY"

STREAM_FAULTS = (
    "timeout waiting for data, killing parser session",
    "rebroadcast error",
    "rtsp read loop exited",
    "camera_rsp_timeout",
    "camera_unexpected_close",
)
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ ")
# ---------------------------------------------------------------------------
# DOOR CONTACTS. The Ring door/contact sensors reach this engine but are NOT
# published to Home Assistant - there is no binary_sensor for any of them, so
# the recorder has never seen one and no monitor here could use them. They are
# the only signal in the stack INDEPENDENT of the camera event path: a door
# physically opened, whatever the cameras did or did not report.
#
# This does NOT try to adjudicate a single camera. Fitting a per-camera
# expectation needs history that does not exist - the add-on log retains ~2.3
# days while the cameras currently dark went quiet 19 and 24 days ago - and
# against what history there is, the one plausible camera/door
# pairing on this fleet is already explained: that camera's historical
# co-fire rate with its own neighbours is ~13%, so zero hits in ten openings is
# the EXPECTED outcome, not evidence of a fault.
#
# What it does do is RECORD the events, so the recorder accumulates the history
# that would make such a test possible later, and publish the one statistic that
# needs no baseline: a door opening that produced no camera motion anywhere.
# Expected value ~0 (measured 1 of 26 openings over 2.3 days), and unlike the
# fleet dead-man - which must wait 6 hours of total silence - it is an immediate,
# physically grounded check that the detection path as a whole is alive.
# Deliberately published WITHOUT an alert threshold: 26 openings is far too thin
# to fit one, and inventing a bar from noise is how this project has gone wrong
# before. Let the history accumulate first.
DOOR_RE = re.compile(r" i ([A-Za-z][A-Za-z ]+?) entryOpen: true\s*$")
MOTION_RE = re.compile(r": ([A-Za-z][A-Za-z ]+?) onMotionDetected\s*$")
DOOR_MOTION_WINDOW_S = 180.0
PROBE_RE = re.compile(r"public/(\d+)/[a-f0-9]+/takePicture")


def emit(payload):
    base = {
        "span_min": None, "rates": None, "counts": None,
        "recording_errors": None, "closed_with_error": None, "stream_errors": None,
        "push_undecryptable": None, "push_undecryptable_rate": None,
        "push_drops": None, "push_drop_rate": None,   # legacy aliases, same value
        "probe_counts": None, "probe_shortfall": [],
        "flapping": [], "flap_count": 0,
        "worst": None, "worst_rate": -1,
        "door_openings": None, "door_orphans": None, "door_orphan_rate": None,
        "doors": None, "door_orphans_by_door": None, "door_orphan_times": None,
        "summary": "", "error": None,
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
    if span_min > WINDOW_MIN:
        # keep only the last WINDOW_MIN minutes (timestamps are lexicographic)
        cutoff = (t1 - timedelta(minutes=WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        kept, past = [], False
        for ln in lines:
            m = TS_RE.match(ln)
            if m:
                past = m.group(1) >= cutoff
            if past:
                kept.append(ln)
        lines = kept
        span_min = WINDOW_MIN

    rec = {n: 0 for n in CAMS.values()}
    closed_err = {n: 0 for n in CAMS.values()}  # context tier, NOT the alert metric
    probes = {n: 0 for n in DEVICE_IDS.values()}
    stream_errors = 0
    push_undecryptable = 0
    door_events = []     # [(ts_string, door_name)]
    cam_motion = []      # [(ts_string, camera_name)]

    for ln in lines:
        # Door and motion lines are their own shapes and are collected BEFORE
        # the fault chain below; they must not disturb its if/elif ordering.
        dm = DOOR_RE.search(ln)
        if dm:
            tsm = TS_RE.match(ln)
            if tsm:
                door_events.append((tsm.group(1), dm.group(1).strip()))
        else:
            mm = MOTION_RE.search(ln)
            if mm and mm.group(1).strip() in CAMS:
                tsm = TS_RE.match(ln)
                if tsm:
                    cam_motion.append((tsm.group(1), mm.group(1).strip()))

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
        elif PUSH_DECRYPT in ln and "code:" not in ln:
            # One Node uncaughtException writes its code on a SECOND line
            # ("  code: 'ERR_...',"), so matching the bare string counted every
            # error twice - measured on this engine as 212 EPIPE lines for 106
            # errors. Skipping the `code:` continuation counts errors, not lines.
            push_undecryptable += 1
        elif any(f in ln for f in STREAM_FAULTS):
            stream_errors += 1

    def _ep(t):
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").timestamp()
    motion_ts = []
    for t, _ in cam_motion:
        try:
            motion_ts.append(_ep(t))
        except Exception:  # noqa: BLE001
            pass
    door_by_name, orphan_by_name, orphan_times = {}, {}, []
    for t, name in door_events:
        door_by_name[name] = door_by_name.get(name, 0) + 1
        try:
            te = _ep(t)
        except Exception:  # noqa: BLE001
            continue
        if not any(abs(mt - te) <= DOOR_MOTION_WINDOW_S for mt in motion_ts):
            orphan_by_name[name] = orphan_by_name.get(name, 0) + 1
            orphan_times.append(t)
    door_openings = len(door_events)
    door_orphans = sum(orphan_by_name.values())
    door_orphan_rate = round(door_orphans / float(door_openings), 3) if door_openings else None

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
    expected = span_min / PROBE_INTERVAL_MIN * PROBERS
    shortfall = sorted(n for n, v in probes.items()
                       if expected >= 10 and v < expected * PROBE_SHORTFALL)

    push_undecryptable_rate = round(push_undecryptable / span_min * 60, 1)
    summary = "worst %s %.1f/hr over %.0fm; %d cam(s) over threshold" % (
        worst, rates[worst], span_min, len(flapping))
    if push_undecryptable_rate >= 1.0:
        summary += "; undecryptable push msgs %.1f/hr" % push_undecryptable_rate
    if door_openings:
        summary += "; doors %d open" % door_openings
        if door_orphans:
            summary += " (%d with NO camera motion)" % door_orphans
    if shortfall:
        summary += "; probe shortfall: " + ",".join(shortfall)
    err_closes = sorted((n for n, v in closed_err.items() if v), key=lambda n: -closed_err[n])
    if err_closes:
        summary += "; error-coded closes: " + ",".join(
            "%s=%d" % (n, closed_err[n]) for n in err_closes)

    emit({
        "span_min": round(span_min, 1),
        "rates": rates, "counts": fails,
        "recording_errors": rec, "closed_with_error": closed_err,
        "stream_errors": stream_errors,
        "push_undecryptable": push_undecryptable,
        "push_undecryptable_rate": push_undecryptable_rate,
        # Legacy names retained so an existing template/automation cannot break
        # on this rename. Both carry the SAME value; new readers should use the
        # push_undecryptable_* pair, whose name does not assert a lost event.
        "push_drops": push_undecryptable, "push_drop_rate": push_undecryptable_rate,
        "probe_counts": probes, "probe_shortfall": shortfall,
        "flapping": flapping, "flap_count": len(flapping),
        "worst": worst, "worst_rate": rates[worst],
        "door_openings": door_openings, "door_orphans": door_orphans,
        "door_orphan_rate": door_orphan_rate, "doors": door_by_name,
        "door_orphans_by_door": orphan_by_name, "door_orphan_times": orphan_times,
        "summary": summary,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - JSON-always contract
        fail("unhandled: %s" % exc)
