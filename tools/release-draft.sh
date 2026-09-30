#!/bin/bash
# Create a DRAFT GitHub release for this version with the ISOs and SHA256SUMS.
# It never publishes, never replaces an existing release, and never pushes.
# Publish the draft yourself after the physical hardware checklist passes.
#
# usage: tools/release-draft.sh [TARGET_COMMIT]   (default: HEAD, which must be on GitHub)
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${COUCHLITEOS_RELEASE_DIR:-$ROOT/build/out}
VERSION=$(< "$ROOT/VERSION")
TAG=$VERSION
NOTES="$ROOT/docs/releases/v$VERSION.md"
REPO=${COUCHLITEOS_RELEASE_REPO:-Dudiebug/couchliteos}
target=$(git -C "$ROOT" rev-parse --verify "${1:-HEAD}^{commit}")

command -v gh >/dev/null || { echo 'GitHub CLI (gh) is required' >&2; exit 127; }
gh auth status >/dev/null 2>&1 || { echo 'Run gh auth login first.' >&2; exit 1; }
[[ -f $NOTES ]] || { echo "Release notes missing: $NOTES" >&2; exit 66; }
git -C "$ROOT" fetch --quiet origin
git -C "$ROOT" branch -r --contains "$target" | grep -q . || {
  echo "Commit $target is not on GitHub yet; push it before creating the draft." >&2
  exit 1
}
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "Release $TAG already exists on $REPO; not replacing it." >&2
  exit 1
fi

COUCHLITEOS_RELEASE_DIR=$OUT "$ROOT/tools/release-assets.sh"
# Lines are "<sha256>  <name>" or, from binary-mode tools, "<sha256> *<name>".
mapfile -t assets < <(sed -E 's/^[0-9a-f]{64} [ *]//' "$OUT/SHA256SUMS")
gh release create "$TAG" \
  --repo "$REPO" \
  --draft \
  --target "$target" \
  --title "CouchLiteOS v$VERSION" \
  --notes-file "$NOTES" \
  "${assets[@]/#/$OUT/}" "$OUT/SHA256SUMS"
gh release view "$TAG" --repo "$REPO" --json isDraft,tagName,targetCommitish,assets \
  --jq '"draft=\(.isDraft) tag=\(.tagName) target=\(.targetCommitish)", (.assets[] | "\(.name) \(.size)")'
