#!/usr/bin/env python3
"""Per-camera motion staleness monitor.

The fleet-wide dead-man (cameras_motion_stale_alert) catches a whole pipeline
outage but is structurally blind to ONE camera going dark: the fleet stays
noisy, so it never trips. That blind spot hid a camera whose motion detection
had been dead for a week.

Source is the RECORDER DATABASE, not entity last_changed, deliberately:
last_changed resets on every HA restart, which would mask staleness for the
whole window after any restart. The recorder survives restarts.

Emits ONE JSON object on stdout, always, exit 0 (command_line sensor contract).
"""
import json
import os
import sqlite3
import sys
import time

DB = "/config/home-assistant_v2.db"
VISION_STATE = "/config/.cam_vision_state.json"  # written by cam_vision.py
STALE_HOURS = 72.0      # a camera silent this long while the fleet is active
# Naturally-quiet cameras get longer windows. Zero events is NOT proof of a
# fault: an interior room can sit genuinely unvisited for a week, and under a
# 24/7-recording plan the camera still records everything regardless — this
# sensor only measures whether motion EVENTS are reaching HA.
STALE_HOURS_OVERRIDES = {
    # e.g.  "<naturally_quiet_outdoor_cam>": 120.0,
    #       "<rarely_visited_interior_cam>": 168.0,
}
FLEET_ACTIVE_HOURS = 24.0   # ...and someone else fired within this window
QUERY_TIMEOUT_S = 20

# binary_sensor.<name>_motion for each camera in the fleet
CAMS = [
    "<cam_1>",
    "<cam_2>",
    "<cam_3>",
    "<cam_4>",
    "<cam_5>",
    "<cam_6>",
    "<cam_7>",
    "<cam_8>",
    "<cam_9>",
]


def emit(payload):
    base = {
        "stale": [],
        "stale_count": 0,
        "visual_hours": None,
        "verdicts": None,
        "oldest_cam": None,
        "oldest_hours": None,
        "hours_since": None,
        "fleet_active": None,
        "summary": "",
        "error": None,
    }
    base.update(payload)
    print(json.dumps(base))
    sys.exit(0)


def fail(msg):
    emit({"summary": "error: " + msg, "error": msg})


def main():
    if not os.path.exists(DB):
        fail("recorder db not found at %s" % DB)

    entities = {"binary_sensor.%s_motion" % c: c for c in CAMS}
    placeholders = ",".join("?" * len(entities))
    sql = (
        "SELECT m.entity_id, MAX(s.last_updated_ts) "
        "FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id "
        "WHERE m.entity_id IN (%s) AND s.state = 'on' "
        "GROUP BY m.entity_id" % placeholders
    )
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=QUERY_TIMEOUT_S)
        try:
            rows = conn.execute(sql, list(entities.keys())).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - any DB problem becomes sensor state
        fail("recorder query: %s" % exc)

    now = time.time()
    last = {entities[e]: ts for e, ts in rows if ts}
    # A camera the recorder has NEVER seen 'on' is reported as None, not 0h.
    hours = {}
    for cam in CAMS:
        ts = last.get(cam)
        hours[cam] = round((now - ts) / 3600.0, 1) if ts else None

    seen = {c: h for c, h in hours.items() if h is not None}
    if not seen:
        fail("no motion history for any camera (recorder empty or entities renamed?)")

    fleet_active = min(seen.values()) <= FLEET_ACTIVE_HOURS
    # Never-seen cameras count as stale only once the recorder has run a while;
    # treat them as stale when the fleet is demonstrably active.
    stale = sorted(
        c for c in CAMS
        if (hours[c] is None
            or hours[c] > STALE_HOURS_OVERRIDES.get(c, STALE_HOURS))
    ) if fleet_active else []

    # Discriminator: cam_vision.py's frame-differencing log tells us whether a
    # motion-event-stale camera's SCENE has actually been changing. Verdicts are
    # annotation-grade and HONEST about their window: the vision log retains only
    # VISION_KEEP_H hours - always less than any staleness window - so "no visual
    # change" can never prove quiet across the whole stale span. (The old form
    # compared vis_h < ev_h - 1.0, which is unconditionally true whenever the log
    # is non-empty: a one-bit test dressed as a comparison.) The state file is
    # age-checked first: a dead/stalled visual monitor must surface as "no
    # verdict", not silently produce the reassuring branch.
    VISION_KEEP_H = 48.0     # cam_vision.py KEEP_HOURS
    VISION_STALE_MIN = 30.0  # vision polls every 2 min; older than this = dead
    vs, vision_age_min = {}, None
    try:
        vision_age_min = (now - os.path.getmtime(VISION_STATE)) / 60.0
        if vision_age_min <= VISION_STALE_MIN:
            vs = json.load(open(VISION_STATE))
    except Exception:
        vs, vision_age_min = {}, None
    visual_hours = {}
    for cam in CAMS:
        log = (vs.get(cam) or {}).get("log") or []
        visual_hours[cam] = round((now - max(log)) / 3600.0, 1) if log else None
    verdicts = {}
    for cam in stale:
        vis_h = visual_hours.get(cam)
        if vision_age_min is None or vision_age_min > VISION_STALE_MIN:
            age = "missing" if vision_age_min is None else "%.0f min old" % vision_age_min
            verdicts[cam] = "visual monitor not reporting (state %s) - no verdict" % age
        elif vis_h is not None:
            verdicts[cam] = ("scene changed %.1fh ago with no motion event - "
                             "detection/event path suspect" % vis_h)
        else:
            verdicts[cam] = ("no visual change in the last %.0fh (vision window; "
                             "shorter than the stale span) - consistent with a "
                             "quiet area" % VISION_KEEP_H)

    oldest_cam = max(seen, key=lambda c: seen[c])
    summary = "%d stale (>%.0fh); oldest %s %.1fh" % (
        len(stale), STALE_HOURS, oldest_cam, seen[oldest_cam],
    )
    if not fleet_active:
        summary = "fleet quiet (no motion anywhere in %.0fh) - staleness not evaluated" % FLEET_ACTIVE_HOURS

    emit({
        "stale": stale,
        "stale_count": len(stale),
        "oldest_cam": oldest_cam,
        "oldest_hours": seen[oldest_cam],
        "hours_since": hours,
        "fleet_active": fleet_active,
        "visual_hours": visual_hours,
        "verdicts": verdicts,
        "summary": summary,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - the sensor contract is JSON-always
        fail("unhandled: %s" % exc)
