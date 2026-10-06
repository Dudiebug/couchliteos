#!/bin/bash
# Writes apps.json, the app manifest couchliteos-app-update reads, from build/applications.lock
# and the downloaded files (`make fetch-apps`), whose SHA-256 it records.
#
#   tools/apps-manifest.sh [--min-os VERSION] [--output FILE]
#
# --min-os defaults to VERSION: a box older than the release that tested these app versions
# is not offered them. Publish the file as the release asset apps.json; to ship a tested app
# version between releases, update build/applications.lock, run `make fetch-apps` and this,
# and replace apps.json on the latest release.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOCK=${COUCHLITEOS_APPS_LOCK:-$ROOT/build/applications.lock}
DOWNLOADS=${COUCHLITEOS_DOWNLOADS:-$ROOT/build/downloads}
min_os=$(< "$ROOT/VERSION")
output=-

while (($#)); do
  case $1 in
    --min-os) min_os=${2:?--min-os needs a version}; shift 2 ;;
    --output) output=${2:?--output needs a file}; shift 2 ;;
    *) echo "usage: $0 [--min-os VERSION] [--output FILE]" >&2; exit 64 ;;
  esac
done
[[ $min_os =~ ^[0-9]+(\.[0-9]+){0,2}(-[0-9A-Za-z.-]+)?$ ]] || { echo "bad --min-os: $min_os" >&2; exit 64; }

entries=()
while IFS='|' read -r name version url filename; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  [[ $name =~ ^[a-z0-9][a-z0-9-]*$ && $version =~ ^[0-9][0-9A-Za-z.+~-]*$ ]] || {
    echo "applications.lock: bad entry for $name" >&2; exit 65; }
  [[ $url == https://github.com/* && $url != *[\"\\[:space:]]* ]] || {
    echo "applications.lock: $name is not downloaded from GitHub over HTTPS: $url" >&2; exit 65; }
  file=$DOWNLOADS/$filename
  [[ -s $file ]] || { echo "$file is missing: run make fetch-apps" >&2; exit 66; }
  sha=$(sha256sum < "$file" | cut -d' ' -f1)  # stdin: a file name with a backslash would be escaped
  size=$(stat -c %s -- "$file")
  entries+=("$(printf '    {"name": "%s", "version": "%s", "url": "%s", "sha256": "%s", "size": %d, "min_os": "%s"}' \
    "$name" "$version" "$url" "$sha" "$size" "$min_os")")
done < "$LOCK"
((${#entries[@]})) || { echo "$LOCK lists no apps" >&2; exit 65; }

manifest=$(
  printf '{\n  "schema": 1,\n  "apps": [\n'
  for index in "${!entries[@]}"; do
    printf '%s%s\n' "${entries[$index]}" "$( ((index + 1 < ${#entries[@]})) && printf ',')"
  done
  printf '  ]\n}\n'
)
if [[ $output == - ]]; then
  printf '%s\n' "$manifest"
else
  printf '%s\n' "$manifest" > "$output.tmp"
  mv -f -- "$output.tmp" "$output"
fi
