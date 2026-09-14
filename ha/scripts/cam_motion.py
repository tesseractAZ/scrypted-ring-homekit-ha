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
import math
import os
import sqlite3
import sys
import time

DB = "/config/home-assistant_v2.db"
VISION_STATE = "/config/.cam_vision_state.json"  # written by cam_vision.py
MOTION_STATE = "/config/.cam_motion_state.json"  # written by THIS script
DOOR_STATE = "/config/.cam_flap_door_state.json"  # written by cam_flap.py
DOOR_STATE_MAX_AGE_MIN = 45.0  # cam_flap runs every 10 min; older means it is not reporting
# ---------------------------------------------------------------------------
# PROOF LATCH. The corroboration fit window is bounded by the SQL fetch at
# `ev_cut` below, which slides with now() while a stale camera's cut_ts is
# FIXED at its last event - so the historical half of the evidence shrinks by a
# day per day and a proof eventually starves itself. Measured on this fleet:
# <cam_9> was proven broken at p=2e-08 on 44 co-fires, and by day six the
# same camera, equally broken, was down to 11 co-fires and ~3 days from falling
# under CORROBORATE_MIN_HITS - at which point the card would have flipped to
# "cannot be tested", wording it itself defines as "no conclusion, not an
# all-clear". Losing a TRUE positive to calendar arithmetic is worse than the
# false positive the MIN_HITS floor exists to prevent.
#
# Widening the window was the obvious fix and is the wrong one: anchoring it at
# [cut-30d, cut] fits the partner rate WORSE (mean absolute error 0.450 vs 0.269
# against the actual post-period rate, measured over the five cameras alive
# across the cut), and because p = (1-rate_lb)**post, overstating the rate makes
# a proof CHEAPER - the zero-hit clusters needed to reach p<1e-3 fell from 61 to
# 9 on one camera. That trades a lost true positive for manufactured false ones.
#
# Latching costs nothing statistically. A proof that once passed every gate is a
# fact about a moment, not a claim that needs re-deriving hourly. The latch is
# keyed to cut_ts, so the instant the camera produces any event its cut moves and
# the latch no longer matches - it clears itself with no explicit reset path to
# get wrong. Nothing here can CREATE a verdict; it can only preserve one that was
# already earned.
LATCH_MAX_AGE_D = 90.0   # forget a latch this old even if the camera stays quiet
STALE_HOURS = 72.0      # a camera silent this long while the fleet is active
# Naturally-quiet cameras get longer windows. Zero events is NOT proof of a
# fault: an interior room can sit genuinely unvisited for a week, and under a
# 24/7-recording plan the camera still records everything regardless — this
# sensor only measures whether motion EVENTS are reaching HA.
STALE_HOURS_OVERRIDES = {
    # e.g.  "<naturally_quiet_outdoor_cam>": 120.0,
    #       "<rarely_visited_interior_cam>": 168.0,
    # Fit these from OCCUPIED-period data only, and only from periods the
    # recorder was actually writing: a window fitted just above a "quiet gap"
    # that is mostly a recorder blackout is fitted to hours in which the camera
    # was UNOBSERVED, not hours in which it was quiet.
}
FLEET_ACTIVE_HOURS = 24.0   # ...and someone else fired within this window
QUERY_TIMEOUT_S = 20
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
# Largest ALL-ENTITY hole in the recorder over this window. Every camera monitor
# is a process inside the thing it monitors, so a host-down outage produces NO
# alert of any kind: the fleet simply stops being observed and every gate is
# evaluated only while the host is up. This is the after-the-fact detector - a
# hole in the recorder spanning every entity means the host was down, and it is
# visible once the host returns. (A 2026-08-26 mains cut took the whole fleet
# dark for 56.9 min and raised nothing.) Measured cost ~0.1 s.
HOST_GAP_LOOKBACK_H = 24.0

# ---------------------------------------------------------------------------
# CROSS-CAMERA CORROBORATION.
# Frame-differencing answers "did this camera's SCENE change?", which on an
# outdoor view is guaranteed by sun, shadow and vegetation - so for a camera
# aimed at open ground the "detection/event path suspect" branch is the only
# reachable one and the verdict carries no information (measured 2026-08-30:
# 416 of 416 recorded samples for one camera read identically).
#
# Cameras that share a sight-line co-fire at a stable rate, and that rate is a
# real per-camera test of the EVENT path, computable from motion rows already in
# the recorder - no walk test, no new hardware, no physical access. For a stale
# camera, take each partner's motion clusters, measure how often the target
# historically appeared in them, then count how often it has appeared since it
# went quiet. Zero out of enough opportunities is a proof, not a suspicion.
#
# Measured on <cam_9>, 2026-08-30: <cam_6> co-fired 49.0% (72/147)
# before 2026-08-23 16:48Z and 0 of 32 after - P(observed | still working)
# = 4.4e-10, with five other partners agreeing. Over the same period
# <cam_8>'s share of <cam_6> clusters rose 28.1% -> 100%, so the scene
# was MORE active, not quiet.
#
# The test is honest about its own power: a camera with no high-rate partner
# (a spatially isolated view, or an interior room) returns "cannot be tested",
# never a false all-clear. <cam_5>'s best partner rate is 6.1%, so it
# is explicitly declared untestable rather than exonerated or condemned.
CORROBORATE_LOOKBACK_D = 30.0    # history used to fit partner co-fire rates
CORROBORATE_LINK_S = 300.0       # events this close chain into one cluster
CORROBORATE_MIN_RATE = 0.15      # a partner below this has too little power
CORROBORATE_MIN_PRE = 20         # ...and needs this many historical clusters
CORROBORATE_MIN_POST = 8
CLIQUE_MIN_SPAN_S = 24 * 3600.0  # a partner is only "silent too" after this long         # ...and this many opportunities since the cut
CORROBORATE_P = 1e-3             # P(silence | still working) below this = broken
CORROBORATE_MIN_HITS = 8         # ...and the rate must rest on at least this many
                                 # actual historical co-fires. A rate fitted from 4
                                 # co-fires has a 95% interval spanning 0.06-0.35;
                                 # calling anything derived from it a "proof" is
                                 # unearned regardless of how many post-clusters
                                 # accumulate. Measured 2026-08-31: a 4/25 fit was
                                 # ~1 day from stamping EVENT PATH BROKEN on a
                                 # camera, and would then have self-retracted two
                                 # days later when the sliding window dropped that
                                 # partner below MIN_PRE - a verdict decided by
                                 # window alignment, not by the camera.
# NO EXCLUDE_SPANS. A recorder blackout is DEFINED by having no rows, so excluding
# one removes nothing - there is nothing there to remove. The mechanism can only
# ever delete real data, and it did: this file shipped 2026-08-31 with a hard-coded
# span whose epochs were four days off their own comment, silently discarding 999 of
# the 2,525 motion rows (39.6%) in the corroboration lookback while the window it
# named held exactly 1 row. Removing it strengthened the one true positive
# (p 2.1e-09 -> 6.4e-11) and dissolved a developing false one. If a future recorder
# gap ever does need masking, mask it where it is measurable - as a gap in the data -
# not as a constant that no test can see.

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


def _wilson_lower(k, n, z=1.96):
    """Lower bound of the 95% CI for k/n. Used instead of the point estimate.

    The historical co-fire rate is ESTIMATED, and the p-value is exponentially
    sensitive to it: p = (1-rate)**post. Plugging in the point estimate asserts
    the rate is known exactly, which turns a thin sample into a confident
    verdict. Using the lower bound makes the strength of the conclusion scale
    with how well the rate is actually known - a 26/61 fit barely moves
    (0.426 -> 0.310, still decisive) while a 4/25 fit collapses
    (0.160 -> 0.064) and can no longer manufacture a proof.
    """
    if n <= 0:
        return 0.0
    p = k / float(n)
    d = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, (centre - margin) / d)


def _clusters(events, link_s):
    """[(ts, cam)] sorted -> [set(cam)] for runs chained within link_s."""
    out = []
    cur_t, cur_c = None, set()
    for ts, cam in events:
        if cur_t is not None and ts - cur_t <= link_s:
            cur_c.add(cam)
        else:
            if cur_c:
                out.append(cur_c)
            cur_c = {cam}
        cur_t = ts
    if cur_c:
        out.append(cur_c)
    return out


def corroborate(events, target, cut_ts, quiet_partners=None):
    """Is `target`'s silence since cut_ts explicable by a quiet scene?

    Returns a dict describing the strongest partner test available, or a dict
    saying plainly that no partner has enough statistical power. Never returns
    a reassuring verdict it cannot support.
    """
    ev = list(events)
    pre = _clusters([e for e in ev if e[0] < cut_ts], CORROBORATE_LINK_S)
    # Start the post window one link-width AFTER the camera's own last event, so
    # the cluster that contains that event cannot count as a co-fire. Leaving it
    # in credits the camera with one hit it earned on the way out and makes the
    # test look better-behaved than it is.
    post = _clusters([e for e in ev if e[0] >= cut_ts + CORROBORATE_LINK_S],
                     CORROBORATE_LINK_S)
    # How long the fleet has been observed since this camera went quiet.
    post_span_s = (max(e[0] for e in ev) - cut_ts) if ev else 0.0
    best = None
    gated_pre, gated_post = [], []
    if quiet_partners is None:
        quiet_partners = []
    for partner in {c for _, c in ev} - {target}:
        n_pre = [cl for cl in pre if partner in cl]
        n_post = [cl for cl in post if partner in cl]
        if len(n_pre) < CORROBORATE_MIN_PRE:
            if n_pre and sum(1 for cl in n_pre if target in cl) >= CORROBORATE_MIN_RATE * len(n_pre):
                gated_pre.append(partner)
            continue
        if len(n_post) < CORROBORATE_MIN_POST:
            if sum(1 for cl in n_pre if target in cl) >= CORROBORATE_MIN_RATE * len(n_pre):
                gated_post.append(partner)
            # This partner has the history to judge but has itself gone quiet
            # since the cut. Remember it: when a whole co-firing GROUP fails
            # together, every member's only high-power partners are the other
            # silent members, so MIN_POST removes exactly the cameras that could
            # adjudicate and the survivor is some 3%-correlated camera that
            # cannot. Reporting that as "no partner shares enough of this view"
            # is misleading - the partners share plenty, they are just silent
            # too, which is itself the more interesting fact.
            # Only call a partner SILENT if it has produced nothing at all, and
            # only once enough time has passed that it should have. Treating
            # "fewer than MIN_POST clusters" as silence is wrong: for a camera
            # that has only just gone quiet the post window is short and EVERY
            # partner looks silent, which would print an alarming group-outage
            # verdict for a perfectly healthy fleet.
            rate_q = sum(1 for cl in n_pre if target in cl) / float(len(n_pre))
            if (quiet_partners is not None and rate_q >= CORROBORATE_MIN_RATE
                    and len(n_post) == 0 and post_span_s >= CLIQUE_MIN_SPAN_S):
                quiet_partners.append((partner, round(rate_q, 3), len(n_post)))
            continue
        pre_hits = sum(1 for cl in n_pre if target in cl)
        rate = pre_hits / float(len(n_pre))
        hits = sum(1 for cl in n_post if target in cl)
        if best is None or rate > best["rate"]:
            best = {"partner": partner, "rate": round(rate, 3),
                    "pre_hits": pre_hits,
                    "rate_lb": round(_wilson_lower(pre_hits, len(n_pre)), 3),
                    "pre_clusters": len(n_pre), "post_clusters": len(n_post),
                    "post_hits": hits}
    if best is None or best["rate"] < CORROBORATE_MIN_RATE:
        if quiet_partners:
            quiet_partners.sort(key=lambda x: -x[1])
            names = ", ".join("%s (%.0f%%)" % (n, 100 * r) for n, r, _ in quiet_partners[:3])
            return {
                "testable": False,
                "best_rate": best["rate"] if best else None,
                "partner": best["partner"] if best else None,
                "quiet_partners": quiet_partners,
                "verdict": (
                    "cannot be tested - this camera's co-firing partners are "
                    "SILENT TOO: %s would each have the history to judge it, but "
                    "none has produced motion since this camera went quiet. A "
                    "whole group going dark together is not evidence that any one "
                    "of them is fine - it is a stronger signal than a single "
                    "quiet camera, and this test cannot see it. Check them "
                    "together, not one at a time." % names),
            }
        return {"testable": False, "best_rate": best["rate"] if best else None,
                "partner": best["partner"] if best else None,
                "verdict": ("cannot be tested by co-firing - no partner with at least %d "
                            "clusters before this camera went quiet and %d since reaches "
                            "the %.0f%% co-fire rate this test needs (best eligible: %s; "
                            "fitted on the %.1f days of history before its last event)%s. "
                            "That can mean the view is not shared, that the camera was "
                            "already firing only intermittently before its last event, or "
                            "that the fetch window has slid past its working period - it "
                            "is not evidence either way" % (
                                CORROBORATE_MIN_PRE, CORROBORATE_MIN_POST,
                                100 * CORROBORATE_MIN_RATE,
                                ("%s %.1f%%" % (best["partner"], 100 * best["rate"]))
                                if best else "none",
                                max(0.0, (cut_ts - min(e[0] for e in ev)) / 86400.0) if ev else 0.0,
                                "".join(x for x in (
                                    ("; %s co-fired at that rate but on too little history"
                                     % ", ".join(sorted(gated_pre))) if gated_pre else "",
                                    ("; %s co-fired at that rate but has produced too few "
                                     "clusters since" % ", ".join(sorted(gated_post)))
                                    if gated_post else ""))))}
    # P(observing zero co-fires | the camera still works at its historical rate).
    # This doubles as the test's POWER: if it is not small even at zero hits,
    # the test could not have detected a break, and no reassuring conclusion may
    # be drawn from silence. Reporting "consistent with a quiet area" from an
    # underpowered test is the exact false-all-clear this whole discriminator
    # exists to remove, so an underpowered result is reported as inconclusive.
    p = (1.0 - best["rate_lb"]) ** best["post_clusters"]
    thin = best["pre_hits"] < CORROBORATE_MIN_HITS
    powered = p < CORROBORATE_P and not thin
    broken = best["post_hits"] == 0 and powered
    best["p_value"] = float("%.2g" % p)
    best["testable"] = True
    best["powered"] = powered
    best["broken"] = broken
    if broken:
        best["verdict"] = (
            "EVENT PATH BROKEN (proof): %s co-fired with this camera in %.1f%% "
            "of its motion clusters historically (%d/%d; 95%% CI lower bound "
            "%.1f%%, which is the figure used), and in %d of %d since this camera "
            "went quiet - P(that | still working) = %.1g. The scene is "
            "demonstrably active; this camera is not reporting it."
            % (best["partner"], 100 * best["rate"], best["pre_hits"],
               best["pre_clusters"], 100 * best["rate_lb"],
               best["post_hits"], best["post_clusters"], p))
    elif thin:
        best["testable"] = False
        best["verdict"] = (
            "cannot be tested - the best partner (%s) shares this view on only %d "
            "historical occasions (%d/%d = %.1f%%, 95%% CI lower bound %.1f%%), too "
            "few to fit a rate that could support any verdict"
            % (best["partner"], best["pre_hits"], best["pre_hits"],
               best["pre_clusters"], 100 * best["rate"], 100 * best["rate_lb"]))
    elif not powered:
        best["testable"] = False
        best["verdict"] = (
            "inconclusive - %s (%.1f%% historically, 95%% CI lower bound %.1f%%) has "
            "produced just %d clusters since this camera went quiet; even total "
            "silence would only reach P=%.2g, so this test cannot distinguish a "
            "quiet area from a broken event path"
            % (best["partner"], 100 * best["rate"], 100 * best["rate_lb"],
               best["post_clusters"], p))
    else:
        best["verdict"] = (
            "consistent with a quiet area - %s co-fires %.1f%% historically and "
            "this camera still appeared in %d of %d of its clusters since"
            % (best["partner"], 100 * best["rate"], best["post_hits"],
               best["post_clusters"]))
    return best


def door_evidence_for(stale, now, path=None):
    """Per-camera door-contact evidence from cam_flap's rolling door record.

    Returns ({camera: sentence}, [failing cameras]) for every camera cam_flap
    reports as FAILING its door, and for any stale camera that has door trips at
    all. It is the strongest per-camera evidence in the stack - a door opened and
    the camera covering it did not fire - and it has to reach this sensor's
    SUMMARY, because the phone push for a stale camera carries the summary and
    nothing else: evidence printed only on the card never reaches the owner.

    A missing, stale or unreadable record returns (None, []) - "not reporting" -
    never a measured-looking {}, which would read as "looked, nothing wrong".
    One garbled camera entry is skipped on its own and never hides another
    camera's evidence. An annotation only: nothing here raises, and nothing here
    marks a camera broken.
    """
    path = path or DOOR_STATE
    try:
        if (now - os.path.getmtime(path)) / 60.0 > DOOR_STATE_MAX_AGE_MIN:
            return None, []
        with open(path) as fh:
            ds = json.load(fh)
        rolling = ds.get("rolling") if isinstance(ds, dict) else None
        if not isinstance(rolling, dict):
            return None, []
        failing = sorted(c for c in (ds.get("failing") or [])
                         if isinstance(c, str) and c in rolling)
    except Exception:  # noqa: BLE001 - door evidence is an annotation, never a failure
        return None, []
    stale_set = set(stale or [])
    out = {}
    for cam, r in sorted(rolling.items()):
        try:  # one garbled camera record never hides another camera's evidence
            if not isinstance(r, dict):
                continue
            trips = int(r.get("trips") or 0)
            if not (cam in failing or (cam in stale_set and trips)):
                continue
            last_saw = r.get("last_saw")
            out[cam] = "%ssaw %d of %d door trips in %.1f days (%d corroborated misses, %d nobody saw); last saw its door %s" % (
                "FAILING - " if cam in failing else "", int(r.get("saw") or 0), trips,
                float(r.get("days") or ds.get("rolling_days") or 0),
                int(r.get("missed") or 0), int(r.get("unseen") or 0),
                ("%sZ" % last_saw) if last_saw else "not in that period")
        except Exception:  # noqa: BLE001
            continue
    return out, failing


def door_summary_suffix(evidence, failing):
    """The text appended to the summary - and therefore to the push."""
    evidence = evidence or {}
    parts = ["%s %s" % (c, evidence[c].replace("FAILING - ", "", 1))
             for c in failing if c in evidence]
    return (" | DOOR COVERAGE FAILING: " + "; ".join(parts)) if parts else ""


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
        "door_evidence": None,
        "corroboration": None,
        "vision_blind": None,
        "oldest_cam": None,
        "oldest_hours": None,
        "hours_since": None,
        "fleet_active": None,
        "summary": "", "updated_at": None,
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
    # truncated to the visible part rather than dropped entirely: without it the
    # first row inside the window has no LAG partner, so an outage still running
    # at the boundary - and every outage longer than the lookback - reported
    # nothing at all, which is the exact blindness this field exists to remove.
    gap_sql = (
        "SELECT MAX(gap) FROM (SELECT last_updated_ts - "
        "COALESCE(LAG(last_updated_ts) OVER (ORDER BY last_updated_ts), ?) "
        "AS gap FROM states WHERE last_updated_ts > ?)"
    )
    # Every motion ON row over the corroboration window, for the co-firing test.
    ev_sql = (
        "SELECT s.last_updated_ts, m.entity_id "
        "FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id "
        "WHERE m.entity_id IN (%s) AND s.state = 'on' AND s.last_updated_ts > ? "
        "ORDER BY s.last_updated_ts" % placeholders
    )
    host_gap_min = None
    motion_events = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=QUERY_TIMEOUT_S)
        try:
            rows = conn.execute(sql, list(entities.keys())).fetchall()
            ev_cut = time.time() - CORROBORATE_LOOKBACK_D * 86400
            motion_events = [
                (float(t), entities[e]) for t, e in
                conn.execute(ev_sql, list(entities.keys()) + [ev_cut]).fetchall()
            ]
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
    # filters give the test something to actually discriminate ON:
    #   1. CO-CHANGE REJECTION. A sample where several cameras change at once is
    #      a global event - a lighting/IR transition, a passing cloud shadow, or
    #      the first frame after a restart (which has no valid prior frame and so
    #      registers on EVERY camera simultaneously). Only changes that are
    #      LOCALIZED to this camera are evidence about this camera.
    #   2. MULTIPLICITY. One surviving sample is noise; a scene that is genuinely
    #      active produces several.
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
    # a 0 count reads as "we looked and found nothing changing", which is the
    # reassuring direction, when the truth is "we could not look at all".
    vision_ok = vision_age_min is not None and vision_age_min <= VISION_STALE_MIN
    # PER-CAMERA freshness. The file's mtime only says the MONITOR ran; it is
    # rewritten every cycle even for a camera whose fetch failed, so a global
    # gate cannot separate "we looked and the scene was quiet" from "we could
    # not look at this camera at all" - and both landed on the reassuring
    # branch. cam_vision.py now stamps probe_ts per camera on a successful
    # sample only; a camera without a fresh stamp gets NO verdict.
    VISION_BLIND_MIN = 15.0
    cam_probe_age = {}
    for cam in CAMS:
        pts = (vs.get(cam) or {}).get("probe_ts") if vision_ok else None
        cam_probe_age[cam] = round((now - float(pts)) / 60.0, 1) if pts else None
    vision_blind = sorted(
        c for c in CAMS
        if vision_ok and (cam_probe_age[c] is None or cam_probe_age[c] > VISION_BLIND_MIN)
    )
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
        elif cam in vision_blind:
            # Reaching the "quiet area" branch from an empty log would be a
            # false all-clear: the log is empty because this camera was never
            # successfully sampled, not because its scene was still.
            seen_ago = ("never" if cam_probe_age[cam] is None
                        else "%.0f min ago" % cam_probe_age[cam])
            verdicts[cam] = ("visual monitor could not SEE this camera (last "
                             "successful frame %s) - no verdict" % seen_ago)
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

    # CORROBORATION. Run for every stale camera. This is stronger evidence than
    # frame-differencing and is reported first by the alert, but it is only
    # available where a partner camera shares enough of the view - where it is
    # not, it says so rather than falling back on a reassuring guess.
    corroboration = {}
    try:
        latches = json.load(open(MOTION_STATE)).get("proofs", {})
    except Exception:  # noqa: BLE001 - a missing/corrupt latch file must not fail the sensor
        latches = {}
    new_latches = {}
    for cam in stale:
        ts = last.get(cam)
        if ts is None:
            corroboration[cam] = {
                "testable": False,
                "verdict": "never seen in the recorder - no baseline to test against",
            }
            continue
        try:
            corroboration[cam] = corroborate(motion_events, cam, float(ts))
        except Exception as exc:  # noqa: BLE001 - an annotation must never fail the sensor
            corroboration[cam] = {"testable": False,
                                  "verdict": "corroboration failed: %s" % exc}
        v = corroboration[cam]
        prior = latches.get(cam)
        # A latch belongs to ONE silent span. Matching on cut_ts means any event
        # from this camera moves its cut and orphans the latch automatically -
        # there is no "clear the proof" path that can be forgotten or mis-fired.
        prior_valid = (
            isinstance(prior, dict)
            and abs(float(prior.get("cut_ts", 0)) - float(ts)) < 1.0
            and (now - float(prior.get("proven_at", 0))) <= LATCH_MAX_AGE_D * 86400
        )
        if v.get("broken"):
            # Keep the FIRST proof's figures: they were measured when the
            # evidence was strongest, and re-stating today's eroded numbers
            # would understate a conclusion that has not weakened.
            new_latches[cam] = prior if prior_valid else {
                "cut_ts": float(ts),
                "proven_at": now,
                "p_value": v.get("p_value"),
                "partner": v.get("partner"),
                "rate": v.get("rate"),
                "rate_lb": v.get("rate_lb"),
                "pre_hits": v.get("pre_hits"),
                "pre_clusters": v.get("pre_clusters"),
                "post_clusters": v.get("post_clusters"),
            }
        elif prior_valid:
            # The proof stands; only the window that re-derives it has shrunk.
            # Report the latched finding rather than letting a true positive
            # decay into "cannot be tested" through calendar arithmetic alone.
            age_d = (now - float(prior["proven_at"])) / 86400.0
            corroboration[cam] = {
                "testable": True,
                "broken": True,
                "latched": True,
                "proven_days_ago": round(age_d, 1),
                "partner": prior.get("partner"),
                "p_value": prior.get("p_value"),
                "rate": prior.get("rate"),
                "rate_lb": prior.get("rate_lb"),
                "pre_hits": prior.get("pre_hits"),
                "pre_clusters": prior.get("pre_clusters"),
                "verdict": (
                    "EVENT PATH BROKEN (proof established %.1f days ago and still "
                    "standing): %s co-fired with this camera in %.1f%% of its "
                    "motion clusters (%s/%s; 95%% CI lower bound %.1f%%) and P(the "
                    "silence since | still working) was %.1g. This camera has "
                    "produced no event at any point since, so the finding is "
                    "unchanged - it is LATCHED because the 30-day fit window has "
                    "slid past the camera's last working period and can no longer "
                    "re-derive it. It clears automatically on the camera's next "
                    "event. Current re-derivation says: %s"
                    % (age_d, prior.get("partner"), 100 * float(prior.get("rate") or 0),
                       prior.get("pre_hits"), prior.get("pre_clusters"),
                       100 * float(prior.get("rate_lb") or 0), prior.get("p_value"),
                       v.get("verdict", "n/a"))),
            }
            new_latches[cam] = prior
    try:
        json.dump({"proofs": new_latches}, open(MOTION_STATE, "w"))
    except Exception:  # noqa: BLE001 - losing the latch must not fail the sensor
        pass

    door_evidence, door_failing = door_evidence_for(stale, now)

    # The card used to print the DEFAULT window even when a per-camera override
    # was the one actually applied, which made the excursion look far worse than
    # it was (e.g. "151h vs 72h" when the applied window was 120h).
    applied_window = {c: STALE_HOURS_OVERRIDES.get(c, STALE_HOURS) for c in CAMS}
    oldest_cam = max(seen, key=lambda c: seen[c])
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
    proven = sorted(c for c, v in corroboration.items() if v.get("broken"))
    if proven:
        summary = "EVENT PATH BROKEN: %s | %s" % (", ".join(proven), summary)
    if host_gap_min and host_gap_min > 15.0:
        summary = ("HOST WAS DOWN %.0f min in the last %.0fh | " %
                   (host_gap_min, HOST_GAP_LOOKBACK_H)) + summary

    summary += door_summary_suffix(door_evidence, door_failing)
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
        "door_evidence": door_evidence,
        "corroboration": corroboration,
        "vision_blind": vision_blind,
        "summary": summary,
        "updated_at": int(time.time() // HEARTBEAT_BUCKET_S) * HEARTBEAT_BUCKET_S,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - the sensor contract is JSON-always
        fail("unhandled: %s" % exc)
