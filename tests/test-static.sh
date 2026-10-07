#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

command -v rg >/dev/null || {
  echo 'ripgrep (rg) is required for the static test suite' >&2
  exit 127
}

# A negated command (`! cmd`) never trips `set -e`, so negative checks use
# refute: it fails on a match (status 0) and on an error (status > 1).
refute() {
  local status=0
  "$@" || status=$?
  case $status in
    0) printf 'static test: unexpected match: %s\n' "$*" >&2; exit 1 ;;
    1) ;;
    *) printf 'static test: %s failed with status %s\n' "$*" "$status" >&2; exit "$status" ;;
  esac
}
refute_control=$(mktemp)
printf 'needle\n' > "$refute_control"
(refute rg -q needle "$refute_control") && { echo 'refute accepted a match' >&2; exit 1; }
(refute rg -q needle "$refute_control.missing" 2>/dev/null) && { echo 'refute accepted an error' >&2; exit 1; }
refute rg -q haystack "$refute_control"
rm -f -- "$refute_control"

while IFS= read -r file; do
  bash -n "$file"
done < <(rg -l -g '!build/work/**' -g '!build/out/**' '^#!/bin/bash' \
  build host scripts tests tools usbip config/live-build/hooks config/profiles)

boot_test=$(mktemp -d)
mkdir -p "$boot_test/binary/boot/grub" "$boot_test/binary/isolinux"
printf 'menuentry "Live system (amd64)" --hotkey=l {\n linux /live/vmlinuz boot=live components persistence ipv6.disable=1\n initrd /live/initrd.img\n}\nsubmenu '\''Advanced install options ...'\'' {}\nsubmenu '\''Utilities...'\'' {}\n' > "$boot_test/binary/boot/grub/grub.cfg"
printf "menuentry 'Start installer' {\n linux /install/vmlinuz vga=788 ipv6.disable=1 --- quiet\n initrd /install/initrd.gz\n}\n" > "$boot_test/binary/boot/grub/install_start.cfg"
printf 'set color_normal=light-gray/black\nset color_highlight=white/dark-gray\nset theme=/boot/grub/live-theme/theme.txt\n' > "$boot_test/binary/boot/grub/theme.cfg"
printf 'include menu.cfg\nprompt 0\ntimeout 0\n' > "$boot_test/binary/isolinux/isolinux.cfg"
printf 'label live-amd64\n menu label ^Live system (amd64)\n menu default\n linux /live/vmlinuz\n initrd /live/initrd.img\n append boot=live components persistence ipv6.disable=1\n' > "$boot_test/binary/isolinux/live.cfg"
printf 'label installstart\n menu label Start ^installer\n linux /install/vmlinuz\n initrd /install/initrd.gz\n append vga=788 ipv6.disable=1 --- quiet\nlabel rescue\n append rescue/enable=true vga=788 ipv6.disable=1 --- quiet\n' > "$boot_test/binary/isolinux/install.cfg"
printf 'menu background splash.png\nmenu color sel * #ffffffff #76a1d0ff *\nmenu color help 37;40 #ffdddd00 #00000000 none\n' > "$boot_test/binary/isolinux/stdmenu.cfg"
chmod 0555 "$boot_test/binary/boot/grub/grub.cfg"
grub_mode=$(stat -c %a "$boot_test/binary/boot/grub/grub.cfg")
isolinux_mode=$(stat -c %a "$boot_test/binary/isolinux/live.cfg")
grub_installer_args=$(rg '^[[:space:]]+linux /install/' "$boot_test/binary/boot/grub/install_start.cfg")
isolinux_installer_args=$(rg '^[[:space:]]+append ' "$boot_test/binary/isolinux/install.cfg")
(cd "$boot_test" && bash "$ROOT/config/live-build/hooks/live/0100-autoboot.hook.binary")
(cd "$boot_test" && bash "$ROOT/config/live-build/hooks/live/0100-autoboot.hook.binary")
rg -q '^set default=0$' "$boot_test/binary/boot/grub/grub.cfg"
rg -q '^set timeout=3$' "$boot_test/binary/boot/grub/grub.cfg"
rg -q '^terminal_output console serial$' "$boot_test/binary/boot/grub/grub.cfg"
rg -q '^menuentry "Start CouchLiteOS"' "$boot_test/binary/boot/grub/grub.cfg"
rg -q '^menuentry "Start CouchLiteOS \(No Persistence\)"' "$boot_test/binary/boot/grub/grub.cfg"
[[ $(rg -c '^menuentry "Start CouchLiteOS \(No Persistence\)"' "$boot_test/binary/boot/grub/grub.cfg") == 1 ]]
rg -q 'boot=live components nopersistence ipv6.disable=1' "$boot_test/binary/boot/grub/grub.cfg"
rg -q "menuentry 'Install CouchLiteOS'" "$boot_test/binary/boot/grub/install_start.cfg"
rg -q 'linux /install/vmlinuz vga=788 theme=dark ipv6.disable=1 --- quiet' "$boot_test/binary/boot/grub/install_start.cfg"
[[ $(sed 's/ theme=dark//' <<< "$(rg '^[[:space:]]+linux /install/' "$boot_test/binary/boot/grub/install_start.cfg")") == "$grub_installer_args" ]]
rg -q "submenu 'Advanced install options \.\.\.'" "$boot_test/binary/boot/grub/grub.cfg"
rg -q "submenu 'Utilities\.\.\.'" "$boot_test/binary/boot/grub/grub.cfg"
rg -q '^set color_normal=white/black$' "$boot_test/binary/boot/grub/theme.cfg"
rg -q '^set color_highlight=black/white$' "$boot_test/binary/boot/grub/theme.cfg"
refute rg -q '^set theme=' "$boot_test/binary/boot/grub/theme.cfg"
[[ $(stat -c %a "$boot_test/binary/boot/grub/grub.cfg") == "$grub_mode" ]]
rg -q 'chmod 0555 "\$output"' "$ROOT/config/live-build/hooks/live/0100-autoboot.hook.binary"
[[ $(stat -c %a "$boot_test/binary/isolinux/live.cfg") == "$isolinux_mode" ]]
rg -q '^timeout 30$' "$boot_test/binary/isolinux/isolinux.cfg"
refute rg -q '^timeout 0$' "$boot_test/binary/isolinux/isolinux.cfg"
rg -q 'menu label \^Start CouchLiteOS$' "$boot_test/binary/isolinux/live.cfg"
rg -q 'menu label Start CouchLiteOS \(No Persistence\)$' "$boot_test/binary/isolinux/live.cfg"
[[ $(rg -c 'menu label Start CouchLiteOS \(No Persistence\)$' "$boot_test/binary/isolinux/live.cfg") == 1 ]]
rg -q 'boot=live components nopersistence ipv6.disable=1' "$boot_test/binary/isolinux/live.cfg"
rg -q 'menu label \^Install CouchLiteOS$' "$boot_test/binary/isolinux/install.cfg"
rg -q 'append vga=788 theme=dark ipv6.disable=1 --- quiet' "$boot_test/binary/isolinux/install.cfg"
rg -q 'rescue/enable=true vga=788 theme=dark ipv6.disable=1' "$boot_test/binary/isolinux/install.cfg"
[[ $(sed 's/ theme=dark//' <<< "$(rg '^[[:space:]]+append ' "$boot_test/binary/isolinux/install.cfg")") == "$isolinux_installer_args" ]]
refute rg -q '^menu background ' "$boot_test/binary/isolinux/stdmenu.cfg"
rg -q '^menu color sel[[:space:]]+\* #ff000000 #ffffffff \*$' "$boot_test/binary/isolinux/stdmenu.cfg"
[[ $(rg -c '^# couchliteos live-update$' "$boot_test/binary/boot/grub/grub.cfg") == 1 ]]
rg -q 'search --no-floppy --label --set=couchliteos_slot couchliteos-sys' "$boot_test/binary/boot/grub/grub.cfg"
rg -Fq 'live-media-path=/live-update/current' "$boot_test/binary/boot/grub/grub.cfg"
rg -Fq 'live-media-path=/live-update/previous' "$boot_test/binary/boot/grub/grub.cfg"
refute rg -q '^menuentry "Start CouchLiteOS \((Updated|Previous Update)\)"' "$boot_test/binary/boot/grub/grub.cfg"
# NVIDIA ISO: one Basic Graphics entry (nouveau, KMS on) after No Persistence.
(cd "$boot_test" && bash "$ROOT/config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary")
(cd "$boot_test" && bash "$ROOT/config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary")
grub_cfg=$boot_test/binary/boot/grub/grub.cfg
live_cfg=$boot_test/binary/isolinux/live.cfg
[[ $(rg -c '^menuentry "Start CouchLiteOS \(Basic Graphics\)" \{$' "$grub_cfg") == 1 ]]
[[ $(rg -c 'couchliteos\.gpu=basic' "$grub_cfg") == 1 ]]
rg -q '^ linux /live/vmlinuz boot=live components persistence ipv6.disable=1 couchliteos\.gpu=basic$' "$grub_cfg"
[[ $(rg '^menuentry ' "$grub_cfg" | cut -d'"' -f2 | paste -sd '|') == 'Start CouchLiteOS|Start CouchLiteOS (No Persistence)|Start CouchLiteOS (Basic Graphics)' ]]
[[ $(stat -c %a "$grub_cfg") == 555 ]]
[[ $(rg -c '^label live-amd64-basic$' "$live_cfg") == 1 ]]
[[ $(rg -c 'couchliteos\.gpu=basic' "$live_cfg") == 1 ]]
rg -q '^ append boot=live components persistence ipv6.disable=1 couchliteos\.gpu=basic$' "$live_cfg"
[[ $(rg -c 'menu default' "$live_cfg") == 1 ]]
[[ $(rg '^label ' "$live_cfg" | paste -sd '|') == 'label live-amd64|label live-amd64-nopersistence|label live-amd64-basic' ]]
refute rg -q 'nomodeset' "$grub_cfg" "$live_cfg"
find "$boot_test" -depth -delete

rg -q -- '--uefi-secure-boot enable' build/build.sh
# The slim dpkg config drops /usr/share/doc, which lb binary_loadlin copies from.
rg -q -- '--loadlin false' build/build.sh
rg -q -- '--win32-loader false' build/build.sh
rg -q -- "--bootappend-live '.*ipv6.disable=1" build/build.sh
[[ "$(< VERSION)" == 0.3.0-beta ]]
refute rg -q 'NONE PAIRED' launcher --glob '*.py'
cmp -s VERSION overlay/etc/couchliteos-version
rg -Fq 'path: build/out/couchliteos-${{ env.VERSION }}-amd64.iso' .github/workflows/build.yml
rg -Fq 'echo "VERSION=$(< VERSION)" >> "$GITHUB_ENV"' .github/workflows/build.yml
refute rg -q 'couchliteos-[0-9]+\.[0-9]+\.[0-9]+-' .github/workflows/build.yml
rg -Fq 'ISO ?= build/out/couchliteos-$(VERSION)-$(if $(ISO_SUFFIX),$(ISO_SUFFIX)-)amd64.iso' Makefile
rg -Fq 'ISO="$OUT/couchliteos-$VERSION-${ISO_SUFFIX:+$ISO_SUFFIX-}amd64.iso"' build/build.sh
rg -q '^PROFILE \?= general$' Makefile
refute rg -qi 'sha-?256|sha256|\.sha256' .github/workflows/build.yml
rg -q 'sudo chown -R .*build/out' .github/workflows/build.yml
rg -q '^ipv6.method=disabled$' overlay/etc/NetworkManager/conf.d/10-couchliteos.conf
rg -q '^net.ipv6.conf.all.disable_ipv6=1$' overlay/etc/sysctl.d/90-couchliteos.conf
refute rg -q 'couchliteos-network-ready.service' services/couchliteos-launcher.service
refute rg -q 'Before=.*couchliteos-launcher.service' services/couchliteos-network-ready.service
refute rg -q 'couchliteos-network-ready.service' services/couchliteos-{moonlight,chiaki,firefox}.service
rg -q 'COUCHLITEOS_LAUNCHER_READY' services/couchliteos-launcher.service tests/qemu-smoke.sh
# Both front ends write launcher-ready: the smoke gates also check which one came up.
rg -q 'COUCHLITEOS_SMOKE_INTERFACE=' scripts/couchliteos-qemu-smoke tests/qemu-smoke.sh
rg -Fq 'record classic' scripts/couchliteos-session
rg -q 'StandardOutput=journal\+console' services/couchliteos-launcher.service
refute rg -q '^Environment=WAYLAND_DISPLAY=' services/couchliteos-launcher.service
rg -q '/usr/bin/cage -s -- /usr/libexec/couchliteos-session 2>&1' services/couchliteos-launcher.service
rg -qF 'exec "$FOOT" --fullscreen --title "CouchLiteOS Launcher" "$LAUNCHER"' scripts/couchliteos-session
rg -q '^Environment=QT_QPA_PLATFORM=xcb$' services/couchliteos-moonlight.service
rg -q '^Environment=QT_QPA_PLATFORM=wayland$' services/couchliteos-chiaki.service
rg -q '^Environment=MOZ_ENABLE_WAYLAND=1$' services/couchliteos-firefox.service
rg -q '^EnvironmentFile=-/run/couchliteos/session.env$' services/couchliteos-{moonlight,chiaki,firefox}.service
rg -q '^ConditionFileIsExecutable=/opt/couchliteos/apps/moonlight/usr/bin/moonlight$' services/couchliteos-moonlight.service
rg -q '^ConditionFileIsExecutable=/opt/couchliteos/apps/chiaki-ng/usr/bin/chiaki$' services/couchliteos-chiaki.service
rg -q '^ConditionFileIsExecutable=/usr/bin/firefox-esr$' services/couchliteos-firefox.service
rg -q 'binary=\$appdir/usr/bin/moonlight' scripts/couchliteos-run-app
rg -q 'binary=\$appdir/usr/bin/chiaki' scripts/couchliteos-run-app
rg -q 'binary=/usr/bin/firefox-esr' scripts/couchliteos-run-app
rg -q 'write_status starting' scripts/couchliteos-run-app
rg -q 'failed: exited before the application became ready' scripts/couchliteos-run-app
rg -q 'unsquashfs -quiet -offset' build/configure.sh
removed_units='couchliteos-escape''-guard|couchliteos-stop''-active-app'
refute rg -q "$removed_units" build/configure.sh
# Browsers on demand: neither browser is in the image; couchliteos-browser installs the user's pick.
refute rg -q '^(firefox-esr|google-chrome-stable)$' config/live-build/package-lists/couchliteos.list.chroot
for profile in general nvidia; do
  rg -q "^FORBIDDEN_IMAGE_PATHS=.*usr/bin/firefox-esr\\$ usr/bin/google-chrome-stable\\$'" "config/profiles/$profile/profile.conf"
done
rg -q '^binary = /usr/bin/firefox-esr$' config/apps.d/30-firefox.ini
rg -q '^binary = /usr/bin/google-chrome-stable$' config/apps.d/35-google-chrome.ini
rg -q '^deb \[signed-by=/usr/share/keyrings/debian-archive-keyring.gpg\] http://deb.debian.org/debian trixie main$' \
  overlay/usr/share/couchliteos/apt/sources.list
rg -q 'trixie-updates main$' overlay/usr/share/couchliteos/apt/sources.list
rg -q 'debian-security trixie-security main$' overlay/usr/share/couchliteos/apt/sources.list
# The tool's Chrome source is the one the image's /etc/apt gets.
chrome_source=$(cat overlay/usr/share/couchliteos/apt/sources.list.d/google-chrome.list)
rg -Fq "$chrome_source" config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -Fq 'python3 -m compileall -q /usr/libexec/couchliteos_*.py' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -Fq 'Dir::Etc::sourcelist={APT_DIR}/sources.list' launcher/couchliteos_browser.py
rg -Fq 'Dir::Etc::sourceparts={APT_DIR}/sources.list.d' launcher/couchliteos_browser.py
rg -Fq 'APT_DIR = "/usr/share/couchliteos/apt"' launcher/couchliteos_browser.py
rg -Fq '"$ROOT/launcher/couchliteos_browser.py" "$CHROOT/usr/libexec/couchliteos-browser"' build/configure.sh
rg -q -- '-m 0755 "\$ROOT/launcher/couchliteos_browser.py"' build/configure.sh
rg -Fq '"$CHROOT/usr/libexec/couchliteos_browser.py"' build/configure.sh
rg -Fq '"$CHROOT/usr/libexec/couchliteos_browsersetup.py"' build/configure.sh
rg -q '^PathExists=/run/couchliteos/browser-install$' services/couchliteos-browser.path
rg -q '^Unit=couchliteos-browser.service$' services/couchliteos-browser.path
rg -q '^User=root$' services/couchliteos-browser.service
rg -q '/usr/libexec/couchliteos-browser request$' services/couchliteos-browser.service
rg -q '^ExecStopPost=/usr/bin/rm -f /run/couchliteos/browser-install$' services/couchliteos-browser.service
rg -q '^ConditionPathExists=/var/lib/couchliteos-update/browser-refresh$' services/couchliteos-browser-refresh.service
rg -q '^After=couchliteos-network-ready.service$' services/couchliteos-browser-refresh.service
rg -q '/usr/libexec/couchliteos-browser refresh$' services/couchliteos-browser-refresh.service
refute rg -q '^Type=oneshot$' services/couchliteos-browser-refresh.service
rg -q '^systemctl enable couchliteos-browser.path couchliteos-browser-refresh.service$' \
  config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -Fq 'REFRESH_REL = "var/lib/couchliteos-update/browser-refresh"' launcher/couchliteos_browser.py
rg -q 'test_browser.py test_browsersetup.py' launcher/Makefile
rg -q '^import couchliteos_browser as browser$' launcher/couchliteos-launcher.py launcher/couchliteos_browsersetup.py
rg -q '^import couchliteos_browsersetup as browsersetup$' launcher/couchliteos-launcher.py
rg -q '^import couchliteos_browser as browser$' launcher/couchliteos_updater.py
refute rg -n 'shell=True|os\.system' launcher/couchliteos_browser.py launcher/couchliteos_browsersetup.py
refute rg -q 'firefox-ready|google-chrome-ready|couchliteos-firefox.service' scripts/couchliteos-qemu-smoke
# Build profiles: shared base plus hardware-specific packages, hooks, and files.
# general and nvidia are the release ISOs; intel and imac2013 are legacy.
for profile in general nvidia intel imac2013; do
  test -s "config/profiles/$profile/profile.conf"
  (source "config/profiles/$profile/profile.conf"; [[ $PROFILE_NAME == "$profile" ]])
done
profile_value() { (source "config/profiles/$1/profile.conf"; printf '%s' "${!2:-}"); }
[[ -z $(profile_value general ISO_SUFFIX) && $(profile_value general RELEASE) == 1 ]]
[[ $(profile_value general NVIDIA_DRIVER) == none && -z $(profile_value general PROFILE_BASE) ]]
[[ $(profile_value nvidia ISO_SUFFIX) == nvidia && $(profile_value nvidia RELEASE) == 1 ]]
[[ $(profile_value nvidia NVIDIA_DRIVER) == proprietary && $(profile_value nvidia PROFILE_BASE) == general ]]
[[ $(profile_value intel ISO_SUFFIX) == intel && $(profile_value intel RELEASE) == 0 ]]
[[ $(profile_value imac2013 ISO_SUFFIX) == imac2013 && $(profile_value imac2013 RELEASE) == 0 ]]
# Every profile names a distinct ISO.
[[ -z $(for p in config/profiles/*/; do p=${p%/}; printf 'iso-%s\n' "$(profile_value "${p##*/}" ISO_SUFFIX)"; done | sort | uniq -d) ]]
rg -q 'release_profiles' tools/release-assets.sh
rg -Fq 'RELEASE:-0' tools/release-assets.sh
rg -q 'PROFILE_BASE' build/configure.sh
rg -q 'must not have a base itself' build/configure.sh
# configure.sh reads profile.conf values in a subshell, which must not inherit
# the caller's value: PROFILE_BASE=general once made general look based itself.
eval "$(sed -n '/^profile_conf_value() /p' build/configure.sh)"
(PROFILE_BASE=general; [[ -z $(profile_conf_value config/profiles/general/profile.conf PROFILE_BASE) ]])
[[ $(profile_conf_value config/profiles/nvidia/profile.conf PROFILE_BASE) == general ]]
# general: open drivers for Intel/AMD/NVIDIA, Broadcom wl per machine, curated firmware.
for package in firmware-intel-graphics firmware-amd-graphics firmware-nvidia-graphics firmware-sof-signed \
  firmware-iwlwifi firmware-brcm80211 intel-microcode amd64-microcode intel-media-va-driver i965-va-driver \
  mesa-va-drivers broadcom-sta-dkms linux-headers-amd64 dkms dmidecode; do
  rg -q "^$package\$" config/profiles/general/package-lists/general.list.chroot
done
rg -q '^options hid_apple fnmode=2$' config/profiles/general/overlay/etc/modprobe.d/couchliteos-input.conf
rg -q 'dpkg-divert --local --rename --divert "\$sta_blacklist.couchliteos-disabled"' config/profiles/general/hooks/0200-general.hook.chroot
rg -q 'NVIDIA_DRIVER:-none\} != proprietary' config/profiles/general/hooks/0200-general.hook.chroot
rg -q 'rm -f /var/lib/dkms/mok.key' config/profiles/general/hooks/0200-general.hook.chroot
rg -q 'systemctl is-enabled --quiet couchliteos-hwdetect-early.service' config/profiles/general/hooks/0200-general.hook.chroot
refute rg -q '^blacklist ' config/profiles/general/overlay
rg -q 'usr/lib/firmware/nvidia/\[0-9.\]\+/gsp' config/profiles/general/profile.conf
rg -q "^LB_EXTRA_CONFIG='--firmware-binary false --firmware-chroot false'$" config/profiles/general/profile.conf config/profiles/nvidia/profile.conf
# nvidia: proprietary 550 driver loaded only where couchliteos-hwdetect allows it.
for package in nvidia-kernel-dkms firmware-nvidia-gsp nvidia-driver-libs libnvidia-egl-gbm1 nvidia-vaapi-driver nvidia-detect; do
  rg -q "^$package\$" config/profiles/nvidia/package-lists/nvidia.list.chroot
done
rg -q '^options nvidia-current-drm modeset=1 fbdev=1$' config/profiles/nvidia/overlay/etc/modprobe.d/couchliteos-nvidia.conf
rg -q 'dkms install "nvidia-current/' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
rg -q 'dpkg-divert --local --rename --divert "\$load_conf.couchliteos-disabled"' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
rg -q 'rm -f /var/lib/dkms/mok.key' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
# live-build's 5020 hook selects Mesa for glx, dropping the nouveau blacklist and nvidia-drm aliases.
rg -q '^update-glx --set glx /usr/lib/nvidia$' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
rg -q 'options nouveau' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
refute rg -q 'gensub' config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary
# Boot-time hardware detection runs on every profile's image.
rg -q 'couchliteos-hwdetect' build/configure.sh
rg -q 'systemctl enable couchliteos-hwdetect-early.service couchliteos-hwdetect.service' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^DefaultDependencies=no$' services/couchliteos-hwdetect-early.service
rg -q '^Before=systemd-udevd.service systemd-udev-trigger.service systemd-modules-load.service sysinit.target$' services/couchliteos-hwdetect-early.service
rg -q '^WantedBy=sysinit.target$' services/couchliteos-hwdetect-early.service
rg -q '^ExecStart=/usr/libexec/couchliteos-hwdetect early$' services/couchliteos-hwdetect-early.service
rg -q '^ExecStart=/usr/libexec/couchliteos-hwdetect late$' services/couchliteos-hwdetect.service
rg -q '^Before=couchliteos-launcher.service$' services/couchliteos-hwdetect.service
rg -q '^Wants=.*couchliteos-hwdetect.service' services/couchliteos-launcher.service
rg -q '^EnvironmentFile=-/run/couchliteos-hardware/compositor.env$' services/couchliteos-launcher.service
rg -q 'COUCHLITEOS_VIDEO_DECODE' scripts/couchliteos-run-app
rg -q 'couchliteos-hwdetect report' scripts/couchliteos-diagnostics scripts/couchliteos-hardware-report
[[ $(head -1 scripts/couchliteos-hwdetect) == '#!/usr/bin/python3 -I' ]]

# Live-USB sticks made for the old name (persistence.conf maps the old
# /var/lib and /var/log directories) keep working: an early root oneshot binds
# the old persistence onto the new paths and fixes group ownership.
python3 -m py_compile scripts/couchliteos-migrate
[[ $(head -1 scripts/couchliteos-migrate) == '#!/usr/bin/python3 -I' ]]
rg -Fq 'install -D -m 0755 "$ROOT/scripts/couchliteos-migrate" "$CHROOT/usr/libexec/couchliteos-migrate"' build/configure.sh
rg -q '^systemctl enable couchliteos-migrate\.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^DefaultDependencies=no$' services/couchliteos-migrate.service
rg -q '^After=local-fs\.target$' services/couchliteos-migrate.service
rg -q '^Before=.*\bsysinit\.target\b' services/couchliteos-migrate.service
rg -q '^Before=.*\bsystemd-tmpfiles-setup\.service\b' services/couchliteos-migrate.service
rg -q '^Before=.*\bcouchliteos-launcher\.service\b' services/couchliteos-migrate.service
rg -q '^Type=oneshot$' services/couchliteos-migrate.service
rg -q '^ExecStart=/usr/libexec/couchliteos-migrate$' services/couchliteos-migrate.service
rg -q '^WantedBy=sysinit\.target$' services/couchliteos-migrate.service
rg -Fq 'ConditionPathIsMountPoint=|/var/lib/moonlightos' services/couchliteos-migrate.service # rename:keep
rg -Fq 'ConditionPathIsMountPoint=|/var/log/moonlightos' services/couchliteos-migrate.service # rename:keep
rg -Fq '("/var/lib/moonlightos", "/var/lib/couchliteos")' scripts/couchliteos-migrate # rename:keep
rg -Fq '("/var/log/moonlightos", "/var/log/couchliteos")' scripts/couchliteos-migrate # rename:keep
rg -Fq 'os.lchown' scripts/couchliteos-migrate
python3 -m py_compile scripts/couchliteos-persist-setup launcher/couchliteos_liveslot.py
[[ $(head -1 scripts/couchliteos-persist-setup) == '#!/usr/bin/python3 -I' ]]
rg -Fq 'install -D -m 0755 "$ROOT/scripts/couchliteos-persist-setup" "$CHROOT/usr/libexec/couchliteos-persist-setup"' build/configure.sh
rg -Fq 'install -D -m 0644 "$ROOT/launcher/couchliteos_liveslot.py" "$CHROOT/usr/libexec/couchliteos_liveslot.py"' build/configure.sh
rg -q 'test_liveslot\.py test_apps_flatpak\.py' launcher/Makefile
rg -q 'couchliteos_liveslot\.py' launcher/Makefile
rg -Fq 'python3 -m unittest -v tests/test_persist_setup.py' Makefile
rg -Fq 'python3 -m unittest -v tests/test_live_update_grub.py' Makefile
rg -q '^systemctl enable couchliteos-live-boot-ok\.service couchliteos-live-journal-save\.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -Fq 'flatpak remote-add --system --if-not-exists flathub /usr/share/couchliteos/flathub.flatpakrepo' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^Url=https://dl\.flathub\.org/repo/$' overlay/usr/share/couchliteos/flathub.flatpakrepo
for package in fdisk dmsetup; do
  rg -q "^$package\$" config/live-build/package-lists/couchliteos.list.chroot
done
rg -q 'Storage=volatile.nRuntimeMaxUse=64M' overlay/usr/lib/systemd/system-generators/couchliteos-live-journal
for script in scripts/couchliteos-persist-setup overlay/usr/lib/systemd/system-generators/couchliteos-live-journal overlay/etc/initramfs-tools/scripts/live-premount/couchliteos-fsck overlay/etc/initramfs-tools/hooks/couchliteos-fsck; do
  [[ $(git ls-files -s "$script" | cut -d' ' -f1) == 100755 ]]
done
refute rg -q 'shell=True' scripts/couchliteos-persist-setup launcher/couchliteos_liveslot.py
refute rg -q 'os\.chown|shutil\.chown|followlinks=True|shell=True' scripts/couchliteos-migrate
rg -Fq 'unittest -v tests/test_migrate.py' Makefile
rg -Fq 'TV_HEADLESS_ARGS ?= --if-available' Makefile
rg -Fq './tests/tv-headless.sh $(TV_HEADLESS_ARGS)' Makefile
rg -q 'make test TV_HEADLESS_ARGS=$' .github/workflows/build.yml
rg -q 'cage grim python3-gi gir1.2-gtk-4.0' .github/workflows/build.yml

refute rg -q '^(intel-media-va-driver|firmware-intel-graphics|intel-gpu-tools)$' config/live-build/package-lists
rg -q '^intel-media-va-driver$' config/profiles/intel/package-lists/intel-graphics.list.chroot
refute rg -q '^(intel-media-va-driver|firmware-intel-graphics|intel-gpu-tools|i965-va-driver)$' config/profiles/imac2013
for package in broadcom-sta-dkms linux-headers-amd64 dkms firmware-misc-nonfree lm-sensors; do
  rg -q "^$package\$" config/profiles/imac2013/package-lists/imac2013.list.chroot
done
# Only the nvidia profile installs the proprietary NVIDIA driver. Kepler (the
# iMac Late 2013) never gets it: no GBM for Cage past the 470 branch.
refute rg -q '^[^#]*nvidia' config/live-build/package-lists config/profiles/intel/package-lists config/profiles/imac2013/package-lists
[[ $(rg --no-filename '^[^#]*nvidia' config/profiles/general/package-lists) == firmware-nvidia-graphics ]]
refute rg -q -g '!build/work' -g '!build/out' -g '!build/downloads' 'nomodeset' config overlay build
for module in b43 bcma ssb brcmsmac; do
  rg -q "^blacklist $module\$" config/profiles/imac2013/overlay/etc/modprobe.d/couchliteos-imac2013.conf
done
rg -q '^options hid_apple fnmode=2$' config/profiles/imac2013/overlay/etc/modprobe.d/couchliteos-imac2013.conf
rg -q '^applesmc$' config/profiles/imac2013/overlay/etc/modules-load.d/couchliteos-imac2013.conf
rg -q 'dkms install "broadcom-sta/' config/profiles/imac2013/hooks/0200-imac2013.hook.chroot
rg -q "modinfo -k \"\\\$kernel\" -F vermagic wl" config/profiles/imac2013/hooks/0200-imac2013.hook.chroot
rg -q 'rm -f /var/lib/dkms/mok.key' config/profiles/imac2013/hooks/0200-imac2013.hook.chroot
rg -q 'decoder = software' config/profiles/imac2013/hooks/0200-imac2013.hook.chroot
rg -q 'updates/dkms/wl' config/profiles/imac2013/profile.conf
rg -q 'unsquashfs -l -d / binary/live/filesystem.squashfs' build/build.sh
rg -q 'profile_dir/package-lists' build/configure.sh
rg -q 'read -r -a profile_options' build/build.sh
rg -Fq '"${profile_options[@]}"' build/build.sh
rg -q "^LB_EXTRA_CONFIG='--firmware-binary false'$" config/profiles/intel/profile.conf
rg -q "^LB_EXTRA_CONFIG='--firmware-binary false --firmware-chroot false'$" config/profiles/imac2013/profile.conf
rg -q '^intel-microcode$' config/profiles/imac2013/package-lists/imac2013.list.chroot
rg -q 'firmware-nvidia-' config/profiles/imac2013/hooks/0200-imac2013.hook.chroot
rg -q '/usr/share/couchliteos/profile.conf' build/configure.sh
# Remote Desktop: FreeRDP 3 SDL3 client on Wayland, passwords only through stdin.
rg -q '^freerdp3-sdl$' config/live-build/package-lists/couchliteos.list.chroot
refute rg -qi 'remmina|freerdp2|xfreerdp' config/live-build/package-lists config/profiles
rg -q '"/from-stdin:force"' launcher/couchliteos_rdp.py
rg -q 'cert:deny,fingerprint:sha256:' launcher/couchliteos_rdp.py
rg -q '"SDL_VIDEO_DRIVER": "wayland"' launcher/couchliteos_rdp.py
rg -q '"SDL_VIDEO_WAYLAND_ALLOW_LIBDECOR": "0"' launcher/couchliteos_rdp.py
refute rg -n '"/p:|/p:\{|--password' launcher/couchliteos_rdp.py launcher/couchliteos_app_runner.py launcher/couchliteos-launcher.py
rg -q 'stdin=subprocess.PIPE' launcher/couchliteos_app_runner.py
# The session unit runs with the appliance user's environment: nothing in it may run as root.
refute rg -q '^Exec[A-Za-z]*=[-@:!]*\+' services/couchliteos-rdp.service
refute rg -q '^ExecStartPre=' services/couchliteos-rdp.service
rg -q '^SuccessExitStatus=130 143$' services/couchliteos-rdp.service
rg -q '^RestartMode=direct$' services/couchliteos-rdp.service
rg -q '^InaccessiblePaths=/dev/fuse$' services/couchliteos-rdp.service
rg -q '^ExecStartPost=\+/usr/bin/systemctl reset-failed couchliteos-rdp.service couchliteos-rdp.path$' services/couchliteos-rdp-cleanup.service
rg -q '^ExecStartPost=\+/usr/bin/systemctl restart couchliteos-rdp.path$' services/couchliteos-rdp-cleanup.service
refute rg -q '^EnvironmentFile=' services/couchliteos-rdp-cleanup.service services/couchliteos-rdp-secret.service
[[ $(head -1 scripts/couchliteos-rdp-secret) == '#!/usr/bin/python3 -I' ]]
rg -q '^ExecStart=/usr/libexec/couchliteos-run-configured-app --rdp$' services/couchliteos-rdp.service
rg -q '^Restart=on-failure$' services/couchliteos-rdp.service
rg -q '^RestartPreventExitStatus=65 66$' services/couchliteos-rdp.service
rg -q '^OnFailure=couchliteos-rdp-cleanup.service$' services/couchliteos-rdp.service
rg -q '^User=couchliteos$' services/couchliteos-rdp.service
rg -q '^PathExists=/run/couchliteos/rdp.request$' services/couchliteos-rdp.path
rg -q '^PathExists=/run/couchliteos/rdp-secret.request$' services/couchliteos-rdp-secret.path
rg -q '^ExecStart=/usr/libexec/couchliteos-rdp-secret request$' services/couchliteos-rdp-secret.service
rg -q '^ProtectSystem=strict$' services/couchliteos-rdp-secret.service
rg -q '^PrivateNetwork=yes$' services/couchliteos-rdp-secret.service
rg -q '^NoNewPrivileges=yes$' services/couchliteos-rdp-secret.service
rg -q 'systemctl enable couchliteos-rdp.path couchliteos-rdp-secret.path' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'install -d -o root -g root -m 0700 /var/lib/couchliteos/rdp-secrets' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'couchliteos_rdp.py' build/configure.sh
rg -q 'couchliteos-rdp-secret' build/configure.sh
rg -q 'InaccessiblePaths=.*-/var/lib/couchliteos/rdp-secrets' services/couchliteos-support-export.service
rg -q 'FREERDP_SECRET_ARGUMENT' scripts/couchliteos-support-export
rg -q 'BTN_NORTH: ecodes.KEY_F12' launcher/gamepad-nav.py
rg -q '"REMOTE DESKTOP"' launcher/couchliteos-launcher.py
refute rg -q '^chromium' config/live-build/package-lists/couchliteos.list.chroot
rg -q 'https://dl.google.com/linux/chrome/deb/ stable main' config/live-build/archives/google-chrome.list.chroot
rg -q 'linux_signing_key.pub' build/sources.lock
rg -q '^command = /usr/bin/google-chrome-stable$' config/apps.d/35-google-chrome.ini
rg -q '"google-chrome-stable", "/usr/bin/google-chrome-stable"' launcher/couchliteos_browser.py
rg -q -- '--ozone-platform=wayland' config/apps.d/35-google-chrome.ini launcher/couchliteos_browser.py
rg -q 'browser.kiosk_command\(url\)' launcher/couchliteos-launcher.py
rg -q '^libavcodec61$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^systemd-timesyncd$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^wlrctl$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^wpasupplicant$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^qrencode$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^bluez$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^steam-devices$' config/live-build/package-lists/couchliteos.list.chroot
refute rg -q -g '!build/work' -g '!build/out' -g '!build/downloads' '^steam(-installer)?$|i386|multilib' config/live-build/package-lists/couchliteos.list.chroot build
refute rg -q -g '!build/work' -g '!build/out' -g '!build/downloads' '\bwvkbd\b' config build launcher scripts services
rg -q '^libspa-0\.2-bluetooth$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^python3-dbus$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^python3-gi$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^rfkill$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^wlr-randr$' config/live-build/package-lists/couchliteos.list.chroot
rg -q 'qrencode -t ANSIUTF8' scripts/couchliteos-tailscale-enrollment
rg -q "trap 'rm -f --.*URL_FILE.*' EXIT" scripts/couchliteos-tailscale
refute rg -q 'libfuse' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^gir1.2-gtk-4.0$' config/live-build/package-lists/couchliteos.list.chroot
rg -q 'OVMF_VARS_4M.fd' tests/qemu-smoke.sh
rg -q 'unit=1,file=' tests/qemu-smoke.sh
rg -q 'screendump' tests/qemu-smoke.sh
rg -q '/boot/grub/grub.cfg' tests/qemu-smoke.sh
rg -q 'COUCHLITEOS_APP_STARTED' scripts/couchliteos-run-app
rg -q 'moonlight-ready' scripts/couchliteos-qemu-smoke
rg -q 'chiaki-ng-ready' scripts/couchliteos-qemu-smoke
rg -q 'name=opt/couchliteos.smoke,string=apps' tests/qemu-smoke.sh
rg -q 'qemu-persistence-smoke' Makefile .github/workflows/build.yml
rg -q 'live-persistence-write' scripts/couchliteos-qemu-smoke tests/qemu-persistence-smoke.sh
rg -q 'live-persistence-absent' scripts/couchliteos-qemu-smoke tests/qemu-persistence-smoke.sh
rg -q 'ConditionPathExists=/sys/firmware/qemu_fw_cfg' services/couchliteos-qemu-smoke.service
rg -q 'COUCHLITEOS_SMOKE_APPS_READY' scripts/couchliteos-qemu-smoke tests/qemu-smoke.sh
rg -q 'COUCHLITEOS_SMOKE_RDP_READY' scripts/couchliteos-qemu-smoke tests/qemu-smoke.sh
rg -q 'verify_rdp_state' scripts/couchliteos-qemu-smoke
rg -q 'opt/couchliteos.timeout-scale' scripts/couchliteos-qemu-smoke tests/qemu-smoke.sh tests/qemu-persistence-smoke.sh tests/qemu-install-smoke.sh
rg -q 'couchliteos-firefox.path' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'couchliteos-support-export.path' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'couchliteos-configured-app.path couchliteos-osk.path' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'bluetooth.service couchliteos-bluetooth.service' config/live-build/hooks/live/0100-couchliteos.hook.chroot
refute rg -q "$removed_units" config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^PathExists=/run/couchliteos/support-export.request$' services/couchliteos-support-export.path
rg -q '^ExecStart=/usr/libexec/couchliteos-support-export$' services/couchliteos-support-export.service
removed_feature='TRIPLE[- ]?TAP|triple[- ]?tap|escape''-guard|stop''-active-app'
refute rg -q -g '!build/work/**' -g '!build/out/**' -g '!tests/test-static.sh' \
  "$removed_feature" launcher scripts services build config docs tests
rg -q 'ACTIVE APPLICATIONS' launcher/couchliteos-launcher.py
rg -Fq 'close-{app.status_id}' launcher/couchliteos-launcher.py
rg -q 'KEY_HOME.*BTN_MODE' launcher/gamepad-nav.py
rg -q 'wlrctl.*toplevel.*focus' launcher/gamepad-nav.py
rg -q 'named_devices' launcher/couchliteos_bluetooth.py
rg -q 'wpctl.*set-default' launcher/couchliteos_audio.py
rg -q 'EncryptedMediaExtensions' overlay/etc/firefox/policies/policies.json
rg -q 'WILL MOUNT TEMPORARILY' launcher/couchliteos_support.py
rg -q 'InaccessiblePaths=.*\/var\/lib\/couchliteos\/home.*\/var\/lib\/tailscale.*-\/var\/lib\/bluetooth' services/couchliteos-support-export.service
rg -q '^RuntimeDirectoryMode=0700$' services/couchliteos-bluetooth.service
rg -q '^User=couchliteos$' services/couchliteos-bluetooth.service
rg -q '^ExecStart=/usr/libexec/couchliteos-bluetoothd$' services/couchliteos-bluetooth.service
rg -q '^After=.*dbus.service.*bluetooth.service$' services/couchliteos-audio.service
for unit in audio bluetooth moonlight chiaki firefox; do
  rg -q '^Environment=PIPEWIRE_RUNTIME_DIR=/run/couchliteos$' "services/couchliteos-$unit.service"
done
rg -q '^d /run/couchliteos 0700 couchliteos couchliteos -$' overlay/etc/tmpfiles.d/couchliteos.conf
refute rg -q '^RuntimeDirectory=' services/couchliteos-launcher.service
rg -q '^/usr/bin/wireplumber --profile main-systemwide ' scripts/couchliteos-audio
rg -q '^PIPEWIRE_DAEMON=true PIPEWIRE_CORE=pipewire-0 /usr/bin/pipewire ' scripts/couchliteos-audio
rg -q 'couchliteos_bluetooth.py' build/configure.sh
rg -q 'couchliteos-bluetoothd' build/configure.sh
rg -q 'couchliteos-run-configured-app' build/configure.sh
rg -q 'couchliteos-osk-session' build/configure.sh
rg -q 'usr/share/couchliteos/apps.d' build/configure.sh
refute rg -q 'terminal_command|curses\.endwin' launcher/couchliteos-launcher.py
refute rg -q 'shell=True|\beval\b' launcher/couchliteos_apps.py launcher/couchliteos_app_runner.py launcher/couchliteos-launcher.py launcher/couchliteos_osk.py launcher/couchliteos_rdp.py scripts/couchliteos-rdp-secret
for forbidden in 'bluetooth''ctl' 'curses\.endwin' 'terminal_''command' 'SIG''INT' 'kill\(' 'shell=True'; do
  refute rg -n "$forbidden" launcher/couchliteos_bluetooth.py scripts/couchliteos-bluetoothd
done
refute rg -q '\bsudo\b' launcher scripts/couchliteos-support-export services/couchliteos-support-export.service
rg -q '\["wlr-randr", "--output"' launcher/couchliteos_display.py
rg -q 'append\("--dryrun"\)' launcher/couchliteos_display.py
rg -q 'support-export.request' launcher/couchliteos_support.py
refute rg -q '\brunuser\b|\bresolvectl\b' scripts/couchliteos-support-export
rg -q '/usr/bin/setpriv' scripts/couchliteos-support-export tests/test_support.py
rg -q '^ExecStart=/usr/sbin/usbipd --ipv4$' services/couchliteos-usbipd.service
refute rg -q -- '--foreground' services/couchliteos-usbipd.service
rg -q 'COUCHLITEOS_SMOKE_USBIP_READY' scripts/couchliteos-qemu-smoke tests/qemu-smoke.sh
rg -q 'COUCHLITEOS_SMOKE_HWDETECT_READY' scripts/couchliteos-qemu-smoke tests/qemu-smoke.sh
rg -q 'COUCHLITEOS_SMOKE_BLUETOOTH_READY' scripts/couchliteos-qemu-smoke tests/qemu-smoke.sh
refute rg -q -- '-kernel|-initrd' tests/qemu-install-smoke.sh
rg -q 'qemu_iso_boot.py' tests/qemu-install-smoke.sh
# The installer skips the account questions (the box logs in as couchliteos by itself); the
# install test adds its own installer-test login for the update check.
rg -q '^d-i passwd/make-user boolean false$' config/live-build/includes.installer/preseed.cfg
rg -q '^d-i passwd/root-login boolean true$' config/live-build/includes.installer/preseed.cfg
rg -q '^d-i passwd/root-password-crypted password \*$' config/live-build/includes.installer/preseed.cfg
refute rg -q '^d-i passwd/' tests/installer-preseed.cfg
rg -q '^d-i preseed/late_command string in-target .*useradd .*--groups sudo .*installer-test' tests/installer-preseed.cfg
rg -q '32G' tests/qemu-install-smoke.sh
rg -q 'blank_disk=true' tests/qemu-install-smoke.sh
rg -q 'COUCHLITEOS_SMOKE_INSTALLED_DISK_READY' scripts/couchliteos-qemu-smoke tests/qemu-install-smoke.sh
rg -q 'COUCHLITEOS_QEMU_INSTALLER_SCREENSHOT' tests/qemu-install-smoke.sh .github/workflows/build.yml
rg -q 'COUCHLITEOS_QEMU_INSTALLED_SCREENSHOT' tests/qemu-install-smoke.sh .github/workflows/build.yml
rg -q 'Screenshot evidence missing' tests/qemu-install-smoke.sh
refute rg -q 'install -d.*dirname.*screenshot' tests/qemu-install-smoke.sh
rg -q 'findmnt -n -o FSTYPE /' scripts/couchliteos-qemu-smoke
rg -q 'lsblk --fs' scripts/couchliteos-qemu-smoke
rg -q '^[[:space:]]*blkid$' scripts/couchliteos-qemu-smoke
rg -q '^release-gauntlet:' Makefile
rg -q 'python3 tools/mutants.py' tools/release-gauntlet.sh
rg -q 'clear_bytecode' tools/mutants.py
rg -q 'make qemu-install-smoke' tools/release-gauntlet.sh
rg -q 'NRestarts' scripts/couchliteos-qemu-smoke
rg -q 'couchliteos-audio.service' scripts/couchliteos-qemu-smoke
rg -q '^runuser -u couchliteos -- env XDG_RUNTIME_DIR=/run/couchliteos' scripts/couchliteos-qemu-smoke
digest_pattern='s''ha-?256|s''ha256|\.s''ha256'
digest_control=$(mktemp)
printf 'sha256\n' > "$digest_control"
rg -q -i "$digest_pattern" "$digest_control"
rm -f -- "$digest_control"
if rg -n -i "$digest_pattern" \
  scripts/couchliteos-support-export launcher/couchliteos_support.py; then
  echo 'Support export must not create checksum sidecars or manifests.' >&2
  exit 1
else
  status=$?
  [[ $status == 1 ]] || exit "$status"
fi
rg -q 'MIN_FREE' scripts/couchliteos-support-export

python3 -m py_compile launcher/couchliteos-launcher.py launcher/couchliteos_apps.py \
  launcher/couchliteos_app_runner.py launcher/couchliteos_setup.py launcher/couchliteos_osk.py \
  launcher/couchliteos_display.py launcher/couchliteos_support.py \
  launcher/couchliteos_bluetooth.py launcher/couchliteos_audio.py launcher/gamepad-nav.py \
  launcher/couchliteos_audiomenu.py launcher/couchliteos_battery.py \
  launcher/couchliteos_rdp.py launcher/couchliteos_stream.py launcher/couchliteos_controllers.py \
  launcher/couchliteos_pcstatus.py launcher/couchliteos_recent.py launcher/couchliteos_home.py \
  launcher/couchliteos_session.py launcher/couchliteos_tvlayout.py \
  launcher/couchliteos_update.py launcher/couchliteos_errors.py launcher/couchliteos_confirm.py \
  launcher/couchliteos_updater.py launcher/couchliteos_softwareupdate.py launcher/couchliteos_snapshot.py \
  launcher/couchliteos_browser.py launcher/couchliteos_browsersetup.py \
  scripts/couchliteos-rdp-secret \
  scripts/couchliteos-host-address scripts/couchliteos-support-export \
  scripts/couchliteos-bluetoothd scripts/couchliteos-hwdetect
python3 -m py_compile launcher/couchliteos_cec.py scripts/couchliteos-cec

python3 - <<'PY'
import configparser, pathlib, re, subprocess
hook = pathlib.Path('config/live-build/hooks/live/0100-couchliteos.hook.chroot').read_text()
assert hook.index('groupadd --system seat') < hook.index('useradd --uid 1000')
block = hook.split("cat > /var/lib/couchliteos/config.ini <<'EOF'", 1)[1].split('\nEOF', 1)[0]
c = configparser.ConfigParser(); c.read_string(block)
assert c.getboolean('tailscale', 'enabled') is False
assert c.getboolean('tailscale', 'ssh_enabled') is False
assert c.getboolean('tailscale', 'remote_usbip') is False
assert c.get('tailscale', 'allowed_usbip_peer') == ''
assert c.get('host:gaming-pc', 'address_mode') == 'auto'
for lock in ('build/applications.lock', 'build/sources.lock'):
    for line in pathlib.Path(lock).read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        fields = line.split('|')
        assert len(fields) == 4, (lock, line)
        assert fields[2].startswith('https://'), (lock, line)

for workflow in pathlib.Path('.github/workflows').glob('*.yml'):
    lines = workflow.read_text().splitlines()
    i = 0
    while i < len(lines):
        match = re.match(r'^(\s*)run:\s*\|\s*$', lines[i])
        if not match:
            i += 1
            continue
        start = i
        parent_indent = len(match.group(1))
        i += 1
        block = []
        while i < len(lines):
            indent = len(lines[i]) - len(lines[i].lstrip())
            if lines[i].strip() and indent <= parent_indent:
                break
            block.append(lines[i])
            i += 1
        nonblank = [line for line in block if line.strip()]
        block_indent = min(len(line) - len(line.lstrip()) for line in nonblank)
        script = '\n'.join(line[block_indent:] if line.strip() else '' for line in block)
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        assert result.returncode == 0, (workflow, start + 1, result.stderr)
PY

if rg -n --hidden -g '!tests/test-static.sh' \
  'tskey-(auth|api|client|scim|webhook)-[A-Za-z0-9]{8,}' .; then
  echo 'Possible Tailscale secret found in source tree' >&2
  exit 1
fi

for required in \
  services/couchliteos-launcher.service \
  services/couchliteos-hwdetect-early.service \
  services/couchliteos-hwdetect.service \
  services/couchliteos-bluetooth.service \
  services/couchliteos-moonlight.service \
  services/couchliteos-chiaki.service \
  services/couchliteos-firefox.service \
  services/couchliteos-firefox.path \
  services/couchliteos-qemu-smoke.service \
  services/couchliteos-support-export.path \
  services/couchliteos-support-export.service \
  services/couchliteos-configured-app.path \
  services/couchliteos-configured-app.service \
  services/couchliteos-osk.path \
  services/couchliteos-osk.service \
  services/couchliteos-rdp.path \
  services/couchliteos-rdp.service \
  services/couchliteos-rdp-cleanup.service \
  services/couchliteos-rdp-secret.path \
  services/couchliteos-rdp-secret.service \
  services/couchliteos-usbipd.service \
  services/couchliteos-network-ready.service \
  services/couchliteos-tailscale-enroll.service; do
  test -s "$required"
done

# A service that hits its start limit also fails the .path unit that triggers it
# (Result: unit-start-limit-hit), so later requests are ignored until reboot.
# Every path-triggered service with a start limit must re-arm its path on failure.
for path_unit in services/*.path; do
  service=services/$(sed -n 's/^Unit=//p' "$path_unit")
  rg -q '^StartLimitBurst=' "$service" || continue
  rg -q '^OnFailure=(couchliteos-path-rearm@.+|couchliteos-rdp-cleanup)\.service$' "$service" || {
    printf 'static test: %s has a start limit but no OnFailure= that re-arms %s\n' "$service" "$path_unit" >&2
    exit 1
  }
  # With the default restart mode OnFailure= fires on every retried crash, which
  # would reset the start limit and retry forever.
  if rg -q '^Restart=' "$service"; then
    rg -q '^RestartMode=direct$' "$service"
  fi
done
rg -q '^ExecStart=/usr/bin/systemctl reset-failed couchliteos-%i.service couchliteos-%i.path$' services/couchliteos-path-rearm@.service
rg -q '^ExecStart=/usr/bin/systemctl restart couchliteos-%i.path$' services/couchliteos-path-rearm@.service

tmp=$(mktemp -d)
trap 'find "$tmp" -depth -delete' EXIT
mkdir -p "$tmp/sys/bus/usb/devices/1-2/1-2:1.0" "$tmp/log"
printf '046d\n' > "$tmp/sys/bus/usb/devices/1-2/idVendor"
printf 'c262\n' > "$tmp/sys/bus/usb/devices/1-2/idProduct"
printf 'wheel-01\n' > "$tmp/sys/bus/usb/devices/1-2/serial"
printf '00\n' > "$tmp/sys/bus/usb/devices/1-2/bDeviceClass"
printf '03\n' > "$tmp/sys/bus/usb/devices/1-2/1-2:1.0/bInterfaceClass"
printf '00\n' > "$tmp/sys/bus/usb/devices/1-2/1-2:1.0/bInterfaceProtocol"
printf '046d:c262:wheel-01\n' > "$tmp/allowlist"
COUCHLITEOS_SYSFS_ROOT="$tmp/sys" \
COUCHLITEOS_USBIP_ALLOWLIST="$tmp/allowlist" \
COUCHLITEOS_USBIP_LOG="$tmp/log/usbip.log" \
  bash usbip/couchliteos-usbip list | grep -q 'allowed'
COUCHLITEOS_SYSFS_ROOT="$tmp/sys" \
COUCHLITEOS_USBIP_ALLOWLIST="$tmp/allowlist" \
COUCHLITEOS_USBIP_LOG="$tmp/log/usbip.log" \
  bash usbip/couchliteos-usbip unbind-all

# /run/couchliteos belongs to the unprivileged appliance user: the root exporter
# must mount in its own private directory and never follow links there.
rg -q 'mkdtemp\(prefix="support-media-"' scripts/couchliteos-support-export
refute rg -q 'RUN / "support-media"|LOCK\.open|os\.chmod\(temporary' scripts/couchliteos-support-export

# Saved Wi-Fi (NetworkManager keyfiles) must survive a live boot with
# persistence: the documented persistence.conf and the smoke test's backend both
# persist the keyfile directory, and the image creates it root-only so the
# persisted copy is never readable by the unprivileged couchliteos user.
for f in tests/qemu-persistence-smoke.sh; do
  rg -q '^/etc/NetworkManager/system-connections source=nm-connections$' "$f"
done
rg -q 'install -d -o root -g root -m 0700 /etc/NetworkManager/system-connections' \
  config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '/etc/NetworkManager/system-connections' scripts/couchliteos-qemu-smoke

# The Bluetooth service (user couchliteos) unblocks a soft-blocked adapter with
# rfkill, which needs write access to /dev/rfkill.
rg -q '"rfkill", "unblock", "bluetooth"' scripts/couchliteos-bluetoothd
rg -q '^KERNEL=="rfkill", SUBSYSTEM=="misc", GROUP="couchliteos", MODE="0660"$' \
  overlay/etc/udev/rules.d/70-couchliteos-rfkill.rules

# PlayStation pads: SDL (Moonlight, Chiaki) reads the touchpad over hidraw, which the couchliteos
# user (group input, no login session) only gets through this rule.
ps_rules=overlay/etc/udev/rules.d/71-couchliteos-playstation.rules
rg -q '^KERNEL=="hidraw\*", SUBSYSTEM=="hidraw", ATTRS\{idVendor\}=="054c", ATTRS\{idProduct\}=="[0-9a-f|]*0ce6[0-9a-f|]*".*GROUP="input", MODE="0660"$' "$ps_rules"
rg -q '^KERNEL=="hidraw\*", SUBSYSTEM=="hidraw", KERNELS=="[^"]*0005:054C:0CE6\.\*[^"]*", GROUP="input", MODE="0660"$' "$ps_rules"
for product in 05c4 09cc 0ba0 0ce6 0df2; do
  rg -q "ATTRS\\{idProduct\\}==\"[^\"]*\\b$product\\b" "$ps_rules"
  # The 0ba0 dongle is USB only; every pad itself also appears over Bluetooth.
  [ "$product" = 0ba0 ] || rg -qi "0005:054C:${product}\\." "$ps_rules"
done
refute rg -q 'MODE="066[^0]|MODE="0666' "$ps_rules"
for unit in services/couchliteos-moonlight.service services/couchliteos-chiaki.service; do
  rg -q '^SupplementaryGroups=.*\binput\b' "$unit"
done

# Sleep and wake: the launcher's SLEEP request, resume marker, wake sources, Wake-on-LAN.
rg -q '^PathExists=/run/couchliteos/suspend$' services/couchliteos-suspend.path
rg -q '^Unit=couchliteos-suspend.service$' services/couchliteos-suspend.path
rg -q '^ExecStart=/usr/bin/systemctl suspend$' services/couchliteos-suspend.service
# /run survives a suspend: the request must be gone before suspending, or waking would suspend again.
rg -q '^ExecStartPre=/usr/bin/rm -f /run/couchliteos/suspend$' services/couchliteos-suspend.service
rg -q '^After=suspend.target$' services/couchliteos-resume.service
rg -q '^WantedBy=suspend.target$' services/couchliteos-resume.service
rg -q '^ExecStart=/usr/bin/touch /run/couchliteos/resumed$' services/couchliteos-resume.service
rg -q '^systemctl enable couchliteos-suspend.path couchliteos-resume.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'launcher/couchliteos_power.py' build/configure.sh
rg -q '"SLEEP", "suspend"' launcher/couchliteos-launcher.py
rg -q 'couchliteos.smoke' launcher/couchliteos_power.py
rg -q '^RUN = pathlib.Path\(os.environ.get\("COUCHLITEOS_RUN_DIR", "/run/couchliteos"\)\)$' launcher/gamepad-nav.py
rg -q '^SLEEP_REQUEST = RUN / "suspend"$' launcher/gamepad-nav.py
rg -q 'ATTR\{bDeviceClass\}=="e0".*ATTR\{power/wakeup\}="enabled"' overlay/etc/udev/rules.d/75-couchliteos-wakeup.rules
rg -q 'ATTR\{bInterfaceClass\}=="e0".*power/wakeup' overlay/etc/udev/rules.d/75-couchliteos-wakeup.rules
rg -q 'ATTR\{bInterfaceClass\}=="03".*ATTR\{bInterfaceProtocol\}=="01".*power/wakeup' overlay/etc/udev/rules.d/75-couchliteos-wakeup.rules
rg -q '^ethernet.wake-on-lan=64$' overlay/etc/NetworkManager/conf.d/20-couchliteos-wol.conf
# nvidia only: keep video memory across suspend. The general profile's drivers are untouched.
rg -q '^nvidia-suspend-common$' config/profiles/nvidia/package-lists/nvidia.list.chroot
rg -q '^options nvidia NVreg_PreserveVideoMemoryAllocations=1 NVreg_TemporaryFilePath=/var/tmp$' config/profiles/nvidia/overlay/etc/modprobe.d/couchliteos-nvidia.conf
rg -q '^options nvidia-current NVreg_PreserveVideoMemoryAllocations=1 NVreg_TemporaryFilePath=/var/tmp$' config/profiles/nvidia/overlay/etc/modprobe.d/couchliteos-nvidia.conf
rg -q 'nvidia-suspend.service nvidia-resume.service' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
refute rg -qi 'nvidia-suspend|PreserveVideoMemory' config/profiles/general config/live-build overlay
# Hardware gating: SLEEP is offered only where the PC can suspend, and it is re-checked at each step.
rg -q '^SLEEP_UNSUPPORTED = "SLEEP: NOT SUPPORTED ON THIS PC"$' launcher/couchliteos-launcher.py
rg -q 'power.effective_settings\(power.load_settings\(\), self.can_sleep, self.can_wake, self.battery.on_battery\(\)' launcher/couchliteos-launcher.py
# Idle sleep also needs something that can wake the box again; the saved timeout is ignored, not erased, and the reason is shown.
rg -q '^IDLE_SLEEP_NO_WAKE = \"IDLE SLEEP OFF: NO CONTROLLER CAN WAKE THIS PC\"$' launcher/couchliteos-launcher.py
rg -q 'settings: Settings, suspend_ok: bool, wake_ok: bool = True, on_battery: bool = False' launcher/couchliteos_power.py
rg -q '^    if not app_owns_pad\(\) and power.can_suspend\(\):$' launcher/gamepad-nav.py
rg -q '^import couchliteos_power as power$' launcher/gamepad-nav.py
# The root side refuses too: systemctl suspend fails when logind says the PC cannot, after the request is gone.
rg -q 'Sleep verb .suspend. is not configured' services/couchliteos-suspend.service
refute rg -q '^Condition|^ExecCondition' services/couchliteos-suspend.service
# Wake-on-LAN support is read by udev as root (ethtool needs CAP_NET_ADMIN) and shown only when it has magic packet.
rg -q '^ethtool$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^ACTION=="add\|move", SUBSYSTEM=="net", SUBSYSTEMS=="pci\|usb", RUN\+="/usr/bin/python3 /usr/libexec/couchliteos_power.py wake-on-lan"$' overlay/etc/udev/rules.d/75-couchliteos-wakeup.rules
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_power.py" "\$CHROOT/usr/libexec/couchliteos_power.py"$' build/configure.sh

# Couch to game: the stream module ships in the image, is reachable from Settings,
# and run-app only ever receives a host and app name that the module validated.
rg -q 'couchliteos_stream.py' build/configure.sh
rg -q '^    "STREAMING",$' launcher/couchliteos-launcher.py
rg -q 'def autostream' launcher/couchliteos-launcher.py
rg -q 'couchliteos_stream.py take-request' scripts/couchliteos-run-app
refute rg -q 'shell=True|\beval\b|os\.system' launcher/couchliteos_stream.py
printf 'pc.lan\nSteam Big Picture\n' > "$tmp/stream.request"
mapfile -t stream_request < <(python3 launcher/couchliteos_stream.py take-request "$tmp/stream.request")
[[ ${#stream_request[@]} == 2 && ${stream_request[0]} == pc.lan && ${stream_request[1]} == 'Steam Big Picture' ]]
test ! -e "$tmp/stream.request"
printf -- '--evil\nDesktop\n' > "$tmp/stream.request"
stream_status=0
python3 launcher/couchliteos_stream.py take-request "$tmp/stream.request" > "$tmp/stream.out" || stream_status=$?
[[ $stream_status == 1 && ! -s "$tmp/stream.out" && ! -e "$tmp/stream.request" ]]

# HDMI-CEC TV control: optional, started only when the kernel exposes /dev/cec*.
for required in services/couchliteos-cec.service services/couchliteos-cec-wake.service \
  'services/couchliteos-pulse8-inputattach@.service' overlay/etc/udev/rules.d/75-couchliteos-cec.rules \
  scripts/couchliteos-cec launcher/couchliteos_cec.py; do
  test -s "$required"
done
rg -q '^ConditionPathExistsGlob=/dev/cec\[0-9\]\*$' services/couchliteos-cec.service
rg -q '^ExecStart=/usr/libexec/couchliteos-cec$' services/couchliteos-cec.service
rg -q 'install -D -m 0755 "\$ROOT/scripts/couchliteos-cec" "\$CHROOT/usr/libexec/couchliteos-cec"' build/configure.sh
rg -q 'install -D -m 0644 "\$ROOT/launcher/couchliteos_cec.py" "\$CHROOT/usr/libexec/couchliteos_cec.py"' build/configure.sh
rg -q '^ConditionPathExistsGlob=/dev/cec\[0-9\]\*$' services/couchliteos-cec-wake.service
rg -q '^After=suspend.target$' services/couchliteos-cec-wake.service
rg -q '^WantedBy=suspend.target$' services/couchliteos-cec-wake.service
rg -q '^ExecStart=/usr/bin/systemctl restart couchliteos-cec.service$' services/couchliteos-cec-wake.service
rg -q '^systemctl enable couchliteos-cec-wake.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
# TV Standby before the PC sleeps: sleep waits for this unit, so it is short and bounded.
for required in 'ConditionPathExistsGlob=/dev/cec\[0-9\]\*' 'Before=sleep.target' 'WantedBy=sleep.target' 'TimeoutStartSec=3' 'ExecStart=/usr/libexec/couchliteos-cec --standby'; do
  rg -q "^$required\$" services/couchliteos-cec-sleep.service
done
rg -q '^systemctl enable couchliteos-cec-sleep.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
refute rg -q 'sh -c|/bin/sh|/bin/bash' services/couchliteos-cec.service services/couchliteos-cec-wake.service services/couchliteos-cec-sleep.service
rg -q 'SUBSYSTEM=="cec".*SYSTEMD_WANTS.*couchliteos-cec\.service' overlay/etc/udev/rules.d/75-couchliteos-cec.rules
rg -q 'idVendor.*2548.*SYSTEMD_WANTS.*couchliteos-pulse8-inputattach@%k\.service' overlay/etc/udev/rules.d/75-couchliteos-cec.rules
# The launcher runs as the couchliteos user (group video) and calls cec-ctl on /dev/cec* itself.
rg -q 'SUBSYSTEM=="cec".*GROUP="video".*MODE="0660"' overlay/etc/udev/rules.d/75-couchliteos-cec.rules
rg -q '^ExecStart=/usr/bin/inputattach --pulse8-cec /dev/%I$' 'services/couchliteos-pulse8-inputattach@.service'
rg -q '^v4l-utils$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^inputattach$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '"TV CONTROL"' launcher/couchliteos-launcher.py
rg -q 'import couchliteos_cec as cec' launcher/gamepad-nav.py
# TV Standby on sleep only when the TV shows this box: the daemon keeps a marker in /run/couchliteos (it is
# otherwise read-only to the daemon), the sleep hook sends Standby only while the marker exists.
rg -q '^ReadWritePaths=-/run/couchliteos$' services/couchliteos-cec.service
rg -q '^RUN = pathlib.Path\(os.environ.get\("COUCHLITEOS_RUN_DIR", "/run/couchliteos"\)\)$' launcher/couchliteos_cec.py
rg -q '^ACTIVE_SOURCE_MARKER = RUN / "cec-active-source"$' launcher/couchliteos_cec.py
rg -q 'O_NOFOLLOW' launcher/couchliteos_cec.py
rg -q 'phys_addr=adapter.phys_addr, marker=cec.ACTIVE_SOURCE_MARKER' scripts/couchliteos-cec
rg -q 'cec.should_standby_on_sleep\(settings, cec.ACTIVE_SOURCE_MARKER\)' scripts/couchliteos-cec
# TEST REMOTE BUTTONS exists only with an adapter; CEC colour keys reach the launcher by the gamepad's focus rule.
rg -q 'cec.REMOTE_TEST_ROW\] if status.adapter is not None' launcher/couchliteos-launcher.py
rg -q 'key in CEC_APP_KEYS or not navigation_blocked\(OSK_ACTIVE.exists\(\)\)' launcher/gamepad-nav.py

# Easier everyday use: controller batteries
rg -q 'couchliteos_controllers.py' build/configure.sh
rg -q '^import couchliteos_controllers as controllers' launcher/couchliteos-launcher.py
rg -q '/sys/class/power_supply' launcher/couchliteos_controllers.py
# Hardware gating: no bluetoothctl polling without an adapter; nothing shown without a reading.
rg -q '/sys/class/bluetooth' launcher/couchliteos_controllers.py
rg -q 'if not bluetooth_present' launcher/couchliteos_controllers.py

# Easier everyday use: sound follows the TV (declarative WirePlumber 0.5 rule)
rg -q 'cp -a "\$ROOT/overlay/\."' build/configure.sh
rg -q -- '--profile main-systemwide' scripts/couchliteos-audio
# UCM files (apt-recommends is off): SOF and AMD ACP cards need them.
rg -q '^alsa-ucm-conf$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^alsa-topology-conf$' config/live-build/package-lists/couchliteos.list.chroot
rg -q 'find /usr/share/alsa/ucm2 ' config/live-build/hooks/live/0100-couchliteos.hook.chroot
# Plain HDA cards: HDMI/DP sinks exist only under an HDMI card profile. A WirePlumber
# hook picks it when a TV is attached; the launcher offers the card's other outputs.
hook=overlay/usr/share/wireplumber/scripts/couchliteos/find-tv-profile.lua
tvconf=overlay/etc/wireplumber/wireplumber.conf.d/51-couchliteos-tv-profile.conf
rg -Fq 'after = "device/find-stored-profile"' "$hook"
rg -Fq 'before = "device/find-preferred-profile"' "$hook"
rg -Fq 'profile.available == "yes"' "$hook"
rg -Fq '"^output:hdmi%-"' "$hook"
rg -Fq 'device.properties ["device.api"] ~= "alsa"' "$hook"
rg -Fq 'name = couchliteos/find-tv-profile.lua, type = script/lua' "$tvconf"
rg -Fq 'hooks.device.profile.couchliteos-find-tv = optional' "$tvconf"
rg -q 'find-tv-profile.lua' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'for tool in pw-dump pw-cli wpctl' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '"pw-cli", "set-param"' launcher/couchliteos_audio.py
rg -Fq '"save": True' launcher/couchliteos_audio.py
rg -q 'audio.query_profile_outputs\(\)' launcher/couchliteos-launcher.py
rg -q '/proc/asound/cards' scripts/couchliteos-support-export
rg -Fq 'card*/eld#*' scripts/couchliteos-support-export
rg -Fq 'node.name = "~.*HDMI.*"' overlay/etc/wireplumber/wireplumber.conf.d/50-couchliteos-hdmi-default.conf
python3 - <<'PY'
import pathlib, re
conf = pathlib.Path('overlay/etc/wireplumber/wireplumber.conf.d/50-couchliteos-hdmi-default.conf').read_text()
body = '\n'.join(line for line in conf.splitlines() if not line.lstrip().startswith('#'))
assert body.count('[') == body.count(']') and body.count('{') == body.count('}'), 'unbalanced brackets'
assert body.lstrip().startswith('monitor.alsa.rules = ['), 'must extend monitor.alsa.rules'
for needle in ('"~.*hdmi.*"', '"~.*HDMI.*"', '"~.*DisplayPort.*"', 'media.class = "Audio/Sink"', 'update-props'):
    assert needle in body, needle
priority = int(re.search(r'priority\.session\s*=\s*(\d+)', body).group(1))
# WirePlumber 0.5 scores ALSA analog sinks 1009 and Bluetooth sinks 1010.
assert priority > 1010, priority
# Hardware gating: the rule only re-ranks sinks that already exist, and sets nothing
# that could activate a profile or route. WirePlumber itself skips profiles and
# routes whose availability is "no", so an unplugged HDMI/DP port stays unused.
props = re.findall(r'^\s*([A-Za-z][\w.-]*)\s*=', body, re.M)
assert [p for p in props if p.startswith(('priority', 'device', 'api.', 'session', 'node.pause', 'node.always'))] == ['priority.session'], props
assert body.count('media.class = "Audio/Sink"') == body.count('node.name =') + body.count('node.description ='), 'every match must be limited to sinks'
PY

# Easier everyday use: update-available notice
rg -q 'couchliteos_update.py' build/configure.sh
rg -q '^import couchliteos_update as update' launcher/couchliteos-launcher.py
rg -q '"CHECK FOR UPDATES"' launcher/couchliteos-launcher.py
rg -q 'https://api.github.com/repos/Dudiebug/couchliteos/releases/latest' launcher/couchliteos_update.py
rg -q '/etc/couchliteos-version' launcher/couchliteos_update.py
refute rg -n 'Authorization|Cookie|machine-id' launcher/couchliteos_update.py
# Hardware gating: the check is skipped without a default route, before anything is recorded.
rg -q '/proc/net/route' launcher/couchliteos_update.py
rg -q 'if not self.online\(\)' launcher/couchliteos_update.py

# Software update: Settings > SOFTWARE UPDATE only asks; a root service copies the new release over the old.
rg -q '^PathExists=/run/couchliteos/update-install$' services/couchliteos-update.path
rg -q '^Unit=couchliteos-update.service$' services/couchliteos-update.path
rg -q '^WantedBy=graphical.target$' services/couchliteos-update.path
# The request goes first, or the path unit would start another update the moment this one ends.
rg -q '^ExecStartPre=/usr/bin/rm -f /run/couchliteos/update-install$' services/couchliteos-update.service
rg -Fq 'ExecStart=/usr/bin/systemd-inhibit --what=sleep:idle --why="Installing a CouchLiteOS update" /usr/libexec/couchliteos-updater run' \
  services/couchliteos-update.service
rg -q '^User=root$' services/couchliteos-update.service
refute rg -q '^Condition|^ExecCondition' services/couchliteos-update.service
rg -q '^systemctl enable couchliteos-update.path$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^rsync$' config/live-build/package-lists/couchliteos.list.chroot
rg -Fq '"$ROOT/launcher/couchliteos_updater.py" "$CHROOT/usr/libexec/couchliteos-updater"' build/configure.sh
rg -q -- '-m 0755 "\$ROOT/launcher/couchliteos_updater.py"' build/configure.sh
rg -Fq '"$CHROOT/usr/libexec/couchliteos_softwareupdate.py"' build/configure.sh
rg -q '^import couchliteos_softwareupdate as softwareupdate' launcher/couchliteos-launcher.py
rg -q '"SOFTWARE UPDATE"' launcher/couchliteos-launcher.py
rg -q 'test_updater.py test_softwareupdate.py' launcher/Makefile
# The QEMU install smoke proves it: update the installed disk from the ISO, boot it, check what must survive.
rg -q '^  update-apply\)$' scripts/couchliteos-qemu-smoke
rg -q '^  update-check\)$' scripts/couchliteos-qemu-smoke
rg -Fq 'couchliteos-updater apply-iso /dev/sr0 --force --no-reboot' scripts/couchliteos-qemu-smoke
rg -q 'echo COUCHLITEOS_SMOKE_UPDATE_APPLIED$' scripts/couchliteos-qemu-smoke
rg -q 'echo COUCHLITEOS_SMOKE_UPDATE_READY$' scripts/couchliteos-qemu-smoke
rg -q '^boot_and_wait update-apply COUCHLITEOS_SMOKE_UPDATE_APPLIED$' tests/qemu-install-smoke.sh
rg -q '^boot_and_wait update-check COUCHLITEOS_SMOKE_UPDATE_READY$' tests/qemu-install-smoke.sh
# The updater is plain standard-library Python with no shell strings and no network credentials.
refute rg -n 'shell=True|os\.system|^import requests|Authorization|Cookie' launcher/couchliteos_updater.py launcher/couchliteos_softwareupdate.py
# Every update first saves the running system as one rolling snapshot (squashfs, zstd).
rg -q '^squashfs-tools$' config/live-build/package-lists/couchliteos.list.chroot
rg -Fq '"$ROOT/launcher/couchliteos_snapshot.py" "$CHROOT/usr/libexec/couchliteos_snapshot.py"' build/configure.sh
rg -q '^import couchliteos_snapshot as snapshot' launcher/couchliteos_updater.py
rg -q 'test_snapshot.py' launcher/Makefile
rg -q '^PathExists=/run/couchliteos/snapshot-delete$' services/couchliteos-snapshot-delete.path
rg -q '^ExecStartPre=/usr/bin/rm -f /run/couchliteos/snapshot-delete$' services/couchliteos-snapshot-delete.service
rg -q '^ExecStart=/usr/libexec/couchliteos-updater delete-snapshot$' services/couchliteos-snapshot-delete.service
rg -q '^systemctl enable couchliteos-snapshot-delete.path$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'test -s /var/lib/couchliteos-update/snapshot/previous.squashfs' scripts/couchliteos-qemu-smoke
# Root never works by path in a directory the couchliteos user owns: the snapshot, the work dirs,
# locks and logs are root's own, and root's writes into the user's directories go through
# couchliteos_safefile (a directory descriptor, O_EXCL|O_NOFOLLOW temporaries, fchmod/fchown).
rg -q '^SNAP_REL = "var/lib/couchliteos-update/snapshot"' launcher/couchliteos_snapshot.py
rg -q '^WORK_DIR = pathlib.Path\("/run/couchliteos-update"\)' launcher/couchliteos_updater.py launcher/couchliteos_browser.py
rg -q '^LOG_REL = "var/log/couchliteos-update/update.log"$' launcher/couchliteos_updater.py
rg -q '^LOG_REL = "var/log/couchliteos-update/browser.log"$' launcher/couchliteos_browser.py
rg -q '^d /run/couchliteos-update 0700 root root -$' overlay/etc/tmpfiles.d/couchliteos.conf
rg -q '^d /var/log/couchliteos-update 0755 root root -$' overlay/etc/tmpfiles.d/couchliteos.conf
rg -q '^d /var/lib/couchliteos-update 0755 root root -$' overlay/etc/tmpfiles.d/couchliteos.conf
rg -Fq '"$ROOT/launcher/couchliteos_safefile.py" "$CHROOT/usr/libexec/couchliteos_safefile.py"' build/configure.sh
rg -q 'test_safefile.py' launcher/Makefile
rg -q '^import couchliteos_safefile as safefile$' launcher/couchliteos_updater.py launcher/couchliteos_snapshot.py launcher/couchliteos_browser.py
refute rg -n 'tempfile\.mkstemp|os\.chmod\(temporary|os\.chown\(temporary' launcher/couchliteos_updater.py launcher/couchliteos_snapshot.py launcher/couchliteos_browser.py
refute rg -n 'run_dir / (PROBE_DIR|DISK_DIR|DISK_IMAGE_DIR|LOCK_NAME)|dir=env\.run_dir|run_dir / "update"' launcher/couchliteos_updater.py launcher/couchliteos_browser.py
rg -q 'couchliteos-update' scripts/couchliteos-support-export
refute rg -n 'shell=True|os\.system' launcher/couchliteos_snapshot.py
# RESTORE PREVIOUS VERSION: Settings asks through a path unit, a boot service restores before the
# launcher, and a boot menu entry starts the saved kernel with couchliteos.restore=1.
rg -q '^PathExists=/run/couchliteos/restore-request$' services/couchliteos-restore-request.path
rg -q '^ExecStartPre=/usr/bin/rm -f /run/couchliteos/restore-request$' services/couchliteos-restore-request.service
rg -q '^ExecStart=/usr/libexec/couchliteos-updater request-restore$' services/couchliteos-restore-request.service
rg -q '^ExecStart=/usr/libexec/couchliteos-updater restore$' services/couchliteos-restore.service
rg -q '^ConditionKernelCommandLine=\|couchliteos\.restore=1$' services/couchliteos-restore.service
rg -q '^ConditionPathExists=\|/var/lib/couchliteos-update/snapshot/restore-request$' services/couchliteos-restore.service
rg -q '^Before=couchliteos-bluetooth\.service couchliteos-audio\.service$' services/couchliteos-restore.service
rg -q 'ext2\|ext3\|ext4' scripts/couchliteos-grub-restore
rg -q '^Before=couchliteos-launcher\.service' services/couchliteos-restore.service
rg -q '^systemctl enable couchliteos-restore-request.path couchliteos-restore.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -Fq 'install -D -m 0755 "$ROOT/scripts/couchliteos-grub-restore" "$CHROOT/etc/grub.d/42_couchliteos_restore"' build/configure.sh
rg -q '^  restore-apply\)$' scripts/couchliteos-qemu-smoke
rg -q '^  restore-check\)$' scripts/couchliteos-qemu-smoke
rg -q '^boot_and_wait restore-apply COUCHLITEOS_SMOKE_RESTORE_REQUESTED$' tests/qemu-install-smoke.sh
rg -q '^boot_and_wait restore-check COUCHLITEOS_SMOKE_RESTORE_READY$' tests/qemu-install-smoke.sh
# The live USB updates an older install on the disk (apply-disk): found by a root service, requested
# by the launcher through a path unit. tests/qemu-legacy-smoke.sh drives it in QEMU (make
# qemu-legacy-smoke OLD_ISO=..., or the release gauntlet with OLD_ISO set); not run without an old ISO.
rg -q '^ConditionKernelCommandLine=boot=live$' services/couchliteos-find-installs.service
rg -q '^ExecStart=/usr/libexec/couchliteos-updater find-installs$' services/couchliteos-find-installs.service
rg -q '^PathExists=/run/couchliteos/disk-update$' services/couchliteos-disk-update.path
rg -q '^ExecStartPre=/usr/bin/rm -f /run/couchliteos/disk-update$' services/couchliteos-disk-update.service
rg -q 'couchliteos-updater apply-disk --found$' services/couchliteos-disk-update.service
rg -q '^systemctl enable couchliteos-find-installs.service couchliteos-disk-update.path$' \
  config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'UPDATE THE INSTALLED SYSTEM \(KEEPS PAIRINGS AND SETTINGS\)' launcher/couchliteos_softwareupdate.py
rg -q 'test_apply_disk.py' launcher/Makefile
rg -q '^  disk-update\)$' scripts/couchliteos-qemu-smoke
rg -q '^  disk-update-check\)$' scripts/couchliteos-qemu-smoke
rg -q '^boot_and_wait disk-update COUCHLITEOS_SMOKE_DISK_UPDATED 2700$' tests/qemu-legacy-smoke.sh
rg -q '^boot_and_wait disk-update-check COUCHLITEOS_SMOKE_DISK_UPDATE_READY 240$' tests/qemu-legacy-smoke.sh
bash -n tests/qemu-legacy-smoke.sh
rg -q '^qemu-legacy-smoke:$' Makefile
rg -Fq './tests/qemu-legacy-smoke.sh "$(OLD_ISO)" "$(ISO)"' Makefile
rg -Fq 'make qemu-legacy-smoke ISO="$ISO" OLD_ISO="$OLD_ISO"' tools/release-gauntlet.sh
rg -Fq 'qemu-legacy-smoke: skipped: OLD_ISO not set' tools/release-gauntlet.sh

# Security fixes and app updates between releases (tests/test_unattended_upgrades.py has the rest).
python3 -m py_compile launcher/couchliteos_busy.py launcher/couchliteos_appupdate.py launcher/couchliteos_securityupdate.py
refute rg -n 'shell=True|os\.system|^import requests|Authorization|Cookie' \
  launcher/couchliteos_busy.py launcher/couchliteos_appupdate.py launcher/couchliteos_securityupdate.py
rg -q '^unattended-upgrades$' config/live-build/package-lists/couchliteos.list.chroot
rg -Fq '> "$CHROOT/etc/apt/apt.conf.d/52couchliteos-unattended"' build/configure.sh
rg -q '^systemctl enable couchliteos-security-update.timer couchliteos-security-update.path$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^systemctl enable couchliteos-app-update.timer couchliteos-app-update.path$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^WantedBy=timers.target$' services/couchliteos-security-update.timer services/couchliteos-app-update.timer
rg -q '^Unit=couchliteos-security-update.service$' services/couchliteos-security-update.path
rg -q '^Unit=couchliteos-app-update.service$' services/couchliteos-app-update.path
rg -q '^ProtectSystem=strict$' services/couchliteos-app-update.service
rg -q 'test_appupdate.py test_securityupdate.py' launcher/Makefile
refute rg -q 'Automatic-Reboot "true"' build/unattended-upgrades.sh

# Easier everyday use: actionable errors
rg -q 'couchliteos_errors.py' build/configure.sh
rg -q '^import couchliteos_errors as errors' launcher/couchliteos-launcher.py
rg -q 'test_errors.py' launcher/Makefile
# NETWORK SETTINGS opens the existing network setup app; its id must stay in step with the launcher.
rg -q '^id = network-setup$' config/apps.d/90-network-setup.ini
rg -q 'launch_by_id\("network-setup"\)' launcher/couchliteos-launcher.py
# Every launcher failure screen goes through the shared helper (no hand-drawn dead ends).
refute rg -n 'ENTER OR ESC RETURNS TO LAUNCHER' launcher/couchliteos-launcher.py
refute rg -n 'show_message\("SUPPORT EXPORT' launcher/couchliteos-launcher.py

# Boot speed: an installed system boots straight in (Shift/Esc shows the menu); the live
# ISO keeps the menu its binary hook writes; the launcher never waits for the network.
# Boot speed: an installed system boots straight in (1 s hidden window; Shift/Esc shows the
# menu); the live ISO keeps the menu its binary hook writes; the launcher never waits for the
# network.
rg -q '^GRUB_TIMEOUT_STYLE=hidden$' overlay/etc/default/grub.d/20-couchliteos.cfg
rg -q '^GRUB_TIMEOUT=1$' overlay/etc/default/grub.d/20-couchliteos.cfg
refute rg -q 'hidden|GRUB_TIMEOUT' config/live-build/hooks/live/0100-autoboot.hook.binary
refute rg -q 'network-online|wait-online|network-ready|tailscale|usbip|firewall' services/couchliteos-launcher.service
rg -Fq 'systemd-analyze --no-pager critical-chain couchliteos-launcher.service' scripts/couchliteos-diagnostics
rg -Fq 'systemd-analyze --no-pager blame 2>&1 | head -n 30' scripts/couchliteos-diagnostics
rg -Fq '"systemd-analyze", "--no-pager", "critical-chain", "couchliteos-launcher.service"' scripts/couchliteos-support-export
rg -q '^source "\$ROOT/build/lb-cache\.sh"$' build/build.sh
rg -q 'COUCHLITEOS_LB_CACHE' build/build.sh

# Faster builds (docs/BUILDING.md). Release ISOs keep xz; test ISOs use zstd.
rg -Fq -- '--release) release=1 ;;' build/build.sh
rg -Fq "squashfs_options=(--chroot-squashfs-compression-type xz)" build/build.sh
rg -q 'squashfs_options=\(--chroot-squashfs-compression-type zstd --chroot-squashfs-compression-level [0-9]+\)' build/build.sh
rg -Fq '"${squashfs_options[@]}"' build/build.sh
# Release squashfs: 1 MiB blocks and the x86 BCJ filter, release builds only.
[[ $(sed -n '/^if ((release)); then$/,/^else$/p' build/build.sh | rg -c "^  export MKSQUASHFS_OPTIONS='-b 1M -Xbcj x86'$") == 1 ]]
[[ $(rg -c 'MKSQUASHFS_OPTIONS=' build/build.sh) == 1 ]]
rg -q '^  --apt-indices false \\$' build/build.sh
# No documentation, manual pages or non-English translations; copyright files stay.
slim_cfg=config/live-build/includes.chroot_before_packages/etc/dpkg/dpkg.cfg.d/couchliteos-slim
for rule in 'path-exclude /usr/share/doc/\*' 'path-include /usr/share/doc/\*/copyright' \
  'path-exclude /usr/share/man/\*' 'path-exclude /usr/share/info/\*' \
  'path-exclude /usr/share/locale/\*' 'path-include /usr/share/locale/en\*'; do
  rg -q "^$rule\$" "$slim_cfg"
done
# dpkg applies the last matching rule: every include follows its exclude.
(($(rg -n '^path-include /usr/share/doc/' "$slim_cfg" | cut -d: -f1) > $(rg -n '^path-exclude /usr/share/doc/' "$slim_cfg" | cut -d: -f1)))
(($(rg -n '^path-include /usr/share/locale/en' "$slim_cfg" | cut -d: -f1) > $(rg -n '^path-exclude /usr/share/locale/' "$slim_cfg" | cut -d: -f1)))
slim_test=$(mktemp -d)
mkdir -p "$slim_test/bin" "$slim_test/root/lib/modules/6.12.94+deb13-amd64/kernel" \
  "$slim_test/root/usr/share/doc/libfoo1/examples" "$slim_test/root/usr/share/doc/couchliteos/examples" \
  "$slim_test/root/usr/share/man/man1" "$slim_test/root/usr/share/info" \
  "$slim_test/root/usr/share/locale/de/LC_MESSAGES" "$slim_test/root/usr/share/locale/en_GB/LC_MESSAGES" \
  "$slim_test/root/usr/share/locale/es/LC_MESSAGES" \
  "$slim_test/root/usr/lib/gcc/x86_64-linux-gnu/14" "$slim_test/root/usr/libexec/gcc/x86_64-linux-gnu/14" \
  "$slim_test/root/usr/lib/x86_64-linux-gnu" "$slim_test/root/usr/bin" \
  "$slim_test/root/usr/lib/firmware/ath10k/QCA4019/hw1.0" "$slim_test/root/usr/lib/firmware/ath10k/QCA6174/hw3.0" \
  "$slim_test/root/usr/lib/firmware/ath11k/IPQ8074/hw2.0" "$slim_test/root/usr/lib/firmware/ath11k/WCN6855/hw2.0" \
  "$slim_test/root/usr/lib/firmware/mediatek/mt8183" "$slim_test/root/usr/lib/firmware/mediatek/sof" \
  "$slim_test/root/usr/lib/firmware/nvidia/ga102/gsp" "$slim_test/root/usr/lib/firmware/tigon" \
  "$slim_test/root/usr/lib/firmware/qcom"
for file in usr/share/doc/libfoo1/copyright usr/share/doc/libfoo1/changelog.Debian.gz \
  usr/share/doc/libfoo1/examples/a.c usr/share/doc/couchliteos/examples/steam.ini \
  usr/share/man/man1/foo.1.gz usr/share/info/foo.info.gz usr/share/locale/locale.alias \
  usr/share/locale/de/LC_MESSAGES/foo.mo usr/share/locale/es/LC_MESSAGES/foo.mo \
  usr/share/locale/en_GB/LC_MESSAGES/foo.mo \
  usr/libexec/gcc/x86_64-linux-gnu/14/lto1 usr/libexec/gcc/x86_64-linux-gnu/14/cc1 \
  usr/bin/x86_64-linux-gnu-lto-dump-14 usr/bin/x86_64-linux-gnu-gcc-14 \
  usr/lib/gcc/x86_64-linux-gnu/14/libasan.so usr/lib/gcc/x86_64-linux-gnu/14/liblto_plugin.so \
  usr/lib/x86_64-linux-gnu/libasan.so.8.0.0 usr/lib/x86_64-linux-gnu/libubsan.so.1.0.0 \
  usr/lib/x86_64-linux-gnu/libhwasan.so.0.0.0 usr/lib/x86_64-linux-gnu/libtsan.so.2.0.0 \
  usr/lib/x86_64-linux-gnu/liblsan.so.0.0.0 usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0 \
  usr/lib/firmware/ath10k/QCA4019/hw1.0/firmware-5.bin usr/lib/firmware/ath10k/QCA6174/hw3.0/firmware-6.bin \
  usr/lib/firmware/ath11k/IPQ8074/hw2.0/m3.bin usr/lib/firmware/ath11k/WCN6855/hw2.0/amss.bin \
  usr/lib/firmware/mediatek/mt8183/scp.img usr/lib/firmware/mediatek/sof/sof-mt8186.ri \
  usr/lib/firmware/mediatek/mt7986_wm.bin usr/lib/firmware/mediatek/mt7622pr2h.bin \
  usr/lib/firmware/mediatek/WIFI_RAM_CODE_MT7922_1.bin usr/lib/firmware/mediatek/mt7915_wm.bin \
  usr/lib/firmware/nvidia/ga102/gsp/gsp-535.113.01.bin usr/lib/firmware/tigon/tg357766.bin \
  usr/lib/firmware/qcom/a630_sqe.fw \
  usr/lib/firmware/iwlwifi-cc-a0-46.ucode usr/lib/firmware/iwlwifi-cc-a0-72.ucode \
  usr/lib/firmware/iwlwifi-cc-a0-77.ucode usr/lib/firmware/iwlwifi-cc-a0-79.ucode \
  usr/lib/firmware/iwlwifi-so-a0-gf-a0-64.ucode usr/lib/firmware/iwlwifi-so-a0-gf-a0-86.ucode \
  usr/lib/firmware/iwlwifi-so-a0-gf-a0.pnvm usr/lib/firmware/iwlwifi-7265D-27.ucode.xz \
  usr/lib/firmware/iwlwifi-7265D-29.ucode.xz usr/lib/firmware/iwlwifi-3160-17.ucode \
  usr/lib/firmware/iwlwifi-Qu-b0-jf-b0-50.ucode usr/lib/firmware/iwlwifi-Qu-b0-jf-b0-77.ucode; do
  printf 'x\n' > "$slim_test/root/$file"
done
# A kept file that is a link keeps the device's older files (they may be its target).
ln -s iwlwifi-Qu-b0-jf-b0-50.ucode "$slim_test/root/usr/lib/firmware/iwlwifi-Qu-b0-jf-b0-72.ucode"
ln -s libfoo1 "$slim_test/root/usr/share/doc/libfoo1-dev"
# The kernel's iwlwifi API maxima: cc-a0 77, so-a0-gf-a0 89 (64 never loads; 86 is the newest
# file), 7265D 29, 3160 17, Qu-b0-jf-b0 72 (a link to 50).
printf '#!/bin/bash\n[[ $1 == -k && $2 == 6.12.94+deb13-amd64 && $3 == -F && $4 == firmware ]] || exit 2\n[[ $5 == iwlwifi ]] || exit 1\nprintf "%%s\\n" iwlwifi-cc-a0-77.ucode iwlwifi-so-a0-gf-a0-89.ucode iwlwifi-7265D-29.ucode iwlwifi-3160-17.ucode iwlwifi-Qu-b0-jf-b0-72.ucode iwlwifi-ma-b0-gf-a0-89.ucode\n' \
  > "$slim_test/bin/modinfo"
chmod 0755 "$slim_test/bin/modinfo"
PATH="$slim_test/bin:$PATH" COUCHLITEOS_SLIM_ROOT="$slim_test/root" \
  bash config/live-build/hooks/live/0900-slim.hook.chroot > "$slim_test/log"
(cd "$slim_test/root" && find . ! -type d | sed 's|^\./||' | LC_ALL=C sort) > "$slim_test/after"
printf '%s\n' usr/bin/x86_64-linux-gnu-gcc-14 usr/lib/firmware/ath10k/QCA6174/hw3.0/firmware-6.bin \
  usr/lib/firmware/ath11k/WCN6855/hw2.0/amss.bin usr/lib/firmware/iwlwifi-3160-17.ucode \
  usr/lib/firmware/iwlwifi-7265D-29.ucode.xz usr/lib/firmware/iwlwifi-Qu-b0-jf-b0-50.ucode \
  usr/lib/firmware/iwlwifi-Qu-b0-jf-b0-72.ucode usr/lib/firmware/iwlwifi-Qu-b0-jf-b0-77.ucode \
  usr/lib/firmware/iwlwifi-cc-a0-77.ucode usr/lib/firmware/iwlwifi-cc-a0-79.ucode \
  usr/lib/firmware/iwlwifi-so-a0-gf-a0-86.ucode usr/lib/firmware/iwlwifi-so-a0-gf-a0.pnvm \
  usr/lib/firmware/mediatek/WIFI_RAM_CODE_MT7922_1.bin usr/lib/firmware/mediatek/mt7915_wm.bin \
  usr/lib/firmware/nvidia/ga102/gsp/gsp-535.113.01.bin usr/lib/firmware/tigon/tg357766.bin \
  usr/lib/gcc/x86_64-linux-gnu/14/liblto_plugin.so usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0 \
  usr/libexec/gcc/x86_64-linux-gnu/14/cc1 usr/share/doc/couchliteos/examples/steam.ini \
  usr/share/doc/libfoo1-dev usr/share/doc/libfoo1/copyright \
  usr/share/locale/en_GB/LC_MESSAGES/foo.mo usr/share/locale/locale.alias | LC_ALL=C sort > "$slim_test/expected"
diff -u "$slim_test/expected" "$slim_test/after"
# What stays passes the release profiles' image checks (build.sh lists the image as //path).
for pattern in $(profile_value general FORBIDDEN_IMAGE_PATHS); do
  refute rg -q -- "$pattern" <(sed 's|^|//|' "$slim_test/after")
done
for removed in usr/share/man/man1/foo.1.gz usr/share/info/foo.info.gz usr/share/doc/libfoo1/changelog.Debian.gz \
  usr/share/locale/de/LC_MESSAGES/foo.mo usr/share/locale/es/LC_MESSAGES/foo.mo \
  usr/libexec/gcc/x86_64-linux-gnu/14/lto1 usr/lib/gcc/x86_64-linux-gnu/14/libasan.so \
  usr/lib/x86_64-linux-gnu/libubsan.so.1.0.0 usr/lib/firmware/ath10k/QCA4019/hw1.0/firmware-5.bin \
  usr/lib/firmware/ath11k/IPQ8074/hw2.0/m3.bin usr/lib/firmware/qcom/a630_sqe.fw \
  usr/lib/firmware/mediatek/mt8183/scp.img usr/lib/firmware/mediatek/sof/sof-mt8186.ri \
  usr/lib/firmware/mediatek/mt7986_wm.bin usr/lib/firmware/mediatek/mt7622pr2h.bin; do
  matched=0
  for pattern in $(profile_value general FORBIDDEN_IMAGE_PATHS); do
    rg -q -- "$pattern" <<< "//$removed" && matched=1
  done
  ((matched)) || { echo "no FORBIDDEN_IMAGE_PATHS pattern catches /$removed" >&2; exit 1; }
done
rg -q '^slim: removed /usr/lib/firmware/iwlwifi-cc-a0-46\.ucode$' "$slim_test/log"
rg -q '^slim: removed /usr/lib/firmware/mediatek/mt8183/scp\.img$' "$slim_test/log"
rg -q '^slim: 1 copyright files kept$' "$slim_test/log"
# More than one image kernel: iwlwifi firmware is kept.
mkdir -p "$slim_test/root/lib/modules/6.12.95+deb13-amd64/kernel"
printf 'x\n' > "$slim_test/root/usr/lib/firmware/iwlwifi-cc-a0-46.ucode"
PATH="$slim_test/bin:$PATH" COUCHLITEOS_SLIM_ROOT="$slim_test/root" \
  bash config/live-build/hooks/live/0900-slim.hook.chroot > "$slim_test/log"
test -f "$slim_test/root/usr/lib/firmware/iwlwifi-cc-a0-46.ucode"
rm -rf -- "$slim_test"
# The release profiles fail a build whose image still has what 0900-slim removes.
for profile in general nvidia; do
  forbidden=$(profile_value "$profile" FORBIDDEN_IMAGE_PATHS)
  # Anchored: the application images under /opt keep their own usr/share.
  for pattern in '^/+usr/share/man/.' '^/+usr/share/info/.' '^/+usr/share/doc/[^/]+/changelog' \
    '^/+usr/share/locale/([^e/]|e[^n/])[^/]*/' '^/+usr/(lib|libexec)/gcc/.*/lto1$' \
    '^/+usr/lib/(gcc/.*|x86_64-linux-gnu)/lib(asan|tsan|lsan|ubsan|hwasan)[^/]*$' \
    '^/+usr/lib/firmware/ath1[0-2]k/(QCA4019|WCN3990|WCN6750|IPQ[0-9]+)/' '^/+usr/lib/firmware/(imx|meson|rockchip|qcom)/' \
    '^/+usr/lib/firmware/mediatek/(.*mt8[0-9]{3}|mt7622|mt7629|mt798[0-9])'; do
    [[ " $forbidden " == *" $pattern "* ]] || { echo "profile $profile does not forbid $pattern" >&2; exit 1; }
  done
done
# chiaki-ng: English only, no DevTools; QtWebEngine stays.
rg -Fq 'rm -f "$CHROOT/opt/couchliteos/apps/$name/usr/resources/qtwebengine_devtools_resources.pak"' build/configure.sh
rg -Fq -- "-name '*.pak' ! -name en-US.pak -delete" build/configure.sh
rg -Fq -- "-name '*.qm' ! -name '*_en.qm' -delete" build/configure.sh
rg -Fq 'test -f "$CHROOT/opt/couchliteos/apps/$name/usr/resources/qtwebengine_resources.pak"' build/configure.sh
# A smaller initrd: zstd, no nouveau (it loads from the root filesystem), on the ISO only.
rg -q '^zstd$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^COMPRESS=zstd$' overlay/etc/initramfs-tools/conf.d/couchliteos.conf
rg -q '^COMPRESSLEVEL=19$' overlay/etc/initramfs-tools/conf.d/couchliteos.conf
initramfs_hook=overlay/etc/initramfs-tools/hooks/couchliteos-slim
test -x "$initramfs_hook"
sh -n "$initramfs_hook"
rg -q '^prereqs\)$' "$initramfs_hook"
rg -Fq '"${DESTDIR}"/lib/modules/*/kernel/drivers/gpu/drm/nouveau' "$initramfs_hook"
rg -Fq '"${DESTDIR}/lib/firmware/nvidia"' "$initramfs_hook"
initramfs_test=$(mktemp -d)
mkdir -p "$initramfs_test/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/nouveau" \
  "$initramfs_test/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/i915" "$initramfs_test/lib/firmware/nvidia/gk104"
touch "$initramfs_test/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/nouveau/nouveau.ko.xz" \
  "$initramfs_test/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/i915/i915.ko.xz" \
  "$initramfs_test/lib/firmware/nvidia/gk104/fecs_inst.bin"
# live-boot's kms hook queues nouveau, and mkinitramfs copies the queue after all hooks.
[[ $("$initramfs_hook" prereqs) == 'kms' ]]
printf 'i915\nnouveau\nradeon\n' > "$initramfs_test/queue"
DESTDIR=$initramfs_test __MODULES_TO_ADD=$initramfs_test/queue "$initramfs_hook"
[[ $(< "$initramfs_test/queue") == $'i915\nradeon' ]]
test ! -e "$initramfs_test/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/nouveau"
test ! -e "$initramfs_test/lib/firmware/nvidia"
test -e "$initramfs_test/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/i915/i915.ko.xz"
rm -rf -- "$initramfs_test"
[[ $(< config/live-build/rootfs/excludes) == 'boot/initrd.img-*' ]]
rg -Fq "grep -E '^/+boot/initrd\.img-' \"\$listing\"" build/build.sh
# An installed system makes the initrd the image leaves out, before 10_linux looks for it.
rg -Fq 'install -D -m 0755 "$ROOT/scripts/couchliteos-grub-initrd" "$CHROOT/etc/grub.d/00_couchliteos_initrd"' build/configure.sh
[[ 00_couchliteos_initrd < 00_header ]]
sh -n scripts/couchliteos-grub-initrd
rg -q 'update-initramfs -c -k "\$version" >&2' scripts/couchliteos-grub-initrd
refute rg -q '^[^#]*(echo|printf|cat)\b[^>]*$' scripts/couchliteos-grub-initrd
rg -Fq 'mode = "-u" if (target / "boot" / f"initrd.img-{kernel}").exists() else "-c"' launcher/couchliteos_updater.py
rg -q '^  initrd_check binary/live/initrd\.img$' build/build.sh
rg -qw zstd docs/BUILDING.md
if command -v zstd >/dev/null; then
  eval "$(sed -n '/^initrd_check() {$/,/^}$/p' build/build.sh)"
  initrd_test=$(mktemp -d)
  python3 - "$initrd_test" <<'EOF'
import pathlib, subprocess, sys

def cpio(names):
    out = b""
    for name in [*names, "TRAILER!!!"]:
        body = b"" if name == "TRAILER!!!" else b"x" * 5
        encoded = name.encode() + b"\0"
        fields = [1, 0o100644, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(encoded), 0]
        header = b"070701" + b"".join(b"%08X" % value for value in fields)
        out += header + encoded
        out += b"\0" * (-len(out) % 4) + body
        out += b"\0" * (-len(out) % 4)
    return out

directory = pathlib.Path(sys.argv[1])
early = cpio(["kernel/x86/microcode/GenuineIntel.bin"]) + b"\0" * 512
modules = ["usr/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/i915/i915.ko.xz"]
nouveau = modules + ["usr/lib/modules/6.12.94+deb13-amd64/kernel/drivers/gpu/drm/nouveau/nouveau.ko.xz"]
zstd = lambda data: subprocess.run(["zstd", "-q", "-c"], input=data, stdout=subprocess.PIPE, check=True).stdout
(directory / "good").write_bytes(early + zstd(cpio(modules)))
(directory / "nouveau").write_bytes(early + zstd(cpio(nouveau)))
(directory / "gzip").write_bytes(early + b"\x1f\x8b\x08\x00" + b"\0" * 64)
EOF
  [[ $(initrd_check "$initrd_test/good") == 'initrd: zstd, no nouveau, 1 entries' ]]
  (initrd_check "$initrd_test/nouveau" 2>&1 && exit 1 || true) | rg -q 'still has nouveau: .*/nouveau\.ko\.xz'
  (initrd_check "$initrd_test/gzip" 2>&1 && exit 1 || true) | rg -q 'is not zstd \(starts 1f8b0800'
  rm -rf -- "$initrd_test"
fi
# Installer trim: module udebs for the installer's kernel only, no graphical installer,
# indexes and Release hashes rewritten for what remains.
trim_hook=config/live-build/hooks/live/0200-installer-trim.hook.binary
test -x "$trim_hook"
refute rg -q 'gensub|asort|PROCINFO' "$trim_hook"
rg -Fq "installer_abis=\$(find binary -path 'binary/pool*' -name '*-di_*.udeb' -printf '%f\\n' |" build/build.sh
if command -v apt-ftparchive >/dev/null && command -v dpkg-deb >/dev/null; then
  trim_test=$(mktemp -d)
  python3 - "$trim_test" <<'EOF'
import pathlib, subprocess, sys

iso = pathlib.Path(sys.argv[1]) / "binary"
kernel = bytearray(0x4000)
kernel[0x20E:0x210] = (0x3000).to_bytes(2, "little")
version = b"6.12.94+deb13-amd64 (debian-kernel@lists.debian.org) #1 SMP PREEMPT_DYNAMIC Debian 6.12.94-1\0"
kernel[0x3200:0x3200 + len(version)] = version
(iso / "install").mkdir(parents=True)
(iso / "install/vmlinuz").write_bytes(kernel)
udebs = {
    "nic-modules-6.12.94+deb13-amd64-di": "l/linux-signed-amd64",
    "kernel-image-6.12.94+deb13-amd64-di": "l/linux-signed-amd64",
    "nic-modules-6.12.38+deb13-amd64-di": "l/linux-signed-amd64",
    "kernel-image-6.12.38+deb13-amd64-di": "l/linux-signed-amd64",
    "libgtk2.0-0-udeb": "g/gtk+2.0",
    "libcairo2-udeb": "c/cairo",
    "libpango1.0-udeb": "p/pango1.0",
    "fonts-dejavu-udeb": "f/fonts-dejavu",
    "rootskel-gtk": "r/rootskel-gtk",
    "cdebconf-gtk-udeb": "c/cdebconf",
    "cdebconf-newt-udeb": "c/cdebconf",
    "espeakup-udeb": "e/espeakup",
    "libespeak-ng1-udeb": "e/espeak-ng",
}
stanzas = []
for package, directory in udebs.items():
    name = f"pool/main/{directory}/{package}_1.0_amd64.udeb"
    (iso / name).parent.mkdir(parents=True, exist_ok=True)
    (iso / name).write_bytes(package.encode())
    stanzas.append(f"Package: {package}\nVersion: 1.0\nFilename: {name}\nSize: {len(package)}\n")
udeb_index = iso / "dists/trixie/main/debian-installer/binary-amd64"
udeb_index.mkdir(parents=True)
(udeb_index / "Packages").write_text("\n".join(stanzas))
deb_index = iso / "dists/trixie/main/binary-amd64"
deb_index.mkdir(parents=True)
# pool/: the live system has this kernel and libicu76 (other version), not GRUB.
debs = [("linux-image-6.12.94+deb13-amd64", "6.12.94-1", "main/l/linux-signed-amd64"),
        ("libicu76", "76.1-4", "main/i/icu"), ("grub-pc", "2.12-9", "main/g/grub2")]
deb_stanzas = []
for package, version, directory in debs:
    tree = iso.parent / "deb" / package
    (tree / "DEBIAN").mkdir(parents=True)
    (tree / "DEBIAN/control").write_text(f"Package: {package}\nVersion: {version}\nArchitecture: amd64\n"
                                         "Maintainer: Test <test@example.org>\nDescription: test\n")
    name = f"pool/{directory}/{package}_{version}_amd64.deb"
    (iso / name).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zgzip", "--build", str(tree), str(iso / name)],
                   check=True, stdout=subprocess.DEVNULL)
    deb_stanzas.append(f"Package: {package}\nVersion: {version}\nFilename: {name}\n")
(deb_index / "Packages").write_text("\n".join(deb_stanzas))
(iso / "live").mkdir()
(iso / "live/filesystem.packages").write_text(
    "grub-common\t2.12-9\nlibicu76:amd64\t76.1-5\nlinux-image-6.12.94+deb13-amd64\t6.12.94-1\n")
(deb_index / "Release").write_text("Archive: trixie\nComponent: main\nArchitecture: amd64\n")
(iso / "dists/stable").symlink_to("trixie")
(iso / "dists/trixie/Release").write_text(
    "Origin: Debian\nLabel: Debian\nSuite: trixie\nVersion: 13\nCodename: trixie\n"
    "Date: Sat, 03 Oct 2026 12:00:00 +0000\nArchitectures: amd64\nComponents: main\n"
    "Description: Last updated: Sat, 03 Oct 2026 12:00:00 +0000\nMD5Sum:\n 0 0 stale\nSHA256:\n 0 0 stale\n")
EOF
  (cd "$trim_test" && bash "$ROOT/$trim_hook") > "$trim_test/log"
  trim_iso=$trim_test/binary
  for package in nic-modules-6.12.38+deb13-amd64-di kernel-image-6.12.38+deb13-amd64-di libgtk2.0-0-udeb \
    libcairo2-udeb libpango1.0-udeb fonts-dejavu-udeb rootskel-gtk cdebconf-gtk-udeb; do
    [[ -z $(find "$trim_iso/pool" -name "${package}_*") ]]
    refute rg -qFx "Package: $package" "$trim_iso/dists/trixie/main/debian-installer/binary-amd64/Packages"
    rg -q "^installer trim: removed pool/main/[^ ]+/${package//+/\\+}_1\.0_amd64\.udeb\$" "$trim_test/log"
  done
  for package in nic-modules-6.12.94+deb13-amd64-di kernel-image-6.12.94+deb13-amd64-di cdebconf-newt-udeb \
    espeakup-udeb libespeak-ng1-udeb; do
    [[ -n $(find "$trim_iso/pool" -name "${package}_*") ]]
    rg -qFx "Package: $package" "$trim_iso/dists/trixie/main/debian-installer/binary-amd64/Packages"
  done
  rg -q '^installer trim: installer kernel 6\.12\.94\+deb13-amd64$' "$trim_test/log"
  cmp <(gzip -dc "$trim_iso/dists/trixie/main/debian-installer/binary-amd64/Packages.gz") \
    "$trim_iso/dists/trixie/main/debian-installer/binary-amd64/Packages"
  # pool/: only the .deb the live system has at the same version goes.
  test ! -e "$trim_iso/pool/main/l/linux-signed-amd64/linux-image-6.12.94+deb13-amd64_6.12.94-1_amd64.deb"
  test -e "$trim_iso/pool/main/i/icu/libicu76_76.1-4_amd64.deb"
  test -e "$trim_iso/pool/main/g/grub2/grub-pc_2.12-9_amd64.deb"
  [[ $(rg '^Package: ' "$trim_iso/dists/trixie/main/binary-amd64/Packages") == $'Package: libicu76\nPackage: grub-pc' ]]
  trim_release=$trim_iso/dists/trixie/Release
  for field in 'Origin: Debian' 'Suite: trixie' 'Version: 13' 'Codename: trixie' \
    'Date: Sat, 03 Oct 2026 12:00:00 \+0000' 'Architectures: amd64' 'Components: main'; do
    rg -q "^$field\$" "$trim_release"
  done
  refute rg -q 'stale' "$trim_release"
  udeb_sha=$(sha256sum < "$trim_iso/dists/trixie/main/debian-installer/binary-amd64/Packages" | cut -d ' ' -f 1)
  rg -q "^ $udeb_sha +[0-9]+ main/debian-installer/binary-amd64/Packages\$" "$trim_release"
  rg -q ' main/binary-amd64/Release$' "$trim_release"
  test -L "$trim_iso/dists/stable"
  # The installer's own kernel must have module udebs: a tree without them fails.
  rm -f "$trim_iso"/pool/main/l/linux-signed-amd64/*6.12.94*
  (cd "$trim_test" && bash "$ROOT/$trim_hook" 2>&1 && exit 1 || true) | rg -q 'no module udebs for the installer kernel'
  rm -rf -- "$trim_test"
fi
rg -Fq '"${cache_options[@]}"' build/build.sh
rg -Fq './build/build.sh $(if $(filter 1,$(RELEASE)),--release)' Makefile
# Release builds reuse a matching chroot snapshot too; only --fresh (FRESH=1) installs afresh.
rg -Fq -- '--fresh) fresh=1 ;;' build/build.sh
rg -Fq 'lb_cache_chroot_stage "$COUCHLITEOS_LB_CACHE" $((fresh ? 0 : 1))' build/build.sh
rg -Fq '$(if $(filter 1,$(FRESH)),--fresh)' Makefile
rg -q '^RELEASE \?= 0$' Makefile
rg -Fq 'sudo make build RELEASE=1' .github/workflows/build.yml
rg -Fq '[[ $compression == 4 ]]' tools/release-assets.sh
# apt-cacher-ng: used when it answers, never written into the image.
rg -Fq 'apt_proxy=${COUCHLITEOS_APT_PROXY-auto}' build/build.sh
rg -Fq 'proxy_answers http://127.0.0.1:3142' build/build.sh
rg -Fq 'export http_proxy=$apt_proxy' build/build.sh
rg -Fq 'cache_options=(--cache-packages false)' build/build.sh
refute rg -q -- '--apt-http-proxy|Acquire::http::Proxy' build/build.sh build/configure.sh config/live-build
rg -Fq "grep -rIEin '^[^#]*(proxy|:3142)' \"\$image_root/root\"" build/build.sh
rg -Fq 'unsquashfs -q -n -d "$image_root/root" binary/live/filesystem.squashfs etc/apt etc/environment' build/build.sh
rg -q '^BindAddress: 127\.0\.0\.1$' tools/setup-apt-cacher-ng.sh
# Cached packages are checked before a build that uses them.
rg -Fq 'timed verify-cache lb_cache_verify_debs cache/packages.bootstrap cache/packages.chroot cache/packages.binary' build/build.sh
rg -Fq 'dpkg-deb --fsys-tarfile' build/lb-cache.sh
# The ISO is hard-linked into build/out, not copied.
rg -Fq 'ln -- "$built_iso" "$ISO" 2>/dev/null || install -m 0644 "$built_iso" "$ISO"' build/build.sh
# Stage times, and no second test run for a tree make test already passed.
for stage in bootstrap chroot installer binary source checks; do
  rg -q "^timed $stage |^  timed $stage " build/build.sh
done
rg -Fq './build/timed.sh tests ./build/test-gate.sh run' Makefile
rg -q '^\t\./build/test-gate\.sh mark$' Makefile
rg -q '^build/\.tests-passed$' .gitignore
rg -Fq 'python3 -m unittest -v tests/test_fast_build.py' Makefile
# Every package=version in the lists is pinned while the image is built, not in the image.
rg -Fq 'couchliteos-pins.pref.chroot' build/configure.sh
refute rg -q 'couchliteos-pins\.pref(\.binary)?"' build/configure.sh
for script in build/timed.sh build/test-gate.sh build/apt-pins.sh tools/setup-apt-cacher-ng.sh; do
  [[ -x $script ]] || { echo "$script is not executable" >&2; exit 1; }
done
# Release notes and the README are written for people using the box: no internal wording.
[[ -x tools/release-check.sh ]]
rg -q '^release-check:$' Makefile
./tools/release-check.sh --notes README.md docs/releases/*.md
refute rg -q -- '--draft' tools Makefile .github

# First-boot wizard (launcher/couchliteos_setup.py): every module it imports ships in the image.
for module in $(rg -o --no-filename '^import (couchliteos_[a-z_]+)' -r '$1' launcher/couchliteos_setup.py); do
  rg -q "launcher/$module.py" build/configure.sh
done
# Its Wi-Fi join talks to NetworkManager over D-Bus as the unprivileged launcher user, which
# NetworkManager only allows through polkit: the image needs polkitd and a rule that grants only
# three NetworkManager actions, only to that user inside the launcher service.
rg -q '^polkitd$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^python3-dbus$' config/live-build/package-lists/couchliteos.list.chroot
rg -q '^python3-evdev$' config/live-build/package-lists/couchliteos.list.chroot
polkit_rule=overlay/etc/polkit-1/rules.d/50-couchliteos-network.rules
rg -q 'subject\.user == "couchliteos"' "$polkit_rule"
rg -q 'couchliteos-launcher\.service' "$polkit_rule"
# Settings > NETWORK > ADVANCED (nmtui, a configured app) keeps working; nothing else in that unit does.
rg -q '^command = /usr/bin/nmtui$' config/apps.d/90-network-setup.ini
rg -Fq 'cgroup.indexOf("/couchliteos-configured-app.service") >= 0 && couchliteosProcFile(subject, "comm") == "nmtui\n"' "$polkit_rule"
# Only the three actions the Wi-Fi join uses, never a prefix match over every NetworkManager action.
[[ $(rg -o 'org\.freedesktop\.NetworkManager\.[A-Za-z.-]+' "$polkit_rule" | sort | tr '\n' ' ') == \
  'org.freedesktop.NetworkManager.network-control org.freedesktop.NetworkManager.settings.modify.system org.freedesktop.NetworkManager.wifi.scan ' ]]
refute rg -q 'indexOf\("org\.' "$polkit_rule"
[[ $(rg -c 'polkit\.Result' "$polkit_rule") == 1 ]]
refute rg -q 'polkit\.Result\.(AUTH_ADMIN|NOT_HANDLED)' "$polkit_rule"
# The wizard never puts a Wi-Fi password on a command line.
refute rg -q 'nmcli.*(password|psk)' launcher/couchliteos_setup.py

# Secure Boot: hwdetect reads the efivar (behavior is covered by tests/test_hwdetect.py).
rg -q 'SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c' scripts/couchliteos-hwdetect
rg -q 'def secure_boot_enabled' scripts/couchliteos-hwdetect

# The decode hint follows the PC: Moonlight's saved video settings are reconciled at every start.
python3 -m py_compile scripts/couchliteos-moonlight-prefs
rg -q '^install -D -m 0755 "\$ROOT/scripts/couchliteos-moonlight-prefs" "\$CHROOT/usr/libexec/couchliteos-moonlight-prefs"$' build/configure.sh
rg -q '^  /usr/libexec/couchliteos-moonlight-prefs ' scripts/couchliteos-run-app
refute rg -q 'declare -A codec_values' scripts/couchliteos-run-app
[[ $(head -1 scripts/couchliteos-moonlight-prefs) == '#!/usr/bin/python3 -I' ]]

# When the launcher gives up, a root-side unit tells the TV in plain words;
# otherwise tty1 stays on frozen kernel text.
rg -q '^OnFailure=couchliteos-display-failed.service$' services/couchliteos-launcher.service
test -s services/couchliteos-display-failed.service
rg -q '^Type=oneshot$' services/couchliteos-display-failed.service
rg -q '^ExecStart=/usr/libexec/couchliteos-display-failed$' services/couchliteos-display-failed.service
refute rg -q '^\[Install\]' services/couchliteos-display-failed.service
rg -q 'is-failed --quiet couchliteos-launcher.service' scripts/couchliteos-display-failed
rg -q 'COUCHLITEOS_TTY:-/dev/tty1' scripts/couchliteos-display-failed
rg -q '^install -D -m 0755 "\$ROOT/scripts/couchliteos-display-failed" "\$CHROOT/usr/libexec/couchliteos-display-failed"$' build/configure.sh

rg -q 'couchliteos_confirm.py' build/configure.sh
rg -q 'import couchliteos_confirm' launcher/couchliteos_bluetooth.py launcher/couchliteos-launcher.py
rg -q 'couchliteos_controls.py' build/configure.sh
rg -q 'import couchliteos_controls' launcher/couchliteos-launcher.py

# Scrolling menus (720p terminals show ~18 rows) need their helper in the image.
rg -q 'couchliteos_listview.py' build/configure.sh

# foot starts through a wrapper that sizes the font for the screen (720p to 4K).
rg -qF 'couchliteos_foot.py" "$CHROOT/usr/libexec/couchliteos-foot"' build/configure.sh
rg -q "^/usr/libexec/couchliteos-foot --app-id=couchliteos-osk -o csd.preferred=none --title 'COUCHLITEOS KEYBOARD' -- /usr/libexec/couchliteos-osk$" \
  scripts/couchliteos-osk-session
refute rg -q -- '--fullscreen' scripts/couchliteos-osk-session
# Typed text goes to the window the keyboard was opened for, not whatever is in front when it closes.
rg -qF 'target=$(/usr/libexec/couchliteos-osk --target) || target=' scripts/couchliteos-osk-session
rg -qF 'exec /usr/libexec/couchliteos-osk --inject "$target"' scripts/couchliteos-osk-session
# Cage docks that app-id along the bottom 40% (not full-screen), and applies the MOUSE SPEED setting.
for cage_patch in foreign-toplevel osk-panel pointer-speed; do
  test -f "config/cage/cage-0.2.0-$cage_patch.patch"
  rg -qF "  \"\$SRC/cage-0.2.0-$cage_patch.patch\"" config/live-build/hooks/live/0050-cage.hook.chroot
  rg -qF "install -D -m 0644 \"\$ROOT/config/cage/cage-0.2.0-$cage_patch.patch\" \\" build/configure.sh
done
rg -qF 'patch -p1 --forward < "$patch"' config/live-build/hooks/live/0050-cage.hook.chroot
rg -qF '+#define CAGE_OSK_APP_ID "couchliteos-osk"' config/cage/cage-0.2.0-osk-panel.patch
rg -qF '+#define CAGE_OSK_PANEL_SHARE 0.40' config/cage/cage-0.2.0-osk-panel.patch
# The open keyboard keeps the focus whatever asks for it: one guard in seat_set_focus.
rg -qF '+	struct cg_view *osk = view_find_osk(server);' config/cage/cage-0.2.0-osk-panel.patch
rg -qF '+#define CAGE_CONTROLLER_MOUSE_NAME "CouchLiteOS Controller Mouse"' config/cage/cage-0.2.0-pointer-speed.patch
rg -q '^OSK_APP_ID = "couchliteos-osk"$' launcher/couchliteos_foot.py
rg -q '^OSK_SHARE = 0\.40 *$' launcher/couchliteos_foot.py
rg -qF '+#define CAGE_POINTER_SPEED_PATH "/var/lib/couchliteos/mouse-speed"' config/cage/cage-0.2.0-pointer-speed.patch
rg -qF 'wl_event_loop_add_signal(event_loop, SIGHUP, handle_signal, &server);' config/cage/cage-0.2.0-pointer-speed.patch
rg -qF "+libinput       = dependency('libinput')" config/cage/cage-0.2.0-pointer-speed.patch
rg -q 'libxkbcommon-dev libinput-dev\)' config/live-build/hooks/live/0050-cage.hook.chroot
rg -qF 'grep -aq /var/lib/couchliteos/mouse-speed "$work/cage-0.2.0/build/cage"' config/live-build/hooks/live/0050-cage.hook.chroot
rg -q '^FOOT = "/usr/libexec/couchliteos-foot"' launcher/couchliteos_app_runner.py
refute rg -q '/usr/bin/foot' services/couchliteos-launcher.service scripts/couchliteos-session scripts/couchliteos-osk-session launcher/couchliteos_app_runner.py
# TYPE ON PHONE: a one-time form on the home network fills a text field; qrencode draws its QR code
rg -q 'couchliteos_phone.py" "\$CHROOT/usr/libexec/couchliteos_phone.py"' build/configure.sh
rg -q '^import couchliteos_phone as phone' launcher/couchliteos-launcher.py
rg -q 'couchliteos_phone.py' launcher/Makefile
rg -q 'test_phone.py' launcher/Makefile
rg -q 'hmac.compare_digest' launcher/couchliteos_phone.py
rg -q 'secrets.token_urlsafe\(16\)' launcher/couchliteos_phone.py
# Artwork lookup (0.3.0): HTTPS allow-list, 5 MB cap, GdkPixbuf re-encode imported only when used, key 0600
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_artwork.py" "\$CHROOT/usr/libexec/couchliteos_artwork.py"$' build/configure.sh
rg -q '^import couchliteos_artwork as artwork$' launcher/couchliteos-launcher.py
rg -q '^import couchliteos_artwork as artwork$' launcher/couchliteos-tv.py
rg -q 'couchliteos_artwork.py' launcher/Makefile
rg -q 'test_artwork.py' launcher/Makefile
rg -q '^MAX_IMAGE = 5 \* 1024 \* 1024$' launcher/couchliteos_artwork.py
rg -q 'parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS' launcher/couchliteos_artwork.py
rg -q '_write\(path, key.encode\("ascii"\), 0o600\)' launcher/couchliteos_artwork.py
refute rg -q '^(import|from) gi\b' launcher/couchliteos_artwork.py
refute rg -q '(print|log)\(.*\bkey\b' launcher/couchliteos_artwork.py
python3 -m py_compile launcher/couchliteos_artwork.py
# Upgrade notice: a one-time "what's new" screen, only for people who finished setup on an older version
rg -q 'couchliteos_whatsnew.py' build/configure.sh
rg -q '^import couchliteos_whatsnew as whatsnew' launcher/couchliteos-launcher.py
rg -q 'couchliteos_whatsnew.py' launcher/Makefile
rg -q 'test_whatsnew.py' launcher/Makefile
# It comes BEFORE the setup wizard: afterwards a new user who just finished setup would look like an upgrader.
# (A restart for a new picture size during setup resumes it instead.)
rg -Uq 'whatsnew\.show_once\(self\.screen, lambda: read_key\(self\.screen\)\)[^\n]*\n\s+if \(RUN / "reopen-setup"\)\.exists\(\):[^\n]*\n[^\n]*\n\s+self\.setup_wizard\(resume=True\)\n\s+else:\n\s+self\.setup_wizard\(\)' launcher/couchliteos-launcher.py
# Its version and state come from the same places as the update check and the setup marker.
rg -q 'update\.VERSION_FILES' launcher/couchliteos_whatsnew.py
rg -q 'SETUP_MARKER\.parent / "whatsnew-seen"' launcher/couchliteos_whatsnew.py
# The notice names the old product once, on a line the rename script leaves alone.
test "$(rg -c 'MOONLIGHTOS IS NOW CALLED COUCHLITEOS\. SAME SYSTEM.*# rename:keep$' launcher/couchliteos_whatsnew.py)" = 1  # rename:keep
# Easier everyday use: gaming PC status line under the title (the probe runs in a thread)
rg -q 'couchliteos_pcstatus.py' build/configure.sh
rg -q '^import couchliteos_pcstatus as pcstatus' launcher/couchliteos-launcher.py
rg -q 'self\.pcstatus\.line\(\)' launcher/couchliteos-launcher.py
rg -q 'probe: Callable\[\[stream\.Host\], str\] = stream\.probe' launcher/couchliteos_pcstatus.py
rg -q 'threading\.Thread' launcher/couchliteos_pcstatus.py
# Easier everyday use: NO CONTROLLER FOUND banner (reads only the kernel's device list)
rg -q 'couchliteos_padcheck.py' build/configure.sh
rg -q 'couchliteos_padcheck.py' launcher/Makefile
rg -q '^import couchliteos_padcheck as padcheck' launcher/couchliteos-launcher.py
rg -q '/proc/bus/input/devices' launcher/couchliteos_padcheck.py
refute rg -q 'evdev|/dev/input' launcher/couchliteos_padcheck.py
# TV interface model (0.3.0): the home screen's rows and the recently played games
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_home.py" "\$CHROOT/usr/libexec/couchliteos_home.py"$' build/configure.sh
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_recent.py" "\$CHROOT/usr/libexec/couchliteos_recent.py"$' build/configure.sh
rg -q 'test_home.py' launcher/Makefile
rg -q 'test_recent.py' launcher/Makefile
rg -q '^import couchliteos_recent as recent$' launcher/couchliteos-launcher.py
rg -q 'recent\.record\(recent\.host_key\(host\), app_name\)' launcher/couchliteos_session.py
refute rg -q '^import (curses|gi)' launcher/couchliteos_home.py launcher/couchliteos_recent.py
# TV interface (0.3.0): GTK shell, shared session logic, fallback wrapper. Only couchliteos-tv touches gi.
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_session.py" "\$CHROOT/usr/libexec/couchliteos_session.py"$' build/configure.sh
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_tvlayout.py" "\$CHROOT/usr/libexec/couchliteos_tvlayout.py"$' build/configure.sh
rg -q '^import couchliteos_session as session$' launcher/couchliteos-launcher.py launcher/couchliteos-tv.py
rg -q '^class Launcher\(session.Session\):$' launcher/couchliteos-launcher.py
rg -q 'test_session.py' launcher/Makefile
rg -q 'test_tvlayout.py' launcher/Makefile
rg -q 'tests/test_tv_fallback.py' Makefile
refute rg -q '^\s*(import|from) (curses|gi)\b' launcher/couchliteos_session.py launcher/couchliteos_tvlayout.py
rg -q '^INIT_FAILED = 3$' launcher/couchliteos-tv.py
rg -q '^INIT_FAILED=3$' scripts/couchliteos-session
# TV interface Settings, power, What's New, update progress (0.3.0 G3); the rest opens the classic
# screen on top (couchliteos-launcher --screen) in the themed foot wrapper, never foot itself.
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_tvscreens.py" "\$CHROOT/usr/libexec/couchliteos_tvscreens.py"$' build/configure.sh
rg -q '^import couchliteos_tvscreens as tvscreens$' launcher/couchliteos-tv.py
rg -q 'test_tvscreens.py' launcher/Makefile
refute rg -q '^\s*(import|from) gi\b' launcher/couchliteos_tvscreens.py
rg -q '^FOOT = os.environ.get\("COUCHLITEOS_FOOT", "/usr/libexec/couchliteos-foot"\)$' launcher/couchliteos_tvscreens.py
refute rg -q 'setup-complete' scripts/couchliteos-session
# Quick menu, prompt bar, toasts and sounds (0.3.0 G4): plain Python next to the GTK window.
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_quick.py" "\$CHROOT/usr/libexec/couchliteos_quick.py"$' build/configure.sh
rg -q '^import couchliteos_quick as quick$' launcher/couchliteos-tv.py
rg -q '^import couchliteos_quick as quick$' launcher/couchliteos-launcher.py
rg -q 'test_quick.py' launcher/Makefile
refute rg -q '^\s*(import|from) (curses|gi)\b' launcher/couchliteos_quick.py
for sound in move select back; do
  [[ -f overlay/usr/share/couchliteos/sounds/$sound.wav ]]
done
rg -q '"pw-play"' launcher/couchliteos_quick.py
rg -q '^pipewire$' config/live-build/package-lists/couchliteos.list.chroot
rg -q 'HOME_REQUEST.write_text\("guide\\n" if guide else "shortcut\\n"' launcher/gamepad-nav.py

# Settings > CONTROLLERS: player order, IDENTIFY, TEST, SWAP A/B and X/Y (gamepad-nav applies the swaps)
rg -q 'couchliteos_pads.py' build/configure.sh
rg -q 'couchliteos_pads.py' launcher/Makefile
rg -q 'test_pads.py' launcher/Makefile
rg -q '^import couchliteos_pads as pads' launcher/couchliteos-launcher.py
rg -q '^import couchliteos_pads as padprefs' launcher/gamepad-nav.py
rg -q '"CONTROLLERS": lambda: pads\.run\(self\.screen, read_key\)' launcher/couchliteos-launcher.py
refute rg -q '\.grab\(' launcher/couchliteos_pads.py
rg -q '^ACTION=="add", SUBSYSTEM=="leds", KERNEL=="xpad\[0-9\]\*\|.*player.*RUN\+="/bin/chgrp couchliteos /sys/class/leds/%k/brightness", RUN\+="/bin/chmod g\+w /sys/class/leds/%k/brightness"$' \
  overlay/etc/udev/rules.d/72-couchliteos-leds.rules

# SETTINGS > NETWORK is the controller Wi-Fi menu; nmtui stays behind ADVANCED. The module never
# starts a process or logs, so a Wi-Fi password cannot reach argv or a log through it.
rg -q '^install -D -m 0644 "\$ROOT/launcher/couchliteos_netmenu.py" "\$CHROOT/usr/libexec/couchliteos_netmenu.py"$' build/configure.sh
rg -q '^import couchliteos_netmenu as netmenu$' launcher/couchliteos-launcher.py
rg -q '"NETWORK": self.run_network,' launcher/couchliteos-launcher.py
rg -Fq 'self.launch("network-setup")' launcher/couchliteos-launcher.py
rg -q 'test_netmenu.py' launcher/Makefile
refute rg -q 'subprocess|nmcli|os\.environ|logging' launcher/couchliteos_netmenu.py
# Recoverable boots: the hidden menu applies only when the previous boot reached the launcher.
# GRUB clears boot_success in grubenv every boot (01_couchliteos_bootcheck runs after 00_header
# sets the hidden timeout) and shows the menu for 5 s unless it was 1; a oneshot sets it again
# once the launcher is ready and does nothing on the live ISO (no grubenv there).
rg -q '^install -D -m 0755 "\$ROOT/scripts/couchliteos-grub-bootcheck" "\$CHROOT/etc/grub.d/01_couchliteos_bootcheck"$' build/configure.sh
bootcheck=$(sh scripts/couchliteos-grub-bootcheck)
rg -q '^  load_env boot_success$' <<< "$bootcheck"
rg -q '^  set boot_success=0$' <<< "$bootcheck"
rg -q '^  if save_env boot_success; then$' <<< "$bootcheck"
rg -q '^  set timeout_style=menu$' <<< "$bootcheck"
rg -q '^  set timeout=5$' <<< "$bootcheck"
refute rg -q 'nomodeset' scripts/couchliteos-grub-bootcheck services/couchliteos-boot-success.service
if command -v grub-script-check >/dev/null; then
  printf '%s\n' "$bootcheck" > "$tmp/bootcheck.cfg"
  grub-script-check "$tmp/bootcheck.cfg"
fi
rg -q '^ConditionPathExists=/boot/grub/grubenv$' services/couchliteos-boot-success.service
rg -q '^ConditionPathExists=/run/couchliteos/launcher-ready$' services/couchliteos-boot-success.service
rg -q '^After=couchliteos-launcher.service$' services/couchliteos-boot-success.service
rg -q '^ExecStart=-/usr/bin/grub-editenv /boot/grub/grubenv set boot_success=1$' services/couchliteos-boot-success.service
rg -q '^WantedBy=multi-user.target$' services/couchliteos-boot-success.service
refute rg -q 'boot-success' services/couchliteos-launcher.service
rg -q '^systemctl enable couchliteos-boot-success.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot

# Boot time: recorded once the launcher is ready, reported on the serial port, checked by
# the QEMU smoke test and included in support bundles.
python3 -m py_compile scripts/couchliteos-boot-time
[[ $(head -1 scripts/couchliteos-boot-time) == '#!/usr/bin/python3 -I' ]]
[[ -x scripts/couchliteos-boot-time ]]
rg -Fq 'install -D -m 0755 "$ROOT/scripts/couchliteos-boot-time" "$CHROOT/usr/libexec/couchliteos-boot-time"' build/configure.sh
rg -q '^systemctl enable couchliteos-boot-time.service$' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q '^After=couchliteos-launcher.service$' services/couchliteos-boot-time.service
refute rg -q '^ConditionPathExists=' services/couchliteos-boot-time.service
rg -q '^ExecStart=-/usr/libexec/couchliteos-boot-time$' services/couchliteos-boot-time.service
rg -q '^User=couchliteos$' services/couchliteos-boot-time.service
rg -q '^StandardOutput=journal\+console$' services/couchliteos-boot-time.service
rg -q '^WantedBy=multi-user.target$' services/couchliteos-boot-time.service
refute rg -q '^Type=oneshot$' services/couchliteos-boot-time.service
rg -Fq 'print(f"COUCHLITEOS_BOOT_SECONDS={ready:.1f}", flush=True)' scripts/couchliteos-boot-time
rg -Fq 'LOG = pathlib.Path("/var/log/couchliteos/boot-time.log")' scripts/couchliteos-boot-time
rg -Fq "wait_for_marker 'COUCHLITEOS_BOOT_SECONDS=[0-9]'" tests/qemu-smoke.sh
rg -Fq 'COUCHLITEOS_BOOT_BASELINE' tests/qemu-smoke.sh
rg -Fq '(n > b * 1.25)' tests/qemu-smoke.sh
rg -Fq 'log_dir / "boot-time.log"' scripts/couchliteos-support-export

# Screen edges and text size: the launcher saves them, the foot wrapper (boot path) reads them.
rg -qF 'couchliteos_screenfit.py" "$CHROOT/usr/libexec/couchliteos_screenfit.py"' build/configure.sh
rg -q '^import couchliteos_screenfit as screenfit' launcher/couchliteos-launcher.py
rg -q 'couchliteos_screenfit.py .*gamepad-nav.py' launcher/Makefile
rg -q 'test_screenfit.py' launcher/Makefile
# The wrapper must start foot even if the module or the setting is broken.
rg -q '^    import couchliteos_screenfit as screenfit' launcher/couchliteos_foot.py
rg -q '^    screenfit = None' launcher/couchliteos_foot.py
rg -qF '"main.pad=' launcher/couchliteos_foot.py
refute rg -q 'systemctl|nomodeset' launcher/couchliteos_screenfit.py

# Themes: built-in files in the image, the module in the image and tested; foot gets the colours.
rg -qF 'couchliteos_theme.py" "$CHROOT/usr/libexec/couchliteos_theme.py"' build/configure.sh
rg -q '^import couchliteos_theme as themes$' launcher/couchliteos-launcher.py
rg -q 'couchliteos_theme.py .*gamepad-nav.py' launcher/Makefile
rg -q 'test_theme.py' launcher/Makefile
rg -q '^    import couchliteos_theme as theme$' launcher/couchliteos_foot.py
rg -q '^    import couchliteos_theme as theme$' launcher/couchliteos_osk.py
for name in midnight slate daylight high-contrast; do
  test -f "overlay/usr/share/couchliteos/themes/$name.theme"
done

# FIND GAMING PCS: one mDNS question for _nvstream._tcp.local, standard library only, in the image and tested.
rg -qF 'couchliteos_discover.py" "$CHROOT/usr/libexec/couchliteos_discover.py"' build/configure.sh
rg -q '^import couchliteos_discover as discover$' launcher/couchliteos_setup.py
rg -q 'couchliteos_discover.py .*gamepad-nav.py' launcher/Makefile
rg -q 'test_discover.py' launcher/Makefile
# A legacy unicast query (RFC 6762 6.7): sent from a random port, so no multicast group is joined, no port is shared, no daemon is needed.
rg -qF 'self.sock.bind(("", 0))' launcher/couchliteos_discover.py
refute rg -q 'IP_ADD_MEMBERSHIP|SO_REUSEADDR|SO_REUSEPORT|^import (dbus|zeroconf)' launcher/couchliteos_discover.py
# The unicast replies (from UDP port 5353) get in because the input chain accepts by default and filters only TCP 3240.
# No rule was added for them, and none may open UDP 5353 or turn the default into a drop.
rg -q 'policy accept;' config/nftables/couchliteos.nft
rg -q 'policy accept;' scripts/couchliteos-firewall
refute rg -q 'policy (drop|reject)|[sd]port 5353' config/nftables/couchliteos.nft scripts/couchliteos-firewall
# STREAM CHECK (Settings > STREAMING): the module ships in the image and the launcher opens it.
rg -qF 'couchliteos_streamcheck.py" "$CHROOT/usr/libexec/couchliteos_streamcheck.py"' build/configure.sh
rg -q '^import couchliteos_streamcheck as streamcheck$' launcher/couchliteos-launcher.py
rg -q 'couchliteos_streamcheck.py .*gamepad-nav.py' launcher/Makefile
rg -q 'test_streamcheck.py' launcher/Makefile
# The launcher runs as an unprivileged user: ping may not go faster than 0.2 s apart for it.
rg -q '^PING_INTERVAL = 0\.2$' launcher/couchliteos_streamcheck.py
refute rg -q 'shell=True|\beval\b|os\.system|\bsudo\b' launcher/couchliteos_streamcheck.py
rg -qF 'scripts/couchliteos-nvidia-firmware" "$CHROOT/usr/libexec/couchliteos-nvidia-firmware"' build/configure.sh
rg -qF 'third_party/envytools/extract_firmware.py" "$CHROOT/usr/libexec/couchliteos/envytools-extract-firmware.py"' build/configure.sh
rg -qF 'couchliteos_firmware.py" "$CHROOT/usr/libexec/couchliteos_firmware.py"' build/configure.sh
rg -q 'couchliteos-nvidia-firmware.path' config/live-build/hooks/live/0100-couchliteos.hook.chroot
rg -q 'test_firmware.py' launcher/Makefile
rg -q 'tests/test_nvidia_firmware.py' Makefile
refute rg -q 'shell=True|\beval\b|os\.system|\bsudo\b' scripts/couchliteos-nvidia-firmware launcher/couchliteos_firmware.py
# The tips send people to a row that exists in Settings > STREAMING.
rg -q '^SMOOTHER_ROW = "SMOOTHER STREAM ' launcher/couchliteos-launcher.py
# Settings > CONTROLS (Home shortcut, pointer speeds): the launcher saves, gamepad-nav re-reads, Cage gets the mouse speed.
rg -qF 'couchliteos_input.py" "$CHROOT/usr/libexec/couchliteos_input.py"' build/configure.sh
rg -q '^import couchliteos_input as inputprefs$' launcher/couchliteos-launcher.py
rg -q '^import couchliteos_input as inputprefs$' launcher/gamepad-nav.py
rg -q '^import couchliteos_input as inputprefs$' launcher/couchliteos_controls.py
rg -q 'couchliteos_input.py .*gamepad-nav.py' launcher/Makefile
rg -q 'test_input.py' launcher/Makefile
rg -q '^DATA = pathlib.Path\(os.environ.get\("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"\)\)$' launcher/couchliteos_input.py
rg -q '^MOUSE_SPEED_FILE = DATA / "mouse-speed"$' launcher/couchliteos_input.py
rg -q '^INPUT_SETTINGS = inputprefs.Watcher\(\)$' launcher/gamepad-nav.py
rg -q '^HOME_HOLD_SECONDS = 1\.5$' launcher/couchliteos_input.py
refute rg -q 'shell=True|\beval\b|os\.system|\bsudo\b|pkill|killall' launcher/couchliteos_input.py

# Brightness and volume keys: gamepad-nav acts on them and the Guide menu has a BRIGHTNESS row. The
# backlight is written as group video (udev rule), not by a root service; volume goes through wpctl.
python3 -m py_compile launcher/couchliteos_brightness.py
rg -qF 'couchliteos_brightness.py" "$CHROOT/usr/libexec/couchliteos_brightness.py"' build/configure.sh
rg -q 'couchliteos_brightness.py' launcher/Makefile
rg -q 'test_brightness.py' launcher/Makefile
for f in launcher/gamepad-nav.py launcher/couchliteos-launcher.py; do
  rg -q '^import couchliteos_brightness as brightness$' "$f"
done
rg -q '^import couchliteos_audio as audio$' launcher/gamepad-nav.py
rg -q 'KEY_BRIGHTNESSUP.*KEY_BRIGHTNESSDOWN' launcher/gamepad-nav.py
rg -q 'KEY_VOLUMEUP.*KEY_VOLUMEDOWN' launcher/gamepad-nav.py
rg -q 'ecodes.BTN_MODE, \*MEDIA_KEYS\} & keys' launcher/gamepad-nav.py
# The Super (Windows) key no longer opens Home; the Home key is a user-set chord from Settings > CONTROLS.
refute rg -q 'SuperTap|SUPER_KEYS' launcher/gamepad-nav.py
rg -q '^class HomeChord' launcher/gamepad-nav.py
rg -q '^DEFAULT_CHORD = "KEY_LEFTCTRL\+KEY_LEFTALT\+KEY_H"$' launcher/couchliteos_input.py
rg -q '^    def capture_home_key' launcher/couchliteos-launcher.py
rg -q 'f"BRIGHTNESS  \{percent\}%"' launcher/couchliteos-launcher.py
rg -q '^ACTION=="add", SUBSYSTEM=="backlight", RUN\+="/bin/chgrp video /sys/class/backlight/%k/brightness", RUN\+="/bin/chmod g\+w /sys/class/backlight/%k/brightness"$' \
  overlay/etc/udev/rules.d/70-couchliteos-backlight.rules
rg -q '^SupplementaryGroups=.*\bvideo\b' services/couchliteos-gamepad-nav.service
rg -q '^SupplementaryGroups=.*\bvideo\b' services/couchliteos-launcher.service
rg -q '^Environment=XDG_RUNTIME_DIR=/run/couchliteos$' services/couchliteos-gamepad-nav.service
rg -q '^Environment=XDG_RUNTIME_DIR=/run/couchliteos$' services/couchliteos-audio.service
refute rg -q 'subprocess|shell=True|\bsudo\b|^import (dbus|evdev)' launcher/couchliteos_brightness.py

# Bluetooth audio: a watcher in the audio service moves sound to a connecting device and back,
# and owns the headset (mic) profile, so WirePlumber's own headset autoswitch is off.
rg -q '^/usr/bin/python3 /usr/libexec/couchliteos_audio.py watch & helpers\+=\("\$!"\)$' scripts/couchliteos-audio
rg -Fq 'bluetooth.autoswitch-to-headset-profile = false' overlay/etc/wireplumber/wireplumber.conf.d/52-couchliteos-bluetooth.conf
rg -q 'class Watcher' launcher/couchliteos_audio.py
rg -Fq 'latencyOffsetNsec' launcher/couchliteos_audio.py
rg -Fq '"pw-record"' launcher/couchliteos_audio.py
rg -Fq 'BLUETOOTH DELAY' launcher/couchliteos-launcher.py
rg -Fq 'MICROPHONE' launcher/couchliteos-launcher.py
rg -q 'bluetooth-audio-last' scripts/couchliteos-bluetoothd launcher/couchliteos_audio.py
rg -Fq 'org.bluez.Battery1' scripts/couchliteos-bluetoothd
# Battery: UPower first, then /sys/class/power_supply; shown only when there is one.
rg -Fq 'org.freedesktop.UPower' launcher/couchliteos_battery.py
rg -Fq '/sys/class/power_supply' launcher/couchliteos_battery.py
rg -q 'battery_blank_minutes' launcher/couchliteos_power.py
for module in couchliteos_audiomenu couchliteos_battery; do
  rg -Fxq "install -D -m 0644 \"\$ROOT/launcher/$module.py\" \"\$CHROOT/usr/libexec/$module.py\"" build/configure.sh
  rg -q "$module.py" launcher/Makefile
done
for test in test_audio_bluetooth test_mic test_battery; do rg -q "$test.py" launcher/Makefile; done
refute rg -q 'shell=True|\bsudo\b' launcher/couchliteos_audio.py launcher/couchliteos_audiomenu.py launcher/couchliteos_battery.py

printf 'Static tests passed.\n'
