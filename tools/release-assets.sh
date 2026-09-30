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
