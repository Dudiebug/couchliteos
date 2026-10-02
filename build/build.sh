#!/bin/bash
# Builds the ISO from the work tree `make configure` prepared.
#   build/build.sh            test build: zstd squashfs (fast to make, a little larger)
#   build/build.sh --release  release build: xz squashfs, and never a reused chroot
# Environment:
#   COUCHLITEOS_LB_CACHE=/dir   share the bootstrap root, the installed chroot and the
#                               package caches between builds (build/lb-cache.sh)
#   COUCHLITEOS_APT_PROXY=auto  (default) fetch Debian packages through apt-cacher-ng on
#                               127.0.0.1:3142 when it answers; an http:// URL to use
#                               another proxy (the build fails if it does not answer);
#                               off to download directly. docs/BUILDING.md has the setup.
#   COUCHLITEOS_TIMINGS=file    append the stage times there (make build does); without
#                               it they go to build/out/build-times.txt and are printed
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK="$ROOT/build/work"
OUT="$ROOT/build/out"
PROFILE=${PROFILE:-general}
VERSION=$(< "$ROOT/VERSION")

release=0
for arg in "$@"; do
  case $arg in
    --release) release=1 ;;
    *) echo "usage: build/build.sh [--release]" >&2; exit 64 ;;
  esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'Run with sudo: sudo make build' >&2; exit 1; }
command -v lb >/dev/null || { echo 'live-build is required (apt install live-build)' >&2; exit 1; }
[[ -f "$WORK/profile" && $(< "$WORK/profile") == "$PROFILE" ]] || {
  echo "build/work was not prepared for profile $PROFILE; run: make configure PROFILE=$PROFILE" >&2
  exit 1
}
# shellcheck source=config/profiles/general/profile.conf
source "$ROOT/config/profiles/$PROFILE/profile.conf"
# shellcheck source=build/lb-cache.sh
source "$ROOT/build/lb-cache.sh"
ISO="$OUT/couchliteos-$VERSION-${ISO_SUFFIX:+$ISO_SUFFIX-}amd64.iso"
read -r -a profile_options <<< "${LB_EXTRA_CONFIG:-}"

mkdir -p "$OUT"
cd "$WORK"
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}

own_timings=0
if [[ -z ${COUCHLITEOS_TIMINGS:-} ]]; then
  export COUCHLITEOS_TIMINGS="$OUT/build-times.txt"
  : > "$COUCHLITEOS_TIMINGS"
  own_timings=1
fi
build_start=$SECONDS
timed() {
  local name=$1 start=$SECONDS
  shift
  "$@"
  "$ROOT/build/timed.sh" --record "$name" $((SECONDS - start))
}
# Seconds between two `[date time] lb STEP` lines live-build printed into lb-binary.log.
lb_step_seconds() {
  local from to
  from=$(sed -n -E "s/^\[([0-9: -]+)\] lb $1( .*)?$/\1/p; T; q" lb-binary.log) || return 0
  to=$(sed -n -E "s/^\[([0-9: -]+)\] lb $2( .*)?$/\1/p; T; q" lb-binary.log) || return 0
  [[ -n $from && -n $to ]] || return 0
  "$ROOT/build/timed.sh" --record "binary/$3" $(($(date -d "$to" +%s) - $(date -d "$from" +%s)))
}

# Debian packages through a local caching proxy. live-build hands http_proxy to debootstrap,
# apt in the chroot and the installer download, and writes it nowhere in the image (the
# image check below fails the build if any apt or environment file names a proxy).
# HTTPS sources (Tailscale, Google Chrome) are fetched directly.
proxy_answers() {
  curl -fsS --max-time 15 --proxy "$1" -o /dev/null http://deb.debian.org/debian/dists/trixie/InRelease
}
apt_proxy=${COUCHLITEOS_APT_PROXY-auto}
case $apt_proxy in
  ''|off|none) apt_proxy= ;;
  auto)
    if proxy_answers http://127.0.0.1:3142 2>/dev/null; then apt_proxy=http://127.0.0.1:3142; else apt_proxy=; fi
    ;;
  http://*)
    proxy_answers "$apt_proxy" || { echo "COUCHLITEOS_APT_PROXY=$apt_proxy does not answer" >&2; exit 1; }
    ;;
  *) echo "COUCHLITEOS_APT_PROXY must be auto, off or an http:// URL: $apt_proxy" >&2; exit 64 ;;
esac
cache_options=()
if [[ -n $apt_proxy ]]; then
  export http_proxy=$apt_proxy
  # The proxy is the package cache: apt checks every file it serves against the package
  # index, so live-build keeps no second copy of the .debs.
  cache_options=(--cache-packages false)
  echo "Package downloads: through $apt_proxy"
else
  echo 'Package downloads: direct (no apt proxy)'
fi

# xz is the smallest squashfs and stays for releases. zstd level 9 took 13 s instead of
# xz's 124 s on the 0.2.3 general chroot (16 CPUs), for a 13% larger image (1.57 GB
# instead of 1.40 GB); level 15 took 50 s for 7%. The Debian 13 kernel the image boots
# (CONFIG_SQUASHFS_ZSTD=y) and live-boot read zstd.
if ((release)); then
  squashfs_options=(--chroot-squashfs-compression-type xz)
  echo 'Build type: release (xz squashfs)'
else
  squashfs_options=(--chroot-squashfs-compression-type zstd --chroot-squashfs-compression-level 9)
  echo 'Build type: test (zstd squashfs; sudo make build RELEASE=1 for a release)'
fi

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
  --bootappend-live 'boot=live components persistence ipv6.disable=1 hostname=couchliteos username=couchliteos locales=en_US.UTF-8 keyboard-layouts=us console=tty1 console=ttyS0,115200n8' \
  --bootappend-install 'ipv6.disable=1' \
  --iso-application 'CouchLiteOS streaming appliance' \
  --iso-publisher 'CouchLiteOS Project' \
  --iso-volume 'COUCHLITEOS' \
  --apt-recommends false \
  --memtest none \
  "${cache_options[@]}" \
  "${squashfs_options[@]}" \
  "${profile_options[@]}"

if [[ -n ${COUCHLITEOS_LB_CACHE:-} ]]; then
  lb_cache_prepare "$COUCHLITEOS_LB_CACHE"
fi
if [[ -z $apt_proxy ]]; then
  timed verify-cache lb_cache_verify_debs cache/packages.bootstrap cache/packages.chroot cache/packages.binary
fi

listing=
image_root=
on_exit() {
  local status=$?
  [[ -z $listing ]] || rm -f -- "$listing"
  [[ -z $image_root ]] || rm -rf -- "$image_root"
  # A failed live-build step can leave /proc, /sys and /dev/pts mounted in the chroot.
  if ((status)) && [[ -d $WORK/chroot ]]; then
    (cd "$WORK" && lb_cache_chroot_unmount)
  fi
}
trap on_exit EXIT

lb_binary() {
  lb binary 2>&1 | tee lb-binary.log
}

# `lb build` is these five stages; they run one by one to time them and, with a
# shared cache, to reuse the installed chroot.
timed bootstrap lb bootstrap
if [[ -n ${COUCHLITEOS_LB_CACHE:-} ]]; then
  timed chroot lb_cache_chroot_stage "$COUCHLITEOS_LB_CACHE" $((release ? 0 : 1))
else
  timed chroot lb chroot
fi
timed installer lb installer
timed binary lb_binary
lb_step_seconds binary_rootfs binary_dm-verity squashfs
lb_step_seconds binary_checksums binary_iso checksums
lb_step_seconds binary_iso binary_onie iso
timed source lb source
if [[ -n ${COUCHLITEOS_LB_CACHE:-} ]]; then
  lb_cache_save_bootstrap "$COUCHLITEOS_LB_CACHE"
fi
built_iso=$(find . -maxdepth 1 -type f -name '*.hybrid.iso' -print -quit)
[[ -n "$built_iso" ]] || {
  echo 'live-build completed without producing an *.hybrid.iso file' >&2
  exit 1
}

image_checks() {
  # Profile checks run against the root filesystem that ships in the ISO.
  listing=$(mktemp)
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

  # The build-time apt proxy must not reach the image's apt configuration.
  image_root=$(mktemp -d)
  unsquashfs -q -n -d "$image_root/root" binary/live/filesystem.squashfs etc/apt etc/environment >/dev/null
  if grep -rIEin '^[^#]*(proxy|:3142)' "$image_root/root"; then
    echo 'The image names an apt or http proxy (above); it must not ship one' >&2
    exit 1
  fi
}
timed checks image_checks

# A hard link: the ISO is not copied (it is 2 GB and the work tree is rebuilt anyway).
rm -f -- "$ISO"
ln -- "$built_iso" "$ISO" 2>/dev/null || install -m 0644 "$built_iso" "$ISO"
chmod 0644 "$ISO"
printf 'ISO: %s\n' "$ISO"
printf 'Build: %s, %ss\n' "$( ((release)) && echo release || echo test)" $((SECONDS - build_start))
if ((own_timings)); then
  "$ROOT/build/timed.sh" --summary
fi
