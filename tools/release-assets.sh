#!/bin/bash
# Write build/out/SHA256SUMS for this version's ISOs and check that every asset
# fits GitHub's 2 GiB per-asset limit. ISOs and checksums are never committed.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${COUCHLITEOS_RELEASE_DIR:-$ROOT/build/out}
VERSION=$(< "$ROOT/VERSION")
LIMIT=$((2 * 1024 * 1024 * 1024))

cd "$OUT"
# Only release profiles (RELEASE=1) are assets; legacy profiles are not.
isos=()
release_profiles=0
for profile_conf in "$ROOT"/config/profiles/*/profile.conf; do
  [[ $(source "$profile_conf"; printf '%s' "${RELEASE:-0}") == 1 ]] || continue
  release_profiles=$((release_profiles + 1))
  suffix=$(source "$profile_conf"; printf '%s' "${ISO_SUFFIX:-}")
  iso="couchliteos-$VERSION-${suffix:+$suffix-}amd64.iso"
  if [[ -f $iso ]]; then
    isos+=("$iso")
  else
    echo "missing: $OUT/$iso" >&2
  fi
done
((${#isos[@]})) || { echo "No CouchLiteOS $VERSION ISOs in $OUT" >&2; exit 66; }
[[ ${COUCHLITEOS_ALLOW_PARTIAL_RELEASE:-0} == 1 ]] || (( ${#isos[@]} == release_profiles )) || {
  echo 'Not every release profile has an ISO; set COUCHLITEOS_ALLOW_PARTIAL_RELEASE=1 to continue.' >&2
  exit 1
}

# Only release builds (sudo make build RELEASE=1, xz squashfs) are assets: a test build's
# zstd squashfs is larger. The compression id is a 16-bit field at byte 20 of the
# squashfs superblock (4 = xz, 6 = zstd).
squashfs_compression() {
  local lba
  lba=$(xorriso -indev "$1" -find /live/filesystem.squashfs -exec report_lba -- 2>/dev/null \
    | sed -n -E 's/^File data lba: *[0-9]+ *, *([0-9]+) *,.*/\1/p') || return 1
  [[ $lba =~ ^[0-9]+$ ]] || return 1
  od -An -tu2 -j $((lba * 2048 + 20)) -N 2 -- "$1" | tr -d ' \n'
}
command -v xorriso >/dev/null || { echo 'xorriso is required to check the ISOs' >&2; exit 1; }
for iso in "${isos[@]}"; do
  compression=$(squashfs_compression "$iso") || compression=
  [[ $compression == 4 ]] || {
    echo "$iso is not a release build (squashfs compression id '${compression:-unreadable}', not xz); rebuild it with: sudo make build RELEASE=1" >&2
    exit 1
  }
done

sha256sum -- "${isos[@]}" > SHA256SUMS
sha256sum -c SHA256SUMS
for asset in "${isos[@]}" SHA256SUMS; do
  size=$(stat -c %s -- "$asset")
  if ((size >= LIMIT)); then
    printf 'TOO LARGE for a GitHub release asset: %s (%s bytes >= %s)\n' "$asset" "$size" "$LIMIT" >&2
    exit 1
  fi
  printf '%-44s %11s bytes (%s MiB of the 2048 MiB limit)\n' "$asset" "$size" "$((size / 1024 / 1024))"
done
printf 'Wrote %s/SHA256SUMS\n' "$OUT"
