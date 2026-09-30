#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK="$ROOT/build/work"
CHROOT="$WORK/config/includes.chroot"
PROFILE=${PROFILE:-general}
PROFILE_DIR="$ROOT/config/profiles/$PROFILE"

[[ $PROFILE =~ ^[a-z0-9][a-z0-9-]{0,31}$ && -f "$PROFILE_DIR/profile.conf" ]] || {
  printf 'Unknown build profile: %s (available: %s)\n' "$PROFILE" \
    "$(find "$ROOT/config/profiles" -mindepth 1 -maxdepth 1 -type d -printf '%f ' | sort)" >&2
  exit 64
}

command -v unsquashfs >/dev/null || {
  echo 'squashfs-tools is required to extract pinned application payloads' >&2
  exit 127
}

if [[ -d "$WORK" ]]; then
  find "$WORK" -depth -mindepth 1 -delete
fi
mkdir -p "$WORK/config" "$CHROOT"
cp -a "$ROOT/config/live-build/." "$WORK/config/"
cp -a "$ROOT/overlay/." "$CHROOT/"
# The build profile adds hardware-specific packages, hooks, and files. A
# profile with PROFILE_BASE gets its base profile's files first.
PROFILE_BASE=$(source "$PROFILE_DIR/profile.conf"; printf '%s' "${PROFILE_BASE:-}")
profile_dirs=()
if [[ -n $PROFILE_BASE ]]; then
  base_dir="$ROOT/config/profiles/$PROFILE_BASE"
  [[ $PROFILE_BASE =~ ^[a-z0-9][a-z0-9-]{0,31}$ && $PROFILE_BASE != "$PROFILE" && -f "$base_dir/profile.conf" ]] || {
    printf 'Profile %s: unknown PROFILE_BASE %s\n' "$PROFILE" "$PROFILE_BASE" >&2
    exit 64
  }
  [[ -z $(source "$base_dir/profile.conf"; printf '%s' "${PROFILE_BASE:-}") ]] || {
    printf 'Profile %s: base profile %s must not have a base itself\n' "$PROFILE" "$PROFILE_BASE" >&2
    exit 64
  }
  profile_dirs+=("$base_dir")
fi
profile_dirs+=("$PROFILE_DIR")
for profile_dir in "${profile_dirs[@]}"; do
  if [[ -d "$profile_dir/package-lists" ]]; then
    cp -a "$profile_dir/package-lists/." "$WORK/config/package-lists/"
  fi
  if [[ -d "$profile_dir/hooks" ]]; then
    cp -a "$profile_dir/hooks/." "$WORK/config/hooks/live/"
  fi
  if [[ -d "$profile_dir/overlay" ]]; then
    cp -a "$profile_dir/overlay/." "$CHROOT/"
  fi
done
install -D -m 0644 "$PROFILE_DIR/profile.conf" "$CHROOT/usr/share/moonlightos/profile.conf"
printf '%s\n' "$PROFILE" > "$WORK/profile"
install -D -m 0644 "$ROOT/build/downloads/tailscale-archive-keyring.gpg" \
  "$WORK/config/archives/tailscale.key.chroot"
install -D -m 0644 "$ROOT/build/downloads/tailscale-archive-keyring.gpg" \
  "$CHROOT/usr/share/keyrings/tailscale-archive-keyring.gpg"
install -D -m 0644 "$ROOT/build/downloads/google-linux-signing-key.asc" \
  "$WORK/config/archives/google-chrome.key.chroot"
install -D -m 0644 "$ROOT/build/downloads/google-linux-signing-key.asc" \
  "$CHROOT/usr/share/keyrings/google-chrome.asc"

install -D -m 0755 "$ROOT/launcher/moonlightos-launcher.py" "$CHROOT/usr/libexec/moonlightos-launcher"
install -D -m 0755 "$ROOT/launcher/gamepad-nav.py" "$CHROOT/usr/libexec/moonlightos-gamepad-nav"
install -D -m 0644 "$ROOT/launcher/moonlightos_apps.py" "$CHROOT/usr/libexec/moonlightos_apps.py"
install -D -m 0644 "$ROOT/launcher/moonlightos_rdp.py" "$CHROOT/usr/libexec/moonlightos_rdp.py"
install -D -m 0755 "$ROOT/scripts/moonlightos-rdp-secret" "$CHROOT/usr/libexec/moonlightos-rdp-secret"
install -D -m 0755 "$ROOT/launcher/moonlightos_app_runner.py" "$CHROOT/usr/libexec/moonlightos-run-configured-app"
install -D -m 0644 "$ROOT/launcher/moonlightos_setup.py" "$CHROOT/usr/libexec/moonlightos_setup.py"
install -D -m 0755 "$ROOT/launcher/moonlightos_osk.py" "$CHROOT/usr/libexec/moonlightos-osk"
install -D -m 0644 "$ROOT/launcher/moonlightos_display.py" "$CHROOT/usr/libexec/moonlightos_display.py"
install -D -m 0644 "$ROOT/launcher/moonlightos_audio.py" "$CHROOT/usr/libexec/moonlightos_audio.py"
install -D -m 0644 "$ROOT/launcher/moonlightos_support.py" "$CHROOT/usr/libexec/moonlightos_support.py"
install -D -m 0644 "$ROOT/launcher/moonlightos_bluetooth.py" "$CHROOT/usr/libexec/moonlightos_bluetooth.py"
install -D -m 0755 "$ROOT/scripts/moonlightos-bluetoothd" "$CHROOT/usr/libexec/moonlightos-bluetoothd"
install -D -m 0755 "$ROOT/scripts/moonlightos-osk-session" "$CHROOT/usr/libexec/moonlightos-osk-session"
install -D -m 0755 "$ROOT/scripts/moonlightos-tailscale-ui" "$CHROOT/usr/bin/moonlightos-tailscale-ui"
install -D -m 0755 "$ROOT/scripts/moonlightos-run-app" "$CHROOT/usr/libexec/moonlightos-run-app"
install -D -m 0755 "$ROOT/scripts/moonlightos-firefox-drm-check" "$CHROOT/usr/libexec/moonlightos-firefox-drm-check"
install -D -m 0755 "$ROOT/scripts/moonlightos-qemu-smoke" "$CHROOT/usr/libexec/moonlightos-qemu-smoke"
install -D -m 0755 "$ROOT/scripts/moonlightos-support-export" "$CHROOT/usr/libexec/moonlightos-support-export"
install -D -m 0755 "$ROOT/scripts/moonlightos-diagnostics" "$CHROOT/usr/bin/moonlightos-diagnostics"
install -D -m 0755 "$ROOT/scripts/moonlightos-hardware-report" "$CHROOT/usr/bin/moonlightos-hardware-report"
install -D -m 0755 "$ROOT/scripts/moonlightos-hwdetect" "$CHROOT/usr/libexec/moonlightos-hwdetect"
install -D -m 0755 "$ROOT/scripts/moonlightos-network-ready" "$CHROOT/usr/libexec/moonlightos-network-ready"
install -D -m 0755 "$ROOT/scripts/moonlightos-firewall" "$CHROOT/usr/libexec/moonlightos-firewall"
install -D -m 0755 "$ROOT/scripts/moonlightos-audio" "$CHROOT/usr/libexec/moonlightos-audio"
install -D -m 0755 "$ROOT/scripts/moonlightos-tailscale" "$CHROOT/usr/sbin/moonlightos-tailscale"
install -D -m 0755 "$ROOT/scripts/moonlightos-tailscale-diagnostics" "$CHROOT/usr/bin/moonlightos-tailscale-diagnostics"
install -D -m 0755 "$ROOT/scripts/moonlightos-host-address" "$CHROOT/usr/bin/moonlightos-host-address"
install -D -m 0755 "$ROOT/scripts/moonlightos-tailscale-enrollment" "$CHROOT/usr/bin/moonlightos-tailscale-enrollment"
install -D -m 0755 "$ROOT/usbip/moonlightos-usbip" "$CHROOT/usr/sbin/moonlightos-usbip"
install -D -m 0644 "$ROOT/config/default/moonlightos" "$CHROOT/etc/default/moonlightos"
install -D -m 0644 "$ROOT/config/nftables/moonlightos.nft" "$CHROOT/etc/moonlightos/nftables.template"
install -D -m 0644 "$ROOT/usbip/usbip-allowlist.conf" "$CHROOT/etc/moonlightos/usbip-allowlist.conf"
install -D -m 0644 "$ROOT/build/applications.lock" \
  "$CHROOT/usr/share/moonlightos/applications.lock"
install -D -m 0644 "$ROOT/build/sources.lock" \
  "$CHROOT/usr/share/moonlightos/sources.lock"
for manifest in "$ROOT"/config/apps.d/*.ini; do
  install -D -m 0644 "$manifest" "$CHROOT/usr/share/moonlightos/apps.d/$(basename "$manifest")"
done
install -D -m 0644 "$ROOT/docs/examples/steam.ini" \
  "$CHROOT/usr/share/doc/moonlightos/examples/steam.ini"
build_commit=$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || printf unknown)
build_state=clean
git -C "$ROOT" status --porcelain --untracked-files=normal 2>/dev/null | grep -q . && build_state=modified
printf 'MoonlightOS: %s\nBuild profile: %s\nSource commit: %s\nSource state: %s\nBuild date: %s\n' \
  "$(< "$ROOT/VERSION")" "$PROFILE" "$build_commit" "$build_state" "$(date --utc --iso-8601=seconds)" \
  > "$CHROOT/usr/share/moonlightos/build-info"

for unit in "$ROOT"/services/*; do
  install -D -m 0644 "$unit" "$CHROOT/etc/systemd/system/$(basename "$unit")"
done

install -d -m 0755 "$CHROOT/opt/moonlightos/apps"
while IFS='|' read -r name _version _url filename; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  extract=$(mktemp -d "$ROOT/build/.appimage.XXXXXX")
  trap 'find "$extract" -depth -delete' EXIT
  install -m 0755 "$ROOT/build/downloads/$filename" "$extract/application.AppImage"
  offset=$("$extract/application.AppImage" --appimage-offset)
  unsquashfs -quiet -offset "$offset" -dest "$extract/squashfs-root" \
    "$extract/application.AppImage"
  install -d -m 0755 "$CHROOT/opt/moonlightos/apps/$name"
  cp -a "$extract/squashfs-root/." "$CHROOT/opt/moonlightos/apps/$name/"
  case "$name" in
    moonlight)
      test -x "$CHROOT/opt/moonlightos/apps/$name/usr/bin/moonlight"
      test -f "$CHROOT/opt/moonlightos/apps/$name/usr/plugins/platforms/libqxcb.so"
      ;;
    chiaki-ng)
      test -x "$CHROOT/opt/moonlightos/apps/$name/usr/bin/chiaki"
      test -f "$CHROOT/opt/moonlightos/apps/$name/usr/plugins/platforms/libqwayland-egl.so"
      ;;
  esac
  find "$extract" -depth -delete
  trap - EXIT
done < "$ROOT/build/applications.lock"

printf 'Prepared %s for build profile %s%s\n' "$WORK" "$PROFILE" "${PROFILE_BASE:+ (base: $PROFILE_BASE)}"
