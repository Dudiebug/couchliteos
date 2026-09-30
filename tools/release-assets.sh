#!/bin/bash
# Write build/out/SHA256SUMS for this version's ISOs and check that every asset
# fits GitHub's 2 GiB per-asset limit. ISOs and checksums are never committed.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${MOONLIGHTOS_RELEASE_DIR:-$ROOT/build/out}
VERSION=$(< "$ROOT/VERSION")
LIMIT=$((2 * 1024 * 1024 * 1024))

cd "$OUT"
isos=()
for profile_conf in "$ROOT"/config/profiles/*/profile.conf; do
  suffix=$(source "$profile_conf"; printf '%s' "${ISO_SUFFIX:-}")
  iso="moonlightos-$VERSION-${suffix:+$suffix-}amd64.iso"
  if [[ -f $iso ]]; then
    isos+=("$iso")
  else
    echo "missing: $OUT/$iso" >&2
  fi
done
((${#isos[@]})) || { echo "No MoonlightOS $VERSION ISOs in $OUT" >&2; exit 66; }
[[ ${MOONLIGHTOS_ALLOW_PARTIAL_RELEASE:-0} == 1 ]] || \
  (( ${#isos[@]} == $(find "$ROOT/config/profiles" -mindepth 1 -maxdepth 1 -type d | wc -l) )) || {
  echo 'Not every build profile has an ISO; set MOONLIGHTOS_ALLOW_PARTIAL_RELEASE=1 to continue.' >&2
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
