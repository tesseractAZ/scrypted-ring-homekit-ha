"""PARTIAL-LOSS detector - one pure function, called by cam_motion.py every poll.

THE GAP IT CLOSES. Motion staleness and corroborate() judge only a camera that
has gone completely SILENT. A camera that still fires now and then resets its
staleness clock with every stray event and is never tested, however much it is
missing. Measured 2026-09-29..10-02: one camera dropped to 2 co-fires in 74 of
its best partner's clusters, against 222/690 (32 %) before, while still firing
a few times a day - and no monitor said anything. That catch rested on a
favourable reference (33.7 %, lb 0.30, over 09-15..09-28): the same camera
losing everything from 09-01 or 09-14 is never flagged before it goes stale
(best p x m 4e-6, at lb 0.23).

THE STATISTIC. Cluster every camera's motion ON rows with corroborate()'s
linkage. For a target T and a partner P:
    baseline  n_B = P's clusters in the baseline window, k_B = those also
              containing T; lb = Wilson 95 % lower bound of k_B / n_B
    recent    n_R = P's clusters in the last RECENT_H hours, k_R = those with T
    tail      = P(X <= k_R),  X ~ Binomial(n_R, lb)
The rate is conditional on P firing, so a quiet week does not move it; only T
dropping out of scenes P still sees does.

A poll HITS for T when some TESTED partner has tail * m < ALPHA (m = eligible
partners, Bonferroni), k_R/n_R <= EFFECT * lb and, when the last NEAR_D days of
the baseline hold >= MIN_NEAR partner clusters, k_R/n_R <= EFFECT_NEAR x that
rate; T's own cluster rate (recent / baseline, per usable hour) is <= OWN_DROP;
and no other tested partner VETOES. Veto (likelihood form): a partner with
n_R >= VETO_MIN_N whose co-fires would be improbable if T really were down to
EFFECT * lb - P(X >= k_R | n_R, EFFECT*lb) < VETO_ALPHA - contradicts the
conviction however few clusters it has. The earlier power-floor veto
(n_R * lb >= 5) let a partner firing on junk convict healthy cameras in the
low-traffic south group: Poisson junk injected into the real history at
1.0-2.5x a partner's cluster rate latched a flag in 21 of 288 runs, every one
below the old 3x chatty bar (e.g. a 2-partner camera on 5/21 vs 101/106 while
its other partner saw it 5/5 - 5 x 0.89 = 4.45 < 5, so no veto). The
likelihood veto plus CHATTY_X = 2 took that to 0 of 288 with the replay and
the injection grid unchanged.

ELIGIBLE: n_B >= MIN_PRE, k_B >= MIN_HITS, lb >= MIN_RATE (corroborate()'s
floors, PASSED IN by the caller - never restated here). TESTED: also
n_R >= MIN_RECENT, P's recent cluster rate <= CHATTY_X x its baseline rate,
and P not back from its OWN stale silence within the last PARTNER_RETURN_H: a
partner that has just come back fires in a regime of its own (measured: one
camera was flagged 17 h after its best partner returned from a 116 h outage,
on that partner's 1/11, and cleared 2 days later).

POWER IS PUBLISHED, NOT IMPLIED. "testable" means even a TOTAL loss could
reach ALPHA this poll: p0 = min over tested partners of (1-lb)^n_R * m <
ALPHA. Otherwise the camera is "underpowered" (tested, but not even zero
co-fires could convict it) or "untestable" (no tested partner). min_loss is
the smallest loss of the camera's usual co-fires that the most sensitive
partner would convict at its EXPECTED count. Measured by removing whole visit
clusters from the real history (6 cameras x 4 healthy onsets, 6-day horizon):
caught 4/6/14/17/18 of 24 at 50/67/80/90/100 % loss, a night-only loss 1 of 24,
a day-only loss 8 of 24. Losses below ~75 % and losses limited to night or day
are mostly missed (see those figures), slow declines are not detected, and a
total loss is usually paged by staleness first.

CLEARING IS EVIDENCE-BASED. While a camera is flagged, the flagged span (from
the start of the recent window that convicted it) is excluded from its
baseline, so the reference stays what it was at entry. Only the OPEN flag is
excluded: replayed over the recorder, keeping closed spans out as well pulled
a camera's later baselines back into the pre-08-06 activity regime (its only
data left) and raised a new 23-day flag in a healthy period. The flag clears
ONLY when the entry partner (judged against its own frozen entry bound;
another tested partner against ITS own bound when the entry partner cannot
re-test) shows k_R/n_R >= RECOVERED_RATIO x lb AND that many co-fires would be
improbable if the loss were still there: P(X >= k_R | n_R, EFFECT*lb) <
ALPHA_EXIT. That evidence must hold, with no hit, for EXIT_SPAN_S. There is no
time-based exit: the earlier design cleared a persistent loss as "absorbed"
after ~2-3 weeks once the loss had become its own baseline (a continued 90 %
loss of the camera above would have cleared ~10-18 and been invisible to every
monitor after). A camera that stays low stays flagged; the caller's
acknowledge list is the only way to silence it. A camera that goes STALE
leaves the set at once (reason "stale" - staleness and its proof outrank this)
but its flag stays DORMANT: the flagged span stays out of its baseline, so on
return it is judged on clusters after its silence against the reference it was
convicted on. A fresh hit span re-enters with the SAME since (reason
"resumed": no second page, an ack still matches); recovery evidence held for
EXIT_SPAN_S forgets the reference. Holding the flag itself through the silence
would re-flag every camera back from a total outage until a partner could
re-test it (~40-46 h on both real outages).

PERSISTENCE IS TIME, NOT POLLS. Enter when hits have run continuously for
ENTER_SPAN_S; exit when recovery evidence has for EXIT_SPAN_S. Every HA
restart, reload and forced update_entity adds a poll, so "2 polls" could be
two polls seconds apart over identical data. POLL_JITTER_S absorbs the jitter
of the 30-min schedule's own clock readings.

EVIDENCE TIMES ARE PUBLISHED, NOT INTERPRETED. For a camera that is flagged or
in a hit run - and only then, so the payload stays bounded - the detail carries
own_recent (the start of each of the camera's own motion clusters in the recent
window) and cofire_recent (the camera's own first detection in each recent
cluster of cofire_partner that contained it; clusters counted by their start,
as k_R is), epoch ints, newest last, at most EVIDENCE_MAX each, with the full
counts in own_recent_n / cofire_recent_n. cofire_partner is the best tested
partner - the one whose recent/baseline counts the detail reports - else (no
partner tested) the one judging recovery, else the entry partner. Measured
2026-10-03: a camera convicted at ~90 % loss still fired only on close
approaches (a mail carrier, door openings), all 13:00-17:00 local, so a
doorstep walk test or a door trip passed under the loss. The times show the
reader when the camera still works; no cause is inferred from them - a time
window and a zone or range restriction fitted that case equally.
len(cofire_recent) before the cap is that partner's k_R by construction, and
every co-fire time is also one of own_recent's: the cluster START can be
another camera's event minutes earlier (up to 492 s, a different local hour on
96 of 4,219 entries in a 30-min replay of the real history), and a camera event
within LINK_S before that start would have chained into the cluster, so the
camera's first event in it also starts one of its own clusters.

Pure: no I/O, no clock, no globals mutated. Deterministic for given inputs.
"""
import bisect
import math

RECENT_H = 48.0
BASE_D = 27.0
MAX_LOOKBACK_D = 60.0
MIN_RECENT = 5            # partner clusters in the recent window
CHATTY_X = 2.0            # partner recent clusters/h vs baseline clusters/h
ALPHA = 1e-6              # per camera-poll, after x m
EFFECT = 0.33             # recent rate <= this fraction of lb
VETO_MIN_N = 3            # a vetoing partner needs this many recent clusters...
VETO_ALPHA = 0.01         # ...and P(its co-fires | T at EFFECT*lb) below this
ENTER_SPAN_S = 1800.0     # hits must have run this long
EXIT_SPAN_S = 6 * 3600.0  # recovery evidence must have held this long
POLL_JITTER_S = 120.0     # tolerance on both spans for the poll clock
ALPHA_EXIT = 1e-2         # recovery: P(co-fires | loss still there) below this
NEAR_D = 7.0              # the last week of the baseline...
MIN_NEAR = 10             # ...if it holds this many partner clusters...
EFFECT_NEAR = 0.5         # ...must also show recent <= this x its rate
OWN_DROP = 0.67           # T's own cluster rate, recent / baseline, must be <= this
RECOVERED_RATIO = 0.5     # recovery: recent rate >= this x the reference lb
PARTNER_RETURN_H = 48.0   # a partner back from its own stale silence sits out this long

PARAMS = dict(RECENT_H=RECENT_H, BASE_D=BASE_D, MAX_LOOKBACK_D=MAX_LOOKBACK_D,
              MIN_RECENT=MIN_RECENT, CHATTY_X=CHATTY_X, ALPHA=ALPHA, EFFECT=EFFECT,
              VETO_MIN_N=VETO_MIN_N, VETO_ALPHA=VETO_ALPHA,
              ENTER_SPAN_S=ENTER_SPAN_S, EXIT_SPAN_S=EXIT_SPAN_S,
              POLL_JITTER_S=POLL_JITTER_S, ALPHA_EXIT=ALPHA_EXIT,
              RECOVERED_RATIO=RECOVERED_RATIO, OWN_DROP=OWN_DROP,
              NEAR_D=NEAR_D, MIN_NEAR=MIN_NEAR, EFFECT_NEAR=EFFECT_NEAR,
              PARTNER_RETURN_H=PARTNER_RETURN_H)

# Evidence times per list (own_recent, cofire_recent): 24 + 24 epoch ints are
# ~0.6 KB per camera, published only for a flagged or hit-run camera.
EVIDENCE_MAX = 24

# Supplied by the caller (cam_motion.partial_shared()) so this module cannot
# drift from the co-firing test it reuses: the cluster linkage, the three
# eligibility floors, the Wilson bound and every camera's staleness window.
SHARED_KEYS = ("LINK_S", "MIN_PRE", "MIN_HITS", "MIN_RATE", "STALE_HOURS", "wilson")


def lookback_s(p=None):
    """How far back the caller must fetch motion rows (and recorder gaps)."""
    P = dict(PARAMS)
    P.update(p or {})
    return float(P["RECENT_H"]) * 3600.0 + float(P["MAX_LOOKBACK_D"]) * 86400.0 + 3600.0


def binom_cdf(k, n, p):
    """P(X <= k), X ~ Binomial(n, p). Exact, summed in log space."""
    if k < 0:
        return 0.0
    if k >= n or p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    lp, lq, lg = math.log(p), math.log1p(-p), math.lgamma
    terms = [lg(n + 1) - lg(i + 1) - lg(n - i + 1) + i * lp + (n - i) * lq
             for i in range(k + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def binom_sf(k, n, p):
    """P(X >= k), X ~ Binomial(n, p). Exact, summed in log space (the small
    upper tail is never computed as 1 - cdf, which loses it to rounding)."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lp, lq, lg = math.log(p), math.log1p(-p), math.lgamma
    terms = [lg(n + 1) - lg(i + 1) - lg(n - i + 1) + i * lp + (n - i) * lq
             for i in range(k, n + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def _max_convictable_k(n, lb, m, alpha, cap_rate):
    """Largest k whose lower tail * m < alpha with k/n <= cap_rate; None if none."""
    if n <= 0 or lb <= 0.0 or lb >= 1.0:
        return None
    lq = math.log1p(-lb)
    ratio = lb / (1.0 - lb)
    logpmf = n * lq                       # P(X = 0)
    cdf, best = 0.0, None
    for k in range(0, n + 1):
        if k > 0:
            logpmf += math.log((n - k + 1) / float(k) * ratio)
        cdf += math.exp(logpmf)
        if cdf * m >= alpha or k / float(n) > cap_rate:
            break
        best = k
    return best


def _clusters(events, link_s):
    """sorted [(ts, cam)] -> [[start, end, set(cams)]] chained within link_s."""
    out = []
    for t, c in events:
        if out and t - out[-1][1] <= link_s:
            out[-1][1] = t
            out[-1][2].add(c)
        else:
            out.append([t, t, {c}])
    return out


def _merge(spans):
    out = []
    for a, b in sorted([float(x), float(y)] for x, y in spans if y > x):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _measure(spans, a, b):
    """Length of [a, b] covered by merged spans."""
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in spans)


def _inside(spans, t):
    return any(x < t < y for x, y in spans)


def _back(excl, limit, end, need):
    """The start s >= limit such that [s, end] holds `need` s of time outside
    excl, or `limit` when there is not that much."""
    if (end - limit) - _measure(excl, limit, end) <= need:
        return limit
    lo, hi = limit, end
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if (end - mid) - _measure(excl, mid, end) > need:
            lo = mid
        else:
            hi = mid
    return hi


def _evidence(cam, own_ts, cl, start, link, partner):
    """cam's own recent detections (the start of each of its own clusters from
    `start` on) and its recent co-fires with `partner` (cam's own first detection
    in each settled joint cluster that STARTS from `start` on, as k_R counts
    them), epoch ints, newest last, at most EVIDENCE_MAX each. Own clusters may
    still be open (a detection is a fact once it happened); the co-fires come
    from the settled clusters the test itself counts. own_ts is in time order."""
    own = [int(a) for a, _, _ in _clusters([(t, cam) for t in own_ts if t >= start], link)]
    # Listed at cam's OWN event, not the cluster start (often the partner's,
    # minutes earlier): `cam in s` puts one of own_ts in [a, b].
    co = [int(own_ts[bisect.bisect_left(own_ts, a)]) for a, _, s in cl
          if a >= start and cam in s and partner in s]
    return {"own_recent": own[-EVIDENCE_MAX:], "own_recent_n": len(own),
            "cofire_partner": partner, "cofire_recent": co[-EVIDENCE_MAX:],
            "cofire_recent_n": len(co)}


def _fmt_p(x):
    return float("%.2g" % x)


def _num(v):
    """A finite real number, or None (bools, NaN, strings and junk are None)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v \
            or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def _clean_state(st):
    """One camera's stored state, every field validated on its own."""
    st = st if isinstance(st, dict) else {}
    out = {k: _num(st.get(k)) for k in ("hit_from", "since", "ok_from", "held_from")}
    entry = st.get("entry")
    if not (isinstance(entry, dict) and isinstance(entry.get("partner"), str)
            and _num(entry.get("lb")) is not None and 0.0 < _num(entry.get("lb")) < 1.0):
        entry = None
    if out["since"] is None or entry is None:
        # A flag is only meaningful with the reference it was convicted
        # against; without one there is nothing to judge a recovery by.
        out["since"], entry, out["ok_from"], out["held_from"] = None, None, None, None
    out["entry"] = entry
    out["dormant"] = st.get("dormant") is True and out["since"] is not None
    return out


def _check_shared(shared):
    if not isinstance(shared, dict) or any(k not in shared for k in SHARED_KEYS):
        raise ValueError("shared constants missing: %s" % ", ".join(
            k for k in SHARED_KEYS if not isinstance(shared, dict) or k not in shared))
    return shared


def _windows(shared, cams):
    win = shared["STALE_HOURS"]
    missing = [c for c in cams if not isinstance(win, dict) or _num(win.get(c)) is None]
    if missing:
        raise ValueError("no staleness window for %s" % ", ".join(missing))
    return {c: float(win[c]) * 3600.0 for c in cams}


def evaluate(events, now, state=None, cams=None, stale_now=None, gaps=(), shared=None,
             p=None):
    """One poll of the partial-loss detector.

    events     [(ts, cam)] motion ON rows covering at least lookback_s() before
               now (extra rows are ignored)
    now        poll time (epoch s)
    state      the dict this function returned on the previous poll, or None
    cams       camera stems to judge (default: every camera seen in events)
    stale_now  the cameras the caller's staleness rule calls stale right now;
               if None it is derived here from the same windows
    gaps       [(a, b)] recorder holes (no rows for any entity): unobserved
    shared     REQUIRED: {LINK_S, MIN_PRE, MIN_HITS, MIN_RATE, STALE_HOURS
               {cam: h}, wilson} from cam_motion (see SHARED_KEYS)
    p          parameter overrides (calibration only)

    Returns (result, new_state). result = {"flagged": [...], "detail": {...},
    "events": [...]}: "flagged" is the latched set (never contains a stale
    camera); "events" lists this poll's transitions as {"cam", "kind":
    "enter"|"exit", "reason", ...}; a flagged or hit-run camera's detail also
    carries the evidence times (see EVIDENCE TIMES). Raises ValueError on
    missing shared input.
    """
    P = dict(PARAMS)
    P.update(p or {})
    state = state if isinstance(state, dict) else {}
    shared = _check_shared(shared)
    R = float(P["RECENT_H"]) * 3600.0
    horizon = now - R - float(P["MAX_LOOKBACK_D"]) * 86400.0
    link = float(shared["LINK_S"])
    wilson = shared["wilson"]
    ev = sorted((float(t), c) for t, c in events if horizon - link <= float(t) <= now)
    cams = list(cams) if cams else sorted({c for _, c in ev})
    windows = _windows(shared, cams)
    cl = [x for x in _clusters(ev, link) if x[1] <= now - link]   # settled only
    gap_spans = _merge([[max(float(a), horizon), min(float(b), now)]
                        for a, b in gaps if float(b) > horizon and float(a) < now])
    by_cam = {}
    for t, c in ev:
        by_cam.setdefault(c, []).append(t)

    # Every camera's stale silences (last event -> next, longer than its window
    # once unobserved time is taken out), and when each last came BACK from one.
    silences, returning = {}, set()
    for c in cams:
        sil, prev = [], horizon
        for t in by_cam.get(c, []) + [now]:
            if (t - prev) - _measure(gap_spans, prev, t) > windows[c]:
                sil.append([prev, t])
            prev = t
        silences[c] = sil
        ends = [b for _, b in sil if b < now]
        if ends and now - ends[-1] < float(P["PARTNER_RETURN_H"]) * 3600.0:
            returning.add(c)

    detail, new_state, flagged, transitions = {}, {}, [], []
    for T in cams:
        st = _clean_state(state.get(T))
        since, entry = st["since"], st["entry"]
        hit_from, ok_from, held_from = st["hit_from"], st["ok_from"], st["held_from"]
        sil = silences[T]
        is_stale = (T in stale_now) if stale_now is not None else (
            bool(sil) and sil[-1][1] == now)
        if is_stale or T not in by_cam:
            if since is not None and not st["dormant"]:
                transitions.append({"cam": T, "kind": "exit", "reason": "stale"})
            detail[T] = {"status": "stale" if T in by_cam else "no events in lookback"}
            if since is not None:
                detail[T]["dormant_since"] = int(since)
            # The flag leaves (staleness outranks it) but its reference stays,
            # DORMANT: the flagged span stays out of the baseline, so a loss still
            # there on return is judged against the camera before it.
            new_state[T] = {"hit_from": None, "since": since, "entry": entry, "ok_from": None,
                            "held_from": None, "dormant": since is not None}
            continue
        dormant = st["dormant"]
        excl = _merge(sil + gap_spans)                       # both windows
        # The loss itself is never the reference: while flagged, everything from
        # the start of the window that convicted it is taken out of the BASELINE
        # (not out of the recent window, which judges it).
        flag_spans = [[since - R, now]] if since is not None else []
        excl_b = _merge(excl + flag_spans)
        b_end = now - R
        b_start = _back(excl_b, horizon, b_end, float(P["BASE_D"]) * 86400.0)
        n_start = _back(excl_b, b_start, b_end, float(P["NEAR_D"]) * 86400.0)
        base_h = max(1e-9, (b_end - b_start - _measure(excl_b, b_start, b_end)) / 3600.0)
        rec_h = max(1e-9, (R - _measure(excl, now - R, now)) / 3600.0)
        nB, kB, nR, kR, nN, kN = {}, {}, {}, {}, {}, {}
        own_b = own_r = 0
        for a, b, s in cl:
            if a < b_start:
                continue
            recent = a >= now - R
            if not recent and _inside(flag_spans, a):
                continue
            if T not in s and _inside(excl, a):
                continue
            if recent:
                n, k = nR, kR
                own_r += T in s
            else:
                n, k = nB, kB
                own_b += T in s
            for q in s:
                if q != T:
                    n[q] = n.get(q, 0) + 1
                    if T in s:
                        k[q] = k.get(q, 0) + 1
                    if not recent and n_start <= a < b_end:
                        nN[q] = nN.get(q, 0) + 1
                        if T in s:
                            kN[q] = kN.get(q, 0) + 1
        eligible, tests, best_any, back_now = [], [], None, []
        for q in cams:
            if q == T:
                continue
            n_b, k_b = nB.get(q, 0), kB.get(q, 0)
            n_r, k_r = nR.get(q, 0), kR.get(q, 0)
            lb = wilson(k_b, n_b)
            if best_any is None or lb > best_any[1]:
                best_any = (q, lb)
            if n_b < shared["MIN_PRE"] or k_b < shared["MIN_HITS"] or lb < shared["MIN_RATE"]:
                continue
            eligible.append(q)
            rate_h = n_b / base_h
            activity = (n_r / rec_h) / max(1e-9, rate_h)
            if q in returning:
                back_now.append(q)
                continue
            if n_r < P["MIN_RECENT"] or activity > P["CHATTY_X"]:
                continue
            n_n, k_n = nN.get(q, 0), kN.get(q, 0)
            near = (k_n / float(n_n)) if n_n >= P["MIN_NEAR"] else None
            tests.append({"partner": q, "k_b": k_b, "n_b": n_b, "lb": lb,
                          "k_r": k_r, "n_r": n_r, "near": near, "n_n": n_n,
                          "tail": binom_cdf(k_r, n_r, lb),
                          "ratio": (k_r / float(n_r)) / lb,
                          "activity": activity, "rate_h": rate_h})
        m = len(eligible)
        own_ratio = (own_r / rec_h) / max(1e-9, own_b / base_h) if own_b else None
        own_ok = own_ratio is None or own_ratio <= P["OWN_DROP"]
        passing = [x for x in tests
                   if x["tail"] * m < P["ALPHA"] and x["ratio"] <= P["EFFECT"]
                   and (x["near"] is None
                        or x["k_r"] / float(x["n_r"]) <= P["EFFECT_NEAR"] * x["near"])
                   ] if own_ok else []
        veto = [x for x in tests if passing and x not in passing
                and x["n_r"] >= P["VETO_MIN_N"]
                and binom_sf(x["k_r"], x["n_r"], P["EFFECT"] * x["lb"]) < P["VETO_ALPHA"]]
        hit = bool(passing) and not veto
        tests.sort(key=lambda x: x["tail"])
        best = tests[0] if tests else None
        p0 = min((1.0 - x["lb"]) ** x["n_r"] * m for x in tests) if tests else None
        min_loss = None
        for x in tests:
            cap = P["EFFECT"] * x["lb"]
            if x["near"] is not None:
                cap = min(cap, P["EFFECT_NEAR"] * x["near"])
            kmax = _max_convictable_k(x["n_r"], x["lb"], m, P["ALPHA"], cap)
            if kmax is not None and x["k_b"] > 0:
                loss = max(0.0, 1.0 - kmax / (x["k_b"] / float(x["n_b"]) * x["n_r"]))
                min_loss = loss if min_loss is None else min(min_loss, loss)

        exit_info = None
        if since is None:
            if hit:
                hit_from = now if hit_from is None else hit_from
                if now - hit_from >= P["ENTER_SPAN_S"] - P["POLL_JITTER_S"]:
                    b0 = min(passing, key=lambda x: x["tail"])     # smallest tail
                    since, hit_from, ok_from, held_from = now, None, None, None
                    entry = {"partner": b0["partner"], "lb": round(b0["lb"], 4),
                             "base": [b0["k_b"], b0["n_b"]],
                             "recent": [b0["k_r"], b0["n_r"]],
                             "p": _fmt_p(b0["tail"] * m),
                             "rate_h": round(b0["rate_h"], 4)}
                    transitions.append({"cam": T, "kind": "enter", "reason": "hit",
                                        "partner": b0["partner"]})
            else:
                hit_from = None
        else:
            # Who may judge a recovery: the entry partner against its FROZEN entry
            # bound when it can re-test this poll; otherwise each other tested
            # partner against its OWN bound.
            ep, lb_e, rh_e = entry["partner"], float(entry["lb"]), _num(entry.get("rate_h"))
            n_re, k_re = nR.get(ep, 0), kR.get(ep, 0)
            chatty_e = rh_e is not None and rh_e > 0 and (n_re / rec_h) > P["CHATTY_X"] * rh_e
            if n_re >= P["MIN_RECENT"] and not chatty_e and ep not in returning:
                judges = [(ep, k_re, n_re, lb_e)]
            else:
                judges = [(x["partner"], x["k_r"], x["n_r"], x["lb"]) for x in tests
                          if x["partner"] != ep]
            recov = [j for j in judges
                     if j[1] / float(j[2]) >= P["RECOVERED_RATIO"] * j[3]
                     and binom_sf(j[1], j[2], P["EFFECT"] * j[3]) < P["ALPHA_EXIT"]]
            if hit or not recov:
                ok_from = None
            else:
                ok_from = now if ok_from is None else ok_from
            held_from = None if judges else (now if held_from is None else held_from)
            if judges:
                j = recov[0] if recov else judges[0]
                exit_info = {"partner": j[0], "recent": "%d/%d" % (j[1], j[2]),
                             "lb": round(j[3], 3)}
            if dormant:
                # Back from a stale spell: not flagged. A fresh hit span re-enters
                # with the SAME since (no second page, an ack still matches);
                # recovery evidence forgets the reference.
                held_from = None
                hit_from = (now if hit_from is None else hit_from) if hit else None
                if hit_from is not None and now - hit_from >= P["ENTER_SPAN_S"] - P["POLL_JITTER_S"]:
                    dormant, hit_from, ok_from = False, None, None
                    transitions.append({"cam": T, "kind": "enter", "reason": "resumed",
                                        "partner": entry["partner"]})
            if ok_from is not None and now - ok_from >= P["EXIT_SPAN_S"] - P["POLL_JITTER_S"]:
                j = recov[0]
                if not dormant:
                    transitions.append({"cam": T, "kind": "exit", "reason": "recovered",
                                        "partner": j[0], "recent": "%d/%d" % (j[1], j[2]),
                                        "lb": round(j[3], 3)})
                since, entry, ok_from, held_from, hit_from = None, None, None, None, None
                dormant = False
        new_state[T] = {"hit_from": hit_from, "since": since, "entry": entry,
                        "ok_from": ok_from, "held_from": held_from, "dormant": dormant}
        if since is not None and not dormant:
            flagged.append(T)
            status = "flagged"
        elif not tests:
            status = "untestable"
        else:
            status = "testable" if p0 < P["ALPHA"] else "underpowered"
        d = {"status": status, "partners": m,
             "own_ratio": round(own_ratio, 2) if own_ratio is not None else None}
        if best:
            d.update({"partner": best["partner"],
                      "recent": "%d/%d" % (best["k_r"], best["n_r"]),
                      "baseline": "%d/%d" % (best["k_b"], best["n_b"]),
                      "lb": round(best["lb"], 3), "p": _fmt_p(min(1.0, best["tail"] * m)),
                      "ratio": round(best["ratio"], 2),
                      "p0": _fmt_p(min(1.0, p0)),
                      "min_loss": round(min_loss, 2) if min_loss is not None else None})
        elif best_any:
            d.update({"best_rate_lb": round(best_any[1], 3), "partner": best_any[0]})
        if back_now:
            d["returning"] = sorted(back_now)
        if veto:
            d["veto"] = veto[0]["partner"]
        if since is not None and dormant:
            d["dormant_since"] = int(since)
            if hit_from is not None:
                d["hit_since"] = int(hit_from)
            if ok_from is not None:
                d["recovering_since"] = int(ok_from)
        elif since is not None:
            d["since"] = int(since)
            d["entry"] = {k: v for k, v in entry.items() if k != "rate_h"}
            if exit_info:
                d["exit_test"] = exit_info
            if ok_from is not None:
                d["recovering_since"] = int(ok_from)
            if held_from is not None:
                d["held_since"] = int(held_from)
        elif hit_from is not None:
            d["hit_since"] = int(hit_from)
        if (since is not None and not dormant) or hit_from is not None:
            # Flagged or in a hit run only (bounded payload). The partner is the
            # best tested one, whose recent/baseline counts this detail reports, so
            # the co-fire list always backs the count printed beside it (the
            # recovery judge differed on 572 of 1,212 real flagged polls); with
            # no partner tested, the judge, else (a held flag) the entry partner.
            co_p = (best["partner"] if best else exit_info["partner"] if exit_info
                    else (entry or {}).get("partner"))
            d.update(_evidence(T, by_cam.get(T, []), cl, now - R, link, co_p))
        d["_tests"] = tests            # full figures for calibration; strip before HA
        detail[T] = d
    return ({"flagged": sorted(flagged), "detail": detail, "events": transitions},
            new_state)
