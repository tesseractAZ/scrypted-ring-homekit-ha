#!/usr/bin/env python3
"""Scrypted engine-log archive: hourly, REDACTED, one gzip file per UTC day, 35 days.

WHY. The engine log is the only place several camera faults can be adjudicated
(door trips, per-camera recording errors, session teardown), and the journal
holds only ~37-38 h of it (measured 2026-10-02: 591,537 lines over 38.1 h),
shrinking in whole-file steps (the oldest line jumped 58 min between two reads
23 min apart). A review run more than ~37 h after the previous one silently lost
the difference. This sensor copies every new engine line into ARCHIVE_DIR,
redacted BEFORE it touches disk. RETAIN_DAYS is 35 because recorded camera
episodes ran 15-29 days and a page can take 10 days to fire: a 14-day archive
would have aged out how an episode BEGAN before anyone looked.

WHERE. /config/cam_engine.log.d/ matches the Supervisor's core-backup exclusion
'*.log.*' (supervisor/homeassistant/module.py HOMEASSISTANT_BACKUP_EXCLUDE,
tested as PurePath.full_match('data/*.log.*'); securetar's atomic_contents_add
skips a matching DIRECTORY without descending into it - both read in the running
Supervisor 2026-10-02). So ~74 MB of archive never enters the 25 local + cloud
backups. The trade, stated because it is real: a core restore empties /config
(remove_folder content_only) and with it the archive.

RESUME = THE JOURNAL CURSOR, NOT A TIMESTAMP. Every run that writes ends its
last gzip member with '#~camlog~ CURSOR seq=N ts=T c=<cursor>': the systemd
cursor of the last journal entry it consumed. The next run asks for
'entries=<cursor>:0:N'. Skip 0 is the point: the anchor entry itself comes back
first, and the X-First-Cursor header PROVES it is still in the journal; then
everything after it is archived in journal order. Measured live 2026-10-02 on
the Supervisor log API: a cursor behind the vacuumed head returns the HEAD with
a different X-First-Cursor, and 'entries=<cursor>:1:N' would have silently
skipped that head line; a garbage cursor returns HTTP 500; a cursor past the tail
returns 0 lines and no header. No tie counts, no clock: the previous design's
timestamp seek lost every line of a reboot whose clock came up behind the
archive and reported 'ok'.

NEVER SILENT LOSS. If the cursor cannot be verified the run fetches the WHOLE
retained journal from its head and
  GAP     - the head is newer than the anchor: the anchor was vacuumed, the span
            between them exists nowhere. '#~camlog~ GAP' line, status=gap.
  RESYNC  - anything else (no CURSOR line, cursor gone while the journal still
            reaches back past it - a lost journal tail, a fresh journal after a
            reboot with the clock behind -, the anchor's timestamp disagrees,
            or the previous run was interrupted between two members). The WHOLE
            fetched window is archived after a '#~camlog~ RESYNC' line:
            duplicates over loss. status=resync.
gap, resync and withheld are also PERSISTED as events (35 days) and published as
counts, so an overnight event outlives the one run that saw it.

EACH RUN (command_line, hourly):
  1. Defers (status=deferred, no fetch) while the HA process is < 5 min old: the
     first update of a command_line sensor is AWAITED inside entity setup
     (CommandSensor.async_added_to_hass, read in HA 2026.9.4), so a backfill
     there would hold HA's startup (10 s warning, 15 s 'blocking startup').
     Exception: an archive already > 24 h behind runs anyway with a 20 s budget,
     so an HA that restarts more often than hourly cannot defer it into a gap.
  2. Repairs the newest day files (truncates a gzip member a killed run left
     half written) and reads the newest CURSOR line.
  3. Fetches from the cursor (or the head, see above), streaming, and stops at
     RUN_BUDGET_S of work (status=catchup; the next run continues from the
     cursor it committed) so no run comes near the command timeout.
  4. Collapses each takePicture request dump to its url line (below), redacts
     every line (REDACTION RULES, fixed order) and runs an independent RESIDUAL
     scan; a line that still trips it is WITHHELD (timestamp kept, text not).
     Ring camera, ding, media-cell and session ids become KEYED PSEUDONYMS and
     ding epoch-ms stamps become offsets from the line's own stamp (below).
  5. Measures interior silences (max gap between consecutive entries).
  6. Appends one gzip member per touched UTC day, the CURSOR line last, fsyncs;
     saves a new pseudonym key AFTER the members (see PSEUDONYMS); applies
     retention; saves state.json; prints ONE JSON object.

FAILURE SEMANTICS IN HA 2026.9.4 (async_check_output_or_log, read in the core
container): on command_timeout HA logs 'Timeout for command', returns None and
does NOT kill the child. The entity goes to unknown with attributes {} while
this process keeps running to completion; the lock makes an overlapping run
report busy. The package therefore treats 'unknown' as a failed run.

CLI (reviewers and tests; the sensor runs with no arguments):
  cam_logarchive.py               archive run (the sensor)
  cam_logarchive.py --dry-run     fetch + collapse + redact + scan, write NOTHING
                                  (an EPHEMERAL pseudonym key; the note carries
                                  the pseudonym census, counts only)
  cam_logarchive.py --scan FILE.. residual-scan archive files (counts only, incl.
                                  the pseudonym census per REKEY segment)
"""
import os
import sys

try:  # stderr-silent by construction: a stray traceback lands in the core log
    _DN = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_DN, 2)
except Exception:  # noqa: BLE001
    pass
import warnings  # noqa: E402

warnings.simplefilter("ignore")

import calendar  # noqa: E402
import collections  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import ipaddress  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import zlib  # noqa: E402

ADDON = "<scrypted_addon_slug>"
LOG_URL = os.environ.get("CAMLOG_URL", "http://supervisor/addons/%s/logs?verbose" % ADDON)
ARCHIVE_DIR = os.environ.get("CAMLOG_DIR", "/config/cam_engine.log.d")
STATE = os.path.join(ARCHIVE_DIR, "state.json")
LOCK = os.path.join(ARCHIVE_DIR, ".lock")
# The pseudonym key (PSEUDONYMS below): 32 random bytes, 0600, beside the day
# files and therefore outside every backup, like the archive it keys. A DOTFILE:
# a shell '*' (a review pull of 'cam_engine.log.d/*', tar of '*') skips it, and
# the key must never travel with the day files - with it, a 9-digit doorbot_id
# is recovered from its token by ~1e9 HMACs.
KEY_FILE = os.path.join(ARCHIVE_DIR, ".pseudonym.key")
KEY_BYTES = 32
PROC_STATUS = "/proc/self/status"
PREFIX = "scrypted-"
SUFFIX = ".log.gz"
RETAIN_DAYS = 35            # UTC day files kept; measured 2.02-2.10 MiB/day before
                            # the dump collapse -> <= ~74 MB
CLOCK_AHEAD_MAX_S = 2 * 86400   # wall clock this far past the newest archived
                                # line -> delete NOTHING, status=clock
MAX_TOTAL_BYTES = 800 * 1024 * 1024   # runaway cap only (~10x the measured need)
MAX_FETCH = 1500000         # > 2.5x the retained journal; the API stops at the tail
# Work budget per run. Measured 2026-10-02: a whole-journal pass is 586,268 lines
# in 88.6 s in the core container (124-137 s with extra analysis from the SSH
# add-on); an hourly increment is ~15k lines (9,197..31,156/h) in ~2-4 s. 60 s
# leaves a 37 h backlog to 2-3 runs while each run stays far below the 600 s
# command_timeout. Env override: tests only.
RUN_BUDGET_S = float(os.environ.get("CAMLOG_BUDGET_S", "60"))
MAX_ENTRIES_RUN = int(os.environ.get("CAMLOG_MAX_ENTRIES", "0"))   # tests only; 0 = off
HTTP_TIMEOUT_S = 60         # per socket operation
STARTUP_DEFER_S = 300
# ...unless the archive is already this far behind: the journal reaches ~37 h, so
# an HA that restarts more often than hourly must not defer the archive into a
# gap. Such a startup run gets a short budget instead.
STARTUP_CATCHUP_LAG_H = 24.0
STARTUP_BUDGET_S = 20.0
LAG_WARN_H = 3.0            # two missed hourly runs plus slack
# HA's own probes put a line in this log about every 120 s: over 591,537 lines
# the largest gap between consecutive lines was 120.1 s (27 gaps > 120 s, none
# > 300 s). A longer silence with lines on BOTH sides is a journal hole or an
# engine hang/restart, and a cheap one to see.
SILENCE_COUNT_S = 300
SILENCE_FLAG_S = 600
REACH_WARN_H = 6.0          # the archive needs the journal to outlast one missed run
EVENTS_KEPT = 200
LONG_SILENCES_KEPT = 5
MARK = "#~camlog~ "         # archive-internal marker lines; never a journal line
HEARTBEAT_BUCKET_S = 3600

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) ")
# '<UTC ts> host ident[pid]: ' as the Supervisor's journal_verbose_formatter
# writes it (the [pid] is absent when the entry has no _PID).
PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) (\S* [\w.-]+(?:\[\d+\])?: )?")
CURSOR_LINE_RE = re.compile(r"^#~camlog~ CURSOR seq=(\d+) ts=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+|-) c=(\S+)$")
# A journal cursor is 's=..;i=..;b=..;m=..;t=..;x=..' with hex values (127 chars
# measured). --scan checks every CURSOR line against this, so a marker line can
# never carry anything but a cursor.
CURSOR_SHAPE = re.compile(r"^(?:[a-z]=[0-9a-f]{1,64};){2,9}[a-z]=[0-9a-f]{1,64}$")

# ---------------------------------------------------------------------------
# REDACTION RULES - applied in this order to every line before it is written.
# Measured on the live journal 2026-10-02 (593,141 lines): 7,200 Ring session
# JWTs, 41,091 IPv4, 7,376 full + 1,671 '::' IPv6 (LAN ULA and global), 3
# IPv4-mapped (::ffff:a.b.c.d), 5,004 ICE credential lines, 3,336 DTLS
# fingerprints, 7,206 doorbot_id, 31,249 16+-hex runs (webhook tokens), 44,034
# 7+-digit numbers, 1,935 RTSP 'Session:' ids (8 hex, ephemeral). The 20,531
# 'username:' keys in webhook dumps hold the JS literal undefined, left as is.
# Placeholders never contain 7+ digits, 16+ hex, dotted quads or colon-hex.
#
# PSEUDONYMS (10-03 review: every doorbot_id was the constant <RID> in 8,474
# signalling blocks, ding/cell ids too and every dialog_id <UUID>, so ~19 % of
# the blocks - mostly pong/timed_metadata keepalives - could not be tied to a
# camera and no message could be tied to its session). These values become
#   <TAG:token>, token = HMAC-SHA256(key, TAG NUL value) in base 20, letters g-z
# under the 32-byte KEY_FILE:
#   DEV  doorbot_id / device_id    8 letters  per camera: attribution
#   DING ding_id                  12 letters  per ding: ding -> session linkage
#   CELL cell_id                  12 letters  Ring media cell of a session
#   SES  dialog_id / session_id   12 letters  one signalling dialog / RMS session
#        (UUID-valued only, lower-cased first; dialog and session share the
#        class so an equal value stays visibly equal)
# location/user/account/owner/household/hardware ids, serials and MACs stay the
# constant <RID>: account- or hardware-level, constant within this archive, so a
# pseudonym would tell nothing. A pseudonym is not reversible without the key,
# which never leaves the host's archive directory (a dotfile, so a '*' copy of the
# day files leaves it behind) and never enters a backup; the
# linkage it reveals (same camera, same ding, same session) the camera-name lines
# around it already reveal. Without a key (redact() called by another tool) every
# one of these falls back to the constant placeholder.
# ENCODING. The alphabet holds no hex letter, no digit and no capital, so a token
# can trip no residual rule whatever its content (8 hex chars are all digits
# ~2 % of the time and would trip digits7; 12+ hex trips hex12; base32 runs 12
# hex-alphabet chars ~8e-6 of the time) - the residual scan keeps its full
# strictness with no exemption. 4.32 bits/letter, birthday bound n^2/2N:
#   DEV  20^8  = 2.6e10: 50 devices -> 5e-8
#   DING 20^12 = 4.1e15: 389 dings in 40.35 h measured (2026-10-03), 35 d ~8k
#                -> 8e-9; even 1e5 -> 1.2e-6
#   SES  20^12: 778 in 40.35 h (dialog + RMS session per ding), 35 d ~16k -> 3e-8
#   CELL 20^12: 20 in 40.35 h
# Measured on the whole live journal (603,004 entries, dry run 2026-10-03
# 03:20Z): 7 DEV tokens for the 7 cameras that held a session, a bijection over
# 389 sdp answers; 7,056 of 7,056 signalling blocks attributable; 0 withheld.
# Ding created_at / requested_at (13-digit epoch-ms) become the signed offset
# from the line's own UTC stamp, '<T:-1.234s>' (|offset| <= 1 day, so at most 5
# integer digits): ding latency is measurable without an absolute id-like number
# (median created_at -1.72 s, requested_at -0.026 s over those 389 dings).
# Anything else there (no stamp, another length, out of range) stays <NUM>.
# ---------------------------------------------------------------------------
_V4NETS = [
    # The private ranges are built from integers, so no private-range dotted quad
    # appears in this file (the public-repo sweep flags them).
    (ipaddress.ip_network((10 << 24, 8)), "lan"),                     # 10/8
    (ipaddress.ip_network(((172 << 24) | (16 << 16), 12)), "lan"),   # 172.16/12
    (ipaddress.ip_network(((192 << 24) | (168 << 16), 16)), "lan"),  # 192.168/16
    (ipaddress.ip_network(((100 << 24) | (64 << 16), 10)), "cgnat"), # 100.64/10
    (ipaddress.ip_network((127 << 24, 8)), "lo"),                    # 127/8
    (ipaddress.ip_network(((169 << 24) | (254 << 16), 16)), "ll"),   # 169.254/16
    (ipaddress.ip_network("224.0.0.0/4"), "mcast"),
    (ipaddress.ip_network("0.0.0.0/32"), "any"),
    (ipaddress.ip_network("255.255.255.255/32"), "bcast"),
]
_V6NETS = [
    (ipaddress.ip_network("::1/128"), "lo"),
    (ipaddress.ip_network("::/128"), "any"),
    (ipaddress.ip_network("fe80::/10"), "lan"),
    (ipaddress.ip_network("fc00::/7"), "lan"),
    (ipaddress.ip_network("ff00::/8"), "mcast"),
]


def _v4class(a):
    for net, cls in _V4NETS:
        if a in net:
            return cls
    return "pub"


def _v6class(a):
    for net, cls in _V6NETS:
        if a in net:
            return cls
    return "pub"


# An OPAQUE run: 12+ characters of a token alphabet that is NOT a plain word
# ('successfully', 'Authentication', 're-authentication'). Prose about tokens is
# outage evidence ('Refresh token is not valid. Unable to authenticate with Ring
# servers.') and must survive; the previous design's residual matched 'token'
# followed by ANY word and withheld exactly those lines.
_TOK = r"[A-Za-z0-9._~+/=-]"
_OPAQUE12 = (r"(?=%s{12,})(?![A-Z]?[a-z]+(?:-[a-z]+)*(?!%s))%s{11,}[A-Za-z0-9_~+/=-]"
             % (_TOK, _TOK, _TOK))
_OPAQUE8 = (r"(?=%s{8,})(?![A-Z]?[a-z]+(?:-[a-z]+)*(?!%s))%s{7,}[A-Za-z0-9_~+/=-]"
            % (_TOK, _TOK, _TOK))
# The same with ':' inside the run, for a credential WORD followed by a value
# (an FCM push token is '<22 chars>:APA91b<140 chars>').
_TOKC = r"[A-Za-z0-9._~+/=:-]"
_OPAQUE12C = (r"(?=%s{12,})(?![A-Z]?[a-z]+(?:-[a-z]+)*(?!%s))%s{11,}[A-Za-z0-9_~+/=-]"
              % (_TOKC, _TOKC, _TOKC))

# R1 JWT (Ring signalling sessions): header.payload.signature, or a bare
# base64url run that starts like a JSON header.
R_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*){0,2}")
# R1b e-mail (defensive; 0 seen).
R_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# R2 IPv6, BEFORE IPv4. LINEAR candidate: a maximal run of [hex : .] that begins
# after a character outside that class; the callback validates it with
# ipaddress, so a DTLS fingerprint (32 colon pairs), a time (03:14:27) or a MAC
# is never taken for an address, and '::ffff:a.b.c.d' is consumed WHOLE - a
# candidate that stops before the dotted tail leaked '.0.2.77' (mutant M9).
R_IP6 = re.compile(r"(?<![0-9A-Fa-f:.])[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")
_PORT_TAIL = re.compile(r"^(.*\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$")
# R3 IPv4.
R_IP4 = re.compile(r"(?<![0-9A-Za-z_.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![0-9]|\.\d)")
# R4 credential values: ICE (SDP 'a=ice-pwd:' / 'a=ice-ufrag:', JS
# 'usernameFragment:', the 'ufrag X' token inside candidate strings), passwords,
# usernames, tokens/keys, and whole Authorization / Cookie values. The JS
# literals undefined/null/true/false are left alone.
R_CRED = re.compile(
    r"(?i)(?<![\w-])((?:a=)?(?:ice-pwd|ice-ufrag|ice_pwd|ice_ufrag|usernameFragment|"
    r"username|user_name|password|passwd|pwd|ufrag|secret|client_secret|api[_-]?key|"
    r"access_token|refresh_token|id_token|auth_token|session_token|token)"
    r"[\"']?\s*[:=]\s*[\"']?)(?!(?:undefined|null|true|false)\b)([^\s\"'\\,;}\])<]+)")
R_CRED_Q = re.compile(r"(?i)(?<![\w-])((?:password|passwd|secret|credential|pwd)\\?[\"']?\s*[:=]\s*\\?(['\"]))[^'\"]*(?=\\?\2)")
R_UFRAG_SP = re.compile(r"(?i)(?<![\w-])(ufrag\s+)([^\s\"'\\,;}\])<]+)")
R_AUTHZ = re.compile(r"(?i)(?<![\w-])((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?|(?:set-)?cookie[\"']?\s*[:=]\s*[\"']?)([^\"'\r\n\\<]+)")
R_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+" + _OPAQUE8)
# R4b a credential word followed by whitespace and an OPAQUE run ('token
# AbC123...'), the shape the assignment-only rules above do not cover.
R_CRED_SP = re.compile(r"(?i)(?<![\w-])((?:token|secret|password|ufrag)\s+)(" + _OPAQUE12C + ")")
# R4c RTSP session ids ('Session: 1a2b3c4d;timeout=60', JS "session: '..'"):
# 8 hex, 366 distinct in 38 h, none seen in two hours - ephemeral and LAN-only,
# redacted anyway. A value without a digit ('Session: started') is prose.
R_SESSION = re.compile(r"(?i)(?<![\w-])(session[\"']?\s*:\s*[\"']?)(?=[A-Za-z0-9$_.+-]*\d)([A-Za-z0-9$_.+-]{6,})")
# R5 DTLS fingerprints: any chain of >= 8 colon-separated hex pairs, whatever
# key carries it (SDP 'a=fingerprint:sha-256 ...' or a JS {value: '...'}).
R_FP = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){7,}(?![0-9A-Fa-f])")
# R5b MAC (defensive; 0 seen): exactly six pairs.
R_MAC = re.compile(r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}(?![0-9A-Fa-f:-])")
# The key/value separator of the Ring-id rules: util.inspect 'key: v', JSON
# '"key":"v"', JSON escaped once or more ('\"key\":\"v\"') and URL-encoded
# ('key%3Dv', '%22key%22%3A%22v'). An id in an array ('cell_ids: [..]'), a
# nested object ('doorbot: { id: .. }'), split across lines or in prose is out of
# reach of ANY key-based rule: there a numeric id still meets num7 and a UUID the
# uuid rule, but a short or alphanumeric one does not (0 such lines measured).
_KV_SEP = r"(?:\\*[\"']|%22)?\s*(?:[:=]|%3[ad])\s*(?:\\*[\"']|%22)?"
# R6a Ring ids that become KEYED PSEUDONYMS (see PSEUDONYMS): group 2 is the key,
# group 3 the value (the same value class as R_RID).
R_RID_PS = re.compile(
    r"(?i)(?<![\w-])((doorbot_?id|device_?id|ding_?id|cell_?id)" + _KV_SEP + ")"
    r"(?!(?:undefined|null|true|false)\b)([0-9A-Za-z_.:-]{4,})")
# R6a' session UUIDs by key (dialog_id, RMS session_id) -> SES pseudonyms. A body
# 'session_id' holds a JWT, which R1 has already replaced.
R_SES_PS = re.compile(
    r"(?i)(?<![\w-])((?:dialog_?id|session_?id)[\"']?\s*[:=]\s*[\"']?)"
    r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})(?![0-9A-Fa-f])")
# R6a'' ding epoch-ms -> offset from the line's stamp.
R_TOFF = re.compile(r"(?i)(?<![\w-])((?:created|requested)_?at[\"']?\s*[:=]\s*[\"']?)(\d{13})(?![\d.])")
T_MAX_MS = 86400 * 1000
# R6 Ring account/device identifiers by key, and generic numeric "id".
R_RID = re.compile(
    r"(?i)(?<![\w-])((?:doorbot_?id|device_?id|location_?id|ding_?id|cell_?id|account_?id|"
    r"user_?id|owner_?id|household_?id|hardware_?id|serial(?:_?number)?|mac_?address)"
    + _KV_SEP + r")(?!(?:undefined|null|true|false)\b)([0-9A-Za-z_.:-]{4,})")
R_ID = re.compile(r"(?<![\w-])([\"']?id[\"']?\s*[:=]\s*[\"']?)(\d{5,})")
# R6b UUIDs (dialog_id / RMS session ids / msid / cname): 2,808 distinct, none
# seen across more than 2 hours.
R_UUID = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
# R6c HomeKit setup code and coordinates (defensive; 0 seen).
R_HK = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{3}(?![\d-])")
R_GEO = re.compile(r"(?i)(?<![\w-])((?:lat|latitude|lng|lon|longitude)[\"']?\s*[:=]\s*[\"']?)(-?\d{1,3}\.\d{3,})")
# R7 runs of 16+ hex (webhook tokens, cursors, digests).
R_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16,}(?![0-9A-Fa-f])")
# R8 bare runs of 7+ digits (ding/cell ids, epoch-ms, SDP session ids). Not
# after '.', so decimal fractions survive.
R_NUM = re.compile(r"(?<![\d.])\d{7,}")


PS_ALPHABET = "ghijklmnopqrstuvwxyz"     # 20 letters: no hex letter, no digit
PS_LEN = {"DEV": 8, "DING": 12, "CELL": 12, "SES": 12}
_PS_CLASS = {"doorbotid": "DEV", "deviceid": "DEV", "dingid": "DING", "cellid": "CELL"}


class Pseudonyms:
    """Keyed pseudonyms. The key never leaves this object: no repr, no log."""

    def __init__(self, key):
        self._key = key
        self._cache = {}

    def token(self, cls, value):
        t = self._cache.get((cls, value))
        if t is None:
            n = int.from_bytes(hmac.new(self._key, ("%s\x00%s" % (cls, value)).encode("utf-8", "replace"),
                                        hashlib.sha256).digest(), "big")
            out = []
            for _ in range(PS_LEN[cls]):
                n, r = divmod(n, len(PS_ALPHABET))
                out.append(PS_ALPHABET[r])
            t = "<%s:%s>" % (cls, "".join(out))
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[(cls, value)] = t
        return t


def _ps_rid(m, hits, ctx=None):
    ps = ctx[0] if ctx else None
    if ps is None:              # no key: the constant, exactly as before
        hits["ring_id"] = hits.get("ring_id", 0) + 1
        return m.group(1) + "<RID>"
    val = m.group(3)
    core = val.rstrip(".:-")    # sentence punctuation is not part of the id
    # (?i) is Unicode: 'i' also matches U+0130/U+0131, so a key such as
    # 'doorbot_<U+0131>d' matches yet lower-cases to no class. FAIL CLOSED to the
    # constant: a KeyError here failed every run until the journal vacuumed the
    # line, and the run after that recorded a GAP (10-03 review).
    cls = _PS_CLASS.get(m.group(2).lower().replace("_", ""))
    if cls is None:
        hits["ring_id"] = hits.get("ring_id", 0) + 1
        return m.group(1) + "<RID>"
    name = "ps_" + cls.lower()
    hits[name] = hits.get(name, 0) + 1
    return m.group(1) + ps.token(cls, core) + val[len(core):]


def _ps_ses(m, hits, ctx=None):
    ps = ctx[0] if ctx else None
    if ps is None:
        hits["uuid"] = hits.get("uuid", 0) + 1
        return m.group(1) + "<UUID>"
    hits["ps_ses"] = hits.get("ps_ses", 0) + 1
    return m.group(1) + ps.token("SES", m.group(2).lower())


def _t_offset(m, hits, ctx=None):
    line_ms = ctx[1] if ctx else None
    if line_ms is None:
        return m.group(0)       # no stamp to refer to: num7 makes it <NUM>
    d = int(m.group(2)) - line_ms
    if abs(d) > T_MAX_MS:
        return m.group(0)       # not an epoch-ms near this line: <NUM>
    hits["ts_offset"] = hits.get("ts_offset", 0) + 1
    return m.group(1) + "<T:%s%d.%03ds>" % ("-" if d < 0 else "+", abs(d) // 1000, abs(d) % 1000)


def _ip6_repl(m, hits, ctx=None):
    run = m.group(0)
    if run.count(":") < 2:
        return run
    lead, core, tail = "", run, ""
    if core.startswith(":") and not core.startswith("::"):
        lead, core = ":", core[1:]
    while core and core[-1] in ".:" and not core.endswith("::"):
        tail = core[-1] + tail
        core = core[:-1]
    for cand, extra in ((core, ""), _port_split(core)):
        if not cand:
            continue
        try:
            a = ipaddress.IPv6Address(cand)
        except ValueError:
            continue
        if "." in cand:            # embedded dotted quad: mapped/compatible/NAT64
            try:
                v4 = a.ipv4_mapped or ipaddress.IPv4Address(cand.rsplit(":", 1)[1])
            except ValueError:
                continue
            hits["ip6"] = hits.get("ip6", 0) + 1
            hits["ip6_v4mapped"] = hits.get("ip6_v4mapped", 0) + 1
            return lead + "<IP4:%s>" % _v4class(v4) + extra + tail
        if len([g for g in cand.split(":") if g]) < 2:
            return run             # '::1', 'Foo::ba' - nothing identifying
        hits["ip6"] = hits.get("ip6", 0) + 1
        return lead + "<IP6:%s>" % _v6class(a) + extra + tail
    return run


def _port_split(core):
    pm = _PORT_TAIL.match(core)
    if pm:
        return pm.group(1), ":" + pm.group(2)
    return "", ""


def _ip4_repl(m, hits, ctx=None):
    try:
        octs = [int(g) for g in m.groups()]
    except ValueError:
        return m.group(0)
    if any(o > 255 for o in octs):
        return m.group(0)
    hits["ip4"] = hits.get("ip4", 0) + 1
    return "<IP4:%s>" % _v4class(ipaddress.IPv4Address(".".join(map(str, octs))))


def _keyed(tag, name):
    def f(m, hits, ctx=None):
        hits[name] = hits.get(name, 0) + 1
        return m.group(1) + tag
    return f


def _whole(tag, name):
    def f(m, hits, ctx=None):
        hits[name] = hits.get(name, 0) + 1
        return tag
    return f


def _bearer(m, hits, ctx=None):
    hits["cred"] = hits.get("cred", 0) + 1
    return m.group(1) + " <CRED>"


# (name, regex, replacement, prefilter) - THE ORDER IS PART OF THE RULE: JWT;
# IPv6 (incl. ::ffff:a.b.c.d) before IPv4; ICE/credential values; DTLS
# fingerprints; Ring ids (the pseudonymised keys before the constant ones, the
# keyed session UUIDs before the generic UUID, the ding stamps before num7);
# 16+ hex; bare 7+ digits. Each prefilter is a NECESSARY condition for its regex
# (it only saves time): msg is the message, low its lower-case copy.
_CRED_KW = ("pwd", "ufrag", "user", "pass", "secret", "api", "token")
RULES = [
    ("jwt", R_JWT, _whole("<JWT>", "jwt"), lambda msg, low: "eyJ" in msg),
    ("email", R_EMAIL, _whole("<EMAIL>", "email"), lambda msg, low: "@" in msg),
    ("ip6", R_IP6, _ip6_repl, lambda msg, low: msg.count(":") >= 2),
    ("ip4", R_IP4, _ip4_repl, lambda msg, low: msg.count(".") >= 3),
    ("cred_authz", R_AUTHZ, _keyed("<CRED>", "cred"), lambda msg, low: "authorization" in low or "cookie" in low),
    ("cred_bearer", R_BEARER, _bearer, lambda msg, low: "bearer" in low or "basic" in low),
    ("cred_q", R_CRED_Q, _keyed("<CRED>", "cred"), lambda msg, low: "pass" in low or "secret" in low or "credential" in low or "pwd" in low),
    ("cred", R_CRED, _keyed("<CRED>", "cred"), lambda msg, low: any(k in low for k in _CRED_KW)),
    ("cred_ufrag", R_UFRAG_SP, _keyed("<CRED>", "cred"), lambda msg, low: "ufrag" in low),
    ("cred_sp", R_CRED_SP, _keyed("<CRED>", "cred"),
     lambda msg, low: "token" in low or "secret" in low or "password" in low or "ufrag" in low),
    ("rtsp_session", R_SESSION, _keyed("<SESS>", "rtsp_session"), lambda msg, low: "session" in low),
    ("fingerprint", R_FP, _whole("<FP>", "fingerprint"), lambda msg, low: msg.count(":") >= 7),
    ("mac", R_MAC, _whole("<MAC>", "mac"), lambda msg, low: msg.count(":") >= 5 or msg.count("-") >= 5),
    ("ring_ps", R_RID_PS, _ps_rid,
     lambda msg, low: "doorbot" in low or "device" in low or "ding" in low or "cell" in low),
    ("ring_id", R_RID, _keyed("<RID>", "ring_id"), lambda msg, low: "id" in low or "serial" in low or "mac" in low),
    ("id_num", R_ID, _keyed("<RID>", "ring_id"), lambda msg, low: "id" in low),
    ("ses_ps", R_SES_PS, _ps_ses, lambda msg, low: "dialog" in low or "session" in low),
    ("ts_offset", R_TOFF, _t_offset, lambda msg, low: "created" in low or "requested" in low),
    ("uuid", R_UUID, _whole("<UUID>", "uuid"), lambda msg, low: msg.count("-") >= 4),
    ("hk_code", R_HK, _whole("<HKCODE>", "hk_code"), lambda msg, low: msg.count("-") >= 2),
    ("geo", R_GEO, _keyed("<GEO>", "geo"), lambda msg, low: "la" in low or "ln" in low or "lo" in low),
    ("hex16", R_HEX, _whole("<HEX>", "hex16"), None),
    ("num7", R_NUM, _whole("<NUM>", "num7"), None),
]


def redact(msg, hits, ps=None, line_ms=None):
    """ps: the run's Pseudonyms (None: constant placeholders); line_ms: the
    line's own UTC stamp in epoch-ms (None: ding stamps stay <NUM>)."""
    ctx = (ps, line_ms)
    for _name, rx, fn, pre in RULES:
        if pre is not None and not pre(msg, msg.lower()):
            continue
        msg = rx.sub(lambda m, fn=fn: fn(m, hits, ctx), msg)
    return msg


def stamp_ms(ts):
    """'YYYY-MM-DD HH:MM:SS.fff..' (UTC) -> epoch-ms, integer arithmetic."""
    return int(utc_epoch(ts[:19])) * 1000 + (int(ts[20:23].ljust(3, "0")) if len(ts) > 20 else 0)


# ---------------------------------------------------------------------------
# RESIDUAL SCAN - written independently of the rules (broader, context-free).
# Any hit withholds the line. Must read 0 on a clean archive.
# Credential KEYS count only as an ASSIGNMENT ('password: x', 'token=x'); a
# credential WORD followed by whitespace counts only before an opaque run.
# After a credential or Ring-id key only an EXACT placeholder of the rules is
# exempt: a raw value that merely starts with '<' ('password=<Hunter2..>',
# 'doorbot_id: <123456>', 'doorbot_id: <DEV:abc123XYZ>') is a value like any other.
# ---------------------------------------------------------------------------
_PH = (r"(?-i:<(?:JWT|EMAIL|CRED|SESS|FP|MAC|RID|UUID|HKCODE|GEO|HEX|NUM|IP[46]:[a-z]+|"
       r"DEV:[g-z]{8}|(?:DING|CELL|SES):[g-z]{12}|T:[+-][0-9]{1,5}\.[0-9]{3}s)>)")
RESIDUAL = [
    ("jwt", re.compile(r"eyJ")),
    ("dotted_quad", re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")),
    ("ffff", re.compile(r"(?i)::ffff")),
    ("ip6_dcolon", re.compile(r"(?i)[0-9a-f]{1,4}::[0-9a-f]{1,4}")),
    ("hex_colon4", re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){3,}")),
    ("cred_key", re.compile(r"(?i)(?:ice-pwd|ice-ufrag|usernamefragment|password|passwd|pwd|"
                            r"username|ufrag|secret|token|authorization|cookie|credential|api[-_]?key|signature)"
                            r"(?:\\?[\"'])?\s*(?:[:=]|%3[ad])\s*(?:\\?[\"'])?(?!" + _PH + r"|(?:bearer|basic)\s+" + _PH
                            + r"|null\b|undefined\b|true\b|false\b|\\?[\"',})\]]|$)\S")),
    ("cred_word", re.compile(r"(?i)(?<![\w-])(?:token|secret|password|ufrag|bearer)\s+(?!<)" + _OPAQUE12C)),
    ("rtsp_session", re.compile(r"(?i)(?<![\w-])session[\"']?\s*:\s*[\"']?(?!<)(?=[A-Za-z0-9$_.+-]*\d)[A-Za-z0-9$_.+-]{6,}")),
    ("ring_id_key", re.compile(r"(?i)(?:doorbot_?id|device_?id|location_?id|ding_?id|cell_?id)"
                               r"(?:\\*[\"']|%22)?\s*(?:[:=]|%3[ad])\s*(?:\\*[\"']|%22)?"
                               r"(?!" + _PH + r"|null\b|undefined\b|true\b|false\b)[<0-9A-Za-z]")),
    ("hex12", re.compile(r"(?i)[0-9a-f]{12,}")),
    ("digits7", re.compile(r"\d{7,}")),
    ("email", re.compile(r"@[A-Za-z0-9-]+\.[A-Za-z]{2,}")),
    ("email_pct", re.compile(r"(?i)%40[A-Za-z0-9-]+(?:\.|%2e)[A-Za-z]{2,}")),
    ("mac_alt", re.compile(r"(?i)\b[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\b|[0-9a-f]{2}(?:-[0-9a-f]{2}){5}")),
    ("hk_uri", re.compile(r"X-HM://")),
    ("dotted_quad_pct", re.compile(r"(?i)\d{1,3}(?:%2e\d{1,3}){3}")),
    # A quoted mixed-class value under a bare auth/key/p256dh key (a Web Push
    # subscription's auth secret is 22 chars, under opaque24's floor).
    ("auth_kv", re.compile(r"(?i:(?<![\w-])(?:auth|key|p256dh)(?:\\?[\"'])?\s*(?:[:=]|%3[ad])\s*\\?[\"'])"
                           r"(?=[A-Za-z0-9_+/=-]*[A-Z])(?=[A-Za-z0-9_+/=-]*[a-z])(?=[A-Za-z0-9_+/=-]*\d)"
                           r"[A-Za-z0-9_+/=-]{16,}")),
    ("opaque24", re.compile(r"(?<![A-Za-z0-9_+/=-])(?=[A-Za-z0-9_+/=-]*[A-Z])(?=[A-Za-z0-9_+/=-]*[a-z])(?=[A-Za-z0-9_+/=-]*\d)[A-Za-z0-9_+/=-]{24,}")),
]
# A ':'-group run of 4+ is also how a TIME with ms never looks (HH:MM:SS.mmm has
# 3), so hex_colon4 does not fire on timestamps.


def residual(line):
    out = []
    for name, rx in RESIDUAL:
        if rx.search(line):
            out.append(name)
    return out


def withhold(line, kinds):
    m = PREFIX_RE.match(line)
    head = line[:m.end()] if m else ""
    if head and residual(re.sub(r"\[\d+\]", "", head)):
        head = TS_RE.match(line).group(0)
    return head + "<WITHHELD residual=%s>" % ",".join(kinds)


def process_line(raw, hits, ps=None):
    """raw journal line -> (archived text, residual kinds or []).
    The verbose prefix '<ts> host ident[pid]: ' is kept VERBATIM: cam_flap's
    MOTION_RE anchors on the ': ' before the camera name, and a stripped archive
    lost 57 of 57 motion lines in a replay. Redaction runs on the message; the
    residual scan on the WHOLE archived line."""
    m = PREFIX_RE.match(raw)
    head, msg = (raw[:m.end()], raw[m.end():]) if m else ("", raw)
    red = head + redact(msg, hits, ps, stamp_ms(m.group(1)) if m else None)
    kinds = residual(re.sub(r"\[\d+\]: $", "[]: ", head) + red[len(head):])
    if kinds:
        return withhold(red, kinds), kinds
    return red, []


# ---------------------------------------------------------------------------
# PROBE-DUMP COLLAPSE. cam_health and cam_vision each fetch the same nine
# takePicture webhooks every 120 s, and the engine logs every request as a
# 15-line object dump ('received webhook {' .. '}'). Measured 2026-10-02: the
# same-millisecond groups holding these dumps were 291,878 of 591,537 lines
# (49.3 %) and 32.9 % of the gzip bytes, and they are where the webhook tokens
# live. A dump is collapsed to its url line plus a note of what was dropped:
#   '  url: '/endpoint/.../public/<id>/<HEX>/takePicture', <request dump
#    collapsed: 15 lines, GET, user-agent Python-urllib/3.14>'
# so cam_flap's 'public/(\d+)/(?:[a-f0-9]+|<HEX>)/takePicture' still counts probes per device and the
# user-agent still tells a watchdog probe from a dashboard pull. The grammar is
# STRICT (exact opener, exact closer, allow-listed keys at fixed indents, one url
# line, no nested object, one writer): anything else - a browser request with
# cookies, a foreign line interleaved, a dump cut by the end of a fetch - is
# archived VERBATIM, line by line. Collapse can only ever drop the lines of a
# dump whose every line was recognised. The 'device N Name' line and the blank
# line that follow a dump are separate log statements and are kept.
# ---------------------------------------------------------------------------
DUMP_OPEN = "received webhook {"
DUMP_CLOSE = "}"
DUMP_MAX_LINES = 40         # measured dumps: 15 lines (HA's own client adds 1-2)
_DUMP_TOP = re.compile(r"^  (body|method|rootPath|url|isPublicEndpoint|username|aclId): (.+)$")
_DUMP_HDR = re.compile(r"^    ('[a-z][a-z-]*'|[a-z]+): '([^'\\]*)',?$")
_DUMP_HDR_KEYS = frozenset(["host", "accept", "'accept-encoding'", "connection", "'user-agent'"])


class Collapser:
    """Streams raw lines in, archive-ready raw lines out, in order."""

    def __init__(self):
        self.buf = []
        self.dumps = 0          # dumps collapsed
        self.dropped = 0        # lines removed by collapsing
        self.verbatim = 0       # dumps that opened but failed the grammar
        self._reset()

    def _reset(self):
        self.buf = []
        self.who = None
        self.in_headers = False
        self.seen_headers = False
        self.url_at = None
        self.method = None
        self.ua = None

    @property
    def idle(self):
        return not self.buf

    def feed(self, raw, ts):
        m = PREFIX_RE.match(raw)
        msg = raw[m.end():] if (m and m.group(2)) else None
        who = m.group(2) if m else None
        if self.buf:
            if msg is None or who != self.who:
                return self._abort() + self.feed(raw, ts)
            step = self._step(msg)
            if step == "bad":
                return self._abort() + self.feed(raw, ts)
            self.buf.append((raw, ts))
            if step == "close":
                return self._close()
            if len(self.buf) > DUMP_MAX_LINES:
                return self._abort()
            return []
        if msg == DUMP_OPEN:
            self._reset()
            self.buf = [(raw, ts)]
            self.who = who
            return []
        return [(raw, ts)]

    def _step(self, msg):
        if self.in_headers:
            if msg in ("  },", "  }"):
                self.in_headers = False
                return "more"
            hm = _DUMP_HDR.match(msg)
            if hm and hm.group(1) in _DUMP_HDR_KEYS:
                if hm.group(1) == "'user-agent'":
                    self.ua = hm.group(2)
                return "more"
            return "bad"
        if msg == DUMP_CLOSE:
            return "close"
        if msg == "  headers: {":
            if self.seen_headers:
                return "bad"
            self.in_headers = self.seen_headers = True
            return "more"
        tm = _DUMP_TOP.match(msg)
        if not tm or tm.group(2).rstrip(",").endswith(("{", "[")):
            return "bad"
        key, val = tm.group(1), tm.group(2).rstrip(",")
        if key == "url":
            if self.url_at is not None:
                return "bad"
            self.url_at = len(self.buf)
        elif key == "method":
            self.method = val.strip("'")
        return "more"

    def _close(self):
        ok = (self.url_at is not None and not self.in_headers
              and "/takePicture'" in self.buf[self.url_at][0])
        if not ok:
            return self._abort()
        url_raw, url_ts = self.buf[self.url_at]
        n = len(self.buf)
        out = "%s <request dump collapsed: %d lines, %s, user-agent %s>" % (
            url_raw.rstrip(), n, self.method or "?", self.ua or "?")
        self.dumps += 1
        self.dropped += n - 1
        self._reset()
        return [(out, url_ts)]

    def _abort(self):
        out = self.buf
        if out:
            self.verbatim += 1
        self._reset()
        return out

    def flush(self):
        return self._abort()


# ---------------------------------------------------------------------------
# Archive files
# ---------------------------------------------------------------------------
def day_path(day):
    return os.path.join(ARCHIVE_DIR, PREFIX + day + SUFFIX)


def list_days():
    try:
        names = os.listdir(ARCHIVE_DIR)
    except FileNotFoundError:
        return []
    days = []
    for n in names:
        if n.startswith(PREFIX) and n.endswith(SUFFIX):
            d = n[len(PREFIX):-len(SUFFIX)]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                days.append(d)
    return sorted(days)


def read_members(path, keep_last=None):
    """Decompress every COMPLETE gzip member (one per run). Returns
    (texts, valid_bytes, size, n_members); with keep_last only the last members'
    texts are kept, which bounds memory."""
    with open(path, "rb") as fh:
        data = fh.read()
    view = memoryview(data)
    pos, n = 0, 0
    texts = collections.deque(maxlen=keep_last) if keep_last else []
    while pos < len(data):
        d = zlib.decompressobj(31)
        try:
            out = d.decompress(view[pos:])
        except zlib.error:
            break
        if not d.eof:
            break          # incomplete member: a run was killed mid-append
        texts.append(out.decode("utf-8", "replace"))
        n += 1
        pos = len(data) - len(d.unused_data)
    return list(texts), pos, len(data), n


def _complete_member_after(path, valid):
    """True when a COMPLETE gzip member starts anywhere after byte `valid`."""
    with open(path, "rb") as fh:
        data = fh.read()
    pos = data.find(b"\x1f\x8b\x08", valid + 1)
    while pos != -1:
        d = zlib.decompressobj(31)
        try:
            d.decompress(data[pos:])
            if d.eof:
                return True
        except zlib.error:
            pass
        pos = data.find(b"\x1f\x8b\x08", pos + 1)
    return False


def _tail_cursor(text):
    """(last CURSOR marker as dict or None, journal lines after it?, newest stamp
    of a journal line or None)"""
    marker, after, newest = None, False, None
    for ln in text.split("\n"):
        if not ln:
            continue
        if ln.startswith(MARK):
            cm = CURSOR_LINE_RE.match(ln)
            if cm:
                marker = {"seq": int(cm.group(1)), "ts": None if cm.group(2) == "-" else cm.group(2),
                          "c": cm.group(3)}
                after = False
            continue
        after = True
        tm = TS_RE.match(ln)
        if tm and (newest is None or tm.group(1) > newest):
            newest = tm.group(1)
        elif newest is None:
            newest = ""
    return marker, (after and marker is not None), newest


def scan_archive(notes, repair=True):
    """Truncate half-written tails of the newest files (unless dry) and return the
    commit point: the LAST CURSOR line of the newest-named day file. Every run that
    writes puts its CURSOR line into the newest-named file, last."""
    days = list_days()
    out = {"has_lines": False, "marker": None, "lines_after": False, "newest": None, "newest_ts": None}
    found = False
    for day in reversed(days[-3:]):
        p = day_path(day)
        try:
            texts, valid, size, nmem = read_members(p, keep_last=3)
        except OSError:
            notes.append("unreadable %s" % os.path.basename(p))
            continue
        if valid < size and _complete_member_after(p, valid):
            # NOT a half-written tail: an undecodable member with intact members
            # after it (on-disk corruption). Keep every byte, out of the day-file
            # namespace (list_days/retention never see it), and resync.
            aside = p + ".corrupt-%d" % int(time.time())
            notes.append("CORRUPT member in %s at byte %d of %d; file set aside as %s%s"
                         % (os.path.basename(p), valid, size, os.path.basename(aside),
                            "" if repair else " (dry run: not moved)"))
            if repair:
                os.replace(p, aside)
                out["corrupt"] = os.path.basename(aside)
                out["has_lines"] = True     # an archive existed: never a START
                continue
        if valid < size:
            notes.append("repaired %s: dropped %d B of an unfinished member%s"
                         % (os.path.basename(p), size - valid, "" if repair else " (dry run: not truncated)"))
            if repair:
                with open(p, "r+b") as fh:
                    fh.truncate(valid)
                    fh.flush()
                    os.fsync(fh.fileno())
                if valid == 0:
                    os.remove(p)
                    continue
        if not found:
            found = True
            marker, after, newest = _tail_cursor("".join(texts))
            if marker is None and nmem > len(texts):
                marker, after, newest2 = _tail_cursor("".join(read_members(p)[0]))
                newest = max([x for x in (newest, newest2) if x is not None] or [None])
            out["newest"] = day
            out["marker"], out["lines_after"] = marker, after
            out["newest_ts"] = newest or None
            out["has_lines"] = out["has_lines"] or newest is not None or marker is not None
        elif not out["has_lines"]:
            out["has_lines"] = any(_tail_cursor(t)[2] is not None for t in texts)
    if not out["has_lines"] and len(days) > 3:
        out["has_lines"] = True        # older files exist: this is not a first run
    return out


def append_member(day, payload):
    p = day_path(day)
    with open(p, "ab") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    return os.path.getsize(p)


def fsync_dir():
    try:
        fd = os.open(ARCHIVE_DIR, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


_EPOCH_CACHE = {}


def utc_epoch(ts):
    """'YYYY-MM-DD HH:MM:SS.fff' (UTC) -> epoch seconds. Cached per second: a full
    journal pass parses ~590k stamps."""
    sec = ts[:19]
    base = _EPOCH_CACHE.get(sec)
    if base is None:
        base = calendar.timegm((int(sec[0:4]), int(sec[5:7]), int(sec[8:10]),
                                int(sec[11:13]), int(sec[14:16]), int(sec[17:19])))
        if len(_EPOCH_CACHE) > 4096:
            _EPOCH_CACHE.clear()
        _EPOCH_CACHE[sec] = base
    return base + (float("0." + ts[20:]) if len(ts) > 20 else 0.0)


def day_of_epoch(e):
    return time.strftime("%Y-%m-%d", time.gmtime(e))


# ---------------------------------------------------------------------------
# Journal access
# ---------------------------------------------------------------------------
def open_log(rng):
    token = os.environ.get("CAMLOG_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN unavailable")
    req = urllib.request.Request(LOG_URL, headers={
        "Authorization": "Bearer " + token, "Accept": "text/plain", "Range": rng})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)


class Stream:
    """One log response read ENTRY by entry: a stamped line plus any unstamped
    continuation lines (a MESSAGE with an embedded newline; 0 of 593,141 lines
    measured, handled anyway). Entry k of a response is what
    'entries=<first_cursor>:<k>:1' returns."""

    def __init__(self, rng):
        self.resp = open_log(rng)
        self.first_cursor = self.resp.headers.get("X-First-Cursor")
        self.lines = 0

    def entries(self):
        cur = None
        for b in self.resp:
            ln = b.decode("utf-8", "replace").rstrip("\n")
            self.lines += 1
            if TS_RE.match(ln) or cur is None:
                if cur is not None:
                    yield cur
                cur = [ln]
            else:
                cur.append(ln)
        if cur is not None:
            yield cur

    def close(self):
        try:
            self.resp.close()
        except Exception:  # noqa: BLE001
            pass


def first_entry(rng):
    s = Stream(rng)
    try:
        return next(s.entries(), None), s.first_cursor
    finally:
        s.close()


def cursor_of(first_cursor, k, expect_line):
    """Cursor of entry k counted from first_cursor, VERIFIED: the API must hand back
    the very line this run archived as entry k, or nothing is committed."""
    ent, cur = first_entry("entries=%s:%d:1" % (first_cursor, k))
    if ent is None or ent[0] != expect_line or not cur or not CURSOR_SHAPE.match(cur):
        raise RuntimeError("cursor lookup for entry %d did not return the archived line" % k)
    return cur


class DayBuf:
    """Streams one day's new lines into an in-memory gzip member."""

    def __init__(self):
        self.c = zlib.compressobj(6, zlib.DEFLATED, 31)
        self.parts = []
        self.lines = 0
        self.raw_bytes = 0
        self.first = None
        self.last = None
        self.withheld = 0

    def add(self, text, ts):
        b = (text + "\n").encode("utf-8", "replace")
        self.raw_bytes += len(b)
        self.parts.append(self.c.compress(b))
        if not text.startswith(MARK):
            self.lines += 1
            if ts is not None:
                self.first = self.first or ts
                self.last = ts

    def finish(self):
        self.parts.append(self.c.flush())
        return b"".join(self.parts)


# ---------------------------------------------------------------------------
# State (health and history; the RESUME POINT is the CURSOR line in the archive)
# ---------------------------------------------------------------------------
def load_state(notes, dry=False):
    try:
        with open(STATE) as fh:
            st = json.load(fh)
        if not isinstance(st, dict):
            raise ValueError("not an object")
        return st
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        notes.append("state.json unreadable - history restarted")
        if not dry:
            try:
                os.replace(STATE, STATE + ".corrupt-%d" % int(time.time()))
            except OSError:
                pass
        return {}


def save_state(st):
    tmp = STATE + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(st, fh, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE)


# ---------------------------------------------------------------------------
# Pseudonym key. Read at the start of a run, BEFORE the archive scan may repair
# or set aside a file; a MISSING one is generated in memory and saved only AFTER
# this run's members are on disk, so every key that ever produced an archived
# token is preceded in the archive by its REKEY line - at the head of EVERY
# member the rekeying run appends, so each day file it touches says where its
# tokens change key:
#  - nothing written (no new entry, an error): the key is dropped unsaved and the
#    next run generates another, with its own REKEY line;
#  - killed between the members and the key save: the next run finds no key and
#    writes a new REKEY line before its tokens.
# A key file of the wrong size is replaced the same way (REKEY says why); any
# other read error FAILS the run (status=error, nothing written, nothing
# repaired): a transient EIO must not silently split the pseudonyms.
# ---------------------------------------------------------------------------
def load_key():
    """-> (key or None, why a new key is needed or None). Creates nothing."""
    try:
        with open(KEY_FILE, "rb") as fh:
            key = fh.read(KEY_BYTES + 1)    # the size is all that is checked
    except FileNotFoundError:
        return None, "no key file"
    if len(key) > KEY_BYTES:
        return None, "the key file held more than %d bytes" % KEY_BYTES
    if len(key) != KEY_BYTES:
        return None, "the key file held %d bytes, not %d" % (len(key), KEY_BYTES)
    return key, None


def save_key(key):
    """Atomic: a 0600 temp file, every byte written, fsync, rename over KEY_FILE,
    fsync the dir. A failure removes the temp file: it holds the key."""
    tmp = KEY_FILE + ".tmp.%d" % os.getpid()
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(key)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, KEY_FILE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    fsync_dir()


def drop_stale_key_tmps():
    """Under the run lock, never in a dry run: a key save KILLED between its temp
    write and the rename (SIGKILL, power cut) left the key under another pid's
    temp name, which nothing else would ever remove."""
    pre = os.path.basename(KEY_FILE) + ".tmp."
    try:
        names = os.listdir(ARCHIVE_DIR)
    except OSError:
        return
    for n in names:
        if n.startswith(pre):
            try:
                os.remove(os.path.join(ARCHIVE_DIR, n))
            except OSError:
                pass


def clean_events(st):
    ev = st.get("events") if isinstance(st.get("events"), list) else []
    return [e for e in ev if isinstance(e, dict) and isinstance(e.get("at"), (int, float))
            and e.get("kind") in ("gap", "resync", "withheld")]


def persisted(st):
    """Event counters from state.json, published on EVERY run (also deferred and
    busy ones), so the events sensor never flips on a run that did not look."""
    ev = clean_events(st)
    gaps = [e for e in ev if e["kind"] == "gap"]
    last = max(ev, key=lambda e: e["at"]) if ev else None
    return {
        "gap_count_35d": len(gaps),
        "gap_min_35d": round(sum(float(e.get("min") or 0) for e in gaps), 1),
        "last_gap": [gaps[-1].get("from"), gaps[-1].get("to"), gaps[-1].get("min")] if gaps else None,
        "withheld_35d": sum(int(e.get("n") or 0) for e in ev if e["kind"] == "withheld"),
        "resync_count_35d": sum(1 for e in ev if e["kind"] == "resync"),
        "events_35d": len(ev),
        "last_event": _event_text(last) if last else None,
        "last_event_at": int(last["at"]) if last else None,
    }


def _event_text(e):
    if e["kind"] == "gap":
        return "gap %s -> %s (%s min)" % (e.get("from"), e.get("to"), e.get("min"))
    if e["kind"] == "withheld":
        return "withheld %s line(s): %s" % (e.get("n"), ",".join(e.get("kinds") or []))
    return "resync (%s)" % e.get("why")


def ha_process_age():
    """Seconds since the Home Assistant process started, from /proc (the core
    container's PID namespace holds 'python3 -P -m homeassistant --config
    /config'); None when it cannot be told (then nothing is deferred)."""
    ov = os.environ.get("CAMLOG_HA_AGE_S")   # tests only
    if ov:
        return float(ov)
    try:
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        tck = os.sysconf("SC_CLK_TCK")
        me = os.getpid()
        youngest = None
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    cmd = fh.read()
                if b"\x00-m\x00homeassistant\x00" not in cmd or b"\x00--script\x00" in cmd:
                    continue
                with open("/proc/%s/stat" % pid) as fh:
                    st = fh.read().rsplit(")", 1)[1].split()
                age = up - int(st[19]) / float(tck)
                youngest = age if youngest is None else min(youngest, age)
            except (OSError, IndexError, ValueError):
                continue
        return youngest
    except Exception:  # noqa: BLE001
        return None


def peak_rss_mb():
    """This script's own peak RSS: VmHWM of /proc/self/status, MB, 1 dp; None
    when unavailable. NOT getrusage(RUSAGE_SELF).ru_maxrss: that is the
    PROCESS's high-water mark, and Linux folds the replaced memory map's peak
    into it at execve. HA Core starts this command by forking itself (the child's
    map starts with Core's resident set) and exec'ing the shell, which execs
    python in the same process, so ru_maxrss read Core's 711.2 MB on every run
    (10-03 review). VmHWM belongs to the map the last exec created."""
    try:
        with open(PROC_STATUS) as fh:
            for ln in fh:
                if ln.startswith("VmHWM:"):
                    return round(int(ln.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def lag_minutes(now, through):
    """Minutes from the newest archived line to now, 1 dp, clamped at 0 AFTER
    rounding: a line stamped after `now` (the run's own start) published -0.0.
    The clamp is deliberate: `now` is the run's START, so every line the engine
    logs while a run streams lands 'in the future' (up to the 60 s budget), and
    such a negative is an artifact, not a clock fault. lag_min's readers (the
    'clock' page text, the problem sensor's attribute) read it as an age; the
    warnings use the unclamped lag."""
    lag = round((now - utc_epoch(through)) / 60.0, 1)
    return lag if lag > 0 else 0.0


# ---------------------------------------------------------------------------
def emit(payload):
    # Every key here must also be in the package's json_attributes (an
    # ALLOWLIST: an unlisted key is silently dropped). The test suite checks both
    # directions against cam_logarchive.yaml.
    base = {
        "status": "error", "archived_through": None, "lag_min": None,
        "lines_run": None, "entries_run": None, "withheld_run": None, "collapsed_run": None,
        "bytes_run": None, "redaction_run": None, "residual_run": None,
        "fetch_lines": None, "fetch_passes": None, "run_s": None,
        "resume": None, "anchor": None, "capped": None, "ts_backsteps": None,
        "max_silence_s": None, "silences_gt300": None, "long_silences": None,
        "journal_oldest": None, "journal_reach_h": None,
        "gap_count_35d": None, "gap_min_35d": None, "last_gap": None,
        "withheld_35d": None, "resync_count_35d": None, "events_35d": None,
        "last_event": None, "last_event_at": None,
        "today": None, "files": None, "total_mb": None, "oldest_day": None,
        "retention_days": RETAIN_DAYS, "deleted_days": None,
        "ha_age_s": None, "rss_mb": None, "warnings": None, "dry_run": None,
        "note": None, "error": None, "summary": "", "updated_at": None,
    }
    base.update(payload)
    base["rss_mb"] = peak_rss_mb()
    try:
        sys.stdout.write(json.dumps(base, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - HA may have stopped reading (timeout)
        pass
    sys.exit(0)


def _bucket(now):
    return int(now // HEARTBEAT_BUCKET_S) * HEARTBEAT_BUCKET_S


def _quiet_state():
    try:
        with open(STATE) as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _since(st, now):
    lr = st.get("last_run") if isinstance(st.get("last_run"), dict) else {}
    through = lr.get("through")
    try:
        lag = lag_minutes(now, through) if through else None
    except Exception:  # noqa: BLE001
        lag = None
    return through, lag


def dry_marker(back):
    """--dry-run only: pretend the archive ends at the entry `back` lines before
    the tail (to time an hourly-sized run in the target container)."""
    ent, cur = first_entry("entries=:-%d:1" % back)
    if ent is None or not cur:
        return None
    m = TS_RE.match(ent[0])
    return {"seq": 0, "ts": m.group(1) if m else None, "c": cur}


def run(dry=False, now=None):
    t_start = time.time()
    now = now if now is not None else t_start
    notes, warns = [], []
    ha_age = ha_process_age()
    if not dry:
        os.umask(0o077)
        os.makedirs(ARCHIVE_DIR, mode=0o700, exist_ok=True)
        lockfh = open(LOCK, "a")
        try:
            fcntl.flock(lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            st = _quiet_state()
            through, lag = _since(st, now)
            emit(dict(persisted(st), status="busy", archived_through=through, lag_min=lag,
                      ha_age_s=None if ha_age is None else int(ha_age),
                      summary="busy: another archive run holds the lock (a previous run is still working)",
                      updated_at=_bucket(now)))
    state = load_state(notes, dry)
    budget = RUN_BUDGET_S
    if not dry and ha_age is not None and ha_age < STARTUP_DEFER_S:
        through, lag = _since(state, now)
        if lag is None or lag <= STARTUP_CATCHUP_LAG_H * 60:
            emit(dict(persisted(state), status="deferred", archived_through=through, lag_min=lag,
                      ha_age_s=int(ha_age), note="; ".join(notes) or None,
                      warnings=["archive %.1f h behind the journal" % (lag / 60.0)]
                      if lag is not None and lag > LAG_WARN_H * 60 else None,
                      summary="deferred: Home Assistant started %d s ago; the archive runs on the next poll" % ha_age,
                      updated_at=_bucket(now)))
        budget = min(budget, STARTUP_BUDGET_S)
        notes.append("ran during HA startup with a %.0f s budget: the archive was %.1f h behind" % (budget, lag / 60.0))

    # ---- pseudonym key (see load_key): read BEFORE scan_archive may repair or set
    # aside a file, so a key read error fails a run that has changed nothing (a
    # set-aside whose run then failed left no RESYNC behind: the next run STARTed)
    if not dry:
        drop_stale_key_tmps()
    disk_key, key_why = load_key()

    arch = scan_archive(notes, repair=not dry)
    had_lines = arch["has_lines"]   # what a REAL run finds (CAMLOG_DRY_BACK pretends)
    marker = arch["marker"]
    if dry and not arch["has_lines"] and os.environ.get("CAMLOG_DRY_BACK"):
        marker = dry_marker(int(os.environ["CAMLOG_DRY_BACK"]))
        arch["has_lines"] = marker is not None
    pend = state.get("pending") if isinstance(state.get("pending"), dict) else None
    interrupted = False
    try:
        interrupted = bool(pend and int(pend.get("seq", 0)) > (marker["seq"] if marker else 0))
        if pend and marker and int(pend.get("seq", 0)) == marker["seq"]:
            notes.append("state.json was one run behind the archive (seq %d committed)" % marker["seq"])
    except (TypeError, ValueError):
        interrupted = True

    # A dry run always uses an ephemeral key and saves nothing; it still reports
    # whether a real run would write REKEY.
    key = disk_key if (disk_key is not None and not dry) else os.urandom(KEY_BYTES)
    new_key = key if (disk_key is None and not dry) else None
    disk_key = None
    ps = Pseudonyms(key)
    key = None
    # REKEY only where tokens from an earlier key (or the constant placeholders
    # of the pre-pseudonym archive) can exist; a first run starts with START.
    rekey = key_why if (key_why and (had_lines or list_days())) else None
    if dry:
        notes.append("pseudonym key: ephemeral (dry run)%s"
                     % ("; a real run would write REKEY (%s)" % key_why if rekey else ""))
    if key_why and state.get("ps_key_at"):
        # after deploy the only REKEY is the first; a later one means the key was
        # deleted, cut short or replaced, and must not pass as an ordinary run
        warns.append("pseudonym key lost (%s): a new key starts at the next REKEY line; tokens on its two "
                     "sides are not comparable" % key_why)
    elif new_key is None and not dry and not state.get("ps_key_at"):
        state["ps_key_at"] = int(now)
    census = Census() if dry else None

    # ---- fetch: from the verified cursor, else the whole journal from its head
    passes = 0
    stream = None
    mode = None            # cursor | start | gap | resync
    why = None             # the RESYNC reason
    anchor = "none"
    # Lines after the last CURSOR line are a run's partial output only when
    # state.json shows that run interrupted (pending seq = marker seq + 1, e.g. a
    # crash between the members of a run that crossed midnight): the marker is
    # still the last commit, so resume from it and let those lines repeat.
    if marker is not None and (not arch["lines_after"] or interrupted):
        passes += 1
        s = None
        try:
            s = Stream("entries=%s:0:%d" % (marker["c"], MAX_FETCH))
        except urllib.error.HTTPError as exc:
            if exc.code != 500:
                raise
            anchor = "rejected"          # measured: a malformed cursor -> HTTP 500
        if s is not None and s.first_cursor == marker["c"]:
            stream, mode, anchor = s, "cursor", "verified"
        else:
            if s is not None:
                s.close()
                anchor = "gone"
            why = "cursor %s" % anchor
    elif marker is not None:
        why, anchor = "journal lines after the last CURSOR line", "lines_after_cursor"
    elif arch["has_lines"]:
        why, anchor = "no CURSOR line in the newest file", "no_cursor_line"
    else:
        mode = "start"
    if arch.get("corrupt"):
        why = (why + "; " if why else "") + "a corrupt archive file was set aside as %s" % arch["corrupt"]
    if interrupted and mode == "cursor":
        why = "the previous run (seq %s) was interrupted between members; its lines may repeat" % pend.get("seq")
    if stream is None:
        passes += 1
        stream = Stream("entries=:0:%d" % MAX_FETCH)

    hits, residual_counts = {}, {}
    bufs = {}
    coll = Collapser()
    n_entries = archived = withheld = backsteps = 0
    s300 = 0
    max_sil = 0.0
    long_sil = []
    prev_e = None          # epoch of the previous entry (backward steps)
    hi_e = hi_ts = None    # newest stamp so far: a silence is measured from it, so
                           # lines stamped by a clock that stepped back do not make
                           # the next correct line look like a two-day silence
    last_idx, last_line, last_ts = -1, None, None
    newest_out = None
    head_ts = None
    gap = None
    capped = False
    cur_ts = [None]

    def new_member(day):
        # A rekeying run heads EVERY member it appends with the REKEY line - also
        # a CURSOR-only one, since later runs append new-key tokens to that file -
        # so each day file says where its tokens change key. Nothing written: no
        # REKEY line and no saved key.
        b = bufs[day] = DayBuf()
        if rekey:
            b.add(MARK + "REKEY new pseudonym key (%s): <DEV:..> <DING:..> <CELL:..> <SES:..> tokens "
                  "below this line are not comparable with those above it" % rekey, None)
        return b

    def put(text, ts, held=False):
        day = (ts or cur_ts[0] or day_of_epoch(now))[:10]
        b = bufs.get(day)
        if b is None:
            b = new_member(day)
        b.add(text, ts)
        if held:
            b.withheld += 1

    def out_line(raw, ts):
        nonlocal archived, withheld, newest_out
        try:
            text, kinds = process_line(raw, hits, ps)
        except Exception:  # noqa: BLE001 - ONE line must never stop the archive
            # (fails closed: no text; a run that raised here was retried on the
            # same line every hour until the journal vacuumed it into a GAP)
            kinds = ["redact_error"]
            text = withhold(raw, kinds)
        if kinds:
            withheld += 1
            for k in kinds:
                residual_counts[k] = residual_counts.get(k, 0) + 1
        if census is not None:
            census.feed(text)
        put(text, ts, bool(kinds))
        archived += 1
        if ts and (newest_out is None or ts > newest_out):
            newest_out = ts

    idx = -1
    try:
        for ent in stream.entries():
            idx += 1
            first = ent[0]
            tm = TS_RE.match(first)
            ts = tm.group(1) if tm else None
            if (ts is None and idx == 0) or len(ent) > DUMP_MAX_LINES:
                raise RuntimeError("journal line format not recognised (TS_RE): nothing archived")
            if idx == 0:
                if mode == "cursor":
                    # the anchor itself: already archived, never written again
                    if ts != marker["ts"]:
                        anchor = "ts_mismatch"
                        why = "the cursor's entry carries %s, the archive says %s" % (ts, marker["ts"])
                    if ts:
                        prev_e = hi_e = utc_epoch(ts)
                        hi_ts = ts
                        cur_ts[0] = ts
                    # the archive's newest stamp may be newer than the anchor's
                    # (the anchor was stamped by a clock that stepped back)
                    try:
                        sh = state.get("hi_ts")
                        if sh and (hi_ts is None or sh > hi_ts) and utc_epoch(sh) <= now + 3600:
                            hi_e, hi_ts = utc_epoch(sh), sh
                    except Exception:  # noqa: BLE001
                        pass
                    last_idx, last_line, last_ts = 0, first, ts
                    if why:
                        put(MARK + "RESYNC %s; the lines that follow may repeat lines above" % why, ts)
                    continue
                head_ts = ts
                if mode is None:
                    ref = (marker.get("ts") if marker else None) or arch["newest_ts"]
                    if ref and ts and ts > ref:
                        mode = "gap"         # the journal's head is newer than the archive's end
                        gap = (ref, ts)
                    else:
                        mode = "resync"
                cur_ts[0] = ts
                if mode == "start":
                    put(MARK + "START archive begins at the oldest line the journal still held (%s)" % ts, ts)
                elif mode == "gap":
                    put(MARK + "GAP lines between %s and %s (%.1f min) were not archived: the journal no longer held them"
                        % (gap[0], gap[1], (utc_epoch(gap[1]) - utc_epoch(gap[0])) / 60.0), ts)
                else:
                    put(MARK + "RESYNC %s; the whole retained journal follows from %s; lines up to %s may repeat lines above"
                        % (why, ts, marker["ts"] if marker and marker.get("ts") else "its end"), ts)
            # ---- work budget: stop between entries, never inside a request dump
            if n_entries and coll.idle and (
                    time.time() - t_start > budget
                    or (MAX_ENTRIES_RUN and n_entries >= MAX_ENTRIES_RUN)):
                capped = True
                break
            n_entries += 1
            if ts:
                cur_ts[0] = ts
                e = utc_epoch(ts)
                if prev_e is not None and e < prev_e:
                    backsteps += 1
                if hi_e is not None and e > hi_e:
                    d = e - hi_e
                    max_sil = max(max_sil, d)
                    if d > SILENCE_COUNT_S:
                        s300 += 1
                    if d > SILENCE_FLAG_S:
                        long_sil.append([hi_ts, ts, round(d / 60.0, 1)])
                        put(MARK + "SILENCE %.1f min without a journal line between %s and %s"
                            % (d / 60.0, hi_ts, ts), ts)
                if (hi_e is None or e > hi_e) and e <= now + 3600:
                    hi_e, hi_ts = e, ts     # a line stamped in the future does not move it
                prev_e = e
            for line in ent:
                for raw_out, ts_out in coll.feed(line, ts):
                    out_line(raw_out, ts_out)
            last_idx, last_line, last_ts = idx, first, ts
        for raw_out, ts_out in coll.flush():
            out_line(raw_out, ts_out)
    finally:
        stream.close()
    fetched = stream.lines

    if mode is None:          # the whole-journal fetch returned nothing at all
        mode = "resync"
        notes.append("the journal returned no lines")

    # ---- commit point: the cursor of the last entry consumed, verified
    commit = None
    if n_entries > 0:
        if not stream.first_cursor:
            raise RuntimeError("the log API sent no X-First-Cursor header")
        commit = (cursor_of(stream.first_cursor, last_idx, last_line), last_ts)
    elif mode == "cursor":
        commit = (marker["c"], marker["ts"])

    # ---- write: one gzip member per touched day; the CURSOR line goes LAST, into
    # the newest-named file, so the next run finds it there whatever days this
    # run's timestamps landed on.
    new_bytes = 0
    seq = None
    touched = set()
    day_stats = state.get("days") if isinstance(state.get("days"), dict) else {}
    resync = (mode == "resync") or (mode == "cursor" and why is not None)
    if bufs and commit is None:
        notes.append("nothing committed: no entry could be anchored")
        bufs = {}
    if bufs and not dry:
        seq = max(marker["seq"] if marker else 0, int(state.get("seq") or 0)) + 1
        state["pending"] = {"seq": seq, "at": int(now), "days": sorted(bufs)}
        save_state(state)
        existing = list_days()
        final_day = max(([existing[-1]] if existing else []) + list(bufs))
        if final_day not in bufs:
            new_member(final_day)
        bufs[final_day].add(MARK + "CURSOR seq=%d ts=%s c=%s" % (seq, commit[1] or "-", commit[0]), None)
        for day in sorted(bufs):
            b = bufs[day]
            payload = b.finish()
            new_bytes += len(payload)
            size = append_member(day, payload)
            touched.add(day)
            ds = day_stats.get(day) if isinstance(day_stats.get(day), dict) else {}
            ds["lines"] = int(ds.get("lines", 0)) + b.lines
            ds["withheld"] = int(ds.get("withheld", 0)) + b.withheld
            ds["raw_bytes"] = int(ds.get("raw_bytes", 0)) + b.raw_bytes
            ds["bytes"] = size
            ds["first"] = ds.get("first") or b.first
            ds["last"] = b.last or ds.get("last")
            day_stats[day] = ds
        fsync_dir()
        if rekey:
            notes.append("REKEY: a new pseudonym key started in this run (%s)" % rekey)
        if new_key is not None:
            # AFTER the members (see load_key); a failure costs a REKEY, not a line
            try:
                save_key(new_key)
                state["ps_key_at"] = int(now)
            except OSError as exc:
                warns.append("pseudonym key not saved (%s): the next run starts another key" % type(exc).__name__)
    elif bufs:
        new_bytes = sum(len(b.finish()) for b in bufs.values())

    # ---- clock check and retention (RETAIN_DAYS of the ARCHIVE, not of the wall
    # clock: a single run with the clock 30 days ahead used to delete every file)
    cands = [t for t in (newest_out, hi_ts, state.get("hi_ts") if isinstance(state.get("hi_ts"), str) else None,
                         marker.get("ts") if marker else None, arch["newest_ts"]) if t]
    newest_line = max(cands) if cands else None
    clock_bad = False
    deleted = []
    if newest_line is not None:
        ahead = now - utc_epoch(newest_line)
        if ahead > CLOCK_AHEAD_MAX_S:
            clock_bad = True
            notes.append("wall clock is %.1f h past the newest archived line (%s): the clock jumped or the "
                         "engine has been silent; retention skipped" % (ahead / 3600.0, newest_line))
    if not dry and not clock_bad and newest_line is not None:
        wall_cut = day_of_epoch(now - (RETAIN_DAYS - 1) * 86400)
        arch_cut = day_of_epoch(utc_epoch(newest_line) - (RETAIN_DAYS - 1) * 86400)
        cutoff = min(wall_cut, arch_cut)
        days = list_days()
        keep = set(days[-RETAIN_DAYS:]) | touched
        for d in days:
            if d >= cutoff or d in keep:
                continue
            try:
                # a file written within RETAIN_DAYS stays, whatever its name says
                # (lines stamped by a clock that came up far behind)
                if os.path.getmtime(day_path(d)) > now - RETAIN_DAYS * 86400:
                    continue
                os.remove(day_path(d))
                deleted.append(d)
            except OSError:
                pass
        days = list_days()
        total = sum(os.path.getsize(day_path(d)) for d in days)
        while total > MAX_TOTAL_BYTES and len(days) > 1:
            d = days.pop(0)
            if d in touched:
                break
            total -= os.path.getsize(day_path(d))
            os.remove(day_path(d))
            deleted.append(d)
            notes.append("size cap removed %s" % d)
        present = set(list_days())
        for d in list(day_stats):
            if d not in present:
                day_stats.pop(d, None)
    if deleted:
        notes.append("retention deleted %s" % ",".join(deleted))

    # ---- health (annotations: a failure here is reported, never fatal)
    through = last_ts if n_entries else (marker["ts"] if marker else None)
    oldest = head_ts if mode != "cursor" else None
    if oldest is None:
        try:
            ent, _c = first_entry("entries=:0:1")
            tm = TS_RE.match(ent[0]) if ent else None
            oldest = tm.group(1) if tm else None
        except Exception as exc:  # noqa: BLE001
            warns.append("journal_oldest unavailable (%s)" % type(exc).__name__)
    reach = round((now - utc_epoch(oldest)) / 3600.0, 2) if oldest else None
    lag_now = (now - utc_epoch(last_ts if n_entries and last_ts else marker["ts"])) / 3600.0 \
        if (n_entries and last_ts) or (marker and marker.get("ts")) else None
    if lag_now is not None and lag_now > LAG_WARN_H:
        if mode == "cursor" and n_entries == 0:
            warns.append("no new engine line for %.1f h: the engine is stopped or hung, or its log no longer "
                         "reaches the journal" % lag_now)
        else:
            warns.append("archive %.1f h behind the journal" % lag_now)
    if reach is not None and reach < REACH_WARN_H:
        warns.append("journal reach %.1f h < %.0f h: one missed run from losing lines" % (reach, REACH_WARN_H))
    if long_sil:
        warns.append("%d silence(s) > %d min inside the journal, longest %.1f min"
                     % (len(long_sil), SILENCE_FLAG_S // 60, max(x[2] for x in long_sil)))
    if backsteps:
        warns.append("%d backward timestamp step(s) in this run" % backsteps)
    if coll.verbatim:
        notes.append("%d request dump(s) kept verbatim (outside the collapse grammar)" % coll.verbatim)

    events = clean_events(state)
    if not dry and seq is not None:
        if mode == "gap":
            events.append({"kind": "gap", "at": int(now), "from": gap[0], "to": gap[1],
                           "min": round((utc_epoch(gap[1]) - utc_epoch(gap[0])) / 60.0, 1)})
        if resync:
            events.append({"kind": "resync", "at": int(now), "why": why})
        if withheld:
            events.append({"kind": "withheld", "at": int(now), "n": withheld, "kinds": sorted(residual_counts)})
    if not clock_bad:
        events = [e for e in events if e["at"] >= now - RETAIN_DAYS * 86400]
    events = events[-EVENTS_KEPT:]
    lag = lag_minutes(now, through) if through else None
    files = list_days() if not dry else []
    total_b = sum(os.path.getsize(day_path(d)) for d in files) if files else 0
    today = day_of_epoch(now)
    tstat = day_stats.get(today)

    if mode == "gap":
        status = "gap"
    elif resync:
        status = "resync"
    elif withheld:
        status = "withheld"
    elif clock_bad:
        status = "clock"
    elif capped:
        status = "catchup"
    else:
        status = "ok"
    if capped:
        notes.append("work budget reached after %d entries; the next run continues from the cursor" % n_entries)
    if census is not None:
        notes.append(census.text())
    if not dry:
        state.update({
            "version": 2, "days": day_stats, "events": events,
            "last_run": {"at": int(now), "status": status, "entries": n_entries, "lines": archived,
                         "withheld": withheld, "bytes": new_bytes, "fetched": fetched, "mode": mode,
                         "through": through},
        })
        if seq is not None:
            state["seq"] = seq
            state.pop("pending", None)
        if hi_ts and (not state.get("hi_ts") or hi_ts > str(state.get("hi_ts"))):
            state["hi_ts"] = hi_ts
        save_state(state)
    pub = persisted({"events": events})
    today_info = (dict(day=today, lines=tstat.get("lines"), bytes=tstat.get("bytes"))
                  if isinstance(tstat, dict) else None)
    collapsed = {"dumps": coll.dumps, "lines": coll.dropped, "verbatim": coll.verbatim}
    summary = "%s: +%d lines from %d entries (%d dumps collapsed, %d withheld) through %s, lag %s min; " \
              "journal reach %s h; %d files %.1f MB" % (
                  status, archived, n_entries, coll.dumps, withheld, through, lag, reach,
                  len(files), total_b / 1048576.0)
    if gap is not None:
        summary += "; GAP %s -> %s" % gap
    if resync:
        summary += "; RESYNC: %s" % why
    if warns:
        summary += "; WARN " + "; ".join(warns)
    if notes:
        summary += "; " + "; ".join(notes)
    out = dict(pub)
    out.update({
        "status": status, "archived_through": through, "lag_min": lag,
        "lines_run": archived, "entries_run": n_entries, "withheld_run": withheld,
        "collapsed_run": collapsed, "bytes_run": new_bytes,
        "redaction_run": hits, "residual_run": residual_counts,
        "fetch_lines": fetched, "fetch_passes": passes, "run_s": round(time.time() - t_start, 1),
        "resume": mode, "anchor": anchor, "capped": capped, "ts_backsteps": backsteps,
        "max_silence_s": round(max_sil, 1), "silences_gt300": s300,
        "long_silences": long_sil[-LONG_SILENCES_KEPT:] or None,
        "journal_oldest": oldest, "journal_reach_h": reach,
        "today": today_info,
        "files": len(files), "total_mb": round(total_b / 1048576.0, 2),
        "oldest_day": files[0] if files else None,
        "deleted_days": deleted or None,
        "ha_age_s": None if ha_age is None else int(ha_age),
        "warnings": warns or None,
        "note": "; ".join(notes) or None,
        "summary": summary,
        "updated_at": _bucket(now),
        "dry_run": dry or None,
    })
    emit(out)


# ---------------------------------------------------------------------------
# PSEUDONYM CENSUS over ARCHIVED (redacted) lines - counts only, for --scan and
# --dry-run. A Ring signalling block ('incoming message {' .. '}') with method
# 'sdp' is answered by '[<camera>] setRemoteDescription': measured on the 10-01..
# 10-03 archive, 476 of 476 sdp blocks within 46 ms and never with another sdp
# block in between. That pairs each <DEV:..> token with a camera name (held in
# memory, never printed); every other block carrying the same token is then
# attributable. The mapping must be a bijection: one token per camera.
# ---------------------------------------------------------------------------
PS_TOKEN_RE = re.compile(r"<(DEV|DING|CELL|SES):([^<>\s]*)>")
T_TOKEN_RE = re.compile(r"<T:([^<>\s]*)>")
_PS_SHAPE = dict((t, re.compile(r"[%s]{%d}" % (PS_ALPHABET, n))) for t, n in PS_LEN.items())
_T_SHAPE = re.compile(r"[+-]\d{1,5}\.\d{3}s")
# Key AND token together (one line may carry both stamps). ASCII case folding:
# under Unicode (?i) 's' also matches U+017F, and the lower-cased key then names
# no list (a KeyError that stopped --scan and the dry run).
_T_KEY = re.compile(r"(created|requested)_?at[\"']?\s*[:=]\s*[\"']?<T:([^<>\s]*)>", re.I | re.A)
_T_NUM = re.compile(r"(?:created|requested)_?at[\"']?\s*[:=]\s*[\"']?<NUM>", re.I)
_WEBRTC_RE = re.compile(r"^\[([^\]]{1,64})\] (setRemoteDescription|sendIceCandidate|iceConnectionState)\b")
_METHOD_RE = re.compile(r"^  method: '(\w+)'")
_DEV_KEY_RE = re.compile(r"(?i)(?:doorbot_?id|device_?id)[\"']?\s*[:=]\s*[\"']?<DEV:([a-z]+)>")


class Census:
    def __init__(self):
        self.tokens = dict((t, collections.Counter()) for t in PS_LEN)
        self.bad = 0
        self.t = {"created": [], "requested": []}
        self.t_other = self.t_num = 0
        self.blocks = self.blocks_dev = self.ambiguous = 0
        self.dev_blocks = collections.Counter()
        self.cams = set()
        self.pairs = {}
        self._blk = None
        self._sdp_dev = None
        self.lines = 0

    def feed(self, line):
        self.lines += 1
        m = PREFIX_RE.match(line)
        msg = line[m.end():] if (m and m.group(2)) else line
        if "<" in msg:
            for tm in PS_TOKEN_RE.finditer(msg):
                if _PS_SHAPE[tm.group(1)].fullmatch(tm.group(2)):
                    self.tokens[tm.group(1)][tm.group(2)] += 1
                else:
                    self.bad += 1
            good = 0
            for tm in T_TOKEN_RE.finditer(msg):
                if _T_SHAPE.fullmatch(tm.group(1)):
                    good += 1
                else:
                    self.bad += 1
            for km in _T_KEY.finditer(msg):
                v = km.group(2)
                if _T_SHAPE.fullmatch(v):
                    good -= 1
                    self.t[km.group(1).lower()].append(
                        (1 if v[0] == "+" else -1) * (int(v[1:-5]) * 1000 + int(v[-4:-1])))
            self.t_other += good        # well-formed <T:> under no ding-stamp key
            if _T_NUM.search(msg):
                self.t_num += 1         # a ding stamp that stayed <NUM>
        if msg == "incoming message {":
            self._blk = {"method": None, "dev": None, "n": 0}
            return
        if self._blk is not None and (msg.startswith("[") or self._blk["n"] > DUMP_MAX_LINES * 10):
            self._blk = None        # never closed: a cut block attributes nothing
        if self._blk is not None:
            self._blk["n"] += 1
            if msg == "}":
                blk, self._blk = self._blk, None
                self.blocks += 1
                if blk["dev"]:
                    self.blocks_dev += 1
                    self.dev_blocks[blk["dev"]] += 1
                    if blk["method"] == "sdp":
                        if self._sdp_dev is not None:
                            self.ambiguous += 1
                        self._sdp_dev = blk["dev"]
                return
            mm = _METHOD_RE.match(msg)
            if mm:
                self._blk["method"] = mm.group(1)
            dm = _DEV_KEY_RE.search(msg)
            if dm:
                self._blk["dev"] = dm.group(1)
            return
        wm = _WEBRTC_RE.match(msg)
        if wm:
            self.cams.add(wm.group(1))
            if wm.group(2) == "setRemoteDescription" and self._sdp_dev is not None:
                self.pairs.setdefault(self._sdp_dev, collections.Counter())[wm.group(1)] += 1
                self._sdp_dev = None

    def result(self):
        major = dict((d, c.most_common(1)[0][0]) for d, c in self.pairs.items())
        conflicts = sum(sum(c.values()) - c.most_common(1)[0][1] for c in self.pairs.values())
        mapped_cams = set(major.values())

        def med(v):
            v = sorted(v)
            return round(v[len(v) // 2] / 1000.0, 3) if v else None
        return {
            "tokens": dict((t, [sum(c.values()), len(c)]) for t, c in self.tokens.items()),
            "bad_tokens": self.bad,
            "t_offsets": {"created_at": len(self.t["created"]), "requested_at": len(self.t["requested"]),
                          "other": self.t_other, "stayed_num": self.t_num},
            "t_median_s": {"created_at": med(self.t["created"]), "requested_at": med(self.t["requested"])},
            "blocks": self.blocks, "blocks_dev": self.blocks_dev,
            "blocks_attributable": sum(n for d, n in self.dev_blocks.items() if d in major),
            "dev_distinct": len(self.tokens["DEV"]), "cameras": len(self.cams),
            "sdp_pairs": sum(sum(c.values()) for c in self.pairs.values()),
            "dev_paired": len(major), "cameras_paired": len(mapped_cams),
            "bijective": bool(major) and len(mapped_cams) == len(major) and conflicts == 0,
            "conflicts": conflicts, "ambiguous": self.ambiguous,
        }

    def text(self):
        r = self.result()
        tk = r["tokens"]
        return ("census: DEV %d distinct, cameras %d; sdp pairs %d -> %d DEV x %d cameras, %s, %d conflicts, "
                "%d ambiguous; blocks %d, %d with DEV, %d attributable; DING %d, CELL %d, SES %d distinct; "
                "T created_at %d (median %s s), requested_at %d (median %s s), other %d, stayed <NUM> %d; "
                "bad tokens %d" % (
                    r["dev_distinct"], r["cameras"], r["sdp_pairs"], r["dev_paired"], r["cameras_paired"],
                    "bijective" if r["bijective"] else "NOT bijective", r["conflicts"], r["ambiguous"],
                    r["blocks"], r["blocks_dev"], r["blocks_attributable"], tk["DING"][1], tk["CELL"][1],
                    tk["SES"][1], r["t_offsets"]["created_at"], r["t_median_s"]["created_at"],
                    r["t_offsets"]["requested_at"], r["t_median_s"]["requested_at"], r["t_offsets"]["other"],
                    r["t_offsets"]["stayed_num"], r["bad_tokens"]))


def scan_files(paths):
    """Reviewer tool: residual-scan archive files; prints counts, never lines.
    Marker lines are checked for SHAPE (a CURSOR line may hold only a cursor).
    'census' counts the pseudonym tokens (a malformed one is a bad token): a
    LIST, one census per key segment - a REKEY line starts the next one, since
    tokens on its two sides are not comparable (one camera, two tokens)."""
    res = {}
    for p in paths:
        texts, valid, size, nmem = read_members(p)
        n = w = 0
        kinds = {}
        marks = {}
        bad_marks = 0
        census = [Census()]
        for ln in "".join(texts).split("\n"):
            if not ln:
                continue
            if ln.startswith(MARK):
                k = ln[len(MARK):].split(" ", 1)[0]
                marks[k] = marks.get(k, 0) + 1
                if k == "CURSOR":
                    cm = CURSOR_LINE_RE.match(ln)
                    if not cm or not CURSOR_SHAPE.match(cm.group(3)):
                        bad_marks += 1
                elif residual(_strip_marker_ts(ln)):
                    bad_marks += 1
                if k == "REKEY" and census[-1].lines:
                    census.append(Census())
                continue
            n += 1
            if "<WITHHELD" in ln:
                w += 1
                continue
            for k in residual(ln):
                kinds[k] = kinds.get(k, 0) + 1
            census[-1].feed(ln)
        res[os.path.basename(p)] = {"lines": n, "withheld": w, "residual": kinds, "markers": marks,
                                    "bad_markers": bad_marks, "members": nmem, "complete": valid == size,
                                    "census": [c.result() for c in census]}
    sys.stdout.write(json.dumps(res) + "\n")


_MARK_TS = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+")


def _strip_marker_ts(ln):
    """A GAP/RESYNC/SILENCE/START line legitimately carries timestamps; anything
    else in it must pass the residual scan like a journal line."""
    return _MARK_TS.sub("<TS>", ln)


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args and args[0] == "--scan":
            scan_files(args[1:])
            sys.exit(0)
        _now = os.environ.get("CAMLOG_NOW")   # tests only
        run(dry=bool(args and args[0] == "--dry-run"), now=float(_now) if _now else None)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - JSON-always contract
        _st = _quiet_state()
        _p = persisted(_st) if _st else {}
        emit(dict(_p, status="error", error="%s: %s" % (type(exc).__name__, exc),
                  summary="error: %s" % exc, updated_at=_bucket(time.time())))
