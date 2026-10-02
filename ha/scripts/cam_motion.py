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
import bisect
import datetime
import json
import math
import os
import sqlite3
import sys
import time

# PARTIAL LOSS (partial_loss.py beside this script). Imported defensively: a
# missing or broken module costs only the partial_* attributes - published as
# partial_loss=None and partial_detail={"error": ...}, never as an empty list
# that would read as "looked, nothing lost" - and never the staleness sensor.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import partial_loss as _pl
except Exception as _pl_exc:  # noqa: BLE001
    _pl = None
    _PL_IMPORT_ERROR = "%s: %s" % (type(_pl_exc).__name__, _pl_exc)
else:
    _PL_IMPORT_ERROR = None

DB = "/config/home-assistant_v2.db"
VISION_STATE = "/config/.cam_vision_state.json"  # written by cam_vision.py
MOTION_STATE = "/config/.cam_motion_state.json"  # written by THIS script
# BACKGROUND FIRING RATES, published in MOTION_STATE for cam_flap.py. For each
# camera and each UTC hour of day: the probability that the camera fires inside
# a random +-BACKGROUND_WINDOW_S window, i.e. by chance. cam_flap uses it to tell
# a door miss "corroborated" by a camera that fires all evening anyway (weak) from
# one corroborated by a camera that fires almost only when someone is there
# (strong). Measured over the last BACKGROUND_LOOKBACK_D days, counting only days
# on which that camera fired at all: a 30-day average diluted by a dead fortnight
# put one camera's evening rate at 0.22 when its live rate was 0.52.
BACKGROUND_LOOKBACK_D = 7.0
BACKGROUND_WINDOW_S = 180.0      # = cam_flap.py DOOR_MOTION_WINDOW_S
BACKGROUND_MIN_OBS_S = 3 * 3600.0  # an hour of day needs this much active-day time
# VISION RECALL. "No visual change in 48 h" only means a quiet area if frame
# differencing would have SEEN activity there. Measured 2026-09-16..30 (own
# motion clusters with any vision change within +-6 min): 79 %, 53 % and 33 % on
# the three south cameras, but 8 %, 6 %, 4 % and 3 % on the four busiest - one
# of which, while broken, was described as "consistent with a quiet area". Each
# poll records, per camera, whether each settled motion cluster inside the vision
# window was matched by a change in that camera's own vision log. The outcomes
# persist in MOTION_STATE, so a camera that has since gone silent is judged by
# what the detector could see while it was working.
RECALL_LINK_S = 300.0            # motion events this close form one cluster
RECALL_MARGIN_S = 360.0          # a vision change this close to a cluster counts
RECALL_SETTLE_S = 600.0          # only clusters that ended this long ago
RECALL_KEEP = 40                 # outcomes kept per camera (most recent)
RECALL_MIN_CLUSTERS = 10         # fewer than this = not yet measured
RECALL_FLOOR = 0.25              # below this the detector cannot call a view quiet
RECALL_MAX_AGE_D = 30.0          # outcomes older than this are dropped
# RECALL RECENCY. The 25% floor over the last 40 outcomes answers "could the
# detector see this view?" with hits from up to 40 clusters ago, so a detector
# that has just gone deaf keeps clearing it on old hits. Measured 2026-10-02:
# one camera's vision bar ratcheted to its cap and it missed its last 8 clusters
# in a row, yet its record read 4/12 (31/40 had the record run since August),
# so a stale page would have called its silence "consistent with a quiet area".
# A trailing run of misses is judged against the camera's OWN rate before the
# run, the way corroboration judges a silent camera: P = (1 - p_lb)**run, p_lb
# the 95% lower bound of that rate. The record before a run always ENDS on the
# hit that stopped it, so that hit is left out - (k-1)/(n-1) - or it inflates
# the rate: 4/4 before a run of 7 gives P = 0.0068, 3/3 gives 0.018. Two
# conditions withhold the quiet verdict, with different wording:
#   RUN TEST: run >= RECALL_RUN_MIN and P < RECALL_RUN_ALPHA - the text may
#     state P, and says only that the silence cannot be read as a quiet area;
#   CAP: run >= RECALL_RUN_CAP regardless of P - for a rate too low or too
#     thinly measured to reach alpha; no P and no causal claim, because a run
#     can also be the camera firing on things frame differencing rightly
#     ignores (insects at the IR lamp) - in i.i.d. simulation of healthy
#     cameras at 25/30/35% recall the cap alone withholds 4.2/3.3/2.1% of the
#     polls the floor allows.
# Replayed on records rebuilt from the recorder (2026-08-12..10-02, hourly
# polls): on its record as if kept since August the deaf camera above is
# withheld from its 4th miss, ~20 h after the first (on the 4/12 record it
# actually had, at the 8th), where the floor alone needed ~30 misses (median
# 188 h over synthetic onsets); synthetic deafness is withheld after a median 4 / 8 / 7 clusters
# (24 / 38 / 29 h) on the three south cameras. On healthy history it withheld
# 0 of 962 and 0 of 1017 polls the floor allowed on two cameras, and 15 of 428
# (3.5%) on the one whose recall sits AT the floor (25%). The (k-1)/(n-1)
# correction changes none of these counts.
RECALL_RUN_MIN = 4
RECALL_RUN_ALPHA = 0.01
RECALL_RUN_CAP = 8
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
# PARTIAL LOSS - see partial_loss.py. The detector reuses this file's co-firing
# constants (partial_shared()) rather than restating them.
#
# ACKNOWLEDGE. A flag clears only on evidence of recovery, never by time, so a
# camera whose co-firing stays low (a deliberately narrowed motion zone, a
# partner re-aimed for good) stays flagged. To silence ONE flag, add the camera
# with the `since` its partial_detail entry shows: {"<camera>": <since>} or
# {"<camera>": {"since": <since>, "note": "why"}}. The ack is keyed to that
# flag, the way a proof latch is keyed to cut_ts: a later, separate flag on the
# same camera gets a new `since` and pages again. An ack that matches no current
# flag is listed in partial_detail.ack_unmatched, so a stale entry is visible.
PARTIAL_LOSS_ACK = {}
PARTIAL_CLEARED_KEEP_S = 24 * 3600   # a cleared flag's reason stays visible this long
PARTIAL_SCOPE = (
    "flags a camera that drops out of most of the motion its usual partners still "
    "record, typically 40-50 h after onset. Replayed on this fleet it caught 18 of 24 "
    "total losses (staleness pages the rest at 72 h), 17 of 24 at 90%, 14 of 24 at 80%, "
    "6 of 24 at 67%, 4 of 24 at 50%, 8 of 24 day-only and 1 of 24 night-only losses; "
    "slow declines are not detected. No flag is not an all-clear: min_loss is the "
    "smallest loss each camera's best partner would convict now, and cameras listed in "
    "cannot_convict could not have been convicted this poll even by a total loss")
# RECORDER HOLES for the partial-loss test: every all-entity gap longer than
# this over its ~62-day lookback is unobserved time, not silence. Listing them
# with the LAG scan above over the whole lookback costs 5.1 s on the Pi
# (4.0 M rows, measured 2026-10-02) against 0.09 s for 24 h, so the list is
# cached in MOTION_STATE and each poll scans only the rows written since the
# previous scan; a missing or unusable cache costs one full scan.
PARTIAL_GAP_MIN_S = 300.0

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
            out[cam] = "%ssaw %d of %d door trips in %.1f days (%d strong + %d weak corroborated misses, %d nobody saw; %d strong misses in a row since it last saw its door); last saw its door %s" % (
                "FAILING - " if cam in failing else "", int(r.get("saw") or 0), trips,
                float(r.get("days") or ds.get("rolling_days") or 0),
                int(r.get("missed") or 0), int(r.get("weak") or 0), int(r.get("unseen") or 0),
                int(r.get("run") or 0),
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


def background_rates(events, now):
    """{camera: [p(fires within +-BACKGROUND_WINDOW_S) for UTC hour 0..23]}.

    Only days on which the camera fired at least once count, so a camera that was
    dead for part of the window keeps the rate it shows while working. An hour of day with less than BACKGROUND_MIN_OBS_S of active-day observation
    is None (unknown), never 0 - a rate from a few minutes of data would swing
    across cam_flap's bar from poll to poll."""
    start = now - BACKGROUND_LOOKBACK_D * 86400
    w = BACKGROUND_WINDOW_S
    out = {}
    for cam in CAMS:
        ts = sorted(t for t, c in events if c == cam and start - w <= t <= now)
        days = {int(t // 86400) for t in ts if t >= start}
        tot = [0.0] * 24
        for h in range(int(start // 3600), int(now // 3600) + 1):
            if (h * 3600) // 86400 not in days:
                continue
            a, b = max(start, h * 3600.0), min(now, (h + 1) * 3600.0)
            if b > a:
                tot[h % 24] += b - a
        cov = [0.0] * 24
        merged = []
        for t in ts:
            a, b = max(start, t - w), min(now, t + w)
            if b <= a:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            t = a
            while t < b:
                nb = min(b, (t // 3600 + 1) * 3600.0)
                if int(t // 86400) in days:
                    cov[int(t // 3600) % 24] += nb - t
                t = nb
        out[cam] = [round(cov[i] / tot[i], 3) if tot[i] >= BACKGROUND_MIN_OBS_S else None
                    for i in range(24)]
    return out


def motion_clusters(ts, link=None):
    """Group sorted timestamps into [first, last] clusters linked by <= link s."""
    link = RECALL_LINK_S if link is None else link
    out = []
    for t in sorted(ts):
        if out and t - out[-1][1] <= link:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def update_recall(prev, events, vision_logs, now, window_start, unusable,
                  usable_since=None, last_event=None):
    """Merge newly settled motion clusters into the per-camera recall record.

    prev: {cam: [[cluster_start, hit], ...]} from MOTION_STATE. A cluster is
    judged once, only when it has settled, its start lies inside the vision window and after
    the camera was last unusable, and
    never for a camera the vision monitor cannot currently see (blind or cached).
    usable_since: {cam: ts} - clusters starting before the camera last became
    visible again are skipped, so motion from a period the vision monitor could not
    see is never scored as a detector miss after it recovers. Outcomes age out
    relative to the camera's own last motion event (last_event), not to now: a
    camera silent for weeks keeps the record of what the detector saw while it
    worked. Returns the new record; malformed entries are dropped one at a time."""
    out = {}
    for cam in CAMS:
        known = {}
        stored = (prev or {}).get(cam) if isinstance(prev, dict) else None
        ref = (last_event or {}).get(cam) or now
        for e in stored if isinstance(stored, list) else []:
            try:
                s, h = float(e[0]), int(e[1])
                if s == s and ref - s <= RECALL_MAX_AGE_D * 86400 and h in (0, 1):
                    known[int(round(s))] = h
            except Exception:  # noqa: BLE001
                continue
        since = (usable_since or {}).get(cam)
        floor = max(window_start, float(since)) if isinstance(since, (int, float)) and since == since else window_start
        log = vision_logs.get(cam)
        if log is not None and cam not in unusable:
            for a, b in motion_clusters([t for t, c in events if c == cam]):
                if a - RECALL_MARGIN_S < floor or b > now - RECALL_SETTLE_S:
                    continue
                key = int(round(a))
                if key in known:
                    continue
                known[key] = 1 if any(a - RECALL_MARGIN_S <= v <= b + RECALL_MARGIN_S
                                      for v in log) else 0
        out[cam] = [[s, known[s]] for s in sorted(known)][-RECALL_KEEP:]
    return out


def recall_recency(ent):
    """(run, hits_before, n_before, p) for a recall record [[start, hit], ...].

    run = misses at the END of the record (oldest-first order); hits_before /
    n_before = the record before the run; p = probability of a run that long if
    the detector still saw this view at the 95% lower bound of its rate before
    the run, with the hit that ENDS the run left out ((k-1)/(n-1)). p is 1.0
    when there is no run. Malformed entries count as neither hit nor miss."""
    clean = []
    for e in ent or []:
        try:
            h = int(e[1])
        except Exception:  # noqa: BLE001
            continue
        if h in (0, 1):
            clean.append(h)
    run = 0
    for h in reversed(clean):
        if h:
            break
        run += 1
    before = clean[:len(clean) - run]
    k, n = sum(before), len(before)
    if run == 0:
        return 0, k, n, 1.0
    kk, nn = (k - 1, n - 1) if n else (0, 0)
    p = (1.0 - (_wilson_lower(kk, nn) if nn > 0 else 0.0)) ** run
    return run, k, n, p


def recall_deaf(ent):
    """'run' when the record ENDS in a miss run its own history makes improbable
    (P < RECALL_RUN_ALPHA, at least RECALL_RUN_MIN long), 'cap' when the run is
    RECALL_RUN_CAP or longer but P does not reach alpha, else None."""
    run, _, _, p = recall_recency(ent)
    if run < RECALL_RUN_MIN:
        return None
    if p < RECALL_RUN_ALPHA:
        return "run"
    return "cap" if run >= RECALL_RUN_CAP else None


def partial_shared():
    """The co-firing constants partial_loss.py reuses - passed, never restated."""
    return {"LINK_S": CORROBORATE_LINK_S, "MIN_PRE": CORROBORATE_MIN_PRE,
            "MIN_HITS": CORROBORATE_MIN_HITS, "MIN_RATE": CORROBORATE_MIN_RATE,
            "STALE_HOURS": {c: STALE_HOURS_OVERRIDES.get(c, STALE_HOURS) for c in CAMS},
            "wilson": _wilson_lower}


def scan_host_gaps(conn, cache, now, horizon):
    """All-entity recorder holes > PARTIAL_GAP_MIN_S since `horizon`.

    cache: MOTION_STATE["host_gaps"] = {"from", "to", "gaps"} from the previous
    poll. Scans only rows from the last row the previous scan saw ("to"), with
    the row before the scan's start seeded as the first LAG partner so a hole
    straddling it is not lost; without a cache that reaches back to the horizon
    it scans the whole lookback once. Returns the new cache (gaps merged and
    pruned to the horizon). Raises on a query failure; the caller keeps the
    previous cache."""
    gaps, frm, to = [], None, None
    if isinstance(cache, dict):
        try:
            frm, to = float(cache["from"]), float(cache["to"])
            gaps = [[float(a), float(b)] for a, b in cache.get("gaps") or []]
        except Exception:  # noqa: BLE001 - an unusable cache costs one full scan
            frm, to, gaps = None, None, []
    if frm is None or to is None or frm > horizon + 3600.0 or to > now or to < horizon:
        frm, start, gaps = horizon, horizon, []
    else:
        start = to
    seed = conn.execute("SELECT MAX(last_updated_ts) FROM states WHERE last_updated_ts < ?",
                        (start,)).fetchone()[0]
    rows = conn.execute(
        "SELECT prev, ts FROM (SELECT last_updated_ts AS ts, COALESCE(LAG(last_updated_ts) "
        "OVER (ORDER BY last_updated_ts), ?) AS prev FROM states WHERE last_updated_ts >= ?) "
        "WHERE ts - prev > ?", (seed, start, PARTIAL_GAP_MIN_S)).fetchall()
    last = conn.execute("SELECT MAX(last_updated_ts) FROM states").fetchone()[0]
    merged = []
    for a, b in sorted(gaps + [[float(a), float(b)] for a, b in rows if a is not None]):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return {"from": frm, "to": float(last) if last is not None else start,
            "gaps": [[round(a, 1), round(b, 1)] for a, b in merged if b > horizon]}


def partial_ack(flag_since):
    """PARTIAL_LOSS_ACK against the current flags {cam: since}.
    Returns ({cam: note-or-True} acknowledged, {cam: reason} unmatched)."""
    acked, unmatched = {}, {}
    for cam, v in sorted((PARTIAL_LOSS_ACK or {}).items()):
        try:
            since = float(v.get("since") if isinstance(v, dict) else v)
            if isinstance(v, bool) or since != since:
                raise ValueError(v)
        except Exception:  # noqa: BLE001 - one malformed ack never hides another
            unmatched[cam] = "malformed (need the flag's since)"
            continue
        if cam in flag_since and abs(float(flag_since[cam]) - since) < 1.0:
            acked[cam] = (v.get("note") if isinstance(v, dict) else None) or True
        else:
            unmatched[cam] = "no current flag since %d" % since
    return acked, unmatched


# ---------------------------------------------------------------------------
# ROOM SWITCH NOTES. For a camera with a room light switch, a stale verdict
# lists the physical presses of that switch since the camera's last motion
# event - an annotation only. A Z-Wave Central Scene event is sent only for a
# paddle pressed by hand (remote HomeKit/Siri toggles produced none), so the
# scene event entities are a clean record of someone at the switch; but the
# switch box is OUTSIDE the camera's view, so a press does not put anyone in
# the room. Measured 2026-07-18..10-02: 8 presses of this switch on 5 visits;
# 3 visits outside the 461.3 h silence were seen (camera fired 106 s before to
# 39 s after a press), the 2 inside it were not - and neither of those shows a
# person entering the room (one left through the patio within 2 min). A press
# of the other light's switch in the same box drew no motion at 08-20 18:52Z
# 23.7 min after a run of 14 motion events from this camera, with the camera
# working. 3 seen visits bound P(seen | press, working camera) only above 0.44
# (Wilson), so 2 misses in a row could still be chance at P ~0.3: not a proof,
# not a tier. Each visit's outcome is logged passively in
# MOTION_STATE["witness_log"] to build that calibration (8 visits is the bar
# CORROBORATE_MIN_HITS sets). A double-tap of either paddle (KeyPressed2x,
# listed in each entity's event_types) is a deliberate walk test, run by the
# camera_room_walk_test automation: one followed by no motion from the
# camera is named in the note.
WITNESS = {
    # "<rarely_visited_interior_cam>": {
    #     "on": ("event.<witness_switch_scene_1>",),   # top paddle: light on
    #     "off": ("event.<witness_switch_scene_2>",),  # bottom paddle: light off
    #     "companions": ("<cam_a>", "<cam_b>"),       # cameras that see the way in
    # },
}
WITNESS_LOOKBACK_D = 90.0
WITNESS_MAX_LAG_S = 120.0    # a NEW press is recorded within this of its own stamp
WITNESS_LINK_S = 1800.0      # presses closer than this are one visit
WITNESS_COMPANION_S = 120.0  # a companion camera this close to a press saw the person
WITNESS_WINDOW_S = 600.0     # the camera firing this close to a visit saw it (log)
WITNESS_BRACKET_S = 86400.0  # log a visit once this has passed after it
WITNESS_LOG_KEEP = 50        # visits kept per camera in MOTION_STATE["witness_log"]
WITNESS_NOTE_MAX = 8         # newest presses listed in a note
WITNESS_WALK_S = 300.0       # = camera_room_walk_test's wait (either paddle)


def _iso_ts(s):
    """An event entity's state (the ISO time of its last event) -> epoch, or None."""
    if not isinstance(s, str) or s[:2] != "20":
        return None
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.timestamp() if d.tzinfo is not None else None


def _z(t):
    return time.strftime("%m-%d %H:%MZ", time.gmtime(t))


def witness_presses(rows, kinds):
    """[(entity_id, state, last_updated_ts, event_type)] -> sorted
    [(press_ts, kind, event_type)].

    A physical press writes a NEW state equal to its own timestamp, recorded
    within 0.001 s of it (8 of 8). A restart or node re-interview re-writes the
    entity with the OLD timestamp 85 s to weeks later, and an unavailable/unknown
    row has no timestamp: rows outside -5..+WITNESS_MAX_LAG_S of their own
    timestamp are dropped and a timestamp seen on several rows is one press."""
    out = {}
    for row in rows:
        eid, st, lu = row[0], row[1], row[2]
        et = row[3] if len(row) > 3 else None
        kind, t = kinds.get(eid), _iso_ts(st)
        if kind is None or t is None or lu is None:
            continue
        if -5.0 <= float(lu) - t <= WITNESS_MAX_LAG_S:
            out.setdefault((eid, round(t, 3)), (t, kind, et if isinstance(et, str) else None))
    return sorted(out.values())


def witness_visits(presses):
    """[(ts, kind, event_type)] sorted -> [{first, last, n, on, tap2, ts}] for
    presses chained <= WITNESS_LINK_S apart."""
    out = []
    for t, kind, et in presses:
        if out and t - out[-1]["last"] <= WITNESS_LINK_S:
            v = out[-1]
            v["last"], v["n"] = t, v["n"] + 1
        else:
            v = {"first": t, "last": t, "n": 1, "on": False, "tap2": False, "ts": []}
            out.append(v)
        v["on"] = v["on"] or kind == "on"
        v["tap2"] = v["tap2"] or et == "KeyPressed2x"
        v["ts"].append(t)
    return out


def _near(ts_sorted, t, w):
    """Timestamps in ts_sorted within +-w of t."""
    i = bisect.bisect_left(ts_sorted, t - w)
    j = bisect.bisect_right(ts_sorted, t + w)
    return ts_sorted[i:j]


def witness_note(cam, cfg, rows, presses, last_event, on_by_cam, data_from, now):
    """The note appended to a stale camera's verdict. Never raises on its inputs."""
    test = ("To test it: double-tap either paddle of the room switch, then walk into "
            "the room - the walk-test automation pushes PASS or FAIL within 5 minutes; if "
            "the camera fires, it leaves the stale list at the next poll.")
    caveat = ("The switch box is outside this camera's view, so a press with no motion "
              "here is NOT proof of a fault - a companion camera firing shows someone "
              "in the garage, not in the room.")
    if not rows:
        return ("ROOM SWITCH: no recorder rows at all for %s in %.0f days - renamed "
                "or removed? Presses cannot be listed. %s"
                % (", ".join(cfg["on"] + cfg["off"]), WITNESS_LOOKBACK_D, test))
    mine = [x for x in presses if last_event is None or x[0] > last_event]
    if not mine:
        last = presses[-1][0] if presses else None
        return ("ROOM SWITCH: no physical press since this camera's last motion "
                "event (last press %s). %s"
                % (_z(last) if last else "none in %.0f days" % WITNESS_LOOKBACK_D, test))
    parts = []
    for t, kind, et in mine[-WITNESS_NOTE_MAX:]:
        what = "light ON (top paddle)" if kind == "on" else "light OFF (bottom paddle)"
        if et == "KeyPressed2x":
            what += (" DOUBLE-TAP = WALK TEST, in progress" if now - t < WITNESS_WALK_S
                     else " DOUBLE-TAP = WALK TEST: no motion from this camera since")
        if t < data_from:
            comp = "companion cameras not checked (before the motion lookback)"
        else:
            hits = []
            for c in cfg["companions"]:
                near = _near(on_by_cam.get(c, []), t, WITNESS_COMPANION_S)
                if near:
                    d = min(near, key=lambda x: abs(x - t)) - t
                    hits.append("%s %+.0f s" % (c, d))
            comp = ("companion fired: " + ", ".join(hits)) if hits else \
                "no companion camera within %.0f min" % (WITNESS_COMPANION_S / 60.0)
        lead = ""
        if last_event is not None and t - last_event <= WITNESS_WINDOW_S:
            lead = "; this camera's last event came %.0f s before it" % (t - last_event)
        parts.append("%s %s - %s%s" % (_z(t), what, comp, lead))
    older = len(mine) - len(parts)
    return ("ROOM SWITCH: %d physical press(es) since this camera's last motion "
            "event%s: %s. %s %s"
            % (len(mine), (" (newest %d listed)" % len(parts)) if older else "",
               "; ".join(parts), caveat, test))


def update_witness_log(prev, visits, own_on, on_by_cam, cfg, gaps, data_from, now):
    """Passive calibration record: one immutable entry per visit to the switch,
    written once WITNESS_BRACKET_S has passed after it (so whether the camera
    fired in the day either side - 'bracketed', i.e. demonstrably working - is
    known). Visits whose bracket reaches before the motion lookback are skipped.
    Returns the newest WITNESS_LOG_KEEP entries; malformed stored entries are
    dropped one at a time."""
    log = {}
    for e in prev if isinstance(prev, list) else []:
        try:
            log[int(e["t"])] = e
        except Exception:  # noqa: BLE001
            continue
    for v in visits:
        key = int(v["first"])
        if key in log or now < v["last"] + WITNESS_WINDOW_S + WITNESS_BRACKET_S:
            continue
        if v["first"] - WITNESS_BRACKET_S < data_from:
            continue
        a, b = v["first"] - WITNESS_WINDOW_S, v["last"] + WITNESS_WINDOW_S
        inside = [t for t in own_on if a <= t <= b]
        near = min((0.0 if v["first"] <= t <= v["last"] else
                    min(abs(t - v["first"]), abs(t - v["last"])) for t in inside),
                   default=None)
        comp = sorted({c for c in cfg["companions"] for t in v["ts"]
                       if _near(on_by_cam.get(c, []), t, WITNESS_COMPANION_S)})
        log[key] = {
            "t": key, "n": v["n"], "on": v["on"], "tap2": v["tap2"],
            "seen": bool(inside), "near_s": round(near, 1) if near is not None else None,
            "comp": comp,
            "before": any(v["first"] - WITNESS_BRACKET_S <= t < a for t in own_on),
            "after": any(b < t <= v["last"] + WITNESS_BRACKET_S for t in own_on),
            "hole": any(x < b and y > a for x, y in gaps),
        }
    return [log[k] for k in sorted(log)][-WITNESS_LOG_KEEP:]


def emit(payload):
    base = {
        "stale": [],
        "stale_count": 0,
        "partial_loss": None,
        "partial_detail": None,
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
    motion_events_all = []   # the partial-loss lookback (~62 d, longer than 30 d)
    try:
        with open(MOTION_STATE) as fh:
            prev_state = json.load(fh)
        if not isinstance(prev_state, dict):
            prev_state = {}
    except Exception:  # noqa: BLE001 - every consumer below has its own fallback
        prev_state = {}
    gap_cache, gap_error = prev_state.get("host_gaps"), None
    witness_rows, witness_error = {}, {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=QUERY_TIMEOUT_S)
        try:
            rows = conn.execute(sql, list(entities.keys())).fetchall()
            ev_cut = time.time() - CORROBORATE_LOOKBACK_D * 86400
            # One fetch serves both: every pre-existing consumer gets EXACTLY the
            # rows it got before (the same `> ev_cut` predicate, applied here).
            pl_cut = ev_cut
            if _pl is not None:
                pl_cut = min(ev_cut, time.time() - _pl.lookback_s())
            motion_events_all = [
                (float(t), entities[e]) for t, e in
                conn.execute(ev_sql, list(entities.keys()) + [pl_cut]).fetchall()
            ]
            motion_events = [x for x in motion_events_all if x[0] > ev_cut]
            cutoff = time.time() - HOST_GAP_LOOKBACK_H * 3600
            g = conn.execute(gap_sql, (cutoff, cutoff)).fetchone()
            if g and g[0] is not None:
                host_gap_min = round(float(g[0]) / 60.0, 1)
            if _pl is not None:
                try:  # an annotation's input: a failure is published, never fatal
                    gap_cache = scan_host_gaps(conn, gap_cache, time.time(),
                                               time.time() - _pl.lookback_s())
                except Exception as exc:  # noqa: BLE001
                    gap_error = "recorder gap scan failed: %s" % exc
            for wcam, cfg in WITNESS.items():
                kinds = list(cfg["on"]) + list(cfg["off"])
                args = kinds + [time.time() - WITNESS_LOOKBACK_D * 86400]
                where = ("FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id "
                         "%s WHERE m.entity_id IN (" + ",".join("?" * len(kinds)) +
                         ") AND s.last_updated_ts > ? ORDER BY s.last_updated_ts")
                try:   # event_type (a double-tap is the walk test) lives in the attributes
                    witness_rows[wcam] = conn.execute(
                        "SELECT m.entity_id, s.state, s.last_updated_ts, "
                        "json_extract(a.shared_attrs, '$.event_type') " + where % (
                            "LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id"),
                        args).fetchall()
                except Exception as exc:  # noqa: BLE001 - presses still list without it
                    witness_error[wcam] = "event types unreadable (%s)" % exc
                    try:
                        witness_rows[wcam] = conn.execute(
                            "SELECT m.entity_id, s.state, s.last_updated_ts " + where % "",
                            args).fetchall()
                    except Exception as exc2:  # noqa: BLE001
                        witness_rows.pop(wcam, None)
                        witness_error[wcam] = "switch rows unreadable (%s)" % exc2
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
    # CACHED SNAPSHOTS (see cam_vision.py). A camera that answers the webhook
    # with the same cached image is reachable but NOT seen: its "no visual
    # change" is the same old picture, not a quiet scene. fresh_ts is the last
    # genuinely new frame; a state file written before fresh_ts existed has none,
    # and that camera is judged as before rather than guessed at.
    cam_fresh_age = {}
    for cam in CAMS:
        fts = (vs.get(cam) or {}).get("fresh_ts") if vision_ok else None
        cam_fresh_age[cam] = round((now - float(fts)) / 60.0, 1) if fts else None
    vision_cached = sorted(
        c for c in CAMS
        if vision_ok and c not in vision_blind
        and cam_fresh_age[c] is not None and cam_fresh_age[c] > VISION_BLIND_MIN
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

    try:
        mstate = json.load(open(MOTION_STATE))
        if not isinstance(mstate, dict):
            mstate = {}
    except Exception:  # noqa: BLE001 - recall/background are annotations
        mstate = {}
    unusable_now = set(vision_blind) | set(vision_cached) | (set() if vision_ok else set(CAMS))
    usable_since = mstate.get("vision_usable_since") if isinstance(mstate.get("vision_usable_since"), dict) else {}
    usable_since = {c: v for c, v in usable_since.items() if c in CAMS and isinstance(v, (int, float))}
    for cam in unusable_now:
        usable_since[cam] = now        # bumped every poll while it cannot be seen
    try:
        recall_state = update_recall(
            mstate.get("vision_recall") if isinstance(mstate.get("vision_recall"), dict) else {},
            motion_events, raw_log if vision_ok else {}, now,
            now - VISION_KEEP_H * 3600, unusable_now, usable_since, last)
    except Exception:  # noqa: BLE001
        recall_state = {}

    def recall_of(cam):
        try:
            ent = recall_state.get(cam) or []
            return (sum(int(h) for _, h in ent), len(ent)) if ent else (0, 0)
        except Exception:  # noqa: BLE001
            return (0, 0)

    def recall_deaf_safe(cam):
        # An annotation: a malformed record withholds nothing and raises nothing.
        try:
            return recall_deaf(recall_state.get(cam) or [])
        except Exception:  # noqa: BLE001
            return None

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
        elif cam in vision_cached:
            # Same false all-clear as above, by another route: the frames were
            # "successful" but every one was the webhook's cached image.
            verdicts[cam] = ("visual monitor cannot judge - this camera's snapshots "
                             "have been the same CACHED image for %.1fh (it is not "
                             "answering snapshot requests) - no verdict"
                             % (cam_fresh_age[cam] / 60.0))
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
            hits, n = recall_of(cam)
            if n >= RECALL_MIN_CLUSTERS and hits < RECALL_FLOOR * n:
                # The detector would not have seen activity here even when the
                # camera was working, so its silence says nothing about the scene.
                verdicts[cam] = ("no visual change in the last %.0fh, but that is NOT "
                                 "evidence of a quiet area: when this camera was firing, "
                                 "frame differencing caught only %d of its last %d motion "
                                 "clusters (%.0f%%) - it cannot see activity in this view "
                                 "reliably - no verdict"
                                 % (VISION_KEEP_H, hits, n, 100.0 * hits / n))
            elif n >= RECALL_MIN_CLUSTERS and recall_deaf_safe(cam):
                # The record clears the floor only on hits from BEFORE a run of
                # misses (see RECALL RECENCY): its silence says nothing about the
                # scene. Two wordings - only the run test has a P to state.
                run, kb, nb, p_run = recall_recency(recall_state.get(cam) or [])
                if recall_deaf_safe(cam) == "run":
                    verdicts[cam] = ("no visual change in the last %.0fh, but frame differencing "
                                     "missed this camera's last %d motion clusters in a row (it "
                                     "caught %d of the %d before them; P <= %.2g if it still saw "
                                     "this view) - its silence cannot be read as a quiet area - "
                                     "no verdict" % (VISION_KEEP_H, run, kb, nb, p_run))
                else:
                    verdicts[cam] = ("no visual change in the last %.0fh, but frame differencing "
                                     "missed this camera's last %d motion clusters in a row - too "
                                     "many to read its silence as a quiet area - no verdict"
                                     % (VISION_KEEP_H, run))
            elif n >= RECALL_MIN_CLUSTERS:
                verdicts[cam] = ("no visual change in the last %.0fh (vision window; "
                                 "shorter than the stale span) - consistent with a "
                                 "quiet area (frame differencing caught %d of this "
                                 "camera's last %d motion clusters)"
                                 % (VISION_KEEP_H, hits, n))
            else:
                # Below RECALL_MIN_CLUSTERS the detector's reach is unknown, so its
                # silence supports no reading of the scene either way.
                verdicts[cam] = ("no visual change in the last %.0fh, but how much of this "
                                 "view's activity frame differencing can see is not yet "
                                 "measured (%d of %d motion clusters needed) - no verdict"
                                 % (VISION_KEEP_H, n, RECALL_MIN_CLUSTERS))

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

    # PARTIAL LOSS. Every non-stale camera, every poll. partial_loss is None
    # (never []) and partial_detail carries the error whenever the test did not
    # run, so "nothing flagged" is never published by a failure.
    partial_loss, partial_detail = None, None
    prev_partial = mstate.get("partial") if isinstance(mstate.get("partial"), dict) else {}
    partial_state = prev_partial
    partial_cleared = {}
    try:
        stored = mstate.get("partial_cleared")
        for c, v in (stored.items() if isinstance(stored, dict) else []):
            try:
                if (c in CAMS and isinstance(v, dict)
                        and now - float(v["at"]) <= PARTIAL_CLEARED_KEEP_S):
                    partial_cleared[c] = v
            except Exception:  # noqa: BLE001 - one bad record never costs another
                continue
        if _pl is None:
            raise RuntimeError("partial_loss module not loaded (%s)" % _PL_IMPORT_ERROR)
        host_gaps = (gap_cache or {}).get("gaps") if isinstance(gap_cache, dict) else None
        pres, partial_state = _pl.evaluate(
            motion_events_all, now, prev_partial, cams=CAMS, stale_now=set(stale),
            gaps=host_gaps or [], shared=partial_shared())
        for e in pres["events"]:
            if e["kind"] == "exit":
                partial_cleared[e["cam"]] = dict(
                    {k: v for k, v in e.items() if k not in ("cam", "kind")}, at=int(now))
            elif e["kind"] == "enter":
                partial_cleared.pop(e["cam"], None)
        flag_since = {c: (pres["detail"].get(c) or {}).get("since") for c in pres["flagged"]}
        for c, d in pres["detail"].items():   # a dormant flag keeps its ack
            if c not in flag_since and (d or {}).get("dormant_since") is not None:
                flag_since[c] = d["dormant_since"]
        acked, ack_unmatched = partial_ack(flag_since)
        cams_detail = {}
        for c in CAMS:
            d = {k: v for k, v in (pres["detail"].get(c) or {}).items() if not k.startswith("_")}
            if c in partial_cleared:
                d["cleared"] = partial_cleared[c]
            if c in acked:
                d["ack"] = acked[c]
            cams_detail[c] = d
        partial_loss = sorted(set(pres["flagged"]) - set(stale) - set(acked))
        partial_detail = {
            "error": None,
            "cannot_convict": sorted(c for c, d in cams_detail.items()
                                     if d.get("status") in ("underpowered", "untestable")),
            "acknowledged": sorted(acked),
            "cams": cams_detail,
            "scope": PARTIAL_SCOPE,
            "gaps": len(host_gaps or []),
        }
        if ack_unmatched:
            partial_detail["ack_unmatched"] = ack_unmatched
        if gap_error or host_gaps is None:
            # The test still runs - a recorder hole is then counted as observed
            # time, which understates a camera's own rate - but it says so.
            partial_detail["gap_error"] = gap_error or "no recorder gap list"
    except Exception as exc:  # noqa: BLE001 - an annotation must never fail the sensor
        partial_loss, partial_state = None, prev_partial
        partial_detail = {"error": "partial loss failed: %s" % exc}

    # ROOM SWITCH NOTES (annotation) and the passive visit log (calibration).
    witness_log = mstate.get("witness_log") if isinstance(mstate.get("witness_log"), dict) else {}
    witness_failed = []
    on_by_cam = {}
    for t, c in motion_events_all:
        on_by_cam.setdefault(c, []).append(t)
    data_from = (now - _pl.lookback_s()) if _pl is not None else ev_cut
    for wcam, cfg in WITNESS.items():
        try:
            kinds = {e: "on" for e in cfg["on"]}
            kinds.update({e: "off" for e in cfg["off"]})
            wrows = witness_rows.get(wcam)
            if wrows is None:
                raise RuntimeError(witness_error.get(wcam) or "switch rows not read")
            presses = witness_presses(wrows, kinds)
            visits = witness_visits(presses)
            witness_log[wcam] = update_witness_log(
                witness_log.get(wcam), visits, on_by_cam.get(wcam, []), on_by_cam, cfg,
                (gap_cache or {}).get("gaps") or [] if isinstance(gap_cache, dict) else [],
                data_from, now)
            if wcam in stale and wcam in corroboration:
                note = witness_note(wcam, cfg, wrows, presses, last.get(wcam), on_by_cam,
                                    data_from, now)
                if witness_error.get(wcam):
                    note += " (%s)" % witness_error[wcam]
                corroboration[wcam]["verdict"] = "%s | %s" % (
                    corroboration[wcam].get("verdict"), note)
        except Exception as exc:  # noqa: BLE001 - an annotation must never fail the sensor
            witness_failed.append("%s: %s" % (wcam, exc))
            if wcam in stale and wcam in corroboration:
                corroboration[wcam]["verdict"] = (
                    "%s | ROOM SWITCH: presses could not be read (%s)"
                    % (corroboration[wcam].get("verdict"), exc))
    try:
        background = {"computed_at": int(now), "lookback_d": BACKGROUND_LOOKBACK_D,
                      "window_s": BACKGROUND_WINDOW_S, "hour_basis": "utc",
                      "p": background_rates(motion_events, now)}
    except Exception:  # noqa: BLE001 - cam_flap falls back to counting every miss
        background = None
    # Written atomically: cam_flap.py reads this file too, and a torn write would
    # read as "no background" (safe) but also cost the proof latches (not safe).
    tmp = "%s.tmp.%d" % (MOTION_STATE, os.getpid())
    try:
        with open(tmp, "w") as fh:
            json.dump({"proofs": new_latches, "background": background,
                       "vision_recall": recall_state,
                       "vision_usable_since": usable_since,
                       "partial": partial_state,
                       "partial_cleared": partial_cleared,
                       "host_gaps": gap_cache if isinstance(gap_cache, dict) else None,
                       "witness_log": witness_log}, fh)
        os.replace(tmp, MOTION_STATE)
    except Exception:  # noqa: BLE001 - losing the latch must not fail the sensor
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
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
    if witness_failed:
        summary += " | WITNESS LOG FAILED: " + "; ".join(witness_failed)
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
        "vision_blind": sorted(set(vision_blind) | set(vision_cached)),
        "summary": summary,
        "partial_loss": partial_loss,
        "partial_detail": partial_detail,
        "updated_at": int(time.time() // HEARTBEAT_BUCKET_S) * HEARTBEAT_BUCKET_S,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - the sensor contract is JSON-always
        fail("unhandled: %s" % exc)
