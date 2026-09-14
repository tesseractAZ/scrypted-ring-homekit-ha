#!/bin/bash
# Public-repo hygiene sweep.
#
# Instance-specific patterns - entity stems, display names, door-contact names,
# device ids, personal identifiers - are NOT stored in this file: a sweep that
# lists what it guards publishes it. They are loaded from the HYGIENE_PATTERNS
# environment variable (a CI secret) or an untracked local file, in bash syntax,
# each set PLAIN NAMES joined by '|' (any other regex metacharacter is refused):
#   CAMS='stem_one|stem_two'   SPACED='Name One|Name Two'   DOORS='Door One'
#   IDS='101|102'              PERSONAL='username|localpart'
# Locally a missing pattern set is a hard failure (exit 2). In CI it degrades to the
# generic checks with a visible warning: CI runs only after a push has already
# published, so the pre-publication gate is a local pre-push hook, not CI.
#
# Matching notes:
#  - \b fails before '_', so stems are matched without word boundaries.
#  - prose uses space-separated, lower-case names, hence the SPACED set (-i).
#  - "front door" is not identifying in prose, and `front_door_doorbell` is an
#    allowed literal; the stem check still catches the bare entity stem.
#  - exclusions are STRIPPED from a matching line and the line is re-checked, so an
#    allowed substring can never hide a real leak elsewhere on the same line.
#  - quoted time_pattern values ("minutes": "NN") are clock values, not device ids;
#    treating them as ids once forced a sanitizer to publish broken automations.
#  - literals that would match this file itself are split with adjacent quotes,
#    so the sweep can scan its own file instead of excluding it.
#  - -a on BOTH greps: without it a match in a file holding a NUL byte prints no
#    content (BSD grep) or goes to stderr (GNU grep) and the hit is lost.
#  - hits are reported as file:line only, never the matched text, so a CI log
#    cannot republish what the sweep found.
set -u
rc=0
PATTERNS_FILE="${HYGIENE_PATTERNS_FILE:-$HOME/.claude-ha/hygiene-patterns}"
CAMS=''; SPACED=''; DOORS=''; IDS=''; PERSONAL=''
if [ -n "${HYGIENE_PATTERNS:-}" ]; then
  # shellcheck disable=SC1090
  eval "$HYGIENE_PATTERNS"   # not . <(...): process substitution races in bash 3.2
elif [ -r "$PATTERNS_FILE" ]; then
  # shellcheck disable=SC1090
  . "$PATTERNS_FILE"
fi
NAMES=1
if [ -z "$CAMS" ] || [ -z "$SPACED" ] || [ -z "$DOORS" ] || [ -z "$IDS" ] || [ -z "$PERSONAL" ]; then
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "::warning title=Hygiene sweep::instance name patterns are not configured (HYGIENE_PATTERNS secret) - only the generic checks ran"
    NAMES=0
  else
    echo "hygiene-sweep: patterns missing - set HYGIENE_PATTERNS or create $PATTERNS_FILE (never commit it)"
    exit 2
  fi
fi
# A pattern set with a regex metacharacter either makes grep reject it (every scan of
# that set then reads empty, i.e. a false CLEAN) or silently changes what matches.
for p in "$CAMS" "$SPACED" "$DOORS" "$IDS" "$PERSONAL"; do
  case "$p" in *[][\\\(\){}.*+?^\$]*) echo "hygiene-sweep: pattern sets must be plain names joined by '|'"; exit 2 ;; esac
done
scan() {
  local n out
  out=$(grep -rEnia "$2" --exclude-dir=.git --exclude-dir=__pycache__ . 2>/dev/null \
        | sed -E "s#${3:-@@NONE@@}##g" | grep -Eia "$2")
  n=$(printf '%s' "$out" | grep -c . )
  printf "  %-40s %s\n" "$1" "$n"
  if [ "$n" -gt 0 ]; then printf '%s\n' "$out" | cut -d: -f1,2 | head -5 | sed 's/^/      /'; rc=1; fi
}
scan "private IPs"            '(^|[^0-9])(192\.168|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9]'
scan "16+ hex tokens"         '[a-f0-9]{16,}'
scan "addon slug 8hex_name"   '\b[a-f0-9]{8}_[a-z_]+'
scan "home-directory paths"    '/Use''rs/'
scan "real notify target"     'mobile_app_''iphone'
scan "config-entry ULIDs"     '01K[A-Z0-9]{23}'
if [ "$NAMES" -eq 1 ]; then
  scan "camera entity stems"  "($CAMS)"   'front_door_doorbell|<cam_[0-9]+>'
  scan "camera display names" "($SPACED)" '<Scrypted Camera Name [0-9]+>'
  scan "real door names"      "($DOORS)"  '<Door Contact Name [0-9]+>'
  scan "bare device ids"      "(\"|public/)($IDS)(\"|/)" '"(minutes|hours|seconds)": "[0-9]+"'
  scan "personal identifiers" "($PERSONAL)"
fi
[ $rc -eq 0 ] && echo "  RESULT: CLEAN" || echo "  RESULT: LEAKS PRESENT"
exit $rc
