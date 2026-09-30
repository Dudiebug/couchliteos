#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ISO=${1:-$ROOT/build/out/moonlightos-$(< "$ROOT/VERSION")-amd64.iso}
LOG=${MOONLIGHTOS_QEMU_PERSISTENCE_LOG:-/tmp/moonlightos-qemu-persistence.log}
# Slow hosts (for example nested software emulation) may stretch every timeout.
SCALE=${MOONLIGHTOS_QEMU_TIMEOUT_SCALE:-1}
[[ $SCALE =~ ^[1-9][0-9]?$ ]] || { echo 'MOONLIGHTOS_QEMU_TIMEOUT_SCALE must be 1-99' >&2; exit 64; }

command -v qemu-system-x86_64 >/dev/null || { echo 'qemu-system-x86_64 is required' >&2; exit 127; }
# Hosts without e2fsprogs may supply a pristine image made by the same mke2fs
# command below; hosts without xorriso may extract with libarchive's bsdtar.
if ! command -v mke2fs >/dev/null && [[ ! -r ${MOONLIGHTOS_QEMU_PERSISTENCE_IMAGE:-} ]]; then
  echo 'mke2fs (or MOONLIGHTOS_QEMU_PERSISTENCE_IMAGE) is required' >&2
  exit 127
fi
BSDTAR=${MOONLIGHTOS_BSDTAR:-bsdtar}
command -v xorriso >/dev/null || command -v "$BSDTAR" >/dev/null || {
  echo 'xorriso or bsdtar is required' >&2
  exit 127
}
[[ -f "$ISO" ]] || { echo "ISO not found: $ISO" >&2; exit 66; }

work=$(mktemp -d)
cleanup() {
  [[ -z ${pid:-} ]] || { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }
  find "$work" -depth -delete
}
trap cleanup EXIT

mkdir "$work/root"
cat > "$work/root/persistence.conf" <<'EOF'
/var/lib/moonlightos source=moonlightos-state
/var/log/moonlightos source=moonlightos-logs
/var/lib/tailscale source=tailscale-state
/var/lib/bluetooth source=bluetooth-state
EOF
if command -v mke2fs >/dev/null; then
  truncate -s 768M "$work/persistence.img"
  mke2fs -q -F -t ext4 -L persistence -d "$work/root" "$work/persistence.img"
else
  cp -- "$MOONLIGHTOS_QEMU_PERSISTENCE_IMAGE" "$work/persistence.img"
fi
if command -v xorriso >/dev/null; then
  xorriso -osirrox on -indev "$ISO" \
    -extract /live/vmlinuz "$work/vmlinuz" \
    -extract /live/initrd.img "$work/initrd.img" >/dev/null 2>&1
else
  "$BSDTAR" -xf "$ISO" -C "$work" live/vmlinuz live/initrd.img
  mv -- "$work/live/vmlinuz" "$work/live/initrd.img" "$work/"
fi

ovmf_code=
ovmf_vars_template=
if [[ -n ${MOONLIGHTOS_OVMF_CODE:-} || -n ${MOONLIGHTOS_OVMF_VARS:-} ]]; then
  [[ -r ${MOONLIGHTOS_OVMF_CODE:-} && -r ${MOONLIGHTOS_OVMF_VARS:-} ]] || {
    echo 'Both readable MOONLIGHTOS_OVMF_CODE and MOONLIGHTOS_OVMF_VARS are required.' >&2
    exit 69
  }
  ovmf_code=$MOONLIGHTOS_OVMF_CODE
  ovmf_vars_template=$MOONLIGHTOS_OVMF_VARS
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
  -drive "file=$work/persistence.img,if=virtio,format=raw"
  -device virtio-vga -display none -monitor none -no-reboot
  -netdev user,id=net0 -device e1000,netdev=net0
)
[[ -r /dev/kvm && -w /dev/kvm ]] && common=(-enable-kvm -cpu host "${common[@]}")
install -D -m 0644 /dev/null "$LOG"

boot_and_wait() {
  local mode=$1 marker=$2
  shift 2
  printf '\n=== live boot: %s ===\n' "$mode" >> "$LOG"
  qemu-system-x86_64 \
    "${common[@]}" \
    -fw_cfg "name=opt/moonlightos.smoke,string=$mode" \
    -fw_cfg "name=opt/moonlightos.timeout-scale,string=$SCALE" \
    -serial stdio "$@" >> "$LOG" 2>&1 &
  pid=$!
  for _ in $(seq 1 $((240 * SCALE))); do
    if grep -q "$marker" "$LOG"; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      pid=
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  cat "$LOG"
  echo "Live system did not emit $marker." >&2
  exit 1
}

boot_and_wait live-persistence-write MOONLIGHTOS_SMOKE_LIVE_PERSISTENCE_WRITTEN \
  -boot d -cdrom "$ISO"
boot_and_wait live-persistence-read MOONLIGHTOS_SMOKE_LIVE_PERSISTENCE_READY \
  -boot d -cdrom "$ISO"
boot_and_wait live-persistence-absent MOONLIGHTOS_SMOKE_LIVE_PERSISTENCE_IGNORED \
  -kernel "$work/vmlinuz" -initrd "$work/initrd.img" \
  -append 'boot=live components nopersistence ipv6.disable=1 console=tty1 console=ttyS0,115200n8' \
  -drive "file=$ISO,media=cdrom,readonly=on"

echo 'QEMU persistence smoke test passed: state survived a persistent live reboot and nopersistence ignored the attached backend.'
