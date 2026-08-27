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
# Largest ALL-ENTITY hole in the recorder over this window. Every camera monitor
# is a process inside the thing it monitors, so a host-down outage produces NO
# alert of any kind: the fleet simply stops being observed and every gate is
# evaluated only while the host is up. This is the after-the-fact detector - a
# hole spanning every entity means the host was down, and it is visible once the
# host returns. (A real mains cut took a fleet dark for 56.9 min and raised
# nothing at all.) Measured cost ~0.1 s.
HOST_GAP_LOOKBACK_H = 24.0

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
        "host_gap_min": None,
        "visual_hours": None,
        "visual_localized_hours": None,
        "visual_localized_count": None,
        "applied_window": None,
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
    # COALESCE to the cutoff so a gap that STRADDLES the lookback edge is
    # truncated to the visible part rather than dropped: without it the first row
    # inside the window has no LAG partner, so an outage still running at the
    # boundary - and every outage longer than the lookback - reported nothing.
    gap_sql = (
        "SELECT MAX(gap) FROM (SELECT last_updated_ts - "
        "COALESCE(LAG(last_updated_ts) OVER (ORDER BY last_updated_ts), ?) "
        "AS gap FROM states WHERE last_updated_ts > ?)"
    )
    host_gap_min = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=QUERY_TIMEOUT_S)
        try:
            rows = conn.execute(sql, list(entities.keys())).fetchall()
            cutoff = time.time() - HOST_GAP_LOOKBACK_H * 3600
            g = conn.execute(gap_sql, (cutoff, cutoff)).fetchone()
            if g and g[0] is not None:
                host_gap_min = round(float(g[0]) / 60.0, 1)
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
    # A bare "the vision log is non-empty" test has NO discriminating power: an
    # outdoor scene guarantees entries via sun, shadow and IR transitions, so
    # every stale outdoor camera reads "suspect" no matter its true state. Two
    # filters give the test something to discriminate ON:
    #   1. CO-CHANGE REJECTION. A sample where several cameras change at once is
    #      a global event - a lighting/IR transition, a cloud shadow, or the first
    #      frame after a restart (which has no valid prior frame and so registers
    #      on EVERY camera simultaneously). Only changes LOCALIZED to this camera
    #      are evidence about this camera.
    #   2. MULTIPLICITY. One surviving sample is noise; an active scene produces
    #      several.
    CO_CHANGE_WINDOW_S = 180.0  # events this close together across cameras...
    CO_CHANGE_MIN = 3           # ...on this many cameras = global, not localized
    MIN_VISUAL_EVENTS = 2       # localized changes needed before calling it active
    vs, vision_age_min = {}, None
    try:
        vision_age_min = (now - os.path.getmtime(VISION_STATE)) / 60.0
        if vision_age_min <= VISION_STALE_MIN:
            vs = json.load(open(VISION_STATE))
    except Exception:
        vs, vision_age_min = {}, None

    # An unusable vision sample must publish None, never a measured-looking 0:
    # a 0 count reads as "we looked and found nothing changing", the reassuring
    # direction, when the truth is "we could not look at all".
    vision_ok = vision_age_min is not None and vision_age_min <= VISION_STALE_MIN
    raw_log = {cam: sorted((vs.get(cam) or {}).get("log") or []) for cam in CAMS}
    visual_hours, localized_hours, localized_count = {}, {}, {}
    for cam in CAMS:
        if not vision_ok:
            visual_hours[cam] = localized_hours[cam] = localized_count[cam] = None
            continue
        mine = raw_log[cam]
        visual_hours[cam] = round((now - max(mine)) / 3600.0, 1) if mine else None
        local = []
        for ts in mine:
            others = sum(1 for c2 in CAMS if c2 != cam and
                         any(abs(t2 - ts) <= CO_CHANGE_WINDOW_S for t2 in raw_log[c2]))
            if others < CO_CHANGE_MIN - 1:
                local.append(ts)
        localized_count[cam] = len(local)
        localized_hours[cam] = round((now - max(local)) / 3600.0, 1) if local else None

    verdicts = {}
    for cam in stale:
        loc_h, loc_n = localized_hours.get(cam), localized_count.get(cam) or 0
        shared = (len(raw_log[cam]) - loc_n) if vision_ok else 0
        if vision_age_min is None or vision_age_min > VISION_STALE_MIN:
            age = "missing" if vision_age_min is None else "%.0f min old" % vision_age_min
            verdicts[cam] = "visual monitor not reporting (state %s) - no verdict" % age
        elif loc_n >= MIN_VISUAL_EVENTS and loc_h is not None:
            verdicts[cam] = ("%d localized scene changes in the last %.0fh (most "
                             "recent %.1fh ago) with no motion event - "
                             "detection/event path suspect"
                             % (loc_n, VISION_KEEP_H, loc_h))
        elif loc_n or shared:
            verdicts[cam] = ("inconclusive - %d scene change(s) in %.0fh, of which "
                             "%d were fleet-wide (lighting/restart, not this "
                             "camera); too little localized evidence to judge"
                             % (loc_n + shared, VISION_KEEP_H, shared))
        else:
            verdicts[cam] = ("no visual change in the last %.0fh (vision window; "
                             "shorter than the stale span) - consistent with a "
                             "quiet area" % VISION_KEEP_H)

    oldest_cam = max(seen, key=lambda c: seen[c])
    # The card used to print the DEFAULT window even when a per-camera override
    # was the one actually applied, making the excursion look far worse than it
    # was (e.g. "151h vs 72h" when the applied window was 120h).
    applied_window = {c: STALE_HOURS_OVERRIDES.get(c, STALE_HOURS) for c in CAMS}
    if stale:
        summary = "%d stale (%s); oldest %s %.1fh" % (
            len(stale),
            ", ".join("%s >%.0fh" % (c, applied_window[c]) for c in stale),
            oldest_cam, seen[oldest_cam],
        )
    else:
        summary = "0 stale; oldest %s %.1fh" % (oldest_cam, seen[oldest_cam])
    if not fleet_active:
        summary = "fleet quiet (no motion anywhere in %.0fh) - staleness not evaluated" % FLEET_ACTIVE_HOURS
    if host_gap_min and host_gap_min > 15.0:
        summary = ("HOST WAS DOWN %.0f min in the last %.0fh | " %
                   (host_gap_min, HOST_GAP_LOOKBACK_H)) + summary

    emit({
        "stale": stale,
        "stale_count": len(stale),
        "oldest_cam": oldest_cam,
        "oldest_hours": seen[oldest_cam],
        "hours_since": hours,
        "fleet_active": fleet_active,
        "host_gap_min": host_gap_min,
        "visual_hours": visual_hours,
        "visual_localized_hours": localized_hours,
        "visual_localized_count": localized_count,
        "applied_window": applied_window,
        "verdicts": verdicts,
        "summary": summary,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - the sensor contract is JSON-always
        fail("unhandled: %s" % exc)
