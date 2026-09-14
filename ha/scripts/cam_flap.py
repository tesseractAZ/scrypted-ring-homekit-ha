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
import calendar
import json
import os
import re
import time
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
    "<device_id_1>": "<cam_1>", "<device_id_2>": "<cam_2>", "<device_id_3>": "<cam_3>",
    "<device_id_4>": "<cam_4>", "<device_id_5>": "<cam_5>", "<device_id_6>": "<cam_6>",
    "<device_id_7>": "<cam_7>", "<device_id_8>": "<cam_8>", "<device_id_9>": "<cam_9>",
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
# The first release deliberately did not adjudicate single cameras: the add-on
# log alone retains ~2.3 days, too little to fit a per-camera expectation. Two
# later changes made a per-camera test possible without fitting any rate.
# DOOR_CAMERA (below) holds owner-confirmed door -> covering-camera pairs, which
# turns "did this door's own camera fire?" into a direct question, and DOOR_STATE
# keeps every judged trip so the record outlives the log rotation. judge_doors
# and update_rolling document the rules; the rolling FAILING set drives
# camera_door_coverage_alert.
#
# The fleet-level statistic is unchanged: a door opening that produced no camera
# motion anywhere (an orphan). Expected value ~0 (measured 1 of 26 openings over
# 2.3 days), and unlike the fleet dead-man - which must wait 6 hours of total
# silence - it is an immediate, physically grounded check that the detection path
# as a whole is alive. It is still published WITHOUT an alert threshold: 26
# openings is far too thin to fit one, and inventing a bar from noise is how this
# project has gone wrong before.
# Name classes are deliberately wider than the current roster. The first version
# accepted [A-Za-z ] only, which meant a device renamed to contain a digit,
# hyphen, apostrophe, period or ampersand would stop matching with NO error - and
# in the motion case that is not a quiet degradation but an actively WRONG
# result: MOTION_RE feeds the pool that decides whether a door opening was
# witnessed, so a camera dropping out of the pool MANUFACTURES orphans on every
# door in the house and reads as a fleet-wide detection failure. A name the
# script does not recognise is now counted and named in the SUMMARY rather than
# silently discarded, so the failure announces itself. It is deliberately not a
# new attribute: that would need a json_attributes entry and a full HA restart.
DOOR_RE = re.compile(r" i ([A-Za-z][\w '&.\-]*?) entryOpen: true\s*$")
MOTION_RE = re.compile(r": ([A-Za-z][\w '&.\-]*?) onMotionDetected\s*$")
DOOR_MOTION_WINDOW_S = 180.0
DOOR_TRIP_S = 300.0   # repeat openings of one door inside this are ONE trip
# Door evidence is judged only where the log slice holds its whole evidence
# window (see judge_doors). DOOR_STATE keeps every judged trip's verdict so a
# camera's door record spans days instead of one 6-hour slice. That is the only
# way to see a camera that still fires now and then but misses its own door
# every time: motion staleness resets on each stray event, so it never trips.
DOOR_STATE = "/config/.cam_flap_door_state.json"
DOOR_ROLLING_D = 7.0          # the per-camera door record covers this many days
# A camera is FAILING its door when, over DOOR_ROLLING_D, it has at least
# DOOR_FAIL_MIN_TRIPS judged trips, saw no more than DOOR_FAIL_MAX_SAW_FRAC of
# them, and at least DOOR_FAIL_MIN_MISSED were corroborated misses (other cameras
# saw activity around the opening). A trip nobody saw is not a miss, but it is
# still a trip the camera did not see, so it counts toward DOOR_FAIL_MIN_TRIPS and
# the seen fraction. At the working covering cameras none of 19 trips went unseen
# by the whole fleet, so an opening nobody saw at a door is itself unusual.
# Measured with full context over the engine log 2026-09-12 17:54 -> 09-14 21:17
# UTC: covering cameras that work saw 8/9, 9/9 and 1/1 of their trips; the
# failing one saw 0/4 with 2 corroborated misses. Even for a camera that sees
# only 79 % of its trips, a run this bad has a 0.2 % chance at 4 trips and 0.8 %
# at 5, before the missed-count rule is applied.
DOOR_FAIL_MIN_TRIPS = 4
DOOR_FAIL_MAX_SAW_FRAC = 0.2
DOOR_FAIL_MIN_MISSED = 2

# ---------------------------------------------------------------------------
# CORROBORATED MISS - the strongest per-camera evidence available here, and the
# only signal in this stack that convicts a single camera from ONE event.
#
# The orphan count above asks "did ANY camera fire?", which is a FLEET question
# and therefore structurally blind to a single camera failing. That blindness was
# demonstrated, not theorised: on 2026-09-11 at 06:49 local a person came in
# through one patio door and walked the length of the property; three downstream
# cameras fired in sequence over the next two minutes and the camera covering
# that door reported nothing at all - in Scrypted as well as in Home Assistant.
# Because a different camera fired 125 s later, inside the 180 s window, the
# opening counted as WITNESSED and the orphan metric stayed at zero. Every other
# instrument missed it too: staleness was 48 h from tripping, the co-firing test
# had lost its fitted rate, and frame differencing registered no change.
#
# A corroborated miss asks the per-camera question instead: this door opened, the
# fleet DID witness activity around it, and yet the camera that covers this door
# did not fire. It needs no historical rate and no baseline - one instance is
# meaningful - which is exactly why it works where the statistical tests have run
# out of data.
#
# The mapping is OWNER KNOWLEDGE and cannot be derived from a 2.3-day log, so it
# is published empty (a commented template); every pair must be confirmed by the
# person who knows the property. A wrong pair manufactures accusations against a healthy camera.
# Read a miss as evidence, not proof: someone can legitimately leave by a door
# and walk out of that camera's view. A COUNT that grows while its peers stay at
# zero is the signal.
DOOR_CAMERA = {
    # "<door contact name>": "<covering_camera_stem>",
}
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
PROBE_RE = re.compile(r"public/(\d+)/[a-f0-9]+/takePicture")


_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _utc(t):
    """Engine-log timestamps are UTC. calendar.timegm, never
    datetime.timestamp(), which applies the container's local offset to a naive
    value and would shift every stored verdict against time.time()."""
    return calendar.timegm(time.strptime(t, _TS_FMT))


def judge_doors(door_events, cam_motion, win_start, win_end):
    """Judge door openings and trips against camera motion within
    DOOR_MOTION_WINDOW_S - but only those whose whole evidence window lies inside
    the log slice [win_start, win_end]; everything else is deferred.

    Without that guard both edges manufactured verdicts. Measured 2026-09-14: 3 of
    the 10 distinct misses recorded since the detector shipped were artifacts. At
    the leading edge the covering camera had fired seconds BEFORE the door and
    that motion line had already scrolled out of the slice (-46 s, -8 s); at the
    trailing edge the door was judged before the camera's later line existed
    (+72 s). The slice start is usually set by the 60,000-line fetch rather than
    the time trim, and the log arrives in probe bursts, so exposure scales with log
    VOLUME between the two lines, not with the seconds between them. The same gap
    produced false orphans and briefly turned real misses into orphans.

    A trip also needs DOOR_TRIP_S of look-back inside the slice, because whether
    an opening STARTS a trip depends on the opening before it: once that earlier
    opening scrolls out, a re-opening would be judged as a new trip and inflate the
    denominator. Both bounds are strict because the first and last seconds of the
    slice can be partial (timestamps are whole seconds). Deferral drops nothing: overlapping samples judge every event
    with full context unless the sensor stops running for hours.

    Rules kept from earlier releases: repeat openings of one door inside
    DOOR_TRIP_S are one TRIP (an edge transition is not a physical event);
    orphan_times are [time, door] pairs so they stay attributable after the log
    rotates; motion maps through CAMS because the log carries display names while
    DOOR_CAMERA holds entity stems.
    """
    W = DOOR_MOTION_WINDOW_S
    lead = max(W, DOOR_TRIP_S)
    motion = []
    for t, c in cam_motion:
        try:
            motion.append((_utc(t), CAMS.get(c)))
        except Exception:  # noqa: BLE001
            pass
    openings = []
    for t, name in door_events:
        try:
            openings.append((t, _utc(t), name))
        except Exception:  # noqa: BLE001
            pass
    res = {"doors": {}, "orphan_by_name": {}, "orphan_times": [], "deferred": 0, "trips": []}
    for t, te, name in openings:
        if not (te - W > win_start and te + W < win_end):
            res["deferred"] += 1
            continue
        res["doors"][name] = res["doors"].get(name, 0) + 1
        if not any(abs(mt - te) <= W for mt, _ in motion):
            res["orphan_by_name"][name] = res["orphan_by_name"].get(name, 0) + 1
            res["orphan_times"].append([t, name])
    last_open = {}
    for t, te, name in openings:
        head = not (name in last_open and te - last_open[name] <= DOOR_TRIP_S)
        last_open[name] = te
        if not head or not (te - lead > win_start and te + W < win_end):
            continue
        cam = DOOR_CAMERA.get(name)
        near = sorted({c for mt, c in motion if c and abs(mt - te) <= W})
        if cam is None:
            verdict = None
        elif cam in near:
            verdict = "saw"
        elif near:
            verdict = "missed"
        else:
            verdict = "unseen"
        res["trips"].append([t, name, cam, near, verdict])
    return res


def _finite(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and x == x and abs(x) != float("inf"))


def update_rolling(trips, now, covered_from=None):
    """Merge newly judged trips into DOOR_STATE; return (rolling, failing, note).

    covered_from is the earliest time this sample could judge a trip. The record
    keeps the earliest such time it has ever seen, so the published "days" says
    how much history the counts really span - never a flat DOOR_ROLLING_D that a
    fresh or rebuilt record has not earned. failing_since stamps the poll at which
    a camera first entered the failing set; the alert pushes only for a stamp
    that is fresh and newer than its own previous run.

    Each trip is stored once, keyed by (time, door); the edge guard means the
    first judgement already had full context, so later samples add nothing new.

    The record is evidence, not a dependency, so nothing here may fail the sensor
    - but nothing may fail SILENTLY either. A missing file is a fresh start. An
    unreadable one is set aside as .corrupt-<ts> and the record restarts - which
    does drop a failing camera out of the set - so the loss is reported in `note`
    on every poll until the lost history has aged out of DOOR_ROLLING_D. A
    record that cannot be written is also reported: its stamps would otherwise be
    re-minted every poll. Stored values are type-checked one trip at a time, so a
    hand-edited or damaged entry costs that entry, not the whole record."""
    note = None
    restarted_at = None
    try:
        with open(DOOR_STATE) as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            raise ValueError("door record is not a JSON object")
    except FileNotFoundError:
        state = {}
    except Exception:  # noqa: BLE001
        state = {}
        restarted_at = int(now)
        try:
            os.replace(DOOR_STATE, "%s.corrupt-%d" % (DOOR_STATE, int(now)))
        except Exception:  # noqa: BLE001
            pass
    try:
        known = state.get("trips") if isinstance(state.get("trips"), dict) else {}
        prev_since = state.get("failing_since") if isinstance(state.get("failing_since"), dict) else {}
        since = state.get("covered_from")
        if not _finite(since):
            since = None
        if _finite(covered_from):
            since = covered_from if since is None else min(since, covered_from)
        horizon = now - DOOR_ROLLING_D * 86400
        if restarted_at is None:
            ra = state.get("restarted_at")
            if _finite(ra) and horizon < ra <= now + 60:
                restarted_at = int(ra)
        if restarted_at is not None:
            note = ("door record unreadable at %s, restarted %.1f h ago - older history (and any "
                    "failing camera) lost; see .corrupt-%d" % (
                        time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(restarted_at)),
                        max(0.0, now - restarted_at) / 3600.0, restarted_at))
        kept = {}
        for k, v in known.items():
            try:
                if (isinstance(v, dict) and isinstance(v.get("cam"), str)
                        and v.get("v") in ("saw", "missed", "unseen")
                        and _utc(k.split("|", 1)[0]) >= horizon):
                    kept[k] = v
            except Exception:  # noqa: BLE001
                pass
        for t, name, cam, near, verdict in trips:
            if cam is None or verdict not in ("saw", "missed", "unseen"):
                continue
            key = "%s|%s" % (t, name)
            try:
                if key not in kept and _utc(t) >= horizon:
                    kept[key] = {"cam": cam, "v": verdict, "near": near}
            except Exception:  # noqa: BLE001
                pass
        days = round(max(0.0, now - max(since if since is not None else now, horizon)) / 86400.0, 1)
        rolling = {c: {"trips": 0, "saw": 0, "missed": 0, "unseen": 0, "days": days,
                       "last_saw": None, "last_trip": None, "failing_since_ts": None}
                   for c in sorted(set(DOOR_CAMERA.values()))}
        for k in sorted(kept):
            v = kept[k]
            r = rolling.get(v["cam"])
            if r is None:
                continue
            t = k.split("|", 1)[0]
            r["trips"] += 1
            r[v["v"]] += 1
            r["last_trip"] = t
            if v["v"] == "saw":
                r["last_saw"] = t
        failing = sorted(c for c, r in rolling.items()
                         if r["trips"] >= DOOR_FAIL_MIN_TRIPS
                         and r["saw"] <= int(DOOR_FAIL_MAX_SAW_FRAC * r["trips"])
                         and r["missed"] >= DOOR_FAIL_MIN_MISSED)
        failing_since = {}
        for c in failing:
            ts = prev_since.get(c)
            failing_since[c] = int(ts) if _finite(ts) and 0 < ts <= now + 60 else int(now)
            rolling[c]["failing_since_ts"] = failing_since[c]
    except Exception:  # noqa: BLE001 - last resort for a shape not foreseen
        return {}, [], "door record could not be evaluated"
    tmp = "%s.tmp.%d" % (DOOR_STATE, os.getpid())
    try:
        with open(tmp, "w") as fh:
            json.dump({"trips": kept, "rolling": rolling, "failing": failing,
                       "failing_since": failing_since, "covered_from": since,
                       "rolling_days": days, "restarted_at": restarted_at,
                       "computed_at": int(now)}, fh)
        os.replace(tmp, DOOR_STATE)
    except Exception:  # noqa: BLE001
        note = (note + "; " if note else "") + "door record not persisting"
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass
    return rolling, failing, note


def emit(payload):
    base = {
        "span_min": None, "rates": None, "counts": None,
        "recording_errors": None, "closed_with_error": None, "stream_errors": None,
        "push_undecryptable": None, "push_undecryptable_rate": None,
        "push_drops": None, "push_drop_rate": None,   # legacy aliases, same value
        "probe_counts": None, "probe_shortfall": [],
        "flapping": [], "flap_count": 0,
        "worst": None, "worst_rate": -1, "updated_at": None,
        "door_openings": None, "door_orphans": None, "door_orphan_rate": None,
        "doors": None, "door_orphans_by_door": None, "door_orphan_times": None,
        "door_missed_by_cam": None, "door_miss_times": None,
        "door_cover_stats": None,
        "door_deferred": None, "door_rolling": None, "door_coverage_failing": None,
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
    unknown_motion = {}  # camera names seen in the log but absent from CAMS

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
            if mm:
                nm = mm.group(1).strip()
                tsm = TS_RE.match(ln)
                if nm in CAMS:
                    if tsm:
                        cam_motion.append((tsm.group(1), nm))
                else:
                    # Do NOT drop it silently: an unrecognised camera is missing
                    # from the witness pool, which inflates the orphan count.
                    unknown_motion[nm] = unknown_motion.get(nm, 0) + 1

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

    # Judge door evidence only where this slice holds its whole window.
    win_start = None
    for ln in lines:
        m = TS_RE.match(ln)
        if m:
            win_start = _utc(m.group(1))
            break
    win_end = _utc(stamps[1])
    dj = judge_doors(door_events, cam_motion,
                     win_start if win_start is not None else win_end, win_end)
    door_by_name, orphan_by_name = dj["doors"], dj["orphan_by_name"]
    orphan_times, door_deferred = dj["orphan_times"], dj["deferred"]
    missed_by_cam, miss_times, cover_stats = {}, [], {}
    for t, name, cam, near, verdict in dj["trips"]:
        if cam is None:
            continue
        st = cover_stats.setdefault(cam, [0, 0])
        st[1] += 1
        if verdict == "saw":
            st[0] += 1
        elif verdict == "missed":
            missed_by_cam[cam] = missed_by_cam.get(cam, 0) + 1
            miss_times.append([t, name, cam, near])
    door_openings = sum(door_by_name.values())
    door_orphans = sum(orphan_by_name.values())
    door_orphan_rate = round(door_orphans / float(door_openings), 3) if door_openings else None
    door_rolling, door_failing, door_note = update_rolling(
        dj["trips"], time.time(),
        (win_start + max(DOOR_MOTION_WINDOW_S, DOOR_TRIP_S)) if win_start is not None else None)

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
    if unknown_motion:
        # Loud, because it silently corrupts the orphan metric.
        summary += ("; UNRECOGNISED CAMERA NAME(S) IN LOG: %s - orphan counts are "
                    "unreliable until CAMS is updated" % ",".join(sorted(unknown_motion)))
    if door_note:
        summary += "; " + door_note
    if door_failing:
        summary += "; DOOR COVERAGE FAILING: " + ", ".join(
            "%s saw %d of %d door trips in %.1f days (%d corroborated misses, %d nobody saw)" % (
                c, door_rolling[c]["saw"], door_rolling[c]["trips"], door_rolling[c]["days"],
                door_rolling[c]["missed"], door_rolling[c]["unseen"]) for c in door_failing)
    elif missed_by_cam:
        summary += "; door misses this window: " + ",".join(
            "%s %d/%d" % (c, cover_stats.get(c, [0, 0])[0], cover_stats.get(c, [0, 0])[1])
            for c in sorted(missed_by_cam, key=lambda k: -missed_by_cam[k]))
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
        "updated_at": int(time.time() // HEARTBEAT_BUCKET_S) * HEARTBEAT_BUCKET_S,
        "door_openings": door_openings, "door_orphans": door_orphans,
        "door_orphan_rate": door_orphan_rate, "doors": door_by_name,
        "door_missed_by_cam": missed_by_cam, "door_miss_times": miss_times,
        "door_cover_stats": cover_stats,
        "door_deferred": door_deferred, "door_rolling": door_rolling,
        "door_coverage_failing": door_failing,
        "door_orphans_by_door": orphan_by_name, "door_orphan_times": orphan_times,
        "summary": summary,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - JSON-always contract
        fail("unhandled: %s" % exc)
