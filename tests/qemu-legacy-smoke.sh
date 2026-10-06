#!/bin/bash
# Update an older install from the new ISO (UPDATE THE INSTALLED SYSTEM, `couchliteos-updater
# apply-disk`), end to end in QEMU:
#   1. install the OLD published ISO (0.2.0, or MoonlightOS 0.1.13) to a blank disk and start it once;
#   2. boot the NEW ISO live with that disk attached (smoke mode disk-update): seed the owner's
#      state in the old layout, ask for the update the way the launcher does, wait for it;
#   3. start from the disk (smoke mode disk-update-check): the state is there under the new names,
#      the old system was saved, the launcher starts, SOFTWARE UPDATE can take over.
# Built from tests/qemu-install-smoke.sh. The old ISO is installed with the same GRUB menu keys and
# preseed as the new one (tests/qemu_iso_boot.py); an old ISO whose menu differs cannot be driven.
#
#   tests/qemu-legacy-smoke.sh OLD.iso [NEW.iso]
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OLD_ISO=${1:?usage: qemu-legacy-smoke.sh OLD.iso [NEW.iso]}
NEW_ISO=${2:-$ROOT/build/out/couchliteos-$(< "$ROOT/VERSION")-amd64.iso}
INSTALL_LOG=${COUCHLITEOS_QEMU_LEGACY_INSTALL_LOG:-/tmp/couchliteos-qemu-legacy-install.log}
BOOT_LOG=${COUCHLITEOS_QEMU_LEGACY_BOOT_LOG:-/tmp/couchliteos-qemu-legacy-boot.log}
SCREENSHOT_DIR=${COUCHLITEOS_QEMU_LEGACY_SCREENSHOTS:-/tmp/couchliteos-qemu-legacy}
# How long the old system gets for its first start (it has no smoke driver this script knows).
FIRST_BOOT=${COUCHLITEOS_QEMU_LEGACY_FIRST_BOOT:-150}
SCALE=${COUCHLITEOS_QEMU_TIMEOUT_SCALE:-1}
[[ $SCALE =~ ^[1-9][0-9]?$ ]] || { echo 'COUCHLITEOS_QEMU_TIMEOUT_SCALE must be 1-99' >&2; exit 64; }
[[ $FIRST_BOOT =~ ^[1-9][0-9]*$ ]] || { echo 'COUCHLITEOS_QEMU_LEGACY_FIRST_BOOT must be seconds' >&2; exit 64; }
export COUCHLITEOS_QEMU_TIMEOUT_SCALE=$SCALE

for command in qemu-system-x86_64 qemu-img python3; do
  command -v "$command" >/dev/null || { echo "$command is required" >&2; exit 127; }
done
for iso in "$OLD_ISO" "$NEW_ISO"; do
  [[ -f $iso ]] || { echo "ISO not found: $iso" >&2; exit 66; }
done

work=$(mktemp -d)
cleanup() {
  [[ -z ${pid:-} ]] || { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }
  [[ -z ${server_pid:-} ]] || { kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }
  find "$work" -depth -delete
}
trap cleanup EXIT
mkdir -p -- "$SCREENSHOT_DIR"

qemu-img create -q -f qcow2 "$work/system.qcow2" 32G

ovmf_code=
ovmf_vars_template=
if [[ -n ${COUCHLITEOS_OVMF_CODE:-} || -n ${COUCHLITEOS_OVMF_VARS:-} ]]; then
  [[ -r ${COUCHLITEOS_OVMF_CODE:-} && -r ${COUCHLITEOS_OVMF_VARS:-} ]] || {
    echo 'Both readable COUCHLITEOS_OVMF_CODE and COUCHLITEOS_OVMF_VARS are required.' >&2
    exit 69
  }
  ovmf_code=$COUCHLITEOS_OVMF_CODE
  ovmf_vars_template=$COUCHLITEOS_OVMF_VARS
fi
for pair in \
  '/usr/share/OVMF/OVMF_CODE_4M.fd|/usr/share/OVMF/OVMF_VARS_4M.fd' \
  '/usr/share/OVMF/OVMF_CODE.fd|/usr/share/OVMF/OVMF_VARS.fd' \
  '/usr/share/pve-edk2-firmware/OVMF_CODE_4M.fd|/usr/share/pve-edk2-firmware/OVMF_VARS_4M.fd'; do
  [[ -z $ovmf_code ]] || break
  code=${pair%%|*}
  vars=${pair#*|}
  if [[ -r $code && -r $vars ]]; then
    ovmf_code=$code
    ovmf_vars_template=$vars
  fi
done
[[ -n $ovmf_code ]] || { echo 'OVMF code/variable pair not found.' >&2; exit 69; }
cp "$ovmf_vars_template" "$work/OVMF_VARS.fd"

common=(
  -m 4096 -smp 4 -machine q35
  -drive "if=pflash,format=raw,unit=0,readonly=on,file=$ovmf_code"
  -drive "if=pflash,format=raw,unit=1,file=$work/OVMF_VARS.fd"
  -drive "file=$work/system.qcow2,if=virtio,format=qcow2"
  -device virtio-vga -display none -no-reboot
)
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
  common=(-enable-kvm -cpu host "${common[@]}")
elif [[ -n ${COUCHLITEOS_QEMU_ACCEL_ARGS:-} ]]; then
  read -r -a accel <<< "$COUCHLITEOS_QEMU_ACCEL_ARGS"
  common=("${accel[@]}" "${common[@]}")
fi
monitor=$work/monitor.sock

# 1. Install the old ISO.
install -D -m 0644 /dev/null "$INSTALL_LOG"
python3 -m http.server 8000 --bind 0.0.0.0 --directory "$ROOT/tests" > "$work/preseed-http.log" 2>&1 &
server_pid=$!
timeout $((25 * SCALE))m qemu-system-x86_64 "${common[@]}" \
  -boot order=d -drive "file=$OLD_ISO,media=cdrom,readonly=on" \
  -netdev user,id=installnet -device e1000,netdev=installnet \
  -serial stdio -monitor "unix:$monitor,server=on,wait=off" > "$INSTALL_LOG" 2>&1 &
pid=$!
if ! python3 "$ROOT/tests/qemu_iso_boot.py" "$monitor" \
  "$SCREENSHOT_DIR/menu.ppm" "$SCREENSHOT_DIR/editor.ppm" "$SCREENSHOT_DIR/installer.ppm" \
  ' auto=true priority=critical preseed/url=http://10.0.2.2:8000/installer-preseed.cfg console=ttyS0,115200n8 DEBIAN_FRONTEND=text'; then
  cat "$INSTALL_LOG"
  echo 'Could not drive the old ISO installer entry (its menu may differ from this one).' >&2
  exit 1
fi
if ! wait "$pid"; then
  pid=
  cat "$INSTALL_LOG"
  echo 'Installing the old ISO failed.' >&2
  exit 1
fi
pid=
kill "$server_pid" 2>/dev/null || true
wait "$server_pid" 2>/dev/null || true
server_pid=
grep -q 'Requesting system reboot' "$INSTALL_LOG" || { cat "$INSTALL_LOG"; echo 'The old installer did not finish.' >&2; exit 1; }

# The old system starts once, so it sets itself up as an owner's box would have.
install -D -m 0644 /dev/null "$BOOT_LOG"
printf '\n=== old system: first start ===\n' >> "$BOOT_LOG"
find "$monitor" -delete 2>/dev/null || true
qemu-system-x86_64 "${common[@]}" -boot order=c \
  -netdev user,id=net0 -device e1000,netdev=net0 \
  -serial stdio -monitor "unix:$monitor,server=on,wait=off" >> "$BOOT_LOG" 2>&1 &
pid=$!
for _ in $(seq 1 $((FIRST_BOOT * SCALE))); do
  kill -0 "$pid" 2>/dev/null || { cat "$BOOT_LOG"; echo 'The old system stopped during its first start.' >&2; exit 1; }
  sleep 1
done
python3 - "$ROOT" "$monitor" "$SCREENSHOT_DIR/old-system.ppm" <<'PY'
import sys
import time

root, monitor_path, screenshot = sys.argv[1:]
sys.path.insert(0, root)
from tests.qemu_iso_boot import connect_monitor

with connect_monitor(monitor_path) as monitor:
    monitor.sendall(f"screendump {screenshot}\n".encode("ascii"))
    time.sleep(1)
    monitor.sendall(b"system_powerdown\n")
    time.sleep(1)
PY
for _ in $(seq 1 $((120 * SCALE))); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
pid=

# 2 and 3. Update it from the new ISO, then start it from the disk.
boot_and_wait() {
  local mode=$1 marker=$2 limit=$3
  local boot=(-boot order=c)
  [[ $mode != disk-update ]] || boot=(-boot order=d -drive "file=$NEW_ISO,media=cdrom,readonly=on")
  printf '\n=== %s ===\n' "$mode" >> "$BOOT_LOG"
  find "$monitor" -delete 2>/dev/null || true
  qemu-system-x86_64 "${common[@]}" "${boot[@]}" -monitor "unix:$monitor,server=on,wait=off" \
    -netdev user,id=net0 -device e1000,netdev=net0 \
    -fw_cfg "name=opt/couchliteos.smoke,string=$mode" \
    -fw_cfg "name=opt/couchliteos.timeout-scale,string=$SCALE" \
    -serial stdio >> "$BOOT_LOG" 2>&1 &
  pid=$!
  for _ in $(seq 1 $((limit * SCALE))); do
    ! grep -q COUCHLITEOS_SMOKE_UPDATE_FAILED "$BOOT_LOG" || break
    if grep -q "$marker" "$BOOT_LOG"; then
      # What the screen shows then (after the update: What's New on the first start).
      python3 -c 'import sys, time; sys.path.insert(0, sys.argv[1]); from tests.qemu_iso_boot import connect_monitor
with connect_monitor(sys.argv[2]) as monitor: monitor.sendall(f"screendump {sys.argv[3]}\n".encode()); time.sleep(1)' \
        "$ROOT" "$monitor" "$SCREENSHOT_DIR/$mode.ppm"
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      pid=
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  cat "$BOOT_LOG"
  echo "$mode did not emit $marker." >&2
  exit 1
}

boot_and_wait disk-update COUCHLITEOS_SMOKE_DISK_UPDATED 2700
grep -q COUCHLITEOS_SMOKE_DISK_SEEDED "$BOOT_LOG" || { cat "$BOOT_LOG"; echo 'The old state was not seeded.' >&2; exit 1; }
boot_and_wait disk-update-check COUCHLITEOS_SMOKE_DISK_UPDATE_READY 240
echo "QEMU legacy smoke test passed: $(basename "$OLD_ISO") installed, updated from $(basename "$NEW_ISO") on the live stick, started from the disk with its state kept."
