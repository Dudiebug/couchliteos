#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST="$ROOT/build/downloads"
PROFILE=${PROFILE:-general}
mkdir -p "$DEST"

# The profile's architecture picks the application images (build/applications.<arch>.lock
# beside amd64's build/applications.lock); its packages.lock adds packages from outside
# Debian (the snapdragon kernel), each with its SHA-256.
profile_conf="$ROOT/config/profiles/$PROFILE/profile.conf"
[[ -f $profile_conf ]] || { echo "Unknown build profile: $PROFILE" >&2; exit 64; }
arch=$(unset ARCH; source "$profile_conf"; printf '%s' "${ARCH:-amd64}")
apps_lock="$ROOT/build/applications.lock"
[[ $arch == amd64 ]] || apps_lock="$ROOT/build/applications.$arch.lock"
locks=("$apps_lock" "$ROOT/build/sources.lock")
packages_lock="$ROOT/config/profiles/$PROFILE/packages.lock"
[[ -f $packages_lock ]] && locks+=("$packages_lock")

for lock in "${locks[@]}"; do
  while IFS='|' read -r name version url filename sha256; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    target="$DEST/$filename"
    if [[ -s "$target" ]]; then
      printf '%s %s already downloaded\n' "$name" "$version"
      continue
    fi
    tmp=$(mktemp "$DEST/.download.XXXXXX")
    trap 'rm -f -- "$tmp"' EXIT
    curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$tmp" "$url"
    [[ -s "$tmp" ]] || { echo "$name download is empty" >&2; exit 1; }
    if [[ -n ${sha256:-} ]]; then
      actual=$(sha256sum "$tmp" | cut -d' ' -f1)
      [[ $actual == "$sha256" ]] || { echo "$name: SHA-256 $actual, expected $sha256" >&2; exit 1; }
    fi
    chmod 0644 "$tmp"
    mv -f -- "$tmp" "$target"
    trap - EXIT
  done < "$lock"
done
