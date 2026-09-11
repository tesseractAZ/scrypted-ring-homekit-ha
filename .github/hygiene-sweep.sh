#!/bin/bash
# Public-repo hygiene sweep. Three holes this fixes over the ad-hoc version:
#  1. \b fails before '_', so `garage_north_motion` never matched \bgarage_north\b
#  2. camera-name patterns had been dropped from the pattern list mid-session
#  3. prose uses space-separated, lower-case names that no \w pattern catches
# "front door" is deliberately NOT treated as identifying in prose: every house has
# one, it is an allowed literal in `front_door_doorbell`, and it appears in generic
# spoken phrases. Its ENTITY STEM is still caught by the underscore-safe check.
set -u
CAMS='front_door|garage_south|garage_west|garage_north|northeast_corner|patio_south|garage_workroom|garage_bay|patio_east'
SPACED='Garage South|Garage West|Garage North|Northeast Corner|Patio South|Garage Workroom|Garage Bay|Patio East'
DOORS='South Patio Door|East Pool Patio Door|Garage Back Door'
rc=0
scan() {
  local n out
  # --exclude the sweep itself: its own patterns necessarily contain the names,
  # which would make it permanently self-flagging.
  out=$(grep -rEni "$2" --exclude-dir=.git --exclude-dir=__pycache__ \
          --exclude=hygiene-sweep.sh . 2>/dev/null | grep -vE "${3:-@@NONE@@}")
  n=$(printf '%s' "$out" | grep -c . )
  printf "  %-40s %s\n" "$1" "$n"
  if [ "$n" -gt 0 ]; then printf '%s\n' "$out" | head -5 | cut -c1-150 | sed 's/^/      /'; rc=1; fi
}
scan "private IPs"            '(^|[^0-9])(192\.168|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9]'
scan "16+ hex tokens"         '[a-f0-9]{16,}'
scan "addon slug 8hex_name"   '\b[a-f0-9]{8}_[a-z_]+'
scan "camera entity stems"    "($CAMS)" 'front_door_doorbell|<cam_|doorbell_announce'
scan "camera display names"   "($SPACED)" 'Scrypted Camera Name'
scan "real door names"        "($DOORS)" '<Door Contact Name>|<door contact>'
scan "bare device ids"        '"(28|29|30|31|34|38|41|44|47)"'
scan "/Users/ paths"          '/Users/'
scan "personal email"         'epaschal|ericpaschal'
scan "real notify target"     'mobile_app_iphone'
scan "config-entry ULIDs"     '01K[A-Z0-9]{23}'
[ $rc -eq 0 ] && echo "  RESULT: CLEAN" || echo "  RESULT: LEAKS PRESENT"
exit $rc
