#!/bin/bash
# Optional cache shared between builds (sourced by build/build.sh, off unless
# MOONLIGHTOS_LB_CACHE=/absolute/dir is set; unset it to build exactly as before).
#
# live-build already caches inside build/work/cache, but `make configure` wipes
# build/work, so the general and nvidia builds each download every package and
# run debootstrap again. This keeps two things outside build/work:
#   packages.{bootstrap,chroot,binary}  the .deb download caches. apt verifies
#       every cached file against the fresh package index, so a cached .deb is
#       only ever used when it is byte-for-byte the one the index asks for.
#   bootstrap-<key>  the debootstrapped root (live-build's own cache/bootstrap).
#       <key> covers everything that decides that root: config/common,
#       config/bootstrap, the package indices the mirror serves right now,
#       SOURCE_DATE_EPOCH (it is written into /etc/os-release as BUILD_ID) and
#       the debootstrap/live-build versions. Any change is a miss, not a stale hit.
# Not shared on purpose: the installer cache (keyed by URL, not content) and
# LB_CACHE_INDICES. Do not run two builds against one cache directory at once.

lb_cache_fetch() {
  curl -fsSL --max-time 30 "$1"
}

# Prints the key for the bootstrap root of the lb config in the current directory.
lb_cache_key() {
  local indices
  indices=$(
    source config/bootstrap
    lb_cache_fetch "${LB_PARENT_MIRROR_BOOTSTRAP%/}/dists/${LB_PARENT_DISTRIBUTION_CHROOT}/InRelease"
  ) || return 1
  # Only the file-hash lines: Date: and the signature change without the packages changing.
  indices=$(grep -E '^ [0-9a-f]{64} +[0-9]+ ' <<< "$indices" | sort) || return 1
  [[ -n $indices ]] || return 1
  {
    cat config/common config/bootstrap
    printf '%s\n%s\n' "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH is required}" "$indices"
    dpkg-query -W -f '${Package} ${Version}\n' debootstrap live-build 2>/dev/null || true
  } | md5sum | cut -d' ' -f1
}

# Run in build/work after `lb config`, before `lb build`. Sets LB_CACHE_KEY
# (empty when the mirror could not be asked: then the bootstrap is never reused or saved).
lb_cache_prepare() {
  local dir=$1 name
  [[ $dir == /* ]] || { echo "lb-cache: MOONLIGHTOS_LB_CACHE must be an absolute path: $dir" >&2; return 1; }
  mkdir -p "$dir" cache
  for name in packages.bootstrap packages.chroot packages.binary; do
    mkdir -p "$dir/$name"
    # A real directory (an earlier build in this same work tree) is left alone.
    [[ -e cache/$name || -L cache/$name ]] || ln -s "$dir/$name" "cache/$name"
  done

  LB_CACHE_KEY=$(lb_cache_key) || LB_CACHE_KEY=
  if [[ -z $LB_CACHE_KEY ]]; then
    echo 'lb-cache: package indices unavailable, not reusing or saving the bootstrap root' >&2
    return 0
  fi
  # live-build restores cache/bootstrap if it exists. Start from nothing, so whatever
  # it holds after the build is the root for this key and no other.
  rm -rf cache/bootstrap
  if [[ -d $dir/bootstrap-$LB_CACHE_KEY ]]; then
    if cp -a "$dir/bootstrap-$LB_CACHE_KEY" cache/bootstrap; then
      touch "$dir/bootstrap-$LB_CACHE_KEY"
      echo "lb-cache: restored the bootstrap root $LB_CACHE_KEY" >&2
    else
      rm -rf cache/bootstrap
      echo 'lb-cache: restoring the bootstrap root failed, bootstrapping normally' >&2
    fi
  else
    echo "lb-cache: no bootstrap root for $LB_CACHE_KEY yet, bootstrapping normally" >&2
  fi
}

# Run in build/work after a successful `lb build`. Never fails the build.
lb_cache_save_bootstrap() {
  local dir=$1 key=${LB_CACHE_KEY:-} tmp
  [[ -n $key && -d cache/bootstrap && ! -e $dir/bootstrap-$key ]] || return 0
  tmp=$dir/.tmp-bootstrap-$key.$$
  if cp -a cache/bootstrap "$tmp" && mv "$tmp" "$dir/bootstrap-$key"; then
    touch "$dir/bootstrap-$key"
    echo "lb-cache: saved the bootstrap root $key" >&2
    # Keep the two newest roots (the lead builds general then nvidia from one key).
    find "$dir" -maxdepth 1 -type d -name 'bootstrap-*' -printf '%T@ %p\n' \
      | sort -rn | tail -n +3 | cut -d' ' -f2- | xargs -r -d '\n' rm -rf
  else
    rm -rf "$tmp"
    echo 'lb-cache: saving the bootstrap root failed (the build itself is fine)' >&2
  fi
  return 0
}
