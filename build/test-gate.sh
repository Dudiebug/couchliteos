#!/bin/bash
# Keeps `make build` from running the unit and static suites a second time.
#   build/test-gate.sh mark  last step of `make test`: records a hash of the source tree
#                            that just passed (build/.tests-passed)
#   build/test-gate.sh run   first step of `make build`: runs `make test` unless the
#                            marker matches this exact tree on this host
# The hash covers every source file's path, type, mode and content (not .git, build
# output, downloads or Python caches), the host name and the Python version, so any
# edit, added or removed file, or a different machine runs the suites again.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MARKER=$ROOT/build/.tests-passed

tree_files() {
  find . \( -path ./.git -o -path ./.claude -o -path ./build/work -o -path ./build/out \
    -o -path ./build/downloads -o -path './build/.tests-passed*' -o -path ./graphify-out \
    -o -name __pycache__ -o -name '*.pyc' \) -prune -o "$@"
}

tree_hash() {
  cd "$ROOT"
  {
    uname -n
    python3 --version 2>&1 || true
    tree_files -printf '%y %m %p %l\n' | LC_ALL=C sort
    tree_files -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
    # tests/test_cage_build.py skips its patch checks without the Cage tarball: a pass
    # made before it was downloaded does not count once it is there.
    sha256sum build/downloads/cage-0.2.0.tar.gz 2>/dev/null || echo 'no cage tarball'
  } | sha256sum | cut -d' ' -f1
}

case ${1:-} in
  mark)
    # Written beside the old marker and renamed over it, so a marker left by a root
    # (`sudo make build`) run never stops a later unprivileged `make test`.
    temporary=$(mktemp "$MARKER.XXXXXX")
    tree_hash > "$temporary"
    chmod 0644 "$temporary"
    mv -f -- "$temporary" "$MARKER"
    ;;
  check)
    [[ -f $MARKER && $(< "$MARKER") == "$(tree_hash)" ]]
    ;;
  run)
    if "$0" check; then
      echo 'tests: skipped, make test already passed for this exact source tree'
      exit 0
    fi
    rm -f -- "$MARKER"
    if [[ $EUID == 0 && -n ${SUDO_UID:-} ]]; then
      # Under sudo the suites run as root; hand what they create (the marker, Python caches)
      # back to the developer, whose own `make test` must still be able to rewrite them.
      give_back() {
        find "$ROOT" \( -path "$ROOT/.git" -o -path "$ROOT/build/work" \) -prune -o \
          \( -name __pycache__ -o -path "$MARKER" \) -user 0 \
          -exec chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" {} + 2>/dev/null || true
      }
      trap give_back EXIT
    fi
    make -C "$ROOT" test
    ;;
  *)
    echo 'usage: test-gate.sh mark|check|run' >&2
    exit 64
    ;;
esac
