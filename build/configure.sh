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
# A command substitution inherits this script's variables, so unset the one
# being read or a profile without it would report the caller's value.
profile_conf_value() { (unset "$2"; source "$1"; printf '%s' "${!2:-}"); }
PROFILE_BASE=$(profile_conf_value "$PROFILE_DIR/profile.conf" PROFILE_BASE)
profile_dirs=()
if [[ -n $PROFILE_BASE ]]; then
  base_dir="$ROOT/config/profiles/$PROFILE_BASE"
  [[ $PROFILE_BASE =~ ^[a-z0-9][a-z0-9-]{0,31}$ && $PROFILE_BASE != "$PROFILE" && -f "$base_dir/profile.conf" ]] || {
    printf 'Profile %s: unknown PROFILE_BASE %s\n' "$PROFILE" "$PROFILE_BASE" >&2
    exit 64
  }
  [[ -z $(profile_conf_value "$base_dir/profile.conf" PROFILE_BASE) ]] || {
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
# Hold every `package=version` of the package lists through live-build's upgrade passes
# (build/apt-pins.sh). Only as .pref.chroot: live-build removes those after the chroot
# stage, but copies .pref and .pref.binary into the image's /etc/apt/preferences.d.
pins=$("$ROOT/build/apt-pins.sh" "$WORK"/config/package-lists/*.list.chroot)
if [[ -n $pins ]]; then
  printf '%s\n' "$pins" > "$WORK/config/archives/couchliteos-pins.pref.chroot"
fi
install -D -m 0644 "$PROFILE_DIR/profile.conf" "$CHROOT/usr/share/couchliteos/profile.conf"
printf '%s\n' "$PROFILE" > "$WORK/profile"
install -D -m 0644 "$ROOT/build/downloads/tailscale-archive-keyring.gpg" \
  "$WORK/config/archives/tailscale.key.chroot"
install -D -m 0644 "$ROOT/build/downloads/tailscale-archive-keyring.gpg" \
  "$CHROOT/usr/share/keyrings/tailscale-archive-keyring.gpg"
install -D -m 0644 "$ROOT/build/downloads/google-linux-signing-key.asc" \
  "$WORK/config/archives/google-chrome.key.chroot"
install -D -m 0644 "$ROOT/build/downloads/google-linux-signing-key.asc" \
  "$CHROOT/usr/share/keyrings/google-chrome.asc"

install -D -m 0755 "$ROOT/launcher/couchliteos-launcher.py" "$CHROOT/usr/libexec/couchliteos-launcher"
install -D -m 0755 "$ROOT/launcher/couchliteos-tv.py" "$CHROOT/usr/libexec/couchliteos-tv"
install -D -m 0755 "$ROOT/scripts/couchliteos-session" "$CHROOT/usr/libexec/couchliteos-session"
install -D -m 0644 "$ROOT/launcher/couchliteos_session.py" "$CHROOT/usr/libexec/couchliteos_session.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_tvlayout.py" "$CHROOT/usr/libexec/couchliteos_tvlayout.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_tvscreens.py" "$CHROOT/usr/libexec/couchliteos_tvscreens.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_quick.py" "$CHROOT/usr/libexec/couchliteos_quick.py"
install -D -m 0755 "$ROOT/launcher/gamepad-nav.py" "$CHROOT/usr/libexec/couchliteos-gamepad-nav"
install -D -m 0644 "$ROOT/launcher/couchliteos_apps.py" "$CHROOT/usr/libexec/couchliteos_apps.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_rdp.py" "$CHROOT/usr/libexec/couchliteos_rdp.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_controllers.py" "$CHROOT/usr/libexec/couchliteos_controllers.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_pcstatus.py" "$CHROOT/usr/libexec/couchliteos_pcstatus.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_recent.py" "$CHROOT/usr/libexec/couchliteos_recent.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_home.py" "$CHROOT/usr/libexec/couchliteos_home.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_update.py" "$CHROOT/usr/libexec/couchliteos_update.py"
install -D -m 0755 "$ROOT/launcher/couchliteos_updater.py" "$CHROOT/usr/libexec/couchliteos-updater"
install -D -m 0644 "$ROOT/launcher/couchliteos_snapshot.py" "$CHROOT/usr/libexec/couchliteos_snapshot.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_safefile.py" "$CHROOT/usr/libexec/couchliteos_safefile.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_softwareupdate.py" "$CHROOT/usr/libexec/couchliteos_softwareupdate.py"
install -D -m 0755 "$ROOT/launcher/couchliteos_browser.py" "$CHROOT/usr/libexec/couchliteos-browser"
install -D -m 0644 "$ROOT/launcher/couchliteos_browser.py" "$CHROOT/usr/libexec/couchliteos_browser.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_browsersetup.py" "$CHROOT/usr/libexec/couchliteos_browsersetup.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_errors.py" "$CHROOT/usr/libexec/couchliteos_errors.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_whatsnew.py" "$CHROOT/usr/libexec/couchliteos_whatsnew.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_padcheck.py" "$CHROOT/usr/libexec/couchliteos_padcheck.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_pads.py" "$CHROOT/usr/libexec/couchliteos_pads.py"
install -D -m 0755 "$ROOT/scripts/couchliteos-rdp-secret" "$CHROOT/usr/libexec/couchliteos-rdp-secret"
install -D -m 0755 "$ROOT/launcher/couchliteos_app_runner.py" "$CHROOT/usr/libexec/couchliteos-run-configured-app"
install -D -m 0644 "$ROOT/launcher/couchliteos_setup.py" "$CHROOT/usr/libexec/couchliteos_setup.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_netmenu.py" "$CHROOT/usr/libexec/couchliteos_netmenu.py"
install -D -m 0755 "$ROOT/launcher/couchliteos_osk.py" "$CHROOT/usr/libexec/couchliteos-osk"
install -D -m 0644 "$ROOT/launcher/couchliteos_display.py" "$CHROOT/usr/libexec/couchliteos_display.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_audio.py" "$CHROOT/usr/libexec/couchliteos_audio.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_brightness.py" "$CHROOT/usr/libexec/couchliteos_brightness.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_power.py" "$CHROOT/usr/libexec/couchliteos_power.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_support.py" "$CHROOT/usr/libexec/couchliteos_support.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_bluetooth.py" "$CHROOT/usr/libexec/couchliteos_bluetooth.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_stream.py" "$CHROOT/usr/libexec/couchliteos_stream.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_cec.py" "$CHROOT/usr/libexec/couchliteos_cec.py"
install -D -m 0755 "$ROOT/scripts/couchliteos-cec" "$CHROOT/usr/libexec/couchliteos-cec"
install -D -m 0644 "$ROOT/launcher/couchliteos_confirm.py" "$CHROOT/usr/libexec/couchliteos_confirm.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_listview.py" "$CHROOT/usr/libexec/couchliteos_listview.py"
install -D -m 0755 "$ROOT/launcher/couchliteos_foot.py" "$CHROOT/usr/libexec/couchliteos-foot"
install -D -m 0644 "$ROOT/launcher/couchliteos_controls.py" "$CHROOT/usr/libexec/couchliteos_controls.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_screenfit.py" "$CHROOT/usr/libexec/couchliteos_screenfit.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_theme.py" "$CHROOT/usr/libexec/couchliteos_theme.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_pointer.py" "$CHROOT/usr/libexec/couchliteos_pointer.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_phone.py" "$CHROOT/usr/libexec/couchliteos_phone.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_artwork.py" "$CHROOT/usr/libexec/couchliteos_artwork.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_input.py" "$CHROOT/usr/libexec/couchliteos_input.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_discover.py" "$CHROOT/usr/libexec/couchliteos_discover.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_streamcheck.py" "$CHROOT/usr/libexec/couchliteos_streamcheck.py"
install -D -m 0644 "$ROOT/launcher/couchliteos_firmware.py" "$CHROOT/usr/libexec/couchliteos_firmware.py"
install -D -m 0755 "$ROOT/scripts/couchliteos-bluetoothd" "$CHROOT/usr/libexec/couchliteos-bluetoothd"
install -D -m 0755 "$ROOT/scripts/couchliteos-osk-session" "$CHROOT/usr/libexec/couchliteos-osk-session"
install -D -m 0755 "$ROOT/scripts/couchliteos-tailscale-ui" "$CHROOT/usr/bin/couchliteos-tailscale-ui"
install -D -m 0755 "$ROOT/scripts/couchliteos-run-app" "$CHROOT/usr/libexec/couchliteos-run-app"
install -D -m 0755 "$ROOT/scripts/couchliteos-moonlight-prefs" "$CHROOT/usr/libexec/couchliteos-moonlight-prefs"
install -D -m 0755 "$ROOT/scripts/couchliteos-display-failed" "$CHROOT/usr/libexec/couchliteos-display-failed"
install -D -m 0755 "$ROOT/scripts/couchliteos-firefox-drm-check" "$CHROOT/usr/libexec/couchliteos-firefox-drm-check"
install -D -m 0755 "$ROOT/scripts/couchliteos-qemu-smoke" "$CHROOT/usr/libexec/couchliteos-qemu-smoke"
install -D -m 0755 "$ROOT/scripts/couchliteos-support-export" "$CHROOT/usr/libexec/couchliteos-support-export"
install -D -m 0755 "$ROOT/scripts/couchliteos-nvidia-firmware" "$CHROOT/usr/libexec/couchliteos-nvidia-firmware"
install -D -m 0644 "$ROOT/third_party/envytools/extract_firmware.py" "$CHROOT/usr/libexec/couchliteos/envytools-extract-firmware.py"
install -D -m 0755 "$ROOT/scripts/couchliteos-diagnostics" "$CHROOT/usr/bin/couchliteos-diagnostics"
install -D -m 0755 "$ROOT/scripts/couchliteos-grub-bootcheck" "$CHROOT/etc/grub.d/01_couchliteos_bootcheck"
install -D -m 0755 "$ROOT/scripts/couchliteos-grub-restore" "$CHROOT/etc/grub.d/42_couchliteos_restore"
install -D -m 0755 "$ROOT/scripts/couchliteos-grub-initrd" "$CHROOT/etc/grub.d/00_couchliteos_initrd"
install -D -m 0755 "$ROOT/scripts/couchliteos-hardware-report" "$CHROOT/usr/bin/couchliteos-hardware-report"
install -D -m 0755 "$ROOT/scripts/couchliteos-hwdetect" "$CHROOT/usr/libexec/couchliteos-hwdetect"
install -D -m 0755 "$ROOT/scripts/couchliteos-migrate" "$CHROOT/usr/libexec/couchliteos-migrate"
install -D -m 0755 "$ROOT/scripts/couchliteos-network-ready" "$CHROOT/usr/libexec/couchliteos-network-ready"
install -D -m 0755 "$ROOT/scripts/couchliteos-boot-time" "$CHROOT/usr/libexec/couchliteos-boot-time"
install -D -m 0755 "$ROOT/scripts/couchliteos-firewall" "$CHROOT/usr/libexec/couchliteos-firewall"
install -D -m 0755 "$ROOT/scripts/couchliteos-audio" "$CHROOT/usr/libexec/couchliteos-audio"
install -D -m 0755 "$ROOT/scripts/couchliteos-tailscale" "$CHROOT/usr/sbin/couchliteos-tailscale"
install -D -m 0755 "$ROOT/scripts/couchliteos-tailscale-diagnostics" "$CHROOT/usr/bin/couchliteos-tailscale-diagnostics"
install -D -m 0755 "$ROOT/scripts/couchliteos-host-address" "$CHROOT/usr/bin/couchliteos-host-address"
install -D -m 0755 "$ROOT/scripts/couchliteos-tailscale-enrollment" "$CHROOT/usr/bin/couchliteos-tailscale-enrollment"
install -D -m 0755 "$ROOT/usbip/couchliteos-usbip" "$CHROOT/usr/sbin/couchliteos-usbip"
install -D -m 0644 "$ROOT/config/default/couchliteos" "$CHROOT/etc/default/couchliteos"
install -D -m 0644 "$ROOT/config/nftables/couchliteos.nft" "$CHROOT/etc/couchliteos/nftables.template"
install -D -m 0644 "$ROOT/usbip/usbip-allowlist.conf" "$CHROOT/etc/couchliteos/usbip-allowlist.conf"
install -D -m 0644 "$ROOT/build/applications.lock" \
  "$CHROOT/usr/share/couchliteos/applications.lock"
install -D -m 0644 "$ROOT/build/sources.lock" \
  "$CHROOT/usr/share/couchliteos/sources.lock"
# Rebuilt with foreign-toplevel support, the docked keyboard and the mouse speed by
# hooks/live/0050-cage.hook.chroot, which deletes them.
install -D -m 0644 "$ROOT/build/downloads/cage-0.2.0.tar.gz" "$CHROOT/usr/src/couchliteos-cage/cage-0.2.0.tar.gz"
install -D -m 0644 "$ROOT/config/cage/cage-0.2.0-foreign-toplevel.patch" \
  "$CHROOT/usr/src/couchliteos-cage/cage-0.2.0-foreign-toplevel.patch"
install -D -m 0644 "$ROOT/config/cage/cage-0.2.0-osk-panel.patch" \
  "$CHROOT/usr/src/couchliteos-cage/cage-0.2.0-osk-panel.patch"
install -D -m 0644 "$ROOT/config/cage/cage-0.2.0-pointer-speed.patch" \
  "$CHROOT/usr/src/couchliteos-cage/cage-0.2.0-pointer-speed.patch"
for manifest in "$ROOT"/config/apps.d/*.ini; do
  install -D -m 0644 "$manifest" "$CHROOT/usr/share/couchliteos/apps.d/$(basename "$manifest")"
done
install -D -m 0644 "$ROOT/docs/examples/steam.ini" \
  "$CHROOT/usr/share/doc/couchliteos/examples/steam.ini"
build_commit=$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || printf unknown)
build_state=clean
git -C "$ROOT" status --porcelain --untracked-files=normal 2>/dev/null | grep -q . && build_state=modified
printf 'CouchLiteOS: %s\nBuild profile: %s\nSource commit: %s\nSource state: %s\nBuild date: %s\n' \
  "$(< "$ROOT/VERSION")" "$PROFILE" "$build_commit" "$build_state" "$(date --utc --iso-8601=seconds)" \
  > "$CHROOT/usr/share/couchliteos/build-info"

for unit in "$ROOT"/services/*; do
  install -D -m 0644 "$unit" "$CHROOT/etc/systemd/system/$(basename "$unit")"
done

install -d -m 0755 "$CHROOT/opt/couchliteos/apps"
while IFS='|' read -r name _version _url filename; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  extract=$(mktemp -d "$ROOT/build/.appimage.XXXXXX")
  trap 'find "$extract" -depth -delete' EXIT
  install -m 0755 "$ROOT/build/downloads/$filename" "$extract/application.AppImage"
  offset=$("$extract/application.AppImage" --appimage-offset)
  unsquashfs -quiet -offset "$offset" -dest "$extract/squashfs-root" \
    "$extract/application.AppImage"
  install -d -m 0755 "$CHROOT/opt/couchliteos/apps/$name"
  cp -a "$extract/squashfs-root/." "$CHROOT/opt/couchliteos/apps/$name/"
  case "$name" in
    moonlight)
      test -x "$CHROOT/opt/couchliteos/apps/$name/usr/bin/moonlight"
      test -f "$CHROOT/opt/couchliteos/apps/$name/usr/plugins/platforms/libqxcb.so"
      ;;
    chiaki-ng)
      test -x "$CHROOT/opt/couchliteos/apps/$name/usr/bin/chiaki"
      test -f "$CHROOT/opt/couchliteos/apps/$name/usr/plugins/platforms/libqwayland-egl.so"
      # English only and no web developer tools; QtWebEngine itself stays (PSN login).
      rm -f "$CHROOT/opt/couchliteos/apps/$name/usr/resources/qtwebengine_devtools_resources.pak"
      find "$CHROOT/opt/couchliteos/apps/$name/usr/translations/qtwebengine_locales" -name '*.pak' ! -name en-US.pak -delete
      find "$CHROOT/opt/couchliteos/apps/$name/usr/translations" -maxdepth 1 -name '*.qm' ! -name '*_en.qm' -delete
      test -f "$CHROOT/opt/couchliteos/apps/$name/usr/translations/qtwebengine_locales/en-US.pak"
      test -f "$CHROOT/opt/couchliteos/apps/$name/usr/resources/qtwebengine_resources.pak"
      ;;
  esac
  find "$extract" -depth -delete
  trap - EXIT
done < "$ROOT/build/applications.lock"

printf 'Prepared %s for build profile %s%s\n' "$WORK" "$PROFILE" "${PROFILE_BASE:+ (base: $PROFILE_BASE)}"
