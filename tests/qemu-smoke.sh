#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ISO=${1:-$ROOT/build/out/couchliteos-$(< "$ROOT/VERSION")-amd64.iso}
SCREENSHOT=${COUCHLITEOS_QEMU_SCREENSHOT:-/tmp/couchliteos-qemu-smoke.ppm}
# Slow hosts (for example nested software emulation) may stretch every timeout.
SCALE=${COUCHLITEOS_QEMU_TIMEOUT_SCALE:-1}
[[ $SCALE =~ ^[1-9][0-9]?$ ]] || { echo 'COUCHLITEOS_QEMU_TIMEOUT_SCALE must be 1-99' >&2; exit 64; }
command -v qemu-system-x86_64 >/dev/null || { echo 'qemu-system-x86_64 is required' >&2; exit 127; }
[[ -f "$ISO" ]] || { echo "ISO not found: $ISO" >&2; exit 66; }

if [[ -n ${COUCHLITEOS_QEMU_LOG:-} ]]; then
  log=$COUCHLITEOS_QEMU_LOG
  install -D -m 0644 /dev/null "$log"
else
  log=$(mktemp)
fi
temporary_files=()
[[ -z ${COUCHLITEOS_QEMU_LOG:-} ]] && temporary_files+=("$log")
monitor_socket=$(mktemp /tmp/couchliteos-qemu-monitor.XXXXXX)
find "$monitor_socket" -delete
# Hosts without Unix-domain sockets in Python can use a local TCP monitor.
if [[ -n ${COUCHLITEOS_QEMU_MONITOR_PORT:-} ]]; then
  monitor_address=tcp:127.0.0.1:$COUCHLITEOS_QEMU_MONITOR_PORT
  monitor_option="$monitor_address,server=on,wait=off"
else
  monitor_address=$monitor_socket
  monitor_option="unix:$monitor_socket,server=on,wait=off"
fi
cleanup() {
  ((${#temporary_files[@]} == 0)) || find "${temporary_files[@]}" -delete
  [[ ! -e "$monitor_socket" ]] || find "$monitor_socket" -delete
}
trap cleanup EXIT

if command -v xorriso >/dev/null; then
  boot_extract=$(mktemp -d)
  temporary_files+=("$boot_extract")
  grub_cfg=$boot_extract/grub.cfg
  xorriso -osirrox on -indev "$ISO" -extract /boot/grub/grub.cfg "$grub_cfg" >/dev/null 2>&1
  grub_listing=$(xorriso -indev "$ISO" \
    -find /boot/grub/grub.cfg -exec lsdl -- 2>/dev/null)
  grep -q '^-r-xr-xr-x' <<< "$grub_listing" || {
    printf 'ISO GRUB config lost mode 0555: %s\n' "$grub_listing" >&2
    exit 65
  }
  grep -q '^set default=0$' "$grub_cfg" || { echo 'ISO GRUB config lacks the default entry' >&2; exit 65; }
  grep -q '^set timeout=3$' "$grub_cfg" || { echo 'ISO GRUB config lacks the appliance timeout' >&2; exit 65; }
  grep -q '^serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1$' "$grub_cfg" || { echo 'ISO GRUB config lacks serial setup' >&2; exit 65; }
  grep -q '^terminal_input console serial$' "$grub_cfg" || { echo 'ISO GRUB config lacks serial input' >&2; exit 65; }
  grep -q '^terminal_output console serial$' "$grub_cfg" || { echo 'ISO GRUB config lacks serial output' >&2; exit 65; }
  grep -q '^menuentry "Start CouchLiteOS"' "$grub_cfg" || { echo 'ISO GRUB config lacks the CouchLiteOS live entry' >&2; exit 65; }
  grep -q '^menuentry "Start CouchLiteOS (No Persistence)"' "$grub_cfg" || { echo 'ISO GRUB config lacks the recovery live entry' >&2; exit 65; }
  grep -q 'boot=live.*components.*persistence.*ipv6.disable=1.*console=tty1.*console=ttyS0,115200n8' "$grub_cfg" || { echo 'ISO GRUB config lacks expected live boot arguments' >&2; exit 65; }
  grep -q 'boot=live.*components.*nopersistence.*ipv6.disable=1' "$grub_cfg" || { echo 'ISO GRUB recovery entry does not disable persistence' >&2; exit 65; }
  installer_cfg=$boot_extract/install_start.cfg
  xorriso -osirrox on -indev "$ISO" -extract /boot/grub/install_start.cfg "$installer_cfg" >/dev/null 2>&1
  grep -q "menuentry 'Install CouchLiteOS'" "$installer_cfg" || { echo 'ISO GRUB config lacks the CouchLiteOS installer entry' >&2; exit 65; }
  grep -q 'vga=788 theme=dark ipv6.disable=1 --- quiet' "$installer_cfg" || { echo 'ISO installer does not use the dark text theme' >&2; exit 65; }
  theme_cfg=$boot_extract/theme.cfg
  xorriso -osirrox on -indev "$ISO" -extract /boot/grub/theme.cfg "$theme_cfg" >/dev/null 2>&1
  grep -q '^set color_normal=white/black$' "$theme_cfg" || { echo 'ISO GRUB normal colors are not high contrast' >&2; exit 65; }
  grep -q '^set color_highlight=black/white$' "$theme_cfg" || { echo 'ISO GRUB selected colors are not inverted' >&2; exit 65; }
  ! grep -q '^set theme=' "$theme_cfg" || { echo 'ISO still enables the graphical Debian GRUB theme' >&2; exit 65; }
fi

capture_screen() {
  python3 - "$ROOT" "$monitor_address" "$SCREENSHOT" <<'PY' || true
import sys
import time

root, monitor, screenshot = sys.argv[1:]
sys.path.insert(0, root)
from tests.qemu_iso_boot import connect_monitor

client = connect_monitor(monitor, timeout=2)
client.settimeout(2)
client.recv(4096)
client.sendall(f"screendump {screenshot}\n".encode())
time.sleep(1)
client.close()
PY
}

args=(
  -m 3072 -smp 2 -boot d -cdrom "$ISO"
  -device virtio-vga -display none -serial stdio -no-reboot
  -monitor "$monitor_option"
  -netdev "user,id=net0" -device "e1000,netdev=net0"
  -fw_cfg "name=opt/couchliteos.smoke,string=apps"
  -fw_cfg "name=opt/couchliteos.timeout-scale,string=$SCALE"
)
# Without KVM, COUCHLITEOS_QEMU_ACCEL_ARGS can name another accelerator, for
# example "-accel whpx,kernel-irqchip=off -cpu max" with QEMU on Windows.
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
  args=(-enable-kvm -cpu host "${args[@]}")
elif [[ -n ${COUCHLITEOS_QEMU_ACCEL_ARGS:-} ]]; then
  read -r -a accel <<< "$COUCHLITEOS_QEMU_ACCEL_ARGS"
  args=("${accel[@]}" "${args[@]}")
fi

# OVMF requires a private writable variable store in addition to its read-only
# code image. A code-only pflash drive can stall before GRUB with no serial log.
ovmf_code=
ovmf_vars_template=
if [[ -n ${COUCHLITEOS_OVMF_CODE:-} || -n ${COUCHLITEOS_OVMF_VARS:-} ]]; then
  [[ -r ${COUCHLITEOS_OVMF_CODE:-} && -r ${COUCHLITEOS_OVMF_VARS:-} ]] || {
    echo 'Both readable COUCHLITEOS_OVMF_CODE and COUCHLITEOS_OVMF_VARS are required.' >&2
    exit 69
  }
  ovmf_code=$COUCHLITEOS_OVMF_CODE
  ovmf_vars_template=$COUCHLITEOS_OVMF_VARS
else
  for pair in \
    '/usr/share/OVMF/OVMF_CODE_4M.fd|/usr/share/OVMF/OVMF_VARS_4M.fd' \
    '/usr/share/OVMF/OVMF_CODE.fd|/usr/share/OVMF/OVMF_VARS.fd' \
    '/usr/share/pve-edk2-firmware/OVMF_CODE_4M.fd|/usr/share/pve-edk2-firmware/OVMF_VARS_4M.fd'; do
    code=${pair%%|*}
    vars=${pair#*|}
    if [[ -r "$code" && -r "$vars" ]]; then
      ovmf_code=$code
      ovmf_vars_template=$vars
      break
    fi
  done
fi
if [[ -n "$ovmf_code" ]]; then
  ovmf_vars=$(mktemp)
  temporary_files+=("$ovmf_vars")
  cp "$ovmf_vars_template" "$ovmf_vars"
  args=(
    -drive "if=pflash,format=raw,unit=0,readonly=on,file=$ovmf_code"
    -drive "if=pflash,format=raw,unit=1,file=$ovmf_vars"
    "${args[@]}"
  )
else
  echo 'OVMF code/variable pair not found; UEFI smoke test cannot run.' >&2
  exit 69
fi

qemu-system-x86_64 "${args[@]}" > "$log" 2>&1 &
pid=$!

wait_for_marker() {
  local marker=$1 timeout=$(($2 * SCALE))
  for _ in $(seq 1 "$timeout"); do
    grep -q "$marker" "$log" && return 0
    kill -0 "$pid" 2>/dev/null || return 1
    sleep 1
  done
  return 1
}

fail() {
  capture_screen
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  cat "$log"
  echo "$1" >&2
  exit 1
}

wait_for_marker 'COUCHLITEOS_LAUNCHER_READY' 180 || fail 'Launcher did not become ready.'
capture_screen
wait_for_marker 'COUCHLITEOS_SMOKE_HWDETECT_READY' 30 || fail 'Hardware detection did not run, or a module failed to load.'
wait_for_marker 'COUCHLITEOS_SMOKE_CONFIGURED_PLATFORM_READY' 30 || fail 'Configured applications, OSK, or setup-ready ordering failed.'
wait_for_marker 'COUCHLITEOS_SMOKE_USBIP_READY' 30 || fail 'USB/IP daemon did not remain active.'
wait_for_marker 'COUCHLITEOS_SMOKE_BLUETOOTH_READY' 30 || fail 'Bluetooth control service or launcher-survival check failed.'

# A QEMU fw_cfg flag activates the otherwise inert smoke driver inside the
# guest. It reports success only after all three real application processes
# have remained alive for five seconds.
wait_for_marker 'COUCHLITEOS_SMOKE_APPS_READY' 180 || fail 'Applications did not remain running.'
capture_screen
wait_for_marker 'COUCHLITEOS_SMOKE_RDP_CLIENT_STARTED' 120 || fail 'Remote Desktop session did not start sdl-freerdp3.'
wait_for_marker 'COUCHLITEOS_SMOKE_RDP_READY' 180 || fail 'Remote Desktop restart, cleanup, password, or persistence checks failed.'

kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
echo 'QEMU smoke test passed: no-backend live boot, launcher/setup readiness, configured apps, OSK, Bluetooth, USB/IP, Moonlight, Chiaki-ng, Firefox, Google Chrome, and Remote Desktop started.'
