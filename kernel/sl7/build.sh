#!/bin/bash
# Builds the CouchLiteOS Snapdragon X (Surface Laptop 7) kernel as Debian packages.
#   kernel/sl7/build.sh [--install-deps]
# Runs on arm64 Debian trixie as root (the kernel-arm64 workflow: GitHub's ubuntu-24.04-arm
# runner in a debian:trixie container). It takes upstream linux-$KERNEL_VERSION from
# kernel.org, Debian's own arm64 kernel config (trixie-backports) as the base, merges
# config.fragment, applies patches/series with no fuzz, and runs `make bindeb-pkg`.
# Output: kernel/out/*.deb and kernel/out/SHA256SUMS.
# Environment:
#   KERNEL_WORK=dir         source and downloads (default kernel/work)
#   KERNEL_OUT=dir          packages (default kernel/out)
#   KERNEL_BASE_CONFIG=file use this .config as the base instead of Debian's
#   KERNEL_JOBS=n           make -j (default nproc)
set -Eeuo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=${KERNEL_WORK:-$ROOT/kernel/work}
OUT=${KERNEL_OUT:-$ROOT/kernel/out}
# shellcheck source=kernel/sl7/version.conf
source "$HERE/version.conf"
JOBS=${KERNEL_JOBS:-$(nproc)}

install_deps=0
for arg in "$@"; do
  case $arg in
    --install-deps) install_deps=1 ;;
    *) echo "usage: kernel/sl7/build.sh [--install-deps]" >&2; exit 64 ;;
  esac
done

[[ $(uname -m) == aarch64 ]] || { echo 'build.sh runs on arm64 (aarch64) only' >&2; exit 1; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'Run as root (apt and the Debian config download)' >&2; exit 1; }

if ((install_deps)); then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install --yes --no-install-recommends \
    build-essential bc bison flex libssl-dev libelf-dev libdw-dev libncurses-dev dwarves \
    python3 rsync kmod cpio xz-utils zstd ca-certificates curl patch \
    dpkg-dev debhelper fakeroot ccache git
fi

mkdir -p "$WORK" "$OUT"
tarball="$WORK/linux-$KERNEL_VERSION.tar.xz"
src="$WORK/linux-$KERNEL_VERSION"

# 1. Source: upstream tarball, checked against the pinned SHA-256.
if [[ ! -f $tarball ]] || ! sha256sum --check --status <<< "$KERNEL_SHA256  $tarball"; then
  curl --fail --location --retry 4 --output "$tarball" \
    "https://cdn.kernel.org/pub/linux/kernel/v${KERNEL_VERSION%%.*}.x/linux-$KERNEL_VERSION.tar.xz"
  sha256sum --check --status <<< "$KERNEL_SHA256  $tarball" || {
    echo "linux-$KERNEL_VERSION.tar.xz does not match KERNEL_SHA256" >&2; exit 1; }
fi
rm -rf "$src"
echo "Extracting linux-$KERNEL_VERSION"
tar -C "$WORK" -xf "$tarball"

# 2. Patches: patches/series in order, exact context only. A patch that does not
#    apply stops the build; nothing is guessed.
cd "$src"
while read -r patch; do
  [[ -z $patch || $patch == \#* ]] && continue
  echo "Applying $patch"
  patch --batch --forward --fuzz=0 --no-backup-if-mismatch -p1 -i "$HERE/patches/$patch"
done < "$HERE/patches/series"

# 3. Config: Debian's arm64 kernel config (the trixie-backports kernel, the one the
#    generic arm64 ISO would use), then config.fragment on top, then olddefconfig.
if [[ -n ${KERNEL_BASE_CONFIG:-} ]]; then
  cp "$KERNEL_BASE_CONFIG" .config
else
  cat > /etc/apt/sources.list.d/couchliteos-backports.list <<LIST
deb http://deb.debian.org/debian $DEBIAN_BASE-backports main
LIST
  apt-get update
  image=$(apt-cache depends --target-release "$DEBIAN_BASE-backports" linux-image-arm64 \
    | sed -n 's/^ *Depends: \(linux-image-[0-9][^ ]*-arm64\)$/\1/p' | head -n 1)
  [[ -n $image ]] || { echo "No linux-image-arm64 in $DEBIAN_BASE-backports" >&2; exit 1; }
  # Since 7.x the image package is a stub; the config is in its linux-base-<ver> dependency
  # (/usr/lib/modules/<ver>/config). Older layouts put it at /boot/config-<ver>.
  base=$(apt-cache depends --target-release "$DEBIAN_BASE-backports" "$image" \
    | sed -n 's/^ *Depends: \(linux-base-[0-9][^ ]*\)$/\1/p' | head -n 1)
  pkg=${base:-$image}
  echo "Base config: Debian $image ($pkg)"
  (cd "$WORK" && apt-get download --target-release "$DEBIAN_BASE-backports" "$pkg")
  deb=$(ls "$WORK/${pkg}_"*.deb | head -n 1)
  member=$(dpkg-deb --fsys-tarfile "$deb" | tar -tf - \
    | grep -E '^\./(usr/lib/modules/[^/]+/config|boot/config-[^/]+)$' | head -n 1)
  [[ -n $member ]] || { echo "No kernel config in $pkg" >&2; exit 1; }
  dpkg-deb --fsys-tarfile "$deb" | tar -xOf - "$member" > .config
  [[ -s .config ]] || { echo "Empty kernel config in $pkg" >&2; exit 1; }
fi
scripts/kconfig/merge_config.sh -m .config "$HERE/config.fragment" > /dev/null
make ARCH=arm64 olddefconfig > /dev/null

# Every option the fragment sets must have survived olddefconfig: a missing one means a
# renamed option or an unmet dependency, and the kernel would silently lack the device.
missing=()
while read -r line; do
  [[ $line =~ ^CONFIG_[A-Z0-9_]+=(y|m|\"[^\"]*\")$ ]] || continue
  grep -qxF "$line" .config || missing+=("$line")
done < "$HERE/config.fragment"
if ((${#missing[@]})); then
  printf 'Not in the final .config:\n' >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

# 4. Build. The package: linux-image-<ver>-sl7, version <ver>-<rev>~couchliteos. Reproducible
#    user, host and timestamp. ccache when present (the workflow caches it).
cc=gcc
command -v ccache > /dev/null && cc='ccache gcc'
export KBUILD_BUILD_USER=couchliteos KBUILD_BUILD_HOST=build
export KBUILD_BUILD_TIMESTAMP=${SOURCE_DATE_EPOCH:+@$SOURCE_DATE_EPOCH}
[[ -n $KBUILD_BUILD_TIMESTAMP ]] || unset KBUILD_BUILD_TIMESTAMP
echo "Building with -j$JOBS"
make ARCH=arm64 CC="$cc" -j"$JOBS" LOCALVERSION=-sl7 KDEB_CHANGELOG_DIST="$DEBIAN_BASE" \
  KDEB_PKGVERSION="$KERNEL_VERSION-$PKG_REVISION~couchliteos" KDEB_COMPRESS=xz bindeb-pkg

# 5. Packages and sums.
rm -f "$OUT"/*.deb "$OUT"/*.changes "$OUT"/*.buildinfo "$OUT"/SHA256SUMS
mv "$WORK"/linux-*_"$KERNEL_VERSION-$PKG_REVISION~couchliteos"_arm64.* "$OUT"/
(cd "$OUT" && sha256sum -- *.deb > SHA256SUMS && cat SHA256SUMS)
