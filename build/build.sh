#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK="$ROOT/build/work"
OUT="$ROOT/build/out"
PROFILE=${PROFILE:-general}
VERSION=$(< "$ROOT/VERSION")

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'Run with sudo: sudo make build' >&2; exit 1; }
command -v lb >/dev/null || { echo 'live-build is required (apt install live-build)' >&2; exit 1; }
[[ -f "$WORK/profile" && $(< "$WORK/profile") == "$PROFILE" ]] || {
  echo "build/work was not prepared for profile $PROFILE; run: make configure PROFILE=$PROFILE" >&2
  exit 1
}
# shellcheck source=config/profiles/general/profile.conf
source "$ROOT/config/profiles/$PROFILE/profile.conf"
ISO="$OUT/moonlightos-$VERSION-${ISO_SUFFIX:+$ISO_SUFFIX-}amd64.iso"
read -r -a profile_options <<< "${LB_EXTRA_CONFIG:-}"

mkdir -p "$OUT"
cd "$WORK"
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}

lb config noauto \
  --mode debian \
  --distribution trixie \
  --architectures amd64 \
  --binary-images iso-hybrid \
  --archive-areas 'main contrib non-free non-free-firmware' \
  --debian-installer live \
  --debian-installer-gui false \
  --uefi-secure-boot enable \
  --debootstrap-options '--include=ca-certificates' \
  --bootappend-live 'boot=live components persistence ipv6.disable=1 hostname=moonlightos username=moonlightos locales=en_US.UTF-8 keyboard-layouts=us console=tty1 console=ttyS0,115200n8' \
  --bootappend-install 'ipv6.disable=1' \
  --iso-application 'MoonlightOS streaming appliance' \
  --iso-publisher 'MoonlightOS Project' \
  --iso-volume 'MOONLIGHTOS' \
  --apt-recommends false \
  --memtest none \
  "${profile_options[@]}"

lb build
built_iso=$(find . -maxdepth 1 -type f -name '*.hybrid.iso' -print -quit)
[[ -n "$built_iso" ]] || {
  echo 'live-build completed without producing an *.hybrid.iso file' >&2
  exit 1
}

# Profile checks run against the root filesystem that ships in the ISO.
listing=$(mktemp)
trap 'rm -f -- "$listing"' EXIT
unsquashfs -l -d / binary/live/filesystem.squashfs > "$listing"
for pattern in ${REQUIRED_IMAGE_PATHS:-}; do
  grep -Eq -- "$pattern" "$listing" || {
    echo "Profile $PROFILE: final image is missing $pattern" >&2
    exit 1
  }
done
for pattern in ${FORBIDDEN_IMAGE_PATHS:-}; do
  if grep -E -- "$pattern" "$listing"; then
    echo "Profile $PROFILE: final image contains forbidden $pattern" >&2
    exit 1
  fi
done
grep -E 'updates/dkms/(wl|nvidia-current)[^/]*\.ko' "$listing" || true

install -m 0644 "$built_iso" "$ISO"
printf 'ISO: %s\n' "$ISO"
