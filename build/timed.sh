#!/bin/bash
# Build stage timings, collected in $COUCHLITEOS_TIMINGS (one "NAME SECONDS" line per stage).
#   build/timed.sh NAME COMMAND [ARG...]  run COMMAND, then record NAME with its wall time
#   build/timed.sh --record NAME SECONDS  record a stage that was timed elsewhere
#   build/timed.sh --summary              print the table; NAME with a / is part of the stage
#                                         before the /, so it is not added to the total
# Without COUCHLITEOS_TIMINGS the time is printed but not recorded.
set -Eeuo pipefail

duration() {
  printf '%dm%02ds' $(($1 / 60)) $(($1 % 60))
}

record() {
  printf 'couchliteos-time: %s %s (%s)\n' "$1" "$2" "$(duration "$2")"
  [[ -z ${COUCHLITEOS_TIMINGS:-} ]] || printf '%s %s\n' "$1" "$2" >> "$COUCHLITEOS_TIMINGS"
}

case ${1:-} in
  --record)
    [[ $# -eq 3 && $3 =~ ^[0-9]+$ ]] || { echo 'usage: timed.sh --record NAME SECONDS' >&2; exit 64; }
    record "$2" "$3"
    ;;
  --summary)
    [[ -s ${COUCHLITEOS_TIMINGS:-} ]] || exit 0
    total=0
    printf '\nBuild stage times:\n'
    while read -r name seconds; do
      if [[ $name == */* ]]; then
        printf '    %-22s %6ss  %s\n' "${name#*/}" "$seconds" "$(duration "$seconds")"
      else
        printf '  %-24s %6ss  %s\n' "$name" "$seconds" "$(duration "$seconds")"
        total=$((total + seconds))
      fi
    done < "$COUCHLITEOS_TIMINGS"
    printf '  %-24s %6ss  %s\n' total "$total" "$(duration "$total")"
    ;;
  ''|-*)
    echo 'usage: timed.sh NAME COMMAND [ARG...] | --record NAME SECONDS | --summary' >&2
    exit 64
    ;;
  *)
    name=$1
    shift
    (($#)) || { echo 'timed.sh: no command given' >&2; exit 64; }
    start=$SECONDS
    "$@"
    record "$name" $((SECONDS - start))
    ;;
esac
