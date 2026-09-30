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
rg -q '^menuentry "Start MoonlightOS"' "$boot_test/binary/boot/grub/grub.cfg"
rg -q '^menuentry "Start MoonlightOS \(No Persistence\)"' "$boot_test/binary/boot/grub/grub.cfg"
[[ $(rg -c '^menuentry "Start MoonlightOS \(No Persistence\)"' "$boot_test/binary/boot/grub/grub.cfg") == 1 ]]
rg -q 'boot=live components nopersistence ipv6.disable=1' "$boot_test/binary/boot/grub/grub.cfg"
rg -q "menuentry 'Install MoonlightOS'" "$boot_test/binary/boot/grub/install_start.cfg"
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
rg -q 'menu label \^Start MoonlightOS$' "$boot_test/binary/isolinux/live.cfg"
rg -q 'menu label Start MoonlightOS \(No Persistence\)$' "$boot_test/binary/isolinux/live.cfg"
[[ $(rg -c 'menu label Start MoonlightOS \(No Persistence\)$' "$boot_test/binary/isolinux/live.cfg") == 1 ]]
rg -q 'boot=live components nopersistence ipv6.disable=1' "$boot_test/binary/isolinux/live.cfg"
rg -q 'menu label \^Install MoonlightOS$' "$boot_test/binary/isolinux/install.cfg"
rg -q 'append vga=788 theme=dark ipv6.disable=1 --- quiet' "$boot_test/binary/isolinux/install.cfg"
rg -q 'rescue/enable=true vga=788 theme=dark ipv6.disable=1' "$boot_test/binary/isolinux/install.cfg"
[[ $(sed 's/ theme=dark//' <<< "$(rg '^[[:space:]]+append ' "$boot_test/binary/isolinux/install.cfg")") == "$isolinux_installer_args" ]]
refute rg -q '^menu background ' "$boot_test/binary/isolinux/stdmenu.cfg"
rg -q '^menu color sel[[:space:]]+\* #ff000000 #ffffffff \*$' "$boot_test/binary/isolinux/stdmenu.cfg"
# NVIDIA ISO: one Basic Graphics entry (nouveau, KMS on) after No Persistence.
(cd "$boot_test" && bash "$ROOT/config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary")
(cd "$boot_test" && bash "$ROOT/config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary")
grub_cfg=$boot_test/binary/boot/grub/grub.cfg
live_cfg=$boot_test/binary/isolinux/live.cfg
[[ $(rg -c '^menuentry "Start MoonlightOS \(Basic Graphics\)" \{$' "$grub_cfg") == 1 ]]
[[ $(rg -c 'moonlightos\.gpu=basic' "$grub_cfg") == 1 ]]
rg -q '^ linux /live/vmlinuz boot=live components persistence ipv6.disable=1 moonlightos\.gpu=basic$' "$grub_cfg"
[[ $(rg '^menuentry ' "$grub_cfg" | cut -d'"' -f2 | paste -sd '|') == 'Start MoonlightOS|Start MoonlightOS (No Persistence)|Start MoonlightOS (Basic Graphics)' ]]
[[ $(stat -c %a "$grub_cfg") == 555 ]]
[[ $(rg -c '^label live-amd64-basic$' "$live_cfg") == 1 ]]
[[ $(rg -c 'moonlightos\.gpu=basic' "$live_cfg") == 1 ]]
rg -q '^ append boot=live components persistence ipv6.disable=1 moonlightos\.gpu=basic$' "$live_cfg"
[[ $(rg -c 'menu default' "$live_cfg") == 1 ]]
[[ $(rg '^label ' "$live_cfg" | paste -sd '|') == 'label live-amd64|label live-amd64-nopersistence|label live-amd64-basic' ]]
refute rg -q 'nomodeset' "$grub_cfg" "$live_cfg"
find "$boot_test" -depth -delete

rg -q -- '--uefi-secure-boot enable' build/build.sh
rg -q -- "--bootappend-live '.*ipv6.disable=1" build/build.sh
[[ "$(< VERSION)" == 0.1.13 ]]
cmp -s VERSION overlay/etc/moonlightos-version
rg -q 'moonlightos-0\.1\.13-amd64\.iso' .github/workflows/build.yml
rg -Fq 'ISO ?= build/out/moonlightos-$(VERSION)-$(if $(ISO_SUFFIX),$(ISO_SUFFIX)-)amd64.iso' Makefile
rg -Fq 'ISO="$OUT/moonlightos-$VERSION-${ISO_SUFFIX:+$ISO_SUFFIX-}amd64.iso"' build/build.sh
rg -q '^PROFILE \?= general$' Makefile
refute rg -qi 'sha-?256|sha256|\.sha256' .github/workflows/build.yml .github/workflows/release-v0.1.11.yml
rg -q 'sudo chown -R .*build/out' .github/workflows/build.yml
rg -q '^  actions: read$' .github/workflows/release-v0.1.11.yml
rg -q 'git/refs/tags/0\.1\.11' .github/workflows/release-v0.1.11.yml
rg -q 'docs/releases/v0\.1\.11\.md' .github/workflows/release-v0.1.11.yml
rg -q 'release delete 0\.1\.11.*--yes' .github/workflows/release-v0.1.11.yml
refute rg -q 'git/ref/tags/v1\.1' .github/workflows/release-v0.1.11.yml
rg -q '^ipv6.method=disabled$' overlay/etc/NetworkManager/conf.d/10-moonlightos.conf
rg -q '^net.ipv6.conf.all.disable_ipv6=1$' overlay/etc/sysctl.d/90-moonlightos.conf
refute rg -q 'moonlightos-network-ready.service' services/moonlightos-launcher.service
refute rg -q 'Before=.*moonlightos-launcher.service' services/moonlightos-network-ready.service
refute rg -q 'moonlightos-network-ready.service' services/moonlightos-{moonlight,chiaki,firefox}.service
rg -q 'MOONLIGHTOS_LAUNCHER_READY' services/moonlightos-launcher.service tests/qemu-smoke.sh
rg -q 'StandardOutput=journal\+console' services/moonlightos-launcher.service
refute rg -q '^Environment=WAYLAND_DISPLAY=' services/moonlightos-launcher.service
rg -q '/usr/bin/cage -s -- /usr/libexec/moonlightos-foot --fullscreen' services/moonlightos-launcher.service
rg -q '^Environment=QT_QPA_PLATFORM=xcb$' services/moonlightos-moonlight.service
rg -q '^Environment=QT_QPA_PLATFORM=wayland$' services/moonlightos-chiaki.service
rg -q '^Environment=MOZ_ENABLE_WAYLAND=1$' services/moonlightos-firefox.service
rg -q '^EnvironmentFile=-/run/moonlightos/session.env$' services/moonlightos-{moonlight,chiaki,firefox}.service
rg -q '^ConditionFileIsExecutable=/opt/moonlightos/apps/moonlight/usr/bin/moonlight$' services/moonlightos-moonlight.service
rg -q '^ConditionFileIsExecutable=/opt/moonlightos/apps/chiaki-ng/usr/bin/chiaki$' services/moonlightos-chiaki.service
rg -q '^ConditionFileIsExecutable=/usr/bin/firefox-esr$' services/moonlightos-firefox.service
rg -q 'binary=\$appdir/usr/bin/moonlight' scripts/moonlightos-run-app
rg -q 'binary=\$appdir/usr/bin/chiaki' scripts/moonlightos-run-app
rg -q 'binary=/usr/bin/firefox-esr' scripts/moonlightos-run-app
rg -q 'write_status starting' scripts/moonlightos-run-app
rg -q 'failed: exited before the application became ready' scripts/moonlightos-run-app
rg -q 'unsquashfs -quiet -offset' build/configure.sh
removed_units='moonlightos-escape''-guard|moonlightos-stop''-active-app'
refute rg -q "$removed_units" build/configure.sh
rg -q '^firefox-esr$' config/live-build/package-lists/moonlightos.list.chroot
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
rg -q '^options hid_apple fnmode=2$' config/profiles/general/overlay/etc/modprobe.d/moonlightos-input.conf
rg -q 'dpkg-divert --local --rename --divert "\$sta_blacklist.moonlightos-disabled"' config/profiles/general/hooks/0200-general.hook.chroot
rg -q 'NVIDIA_DRIVER:-none\} != proprietary' config/profiles/general/hooks/0200-general.hook.chroot
rg -q 'rm -f /var/lib/dkms/mok.key' config/profiles/general/hooks/0200-general.hook.chroot
rg -q 'systemctl is-enabled --quiet moonlightos-hwdetect-early.service' config/profiles/general/hooks/0200-general.hook.chroot
refute rg -q '^blacklist ' config/profiles/general/overlay
rg -q 'usr/lib/firmware/nvidia/\[0-9.\]\+/gsp' config/profiles/general/profile.conf
rg -q "^LB_EXTRA_CONFIG='--firmware-binary false --firmware-chroot false'$" config/profiles/general/profile.conf config/profiles/nvidia/profile.conf
# nvidia: proprietary 550 driver loaded only where moonlightos-hwdetect allows it.
for package in nvidia-kernel-dkms firmware-nvidia-gsp nvidia-driver-libs libnvidia-egl-gbm1 nvidia-vaapi-driver nvidia-detect; do
  rg -q "^$package\$" config/profiles/nvidia/package-lists/nvidia.list.chroot
done
rg -q '^options nvidia-current-drm modeset=1 fbdev=1$' config/profiles/nvidia/overlay/etc/modprobe.d/moonlightos-nvidia.conf
rg -q 'dkms install "nvidia-current/' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
rg -q 'dpkg-divert --local --rename --divert "\$load_conf.moonlightos-disabled"' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
rg -q 'rm -f /var/lib/dkms/mok.key' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
# live-build's 5020 hook selects Mesa for glx, dropping the nouveau blacklist and nvidia-drm aliases.
rg -q '^update-glx --set glx /usr/lib/nvidia$' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
rg -q 'options nouveau' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
refute rg -q 'gensub' config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary
# Boot-time hardware detection runs on every profile's image.
rg -q 'moonlightos-hwdetect' build/configure.sh
rg -q 'systemctl enable moonlightos-hwdetect-early.service moonlightos-hwdetect.service' config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q '^DefaultDependencies=no$' services/moonlightos-hwdetect-early.service
rg -q '^Before=systemd-udevd.service systemd-udev-trigger.service systemd-modules-load.service sysinit.target$' services/moonlightos-hwdetect-early.service
rg -q '^WantedBy=sysinit.target$' services/moonlightos-hwdetect-early.service
rg -q '^ExecStart=/usr/libexec/moonlightos-hwdetect early$' services/moonlightos-hwdetect-early.service
rg -q '^ExecStart=/usr/libexec/moonlightos-hwdetect late$' services/moonlightos-hwdetect.service
rg -q '^Before=moonlightos-launcher.service$' services/moonlightos-hwdetect.service
rg -q '^Wants=.*moonlightos-hwdetect.service' services/moonlightos-launcher.service
rg -q '^EnvironmentFile=-/run/moonlightos-hardware/compositor.env$' services/moonlightos-launcher.service
rg -q 'MOONLIGHTOS_VIDEO_DECODE' scripts/moonlightos-run-app
rg -q 'moonlightos-hwdetect report' scripts/moonlightos-diagnostics scripts/moonlightos-hardware-report
[[ $(head -1 scripts/moonlightos-hwdetect) == '#!/usr/bin/python3 -I' ]]
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
refute rg -q 'nomodeset' config overlay build
for module in b43 bcma ssb brcmsmac; do
  rg -q "^blacklist $module\$" config/profiles/imac2013/overlay/etc/modprobe.d/moonlightos-imac2013.conf
done
rg -q '^options hid_apple fnmode=2$' config/profiles/imac2013/overlay/etc/modprobe.d/moonlightos-imac2013.conf
rg -q '^applesmc$' config/profiles/imac2013/overlay/etc/modules-load.d/moonlightos-imac2013.conf
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
rg -q '/usr/share/moonlightos/profile.conf' build/configure.sh
# Remote Desktop: FreeRDP 3 SDL3 client on Wayland, passwords only through stdin.
rg -q '^freerdp3-sdl$' config/live-build/package-lists/moonlightos.list.chroot
refute rg -qi 'remmina|freerdp2|xfreerdp' config/live-build/package-lists config/profiles
rg -q '"/from-stdin:force"' launcher/moonlightos_rdp.py
rg -q 'cert:deny,fingerprint:sha256:' launcher/moonlightos_rdp.py
rg -q '"SDL_VIDEO_DRIVER": "wayland"' launcher/moonlightos_rdp.py
refute rg -n '"/p:|/p:\{|--password' launcher/moonlightos_rdp.py launcher/moonlightos_app_runner.py launcher/moonlightos-launcher.py
rg -q 'stdin=subprocess.PIPE' launcher/moonlightos_app_runner.py
# The session unit runs with the appliance user's environment: nothing in it may run as root.
refute rg -q '^Exec[A-Za-z]*=[-@:!]*\+' services/moonlightos-rdp.service
refute rg -q '^ExecStartPre=' services/moonlightos-rdp.service
rg -q '^SuccessExitStatus=130 143$' services/moonlightos-rdp.service
rg -q '^RestartMode=direct$' services/moonlightos-rdp.service
rg -q '^ExecStartPost=\+/usr/bin/systemctl reset-failed moonlightos-rdp.service moonlightos-rdp.path$' services/moonlightos-rdp-cleanup.service
rg -q '^ExecStartPost=\+/usr/bin/systemctl restart moonlightos-rdp.path$' services/moonlightos-rdp-cleanup.service
refute rg -q '^EnvironmentFile=' services/moonlightos-rdp-cleanup.service services/moonlightos-rdp-secret.service
[[ $(head -1 scripts/moonlightos-rdp-secret) == '#!/usr/bin/python3 -I' ]]
rg -q '^ExecStart=/usr/libexec/moonlightos-run-configured-app --rdp$' services/moonlightos-rdp.service
rg -q '^Restart=on-failure$' services/moonlightos-rdp.service
rg -q '^RestartPreventExitStatus=65 66$' services/moonlightos-rdp.service
rg -q '^OnFailure=moonlightos-rdp-cleanup.service$' services/moonlightos-rdp.service
rg -q '^User=moonlightos$' services/moonlightos-rdp.service
rg -q '^PathExists=/run/moonlightos/rdp.request$' services/moonlightos-rdp.path
rg -q '^PathExists=/run/moonlightos/rdp-secret.request$' services/moonlightos-rdp-secret.path
rg -q '^ExecStart=/usr/libexec/moonlightos-rdp-secret request$' services/moonlightos-rdp-secret.service
rg -q '^ProtectSystem=strict$' services/moonlightos-rdp-secret.service
rg -q '^PrivateNetwork=yes$' services/moonlightos-rdp-secret.service
rg -q '^NoNewPrivileges=yes$' services/moonlightos-rdp-secret.service
rg -q 'systemctl enable moonlightos-rdp.path moonlightos-rdp-secret.path' config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q 'install -d -o root -g root -m 0700 /var/lib/moonlightos/rdp-secrets' config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q 'moonlightos_rdp.py' build/configure.sh
rg -q 'moonlightos-rdp-secret' build/configure.sh
rg -q 'InaccessiblePaths=.*-/var/lib/moonlightos/rdp-secrets' services/moonlightos-support-export.service
rg -q 'FREERDP_SECRET_ARGUMENT' scripts/moonlightos-support-export
rg -q 'BTN_NORTH: ecodes.KEY_F12' launcher/gamepad-nav.py
rg -q '"REMOTE DESKTOP"' launcher/moonlightos-launcher.py
rg -q '^google-chrome-stable$' config/live-build/package-lists/moonlightos.list.chroot
refute rg -q '^chromium' config/live-build/package-lists/moonlightos.list.chroot
rg -q 'https://dl.google.com/linux/chrome/deb/ stable main' config/live-build/archives/google-chrome.list.chroot
rg -q 'linux_signing_key.pub' build/sources.lock
rg -q '^command = /usr/bin/google-chrome-stable$' config/apps.d/35-google-chrome.ini
rg -q 'command="/usr/bin/google-chrome-stable"' launcher/moonlightos-launcher.py
rg -q -- '--ozone-platform=wayland' config/apps.d/35-google-chrome.ini launcher/moonlightos-launcher.py
rg -q '^libavcodec61$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^systemd-timesyncd$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^wlrctl$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^wpasupplicant$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^qrencode$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^bluez$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^steam-devices$' config/live-build/package-lists/moonlightos.list.chroot
refute rg -q '^steam(-installer)?$|i386|multilib' config/live-build/package-lists/moonlightos.list.chroot build
refute rg -q '\bwvkbd\b' config build launcher scripts services
rg -q '^libspa-0\.2-bluetooth$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^python3-dbus$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^python3-gi$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^rfkill$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^wlr-randr$' config/live-build/package-lists/moonlightos.list.chroot
rg -q 'qrencode -t ANSIUTF8' scripts/moonlightos-tailscale-enrollment
rg -q "trap 'rm -f --.*URL_FILE.*' EXIT" scripts/moonlightos-tailscale
refute rg -q 'gir1.2-gtk|libfuse' config/live-build/package-lists/moonlightos.list.chroot
rg -q 'OVMF_VARS_4M.fd' tests/qemu-smoke.sh
rg -q 'unit=1,file=' tests/qemu-smoke.sh
rg -q 'screendump' tests/qemu-smoke.sh
rg -q '/boot/grub/grub.cfg' tests/qemu-smoke.sh
rg -q 'MOONLIGHTOS_APP_STARTED' scripts/moonlightos-run-app
rg -q 'moonlight-ready' scripts/moonlightos-qemu-smoke
rg -q 'chiaki-ng-ready' scripts/moonlightos-qemu-smoke
rg -q 'firefox-ready' scripts/moonlightos-qemu-smoke
rg -q 'systemctl start --no-block moonlightos-firefox.service' scripts/moonlightos-qemu-smoke
rg -q 'google-chrome-ready' scripts/moonlightos-qemu-smoke
rg -q 'name=opt/moonlightos.smoke,string=apps' tests/qemu-smoke.sh
rg -q 'qemu-persistence-smoke' Makefile .github/workflows/build.yml
rg -q 'live-persistence-write' scripts/moonlightos-qemu-smoke tests/qemu-persistence-smoke.sh
rg -q 'live-persistence-absent' scripts/moonlightos-qemu-smoke tests/qemu-persistence-smoke.sh
rg -q 'ConditionPathExists=/sys/firmware/qemu_fw_cfg' services/moonlightos-qemu-smoke.service
rg -q 'MOONLIGHTOS_SMOKE_APPS_READY' scripts/moonlightos-qemu-smoke tests/qemu-smoke.sh
rg -q 'MOONLIGHTOS_SMOKE_RDP_READY' scripts/moonlightos-qemu-smoke tests/qemu-smoke.sh
rg -q 'verify_rdp_state' scripts/moonlightos-qemu-smoke
rg -q 'opt/moonlightos.timeout-scale' scripts/moonlightos-qemu-smoke tests/qemu-smoke.sh tests/qemu-persistence-smoke.sh tests/qemu-install-smoke.sh
rg -q 'moonlightos-firefox.path' config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q 'moonlightos-support-export.path' config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q 'moonlightos-configured-app.path moonlightos-osk.path' config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q 'bluetooth.service moonlightos-bluetooth.service' config/live-build/hooks/live/0100-moonlightos.hook.chroot
refute rg -q "$removed_units" config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q '^PathExists=/run/moonlightos/support-export.request$' services/moonlightos-support-export.path
rg -q '^ExecStart=/usr/libexec/moonlightos-support-export$' services/moonlightos-support-export.service
removed_feature='TRIPLE[- ]?TAP|triple[- ]?tap|escape''-guard|stop''-active-app'
refute rg -q -g '!build/work/**' -g '!build/out/**' -g '!tests/test-static.sh' \
  "$removed_feature" launcher scripts services build config docs tests
rg -q 'ACTIVE APPLICATIONS' launcher/moonlightos-launcher.py
rg -Fq 'close-{app.status_id}' launcher/moonlightos-launcher.py
rg -q 'KEY_HOME.*BTN_MODE' launcher/gamepad-nav.py
rg -q 'wlrctl.*toplevel.*focus' launcher/gamepad-nav.py
rg -q 'named_devices' launcher/moonlightos_bluetooth.py
rg -q 'wpctl.*set-default' launcher/moonlightos_audio.py
rg -q 'EncryptedMediaExtensions' overlay/etc/firefox/policies/policies.json
rg -q 'WILL MOUNT TEMPORARILY' launcher/moonlightos_support.py
rg -q 'InaccessiblePaths=.*\/var\/lib\/moonlightos\/home.*\/var\/lib\/tailscale.*-\/var\/lib\/bluetooth' services/moonlightos-support-export.service
rg -q '^RuntimeDirectoryMode=0700$' services/moonlightos-bluetooth.service
rg -q '^User=moonlightos$' services/moonlightos-bluetooth.service
rg -q '^ExecStart=/usr/libexec/moonlightos-bluetoothd$' services/moonlightos-bluetooth.service
rg -q '^After=.*dbus.service.*bluetooth.service$' services/moonlightos-audio.service
for unit in audio bluetooth moonlight chiaki firefox; do
  rg -q '^Environment=PIPEWIRE_RUNTIME_DIR=/run/moonlightos$' "services/moonlightos-$unit.service"
done
rg -q '^d /run/moonlightos 0700 moonlightos moonlightos -$' overlay/etc/tmpfiles.d/moonlightos.conf
refute rg -q '^RuntimeDirectory=' services/moonlightos-launcher.service
rg -q '^/usr/bin/wireplumber --profile main-systemwide ' scripts/moonlightos-audio
rg -q '^PIPEWIRE_DAEMON=true PIPEWIRE_CORE=pipewire-0 /usr/bin/pipewire ' scripts/moonlightos-audio
rg -q 'moonlightos_bluetooth.py' build/configure.sh
rg -q 'moonlightos-bluetoothd' build/configure.sh
rg -q 'moonlightos-run-configured-app' build/configure.sh
rg -q 'moonlightos-osk-session' build/configure.sh
rg -q 'usr/share/moonlightos/apps.d' build/configure.sh
refute rg -q 'terminal_command|curses\.endwin' launcher/moonlightos-launcher.py
refute rg -q 'shell=True|\beval\b' launcher/moonlightos_apps.py launcher/moonlightos_app_runner.py launcher/moonlightos-launcher.py launcher/moonlightos_osk.py launcher/moonlightos_rdp.py scripts/moonlightos-rdp-secret
for forbidden in 'bluetooth''ctl' 'curses\.endwin' 'terminal_''command' 'SIG''INT' 'kill\(' 'shell=True'; do
  refute rg -n "$forbidden" launcher/moonlightos_bluetooth.py scripts/moonlightos-bluetoothd
done
refute rg -q '\bsudo\b' launcher scripts/moonlightos-support-export services/moonlightos-support-export.service
rg -q '\["wlr-randr", "--output"' launcher/moonlightos_display.py
rg -q 'append\("--dryrun"\)' launcher/moonlightos_display.py
rg -q 'support-export.request' launcher/moonlightos_support.py
refute rg -q '\brunuser\b|\bresolvectl\b' scripts/moonlightos-support-export
rg -q '/usr/bin/setpriv' scripts/moonlightos-support-export tests/test_support.py
rg -q '^ExecStart=/usr/sbin/usbipd --ipv4$' services/moonlightos-usbipd.service
refute rg -q -- '--foreground' services/moonlightos-usbipd.service
rg -q 'MOONLIGHTOS_SMOKE_USBIP_READY' scripts/moonlightos-qemu-smoke tests/qemu-smoke.sh
rg -q 'MOONLIGHTOS_SMOKE_HWDETECT_READY' scripts/moonlightos-qemu-smoke tests/qemu-smoke.sh
rg -q 'MOONLIGHTOS_SMOKE_BLUETOOTH_READY' scripts/moonlightos-qemu-smoke tests/qemu-smoke.sh
refute rg -q -- '-kernel|-initrd' tests/qemu-install-smoke.sh
rg -q 'qemu_iso_boot.py' tests/qemu-install-smoke.sh
rg -q '32G' tests/qemu-install-smoke.sh
rg -q 'blank_disk=true' tests/qemu-install-smoke.sh
rg -q 'MOONLIGHTOS_SMOKE_INSTALLED_DISK_READY' scripts/moonlightos-qemu-smoke tests/qemu-install-smoke.sh
rg -q 'MOONLIGHTOS_QEMU_INSTALLER_SCREENSHOT' tests/qemu-install-smoke.sh .github/workflows/build.yml
rg -q 'MOONLIGHTOS_QEMU_INSTALLED_SCREENSHOT' tests/qemu-install-smoke.sh .github/workflows/build.yml
rg -q 'Screenshot evidence missing' tests/qemu-install-smoke.sh
refute rg -q 'install -d.*dirname.*screenshot' tests/qemu-install-smoke.sh
rg -q 'findmnt -n -o FSTYPE /' scripts/moonlightos-qemu-smoke
rg -q 'lsblk --fs' scripts/moonlightos-qemu-smoke
rg -q '^[[:space:]]*blkid$' scripts/moonlightos-qemu-smoke
rg -q '^release-gauntlet:' Makefile
rg -q 'python3 tools/mutants.py' tools/release-gauntlet.sh
rg -q 'clear_bytecode' tools/mutants.py
rg -q 'make qemu-install-smoke' tools/release-gauntlet.sh
rg -q 'NRestarts' scripts/moonlightos-qemu-smoke
rg -q 'moonlightos-audio.service' scripts/moonlightos-qemu-smoke
rg -q '^runuser -u moonlightos -- env XDG_RUNTIME_DIR=/run/moonlightos' scripts/moonlightos-qemu-smoke
digest_pattern='s''ha-?256|s''ha256|\.s''ha256'
digest_control=$(mktemp)
printf 'sha256\n' > "$digest_control"
rg -q -i "$digest_pattern" "$digest_control"
rm -f -- "$digest_control"
if rg -n -i "$digest_pattern" \
  scripts/moonlightos-support-export launcher/moonlightos_support.py docs/SUPPORT.md; then
  echo 'Support export must not create checksum sidecars or manifests.' >&2
  exit 1
else
  status=$?
  [[ $status == 1 ]] || exit "$status"
fi
rg -q 'MIN_FREE' scripts/moonlightos-support-export

python3 -m py_compile launcher/moonlightos-launcher.py launcher/moonlightos_apps.py \
  launcher/moonlightos_app_runner.py launcher/moonlightos_setup.py launcher/moonlightos_osk.py \
  launcher/moonlightos_display.py launcher/moonlightos_support.py \
  launcher/moonlightos_bluetooth.py launcher/moonlightos_audio.py launcher/gamepad-nav.py \
  launcher/moonlightos_rdp.py launcher/moonlightos_stream.py launcher/moonlightos_controllers.py \
  launcher/moonlightos_pcstatus.py \
  launcher/moonlightos_update.py launcher/moonlightos_errors.py launcher/moonlightos_confirm.py \
  scripts/moonlightos-rdp-secret \
  scripts/moonlightos-host-address scripts/moonlightos-support-export \
  scripts/moonlightos-bluetoothd scripts/moonlightos-hwdetect
python3 -m py_compile launcher/moonlightos_cec.py scripts/moonlightos-cec

python3 - <<'PY'
import configparser, pathlib, re, subprocess
hook = pathlib.Path('config/live-build/hooks/live/0100-moonlightos.hook.chroot').read_text()
assert hook.index('groupadd --system seat') < hook.index('useradd --uid 1000')
block = hook.split("cat > /var/lib/moonlightos/config.ini <<'EOF'", 1)[1].split('\nEOF', 1)[0]
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
  services/moonlightos-launcher.service \
  services/moonlightos-hwdetect-early.service \
  services/moonlightos-hwdetect.service \
  services/moonlightos-bluetooth.service \
  services/moonlightos-moonlight.service \
  services/moonlightos-chiaki.service \
  services/moonlightos-firefox.service \
  services/moonlightos-firefox.path \
  services/moonlightos-qemu-smoke.service \
  services/moonlightos-support-export.path \
  services/moonlightos-support-export.service \
  services/moonlightos-configured-app.path \
  services/moonlightos-configured-app.service \
  services/moonlightos-osk.path \
  services/moonlightos-osk.service \
  services/moonlightos-rdp.path \
  services/moonlightos-rdp.service \
  services/moonlightos-rdp-cleanup.service \
  services/moonlightos-rdp-secret.path \
  services/moonlightos-rdp-secret.service \
  services/moonlightos-usbipd.service \
  services/moonlightos-network-ready.service \
  services/moonlightos-tailscale-enroll.service; do
  test -s "$required"
done

# A service that hits its start limit also fails the .path unit that triggers it
# (Result: unit-start-limit-hit), so later requests are ignored until reboot.
# Every path-triggered service with a start limit must re-arm its path on failure.
for path_unit in services/*.path; do
  service=services/$(sed -n 's/^Unit=//p' "$path_unit")
  rg -q '^StartLimitBurst=' "$service" || continue
  rg -q '^OnFailure=(moonlightos-path-rearm@.+|moonlightos-rdp-cleanup)\.service$' "$service" || {
    printf 'static test: %s has a start limit but no OnFailure= that re-arms %s\n' "$service" "$path_unit" >&2
    exit 1
  }
  # With the default restart mode OnFailure= fires on every retried crash, which
  # would reset the start limit and retry forever.
  if rg -q '^Restart=' "$service"; then
    rg -q '^RestartMode=direct$' "$service"
  fi
done
rg -q '^ExecStart=/usr/bin/systemctl reset-failed moonlightos-%i.service moonlightos-%i.path$' services/moonlightos-path-rearm@.service
rg -q '^ExecStart=/usr/bin/systemctl restart moonlightos-%i.path$' services/moonlightos-path-rearm@.service

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
MOONLIGHTOS_SYSFS_ROOT="$tmp/sys" \
MOONLIGHTOS_USBIP_ALLOWLIST="$tmp/allowlist" \
MOONLIGHTOS_USBIP_LOG="$tmp/log/usbip.log" \
  bash usbip/moonlightos-usbip list | grep -q 'allowed'
MOONLIGHTOS_SYSFS_ROOT="$tmp/sys" \
MOONLIGHTOS_USBIP_ALLOWLIST="$tmp/allowlist" \
MOONLIGHTOS_USBIP_LOG="$tmp/log/usbip.log" \
  bash usbip/moonlightos-usbip unbind-all

# /run/moonlightos belongs to the unprivileged appliance user: the root exporter
# must mount in its own private directory and never follow links there.
rg -q 'mkdtemp\(prefix="support-media-"' scripts/moonlightos-support-export
refute rg -q 'RUN / "support-media"|LOCK\.open|os\.chmod\(temporary' scripts/moonlightos-support-export

# Saved Wi-Fi (NetworkManager keyfiles) must survive a live boot with
# persistence: the documented persistence.conf and the smoke test's backend both
# persist the keyfile directory, and the image creates it root-only so the
# persisted copy is never readable by the unprivileged moonlightos user.
for f in docs/INSTALL.md tests/qemu-persistence-smoke.sh; do
  rg -q '^/etc/NetworkManager/system-connections source=nm-connections$' "$f"
done
rg -q 'install -d -o root -g root -m 0700 /etc/NetworkManager/system-connections' \
  config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q '/etc/NetworkManager/system-connections' scripts/moonlightos-qemu-smoke

# The Bluetooth service (user moonlightos) unblocks a soft-blocked adapter with
# rfkill, which needs write access to /dev/rfkill.
rg -q '"rfkill", "unblock", "bluetooth"' scripts/moonlightos-bluetoothd
rg -q '^KERNEL=="rfkill", SUBSYSTEM=="misc", GROUP="moonlightos", MODE="0660"$' \
  overlay/etc/udev/rules.d/70-moonlightos-rfkill.rules

# Sleep and wake: the launcher's SLEEP request, resume marker, wake sources, Wake-on-LAN.
rg -q '^PathExists=/run/moonlightos/suspend$' services/moonlightos-suspend.path
rg -q '^Unit=moonlightos-suspend.service$' services/moonlightos-suspend.path
rg -q '^ExecStart=/usr/bin/systemctl suspend$' services/moonlightos-suspend.service
# /run survives a suspend: the request must be gone before suspending, or waking would suspend again.
rg -q '^ExecStartPre=/usr/bin/rm -f /run/moonlightos/suspend$' services/moonlightos-suspend.service
rg -q '^After=suspend.target$' services/moonlightos-resume.service
rg -q '^WantedBy=suspend.target$' services/moonlightos-resume.service
rg -q '^ExecStart=/usr/bin/touch /run/moonlightos/resumed$' services/moonlightos-resume.service
rg -q '^systemctl enable moonlightos-suspend.path moonlightos-resume.service$' config/live-build/hooks/live/0100-moonlightos.hook.chroot
rg -q 'launcher/moonlightos_power.py' build/configure.sh
rg -q '"SLEEP", "suspend"' launcher/moonlightos-launcher.py
rg -q 'moonlightos.smoke' launcher/moonlightos_power.py
rg -q 'SLEEP_REQUEST = pathlib.Path\("/run/moonlightos/suspend"\)' launcher/gamepad-nav.py
rg -q 'ATTR\{bDeviceClass\}=="e0".*ATTR\{power/wakeup\}="enabled"' overlay/etc/udev/rules.d/75-moonlightos-wakeup.rules
rg -q 'ATTR\{bInterfaceClass\}=="e0".*power/wakeup' overlay/etc/udev/rules.d/75-moonlightos-wakeup.rules
rg -q 'ATTR\{bInterfaceClass\}=="03".*ATTR\{bInterfaceProtocol\}=="01".*power/wakeup' overlay/etc/udev/rules.d/75-moonlightos-wakeup.rules
rg -q '^ethernet.wake-on-lan=64$' overlay/etc/NetworkManager/conf.d/20-moonlightos-wol.conf
# nvidia only: keep video memory across suspend. The general profile's drivers are untouched.
rg -q '^nvidia-suspend-common$' config/profiles/nvidia/package-lists/nvidia.list.chroot
rg -q '^options nvidia NVreg_PreserveVideoMemoryAllocations=1 NVreg_TemporaryFilePath=/var/tmp$' config/profiles/nvidia/overlay/etc/modprobe.d/moonlightos-nvidia.conf
rg -q '^options nvidia-current NVreg_PreserveVideoMemoryAllocations=1 NVreg_TemporaryFilePath=/var/tmp$' config/profiles/nvidia/overlay/etc/modprobe.d/moonlightos-nvidia.conf
rg -q 'nvidia-suspend.service nvidia-resume.service' config/profiles/nvidia/hooks/0300-nvidia.hook.chroot
refute rg -qi 'nvidia-suspend|PreserveVideoMemory' config/profiles/general config/live-build overlay
# Hardware gating: SLEEP is offered only where the PC can suspend, and it is re-checked at each step.
rg -q '^SLEEP_UNSUPPORTED = "SLEEP: NOT SUPPORTED ON THIS PC"$' launcher/moonlightos-launcher.py
rg -q 'power.effective_settings\(power.load_settings\(\), self.can_sleep\)' launcher/moonlightos-launcher.py
rg -q '^    if power.can_suspend\(\):$' launcher/gamepad-nav.py
rg -q '^import moonlightos_power as power$' launcher/gamepad-nav.py
# The root side refuses too: systemctl suspend fails when logind says the PC cannot, after the request is gone.
rg -q 'Sleep verb .suspend. is not configured' services/moonlightos-suspend.service
refute rg -q '^Condition|^ExecCondition' services/moonlightos-suspend.service
# Wake-on-LAN support is read by udev as root (ethtool needs CAP_NET_ADMIN) and shown only when it has magic packet.
rg -q '^ethtool$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^ACTION=="add\|move", SUBSYSTEM=="net", SUBSYSTEMS=="pci\|usb", RUN\+="/usr/bin/python3 /usr/libexec/moonlightos_power.py wake-on-lan"$' overlay/etc/udev/rules.d/75-moonlightos-wakeup.rules
rg -q '^install -D -m 0644 "\$ROOT/launcher/moonlightos_power.py" "\$CHROOT/usr/libexec/moonlightos_power.py"$' build/configure.sh

# Couch to game: the stream module ships in the image, is reachable from Settings,
# and run-app only ever receives a host and app name that the module validated.
rg -q 'moonlightos_stream.py' build/configure.sh
rg -q '^    "STREAMING",$' launcher/moonlightos-launcher.py
rg -q 'def autostream' launcher/moonlightos-launcher.py
rg -q 'moonlightos_stream.py take-request' scripts/moonlightos-run-app
refute rg -q 'shell=True|\beval\b|os\.system' launcher/moonlightos_stream.py
printf 'pc.lan\nSteam Big Picture\n' > "$tmp/stream.request"
mapfile -t stream_request < <(python3 launcher/moonlightos_stream.py take-request "$tmp/stream.request")
[[ ${#stream_request[@]} == 2 && ${stream_request[0]} == pc.lan && ${stream_request[1]} == 'Steam Big Picture' ]]
test ! -e "$tmp/stream.request"
printf -- '--evil\nDesktop\n' > "$tmp/stream.request"
stream_status=0
python3 launcher/moonlightos_stream.py take-request "$tmp/stream.request" > "$tmp/stream.out" || stream_status=$?
[[ $stream_status == 1 && ! -s "$tmp/stream.out" && ! -e "$tmp/stream.request" ]]

# HDMI-CEC TV control: optional, started only when the kernel exposes /dev/cec*.
for required in services/moonlightos-cec.service services/moonlightos-cec-wake.service \
  'services/moonlightos-pulse8-inputattach@.service' overlay/etc/udev/rules.d/75-moonlightos-cec.rules \
  scripts/moonlightos-cec launcher/moonlightos_cec.py; do
  test -s "$required"
done
rg -q '^ConditionPathExistsGlob=/dev/cec\[0-9\]\*$' services/moonlightos-cec.service
rg -q '^ExecStart=/usr/libexec/moonlightos-cec$' services/moonlightos-cec.service
rg -q 'install -D -m 0755 "\$ROOT/scripts/moonlightos-cec" "\$CHROOT/usr/libexec/moonlightos-cec"' build/configure.sh
rg -q 'install -D -m 0644 "\$ROOT/launcher/moonlightos_cec.py" "\$CHROOT/usr/libexec/moonlightos_cec.py"' build/configure.sh
rg -q '^ConditionPathExistsGlob=/dev/cec\[0-9\]\*$' services/moonlightos-cec-wake.service
rg -q '^After=suspend.target$' services/moonlightos-cec-wake.service
rg -q '^WantedBy=suspend.target$' services/moonlightos-cec-wake.service
rg -q '^ExecStart=/usr/bin/systemctl restart moonlightos-cec.service$' services/moonlightos-cec-wake.service
rg -q '^systemctl enable moonlightos-cec-wake.service$' config/live-build/hooks/live/0100-moonlightos.hook.chroot
refute rg -q 'sh -c|/bin/sh|/bin/bash' services/moonlightos-cec.service services/moonlightos-cec-wake.service
rg -q 'SUBSYSTEM=="cec".*SYSTEMD_WANTS.*moonlightos-cec\.service' overlay/etc/udev/rules.d/75-moonlightos-cec.rules
rg -q 'idVendor.*2548.*SYSTEMD_WANTS.*moonlightos-pulse8-inputattach@%k\.service' overlay/etc/udev/rules.d/75-moonlightos-cec.rules
rg -q '^ExecStart=/usr/bin/inputattach --pulse8-cec /dev/%I$' 'services/moonlightos-pulse8-inputattach@.service'
rg -q '^v4l-utils$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^inputattach$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '"TV CONTROL"' launcher/moonlightos-launcher.py
rg -q 'import moonlightos_cec as cec' launcher/gamepad-nav.py

# Easier everyday use: controller batteries
rg -q 'moonlightos_controllers.py' build/configure.sh
rg -q '^import moonlightos_controllers as controllers' launcher/moonlightos-launcher.py
rg -q '/sys/class/power_supply' launcher/moonlightos_controllers.py
# Hardware gating: no bluetoothctl polling without an adapter; nothing shown without a reading.
rg -q '/sys/class/bluetooth' launcher/moonlightos_controllers.py
rg -q 'if not bluetooth_present' launcher/moonlightos_controllers.py

# Easier everyday use: sound follows the TV (declarative WirePlumber 0.5 rule)
rg -q 'cp -a "\$ROOT/overlay/\."' build/configure.sh
rg -q -- '--profile main-systemwide' scripts/moonlightos-audio
python3 - <<'PY'
import pathlib, re
conf = pathlib.Path('overlay/etc/wireplumber/wireplumber.conf.d/50-moonlightos-hdmi-default.conf').read_text()
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
rg -q 'moonlightos_update.py' build/configure.sh
rg -q '^import moonlightos_update as update' launcher/moonlightos-launcher.py
rg -q '"CHECK FOR UPDATES"' launcher/moonlightos-launcher.py
rg -q 'https://api.github.com/repos/Dudiebug/moonlightos/releases/latest' launcher/moonlightos_update.py
rg -q '/etc/moonlightos-version' launcher/moonlightos_update.py
refute rg -n 'Authorization|Cookie|machine-id' launcher/moonlightos_update.py
# Hardware gating: the check is skipped without a default route, before anything is recorded.
rg -q '/proc/net/route' launcher/moonlightos_update.py
rg -q 'if not self.online\(\)' launcher/moonlightos_update.py

# Easier everyday use: actionable errors
rg -q 'moonlightos_errors.py' build/configure.sh
rg -q '^import moonlightos_errors as errors' launcher/moonlightos-launcher.py
rg -q 'test_errors.py' launcher/Makefile
# NETWORK SETTINGS opens the existing network setup app; its id must stay in step with the launcher.
rg -q '^id = network-setup$' config/apps.d/90-network-setup.ini
rg -q 'launch_by_id\("network-setup"\)' launcher/moonlightos-launcher.py
# Every launcher failure screen goes through the shared helper (no hand-drawn dead ends).
refute rg -n 'ENTER OR ESC RETURNS TO LAUNCHER' launcher/moonlightos-launcher.py
refute rg -n 'show_message\("SUPPORT EXPORT' launcher/moonlightos-launcher.py

# Boot speed: an installed system boots straight in (Shift/Esc shows the menu); the live
# ISO keeps the menu its binary hook writes; the launcher never waits for the network.
rg -q '^GRUB_TIMEOUT_STYLE=hidden$' overlay/etc/default/grub.d/20-moonlightos.cfg
rg -q '^GRUB_TIMEOUT=1$' overlay/etc/default/grub.d/20-moonlightos.cfg
refute rg -q 'hidden|GRUB_TIMEOUT' config/live-build/hooks/live/0100-autoboot.hook.binary
refute rg -q 'network-online|wait-online|network-ready|tailscale|usbip|firewall' services/moonlightos-launcher.service
rg -Fq 'systemd-analyze --no-pager critical-chain moonlightos-launcher.service' scripts/moonlightos-diagnostics
rg -Fq 'systemd-analyze --no-pager blame 2>&1 | head -n 30' scripts/moonlightos-diagnostics
rg -Fq '"systemd-analyze", "--no-pager", "critical-chain", "moonlightos-launcher.service"' scripts/moonlightos-support-export
rg -q '^source "\$ROOT/build/lb-cache\.sh"$' build/build.sh
rg -q 'MOONLIGHTOS_LB_CACHE' build/build.sh

# First-boot wizard (launcher/moonlightos_setup.py): every module it imports ships in the image.
for module in $(rg -o --no-filename '^import (moonlightos_[a-z_]+)' -r '$1' launcher/moonlightos_setup.py); do
  rg -q "launcher/$module.py" build/configure.sh
done
# Its Wi-Fi join talks to NetworkManager over D-Bus as the unprivileged launcher user, which
# NetworkManager only allows through polkit: the image needs polkitd and a rule that grants only
# three NetworkManager actions, only to that user inside the launcher service.
rg -q '^polkitd$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^python3-dbus$' config/live-build/package-lists/moonlightos.list.chroot
rg -q '^python3-evdev$' config/live-build/package-lists/moonlightos.list.chroot
polkit_rule=overlay/etc/polkit-1/rules.d/50-moonlightos-network.rules
rg -q 'subject\.user == "moonlightos"' "$polkit_rule"
rg -q 'moonlightos-launcher\.service' "$polkit_rule"
# Settings > NETWORK > ADVANCED (nmtui, a configured app) keeps working; nothing else in that unit does.
rg -q '^command = /usr/bin/nmtui$' config/apps.d/90-network-setup.ini
rg -Fq 'cgroup.indexOf("/moonlightos-configured-app.service") >= 0 && moonlightosProcFile(subject, "comm") == "nmtui\n"' "$polkit_rule"
# Only the three actions the Wi-Fi join uses, never a prefix match over every NetworkManager action.
[[ $(rg -o 'org\.freedesktop\.NetworkManager\.[A-Za-z.-]+' "$polkit_rule" | sort | tr '\n' ' ') == \
  'org.freedesktop.NetworkManager.network-control org.freedesktop.NetworkManager.settings.modify.system org.freedesktop.NetworkManager.wifi.scan ' ]]
refute rg -q 'indexOf\("org\.' "$polkit_rule"
[[ $(rg -c 'polkit\.Result' "$polkit_rule") == 1 ]]
refute rg -q 'polkit\.Result\.(AUTH_ADMIN|NOT_HANDLED)' "$polkit_rule"
# The wizard never puts a Wi-Fi password on a command line.
refute rg -q 'nmcli.*(password|psk)' launcher/moonlightos_setup.py

# Secure Boot: hwdetect reads the efivar (behavior is covered by tests/test_hwdetect.py).
rg -q 'SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c' scripts/moonlightos-hwdetect
rg -q 'def secure_boot_enabled' scripts/moonlightos-hwdetect

# The decode hint follows the PC: Moonlight's saved video settings are reconciled at every start.
python3 -m py_compile scripts/moonlightos-moonlight-prefs
rg -q '^install -D -m 0755 "\$ROOT/scripts/moonlightos-moonlight-prefs" "\$CHROOT/usr/libexec/moonlightos-moonlight-prefs"$' build/configure.sh
rg -q '^  /usr/libexec/moonlightos-moonlight-prefs ' scripts/moonlightos-run-app
refute rg -q 'declare -A codec_values' scripts/moonlightos-run-app
[[ $(head -1 scripts/moonlightos-moonlight-prefs) == '#!/usr/bin/python3 -I' ]]

# When the launcher gives up, a root-side unit tells the TV in plain words;
# otherwise tty1 stays on frozen kernel text.
rg -q '^OnFailure=moonlightos-display-failed.service$' services/moonlightos-launcher.service
test -s services/moonlightos-display-failed.service
rg -q '^Type=oneshot$' services/moonlightos-display-failed.service
rg -q '^ExecStart=/usr/libexec/moonlightos-display-failed$' services/moonlightos-display-failed.service
refute rg -q '^\[Install\]' services/moonlightos-display-failed.service
rg -q 'is-failed --quiet moonlightos-launcher.service' scripts/moonlightos-display-failed
rg -q 'MOONLIGHTOS_TTY:-/dev/tty1' scripts/moonlightos-display-failed
rg -q '^install -D -m 0755 "\$ROOT/scripts/moonlightos-display-failed" "\$CHROOT/usr/libexec/moonlightos-display-failed"$' build/configure.sh

rg -q 'moonlightos_confirm.py' build/configure.sh
rg -q 'import moonlightos_confirm' launcher/moonlightos_bluetooth.py launcher/moonlightos-launcher.py
rg -q 'moonlightos_controls.py' build/configure.sh
rg -q 'import moonlightos_controls' launcher/moonlightos-launcher.py

# Scrolling menus (720p terminals show ~18 rows) need their helper in the image.
rg -q 'moonlightos_listview.py' build/configure.sh

# foot starts through a wrapper that sizes the font for the screen (720p to 4K).
rg -qF 'moonlightos_foot.py" "$CHROOT/usr/libexec/moonlightos-foot"' build/configure.sh
rg -q '/usr/libexec/moonlightos-foot --fullscreen' scripts/moonlightos-osk-session
rg -q '^FOOT = "/usr/libexec/moonlightos-foot"' launcher/moonlightos_app_runner.py
refute rg -q '/usr/bin/foot' services/moonlightos-launcher.service scripts/moonlightos-osk-session launcher/moonlightos_app_runner.py
# Upgrade notice: a one-time "what's new" screen, only for people who finished setup on an older version
rg -q 'moonlightos_whatsnew.py' build/configure.sh
rg -q '^import moonlightos_whatsnew as whatsnew' launcher/moonlightos-launcher.py
rg -q 'moonlightos_whatsnew.py' launcher/Makefile
rg -q 'test_whatsnew.py' launcher/Makefile
# It comes BEFORE the setup wizard: afterwards a new user who just finished setup would look like an upgrader.
rg -Uq 'whatsnew\.show_once\(self\.screen, read_key\)[^\n]*\n\s+self\.setup_wizard\(\)' launcher/moonlightos-launcher.py
# Its version and state come from the same places as the update check and the setup marker.
rg -q 'update\.VERSION_FILES' launcher/moonlightos_whatsnew.py
rg -q 'setup\.MARKER\.parent / "whatsnew-seen"' launcher/moonlightos_whatsnew.py
# The notice names the old product once, on a line the rename script leaves alone.
test "$(rg -c 'MOONLIGHTOS IS NOW CALLED COUCHLITEOS\. SAME SYSTEM.*# rename:keep$' launcher/moonlightos_whatsnew.py)" = 1  # rename:keep
# Easier everyday use: gaming PC status line under the title (the probe runs in a thread)
rg -q 'moonlightos_pcstatus.py' build/configure.sh
rg -q '^import moonlightos_pcstatus as pcstatus' launcher/moonlightos-launcher.py
rg -q 'self\.pcstatus\.line\(\)' launcher/moonlightos-launcher.py
rg -q 'probe: Callable\[\[stream\.Host\], str\] = stream\.probe' launcher/moonlightos_pcstatus.py
rg -q 'threading\.Thread' launcher/moonlightos_pcstatus.py
# Easier everyday use: NO CONTROLLER FOUND banner (reads only the kernel's device list)
rg -q 'moonlightos_padcheck.py' build/configure.sh
rg -q 'moonlightos_padcheck.py' launcher/Makefile
rg -q '^import moonlightos_padcheck as padcheck' launcher/moonlightos-launcher.py
rg -q '/proc/bus/input/devices' launcher/moonlightos_padcheck.py
refute rg -q 'evdev|/dev/input' launcher/moonlightos_padcheck.py

# SETTINGS > NETWORK is the controller Wi-Fi menu; nmtui stays behind ADVANCED. The module never
# starts a process or logs, so a Wi-Fi password cannot reach argv or a log through it.
rg -q '^install -D -m 0644 "\$ROOT/launcher/moonlightos_netmenu.py" "\$CHROOT/usr/libexec/moonlightos_netmenu.py"$' build/configure.sh
rg -q '^import moonlightos_netmenu as netmenu$' launcher/moonlightos-launcher.py
rg -q '"NETWORK": self.run_network,' launcher/moonlightos-launcher.py
rg -Fq 'self.launch("network-setup")' launcher/moonlightos-launcher.py
rg -q 'test_netmenu.py' launcher/Makefile
refute rg -q 'subprocess|nmcli|os\.environ|logging' launcher/moonlightos_netmenu.py

printf 'Static tests passed.\n'
