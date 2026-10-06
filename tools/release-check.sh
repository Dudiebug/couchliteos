#!/bin/bash
# Check that a release is ready to publish (make release-check).
#   tools/release-check.sh                 everything below, for the version in VERSION
#   tools/release-check.sh --notes FILE... only the public-wording check, on the given files
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# Words that belong in the maintainer's notes, not in public release notes or docs.
INTERNAL='testing status|testing\.md|release-spec|-plan\.md|\bqemu\b|\bvm\b|unit tests?|static tests?|review pass|\bgates?\b|\bagents?\b|\bcodex\b|\bclaude\b|build took|build time|\bci\b|not yet tested|draft|not (yet )?(been )?tried|were not checked|had not yet been'

failed=0
fail() { printf 'release-check: %s\n' "$*" >&2; failed=1; }

check_wording() {
  local file hits
  for file; do
    [[ -f $file ]] || { fail "missing: $file"; continue; }
    if hits=$(grep -n -i -E -- "$INTERNAL" "$file"); then
      fail "$file has internal wording:"
      printf '  %s\n' "${hits//$'\n'/$'\n'  }" >&2
    fi
  done
}

if [[ ${1:-} == --notes ]]; then
  shift
  (($#)) || { echo 'usage: release-check.sh --notes FILE...' >&2; exit 64; }
  check_wording "$@"
  exit "$failed"
fi

VERSION=$(< VERSION)
TAG=v$VERSION
NOTES=docs/releases/$TAG.md
OUT=${COUCHLITEOS_RELEASE_DIR:-$ROOT/build/out}

[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || fail "VERSION '$VERSION' is not MAJOR.MINOR.PATCH[-PRERELEASE]"
cmp -s VERSION overlay/etc/couchliteos-version || fail 'VERSION and overlay/etc/couchliteos-version differ'

whatsnew=$(python3 -c 'import sys; sys.path.insert(0, "launcher"); import couchliteos_whatsnew as w; print(w.RELEASES[0][0])') \
  || fail 'could not read RELEASES from launcher/couchliteos_whatsnew.py'
# A pre-release (0.3.0-beta) shows the notes of the release it leads to.
[[ ${whatsnew:-} == "${VERSION%%-*}" ]] || fail "the newest What's New entry is '${whatsnew:-}', not ${VERSION%%-*}"

if [[ -f $NOTES ]]; then
  [[ $(head -n 1 "$NOTES") == "# CouchLiteOS $TAG" ]] || fail "$NOTES must start with '# CouchLiteOS $TAG'"
  grep -q '^## Updating$' "$NOTES" || fail "$NOTES has no '## Updating' section"
  check_wording "$NOTES"
else
  fail "missing release notes: $NOTES"
fi

# The README links to the latest release instead of naming a version.
if hits=$(grep -n -E 'v?[0-9]+\.[0-9]+\.[0-9]+' README.md); then
  fail 'README.md names a version:'
  printf '  %s\n' "${hits//$'\n'/$'\n'  }" >&2
fi
check_wording README.md

[[ -z $(git status --porcelain --untracked-files=no) ]] || fail 'the working tree has uncommitted changes'
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && fail "tag $TAG already exists locally"
remote_status=0
git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1 || remote_status=$?
case $remote_status in
  0) fail "tag $TAG already exists on GitHub; a published version is never reused" ;;
  2) ;;
  *) fail 'could not reach origin to check the tag' ;;
esac

if [[ -f $OUT/SHA256SUMS ]]; then
  (cd "$OUT" && sha256sum --quiet -c SHA256SUMS) || fail "$OUT/SHA256SUMS does not verify"
  grep -q " couchliteos-$VERSION-amd64\.iso$" "$OUT/SHA256SUMS" \
    || fail "$OUT/SHA256SUMS has no couchliteos-$VERSION-amd64.iso"
  if grep -v -q -E " couchliteos-$VERSION-([a-z0-9]+-)?amd64\.iso$" "$OUT/SHA256SUMS"; then
    fail "$OUT/SHA256SUMS lists files from another version"
  fi
else
  fail "missing $OUT/SHA256SUMS; run make release-assets"
fi

((failed == 0)) || exit 1
echo "release-check: $TAG is ready to publish"
