#!/usr/bin/env python3
"""Camera-health watchdog probe (Ring -> Scrypted single engine). v3

Actively fetches each of the 9 Scrypted webhook snapshot URLs on :11080 and
reports honest per-camera liveness AND degradation.

Why NOT /api/camera_proxy: it returns HTTP 200 + a STALE cached JPEG when the
upstream camera is dead, so it can't detect failure. The :11080 webhook is
honest: valid->200 fresh JPEG, bad token->401, bad/dead device->error.

Detects (v3 hardening, after the <cam_3> wedge the old logic missed):
  - down   : hard failure (non-200 / tiny) for DOWN_AFTER consecutive probes.
  - frozen : BYTE-IDENTICAL JPEG for FREEZE_PROBES consecutive probes.
             HONEST NAMING (corrected): this is a STALE SNAPSHOT, not proof of a
             wedged camera. On a snapshot timeout the webhook serves Scrypted's
             CACHED last-good JPEG - HTTP 200, full size - so the probe scores it
             a success and only the identical hash betrays it. The tier therefore
             measures "the snapshot request is timing out and we are being served
             cache", which is inherently bursty and rotates across cameras (it
             falls hardest on the BUSIEST ones). Treat it as a snapshot-pipeline
             signal, not as a per-camera hardware verdict.
  - slow   : takePicture latency > SLOW_SECS for SLOW_AFTER consecutive probes
             (degraded / near-timeout). NOTE the probe fires all 9 cameras
             concurrently, so a healthy camera can transiently spike to 4-7s from
             contention; SLOW_SECS sits ABOVE that band so only a genuinely-stalled
             source (the wedge ran ~10s / near the 15s timeout) trips it. FREEZE is
             the primary wedge catcher; SLOW is a conservative near-death backstop.

Emits ONE JSON object to stdout (parsed by the command_line sensor).
"""
import json, hashlib, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HOST = "<HA_HOST_IP>:11080"
CAMS = [
    ("<cam_1>", "28", "<webhook_token>"),
    ("<cam_2>", "29", "<webhook_token>"),
    ("<cam_3>", "30", "<webhook_token>"),
    ("<cam_4>", "31", "<webhook_token>"),
    ("<cam_5>", "34", "<webhook_token>"),
    ("<cam_6>", "38", "<webhook_token>"),
    ("<cam_7>", "41", "<webhook_token>"),
    ("<cam_8>", "44", "<webhook_token>"),
    ("<cam_9>", "47", "<webhook_token>"),
]
STATE = "/config/.cam_health_state.json"
MIN_BYTES = 2000        # smaller => error placeholder, not a real frame
DOWN_AFTER = 2          # consecutive hard-fails before 'down'
FREEZE_PROBES = 5       # byte-identical JPEG for this many consecutive probes => frozen (~10 min)
SLOW_SECS = 9.0         # >this = degraded/near-timeout. Above the 4-7s concurrent-probe
                        # contention band so healthy cameras aren't false-flagged.
SLOW_AFTER = 2          # ...for this many consecutive probes (tolerate one-off spikes)
TIMEOUT = 15
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
FLEET_STALE_MIN = 5     # majority of cams identical-to-previous-probe => fleet-level wedge
FLEET_MISS_MIN = 5      # majority of cams failing THIS cycle => fleet-level outage,
                        # reported immediately instead of waiting out DOWN_AFTER


def probe(cam):
    name, cid, token = cam
    url = f"http://{HOST}/endpoint/@scrypted/webhook/public/{cid}/{token}/takePicture"
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=TIMEOUT) as r:
            body = r.read()
            return name, r.status, len(body), hashlib.sha256(body).hexdigest(), time.time() - t0
    except urllib.error.HTTPError as e:
        return name, e.code, 0, "", time.time() - t0
    except Exception:
        return name, 0, 0, "", time.time() - t0


def main():
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {}

    with ThreadPoolExecutor(max_workers=9) as ex:
        results = list(ex.map(probe, CAMS))

    down, frozen, slow, detail, healthy = [], [], [], {}, 0
    stale_now = 0   # cams whose frame is byte-identical to the PREVIOUS probe
    miss_now = 0    # cams whose probe failed THIS cycle, before DOWN_AFTER tolerance
    pending = 0     # first miss of the DOWN_AFTER tolerance: not alerted, NOT healthy
    for name, code, nbytes, h, latency in results:
        prev = st.get(name, {})
        if code == 200 and nbytes >= MIN_BYTES and h:
            # success: track the same-hash streak (freeze) + slow streak
            same = prev.get("same", 0) + 1 if h == prev.get("hash") else 0
            if same > 0:
                stale_now += 1
            slowc = prev.get("slowc", 0) + 1 if latency > SLOW_SECS else 0
            st[name] = {"hash": h, "same": same, "fails": 0, "slowc": slowc}
            if same >= FREEZE_PROBES:
                frozen.append(name)
                detail[name] = f"stale snapshot ({same + 1} identical frames - cached)"
            elif slowc >= SLOW_AFTER:
                slow.append(name)
                detail[name] = f"slow ({latency:.1f}s x{slowc})"
            else:
                healthy += 1
                detail[name] = "ok" if latency < 1 else f"ok ({int(latency * 1000)}ms)"
        else:
            # hard failure: consecutive-miss tolerance; keep prior hash/streak
            miss_now += 1
            fails = prev.get("fails", 0) + 1
            st[name] = {**prev, "fails": fails, "slowc": 0}
            reason = f"down({code or 'timeout'})" if code != 200 else f"tiny({nbytes}b)"
            if fails >= DOWN_AFTER:
                down.append(name)
                detail[name] = f"{reason} x{fails}"
            else:
                # First miss of the tolerance window. Previously counted healthy,
                # which published '9/9 OK' during a 9/9 probe blackout (6 recorder
                # samples read 'FLEET-MISS: 9/9 probes failed | 9/9 OK'). A missed
                # probe is pending, not healthy.
                pending += 1
                detail[name] = f"miss({code or 'timeout'})"

    try:
        json.dump(st, open(STATE, "w"))
    except Exception:
        pass

    n_down, n_frozen, n_slow = len(down), len(frozen), len(slow)
    all_down = n_down == len(CAMS)
    # Fleet-stale: EVERY camera responded AND returned a frame identical to its
    # previous probe. A healthy prebuffered camera returns a distinct frame every
    # probe, so even one all-identical cycle is extraordinary -> very fast, very
    # low-false-positive indicator of a fleet-wide snapshot/stream wedge.
    all_stale = stale_now == len(CAMS)
    fleet_stale = stale_now >= FLEET_STALE_MIN
    fleet_miss = miss_now >= FLEET_MISS_MIN
    if all_down:
        summary = "ALL 9 cameras OFFLINE (Ring/Scrypted outage?)"
    elif down or frozen or slow:
        parts = []
        if down:
            parts.append(f"{n_down} down: " + ", ".join(down))
        if frozen:
            parts.append(f"{n_frozen} stale-snapshot: " + ", ".join(frozen))
        if slow:
            parts.append(f"{n_slow} slow: " + ", ".join(slow))
        summary = " | ".join(parts)
    else:
        summary = "%d/%d OK" % (healthy, len(CAMS))
        if pending:
            summary += " | %d pending (missed this probe, within tolerance)" % pending
    if fleet_miss:
        summary = "FLEET-MISS: %d/%d probes failed this cycle | " % (miss_now, len(CAMS)) + summary
    if fleet_stale:
        summary = "FLEET-STALE: %d/%d frames identical to last probe (snapshot pipeline serving cache) | " % (stale_now, len(CAMS)) + summary

    print(json.dumps({
        "healthy": healthy, "down_count": n_down, "frozen_count": n_frozen, "slow_count": n_slow,
        "down": down, "frozen": frozen, "slow": slow, "all_down": all_down,
        "stale_count": stale_now, "all_stale": all_stale, "fleet_stale": fleet_stale,
        "miss_count": miss_now, "fleet_miss": fleet_miss, "pending": pending,
        "summary": summary, "detail": detail,
        "updated_at": int(time.time() // HEARTBEAT_BUCKET_S) * HEARTBEAT_BUCKET_S,
    }))


try:
    main()
except Exception as e:
    # The error path must emit the SAME key set as the success path. Omitting a
    # key does not make it null - json_attributes is an allowlist and a missing
    # key simply vanishes, so `state_attr(...)|int(0)` reads 0, i.e. "no fault",
    # which is the reassuring direction from a script that has just failed.
    print(json.dumps({"healthy": -1, "down_count": 0, "frozen_count": 0, "slow_count": 0,
                      "down": [], "frozen": [], "slow": [], "all_down": False,
                      "stale_count": 0, "all_stale": False, "fleet_stale": False,
                      "miss_count": 0, "fleet_miss": False, "pending": 0,
                      "summary": f"probe script error: {e}", "detail": {},
                      "updated_at": int(time.time() // HEARTBEAT_BUCKET_S) * HEARTBEAT_BUCKET_S}))
