#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ISO=${1:-$ROOT/build/out/couchliteos-$(< "$ROOT/VERSION")-amd64.iso}
INSTALL_LOG=${COUCHLITEOS_QEMU_INSTALL_LOG:-/tmp/couchliteos-qemu-install.log}
BOOT_LOG=${COUCHLITEOS_QEMU_INSTALLED_BOOT_LOG:-/tmp/couchliteos-qemu-installed-boot.log}
MENU_SCREENSHOT=${COUCHLITEOS_QEMU_INSTALL_MENU_SCREENSHOT:-/tmp/couchliteos-qemu-install-menu.ppm}
EDITOR_SCREENSHOT=${COUCHLITEOS_QEMU_INSTALL_EDITOR_SCREENSHOT:-/tmp/couchliteos-qemu-install-editor.ppm}
INSTALLER_SCREENSHOT=${COUCHLITEOS_QEMU_INSTALLER_SCREENSHOT:-/tmp/couchliteos-qemu-installer.ppm}
INSTALLED_SCREENSHOT=${COUCHLITEOS_QEMU_INSTALLED_SCREENSHOT:-/tmp/couchliteos-qemu-installed-launcher.ppm}
CONFIG_LOG=${COUCHLITEOS_QEMU_INSTALL_CONFIG:-/tmp/couchliteos-qemu-install-config.log}
# Slow hosts (for example nested software emulation) may stretch every timeout.
SCALE=${COUCHLITEOS_QEMU_TIMEOUT_SCALE:-1}
[[ $SCALE =~ ^[1-9][0-9]?$ ]] || { echo 'COUCHLITEOS_QEMU_TIMEOUT_SCALE must be 1-99' >&2; exit 64; }
export COUCHLITEOS_QEMU_TIMEOUT_SCALE=$SCALE

for command in qemu-system-x86_64 qemu-img python3; do
  command -v "$command" >/dev/null || { echo "$command is required" >&2; exit 127; }
done
[[ -f "$ISO" ]] || { echo "ISO not found: $ISO" >&2; exit 66; }

work=$(mktemp -d)
cleanup() {
  [[ -z ${pid:-} ]] || { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }
  [[ -z ${server_pid:-} ]] || { kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }
  find "$work" -depth -delete
}
trap cleanup EXIT

for screenshot in "$MENU_SCREENSHOT" "$EDITOR_SCREENSHOT" "$INSTALLER_SCREENSHOT" "$INSTALLED_SCREENSHOT"; do
  mkdir -p -- "$(dirname "$screenshot")"
  rm -f -- "$screenshot"
done

[[ ! -e $work/system.qcow2 ]]
qemu-img create -q -f qcow2 "$work/system.qcow2" 32G
virtual_size=$(qemu-img info --output=json "$work/system.qcow2" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')
[[ $virtual_size == 34359738368 ]] || { echo 'Disposable disk is not 32 GiB.' >&2; exit 1; }

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
  if [[ -r "$code" && -r "$vars" ]]; then
    ovmf_code=$code
    ovmf_vars_template=$vars
    break
  fi
done
[[ -n "$ovmf_code" ]] || { echo 'OVMF code/variable pair not found.' >&2; exit 69; }
cp "$ovmf_vars_template" "$work/OVMF_VARS.fd"

common=(
  -m 4096 -smp 4 -machine q35
  -drive "if=pflash,format=raw,unit=0,readonly=on,file=$ovmf_code"
  -drive "if=pflash,format=raw,unit=1,file=$work/OVMF_VARS.fd"
  -drive "file=$work/system.qcow2,if=virtio,format=qcow2"
  -device virtio-vga -display none -no-reboot
)
# Without KVM, COUCHLITEOS_QEMU_ACCEL_ARGS can name another accelerator, for
# example "-accel whpx,kernel-irqchip=off -cpu max" with QEMU on Windows.
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
  common=(-enable-kvm -cpu host "${common[@]}")
elif [[ -n ${COUCHLITEOS_QEMU_ACCEL_ARGS:-} ]]; then
  read -r -a accel <<< "$COUCHLITEOS_QEMU_ACCEL_ARGS"
  common=("${accel[@]}" "${common[@]}")
fi

# Hosts without Unix-domain sockets in Python can use local TCP monitors.
if [[ -n ${COUCHLITEOS_QEMU_MONITOR_PORT:-} ]]; then
  install_monitor=tcp:127.0.0.1:$COUCHLITEOS_QEMU_MONITOR_PORT
  installed_monitor=tcp:127.0.0.1:$((COUCHLITEOS_QEMU_MONITOR_PORT + 1))
  install_monitor_option="$install_monitor,server=on,wait=off"
  installed_monitor_option="$installed_monitor,server=on,wait=off"
else
  install_monitor=$work/monitor.sock
  installed_monitor=$work/installed-monitor.sock
  install_monitor_option="unix:$install_monitor,server=on,wait=off"
  installed_monitor_option="unix:$installed_monitor,server=on,wait=off"
fi

install -D -m 0644 /dev/null "$INSTALL_LOG"
install -D -m 0644 /dev/null "$CONFIG_LOG"
printf 'blank_disk=true\nvirtual_size=%s\n' "$virtual_size" >> "$CONFIG_LOG"
python3 -m http.server 8000 --bind 0.0.0.0 --directory "$ROOT/tests" \
  > "$work/preseed-http.log" 2>&1 &
server_pid=$!
if ! python3 "$ROOT/tests/preseed_ready.py" "$ROOT/tests/installer-preseed.cfg"; then
  cat "$work/preseed-http.log" >&2
  exit 1
fi

printf '%q ' qemu-system-x86_64 "${common[@]}" \
  -boot order=d \
  -drive "file=$ISO,media=cdrom,readonly=on" \
  -netdev user,id=installnet -device e1000,netdev=installnet \
  -serial stdio -monitor "$install_monitor_option" \
  > "$CONFIG_LOG"
printf '\n' >> "$CONFIG_LOG"
qemu-img info "$work/system.qcow2" >> "$CONFIG_LOG"

timeout $((25 * SCALE))m qemu-system-x86_64 \
  "${common[@]}" \
  -boot order=d \
  -drive "file=$ISO,media=cdrom,readonly=on" \
  -netdev user,id=installnet -device e1000,netdev=installnet \
  -serial stdio -monitor "$install_monitor_option" \
  > "$INSTALL_LOG" 2>&1 &
pid=$!

if ! python3 "$ROOT/tests/qemu_iso_boot.py" \
  "$install_monitor" "$MENU_SCREENSHOT" "$EDITOR_SCREENSHOT" "$INSTALLER_SCREENSHOT" \
  ' auto=true priority=critical preseed/url=http://10.0.2.2:8000/installer-preseed.cfg console=ttyS0,115200n8 DEBIAN_FRONTEND=text'; then
  cat "$INSTALL_LOG"
  echo 'Could not drive the ISO installer entry.' >&2
  exit 1
fi

if ! wait "$pid"; then
  pid=
  cat "$INSTALL_LOG"
  echo 'Automated UEFI ISO installation failed.' >&2
  exit 1
fi
pid=
kill "$server_pid" 2>/dev/null || true
wait "$server_pid" 2>/dev/null || true
server_pid=
grep -q 'Requesting system reboot' "$INSTALL_LOG" || {
  cat "$INSTALL_LOG"
  echo 'Installer exited without completing.' >&2
  exit 1
}

install -D -m 0644 /dev/null "$BOOT_LOG"
boot_and_wait() {
  local mode=$1 marker=$2
  local monitor=(-monitor none) extra=() limit=240
  if [[ $mode == persistence-write ]]; then
    find "$work/installed-monitor.sock" -delete 2>/dev/null || true
    monitor=(-monitor "$installed_monitor_option")
  fi
  if [[ $mode == update-* || $mode == restore-* ]]; then
    # The update boots have the release ISO as a CD-ROM (/dev/sr0) but still start from the disk.
    extra=(-boot order=c -drive "file=$ISO,media=cdrom,readonly=on")
  fi
  # Saving the old system, copying the new one and rebuilding the initramfs take far longer than a boot.
  [[ $mode != update-apply && $mode != restore-run ]] || limit=2700
  printf '\n=== installed boot: %s ===\n' "$mode" >> "$BOOT_LOG"
  qemu-system-x86_64 \
    "${common[@]}" "${monitor[@]}" "${extra[@]}" \
    -netdev user,id=net0 -device e1000,netdev=net0 \
    -fw_cfg "name=opt/couchliteos.smoke,string=$mode" \
    -fw_cfg "name=opt/couchliteos.timeout-scale,string=$SCALE" \
    -serial stdio >> "$BOOT_LOG" 2>&1 &
  pid=$!
  for _ in $(seq 1 $((limit * SCALE))); do
    # The smoke script says so when an update mode fails; no need to wait out the timeout.
    ! grep -q COUCHLITEOS_SMOKE_UPDATE_FAILED "$BOOT_LOG" || break
    if grep -q "$marker" "$BOOT_LOG"; then
      if [[ $mode == persistence-write ]]; then
        python3 - "$ROOT" "$installed_monitor" "$INSTALLED_SCREENSHOT" <<'PY'
import sys
import time

root, monitor_path, screenshot = sys.argv[1:]
sys.path.insert(0, root)
from tests.qemu_iso_boot import connect_monitor

with connect_monitor(monitor_path) as monitor:
    monitor.sendall(f"screendump {screenshot}\n".encode("ascii"))
    time.sleep(1)
PY
      fi
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      pid=
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      # The guest restarted (-no-reboot ends QEMU there): what restore-apply and restore-run end with.
      wait "$pid" 2>/dev/null || true
      pid=
      if [[ $mode == restore-* ]] && grep -q "$marker" "$BOOT_LOG"; then
        return 0
      fi
      break
    fi
    sleep 1
  done
  cat "$BOOT_LOG"
  echo "Installed system did not emit $marker." >&2
  exit 1
}

boot_and_wait persistence-write COUCHLITEOS_SMOKE_PERSISTENCE_WRITTEN
grep -q COUCHLITEOS_SMOKE_INSTALLED_DISK_READY "$BOOT_LOG" || {
  cat "$BOOT_LOG"
  echo 'Installed root filesystem identity was not verified.' >&2
  exit 1
}
boot_and_wait persistence-read COUCHLITEOS_SMOKE_PERSISTENCE_READY
# Update the installed system from the same ISO (forced, no reboot), then boot the result: the
# installer's GRUB and shim, the maintenance user and the owner's saved state must all survive.
boot_and_wait update-apply COUCHLITEOS_SMOKE_UPDATE_APPLIED
boot_and_wait update-check COUCHLITEOS_SMOKE_UPDATE_READY
# Restore the version the update saved: Settings' request, then the boot service restores and
# restarts on its own, then the restored system must be the saved one with its settings.
boot_and_wait restore-apply COUCHLITEOS_SMOKE_RESTORE_REQUESTED
boot_and_wait restore-run 'RESTORED COUCHLITEOS'
boot_and_wait restore-check COUCHLITEOS_SMOKE_RESTORE_READY
for screenshot in "$MENU_SCREENSHOT" "$EDITOR_SCREENSHOT" "$INSTALLER_SCREENSHOT" "$INSTALLED_SCREENSHOT"; do
  [[ -s $screenshot ]] || { echo "Screenshot evidence missing: $screenshot" >&2; exit 1; }
done
echo 'QEMU install smoke test passed: firmware and ISO menu install, independent disk boot, launcher readiness, configuration persistence across a cold reboot, an in-place update that boots with everything kept, and a restore of the saved version.'
