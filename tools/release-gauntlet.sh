#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ISO=${1:-$ROOT/build/out/couchliteos-$(< "$ROOT/VERSION")-amd64.iso}
cd "$ROOT"

for report in \
  "${COUCHLITEOS_QEMU_LOG:-}" \
  "${COUCHLITEOS_QEMU_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_LOG:-}" \
  "${COUCHLITEOS_QEMU_INSTALLED_BOOT_LOG:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_MENU_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_EDITOR_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALLER_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALLED_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_CONFIG:-}" \
  "${COUCHLITEOS_QEMU_PERSISTENCE_LOG:-}"; do
  [[ -z "$report" ]] || rm -f -- "$report"
done

tools/source-state.sh
git diff --check

# The stages do not depend on each other, so they run in three lanes at once: the source checks,
# the live boot then the install, and the stick (persistence) then the update of an older
# install. Each lane has its own temporary directory on disk (an install writes several GB; /tmp
# may be a small RAM disk) and its own preseed port. Peak memory: two 4 GB guests.
# COUCHLITEOS_GAUNTLET_SERIAL=1 runs the stages one after another, as before.
LOGS=${COUCHLITEOS_GAUNTLET_LOGS:-$ROOT/build/gauntlet}
rm -rf -- "$LOGS"
mkdir -p "$LOGS"

stage() {  # stage NAME COMMAND...: run it into $LOGS/NAME.log and say how long it took
  local name=$1 start=$SECONDS status=0
  shift
  "$@" > "$LOGS/$name.log" 2>&1 || status=$?
  if (( status == 0 )); then
    printf '%s: passed in %dm%02ds\n' "$name" $(((SECONDS - start) / 60)) $(((SECONDS - start) % 60))
  else
    printf '%s: FAILED (exit %d) after %dm%02ds; the end of %s:\n' "$name" "$status" \
      $(((SECONDS - start) / 60)) $(((SECONDS - start) % 60)) "$LOGS/$name.log"
    tail -n 40 "$LOGS/$name.log" | sed 's/^/    /'
  fi
  return "$status"
}

lane() {  # lane NAME PORT: a temporary directory and preseed port of its own
  mkdir -p "$LOGS/tmp-$1"
  export TMPDIR=$LOGS/tmp-$1 COUCHLITEOS_PRESEED_PORT=$2
}

legacy() {
  # Updating an older install needs that older ISO (OLD_ISO=..., for example 0.2.0).
  if [[ -n ${OLD_ISO:-} ]]; then
    make qemu-legacy-smoke ISO="$ISO" OLD_ISO="$OLD_ISO"
  else
    printf 'qemu-legacy-smoke: skipped: OLD_ISO not set\n'
  fi
}

checks_lane() {
  lane checks 8010
  stage make-test make test && stage mutants python3 tools/mutants.py
}
live_lane() {
  lane live 8011
  stage qemu-smoke make qemu-smoke ISO="$ISO" && stage qemu-install-smoke make qemu-install-smoke ISO="$ISO"
}
stick_lane() {
  lane stick 8012
  stage qemu-persistence-smoke make qemu-persistence-smoke ISO="$ISO" && stage qemu-legacy-smoke legacy
}

start=$SECONDS
failed=0
if [[ ${COUCHLITEOS_GAUNTLET_SERIAL:-} == 1 ]]; then
  { checks_lane && live_lane && stick_lane; } || failed=1
else
  # Each lane stops at its own first failure; the others finish, so one run shows every problem.
  ( checks_lane ) & checks=$!
  ( live_lane ) & live=$!
  ( stick_lane ) & stick=$!
  for pid in "$checks" "$live" "$stick"; do
    wait "$pid" || failed=1
  done
fi
rm -rf -- "$LOGS"/tmp-*
printf 'gauntlet: %dm%02ds in all; logs in %s\n' $(((SECONDS - start) / 60)) $(((SECONDS - start) % 60)) "$LOGS"
(( failed == 0 )) || { printf 'CouchLiteOS release gauntlet FAILED.\n' >&2; exit 1; }
printf 'CouchLiteOS release gauntlet passed.\n'
