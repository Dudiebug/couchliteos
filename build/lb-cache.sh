#!/bin/bash
# Optional cache shared between builds (sourced by build/build.sh, off unless
# COUCHLITEOS_LB_CACHE=/absolute/dir is set; unset it to build exactly as before).
#
# live-build already caches inside build/work/cache, but `make configure` wipes
# build/work, so the general and nvidia builds each download every package and
# run debootstrap again. This keeps three things outside build/work:
#   packages.{bootstrap,chroot,binary}  the .deb download caches (unused when the
#       build goes through an apt proxy). Every cached .deb is unpacked before a
#       build (lb_cache_verify_debs) and a damaged one is deleted, so a truncated
#       or corrupt download cannot break the build at dpkg time.
#   bootstrap-<key>  the debootstrapped root (live-build's own cache/bootstrap).
#       <key> covers everything that decides that root: config/common,
#       config/bootstrap, the package indices the mirror serves right now and
#       the debootstrap/live-build versions. Any change is a miss, not a stale hit.
#       The one build-specific value in the root, BUILD_ID in /etc/os-release
#       (from SOURCE_DATE_EPOCH), is rewritten on every restore.
#   chroot-<key>.tar.zst  the chroot with every package installed, taken before
#       the includes and hooks run (see lb_cache_chroot_stage). Hooks and
#       includes always run again, so launcher and script changes always land.
# Not shared on purpose: the installer cache (keyed by URL, not content) and
# LB_CACHE_INDICES. Do not run two builds against one cache directory at once.

lb_cache_fetch() {
  curl -fsSL --max-time 30 "$1"
}

# The lb config files, without the settings that only say how packages are fetched or
# compressed: they do not change what is installed.
lb_cache_config() {
  grep -hvE '^(LB_CACHE|LB_APT_HTTP_PROXY|LB_CHROOT_SQUASHFS_COMPRESSION)' "$@" || true
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
    lb_cache_config config/common config/bootstrap
    printf '%s\n' "$indices"
    dpkg-query -W -f '${Package} ${Version}\n' debootstrap live-build 2>/dev/null || true
  } | md5sum | cut -d' ' -f1
}

# live-build writes BUILD_ID (from SOURCE_DATE_EPOCH) into /etc/os-release while
# bootstrapping; a restored root gets this build's value.
lb_cache_set_build_id() {
  local release=$1/etc/os-release id
  id=$(date --utc -d "@${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH is required}" +%Y%m%dT%H%M%SZ) || return 1
  [[ -f $release ]] || return 0
  grep -q '^BUILD_ID=' "$release" || return 0
  sed -i "s/^BUILD_ID=.*/BUILD_ID=$id/" "$release"
}

# Run in build/work after `lb config`, before `lb build`. Sets LB_CACHE_KEY
# (empty when the mirror could not be asked: then the bootstrap is never reused or saved).
lb_cache_prepare() {
  local dir=$1 name
  [[ $dir == /* ]] || { echo "lb-cache: COUCHLITEOS_LB_CACHE must be an absolute path: $dir" >&2; return 1; }
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
    if cp -a "$dir/bootstrap-$LB_CACHE_KEY" cache/bootstrap && lb_cache_set_build_id cache/bootstrap; then
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

# Unpacks every cached .deb under the given directories (16 at a time per CPU) and
# deletes the ones that do not unpack: a truncated or corrupt download would otherwise
# stop the build in dpkg ("lzma error", "gzip: unexpected end of file"). Prints a report.
lb_cache_verify_debs() {
  local dirs=() dir bad checked
  for dir in "$@"; do
    [[ -d $dir/ ]] && dirs+=("$dir/")
  done
  ((${#dirs[@]})) || return 0
  checked=$(find "${dirs[@]}" -maxdepth 1 -type f -name '*.deb' | wc -l)
  # shellcheck disable=SC2016
  bad=$(find "${dirs[@]}" -maxdepth 1 -type f -name '*.deb' -print0 \
    | xargs -0 -r -n 16 -P "$(nproc)" sh -c '
        for deb; do
          if ! dpkg-deb --ctrl-tarfile "$deb" >/dev/null 2>&1 || ! dpkg-deb --fsys-tarfile "$deb" >/dev/null 2>&1; then
            rm -f -- "$deb" && printf "%s\n" "$deb"
          fi
        done' sh) || true
  if [[ -n $bad ]]; then
    printf 'lb-cache: deleted %s damaged cached package(s) of %s:\n%s\n' "$(wc -l <<< "$bad")" "$checked" "$bad" >&2
  else
    printf 'lb-cache: all %s cached packages unpack cleanly\n' "$checked" >&2
  fi
}

# The live-build chroot script this replacement follows, step for step (live-build
# 1:20250505+deb13u1). Any other version builds with plain `lb chroot`.
LB_CACHE_CHROOT_SCRIPT=/usr/lib/live/build/chroot
LB_CACHE_CHROOT_SCRIPT_MD5=3ccc313652c32941a58bb17c8501d1f0
LB_CACHE_CHROOT_DAYS=${COUCHLITEOS_CHROOT_CACHE_DAYS:-7}

lb_cache_chroot_supported() {
  command -v zstd >/dev/null || { echo 'lb-cache: zstd is not installed, not reusing the chroot' >&2; return 1; }
  [[ $(md5sum < "$LB_CACHE_CHROOT_SCRIPT" 2>/dev/null | cut -d' ' -f1) == "$LB_CACHE_CHROOT_SCRIPT_MD5" ]] || {
    echo "lb-cache: $LB_CACHE_CHROOT_SCRIPT is not the version this cache follows, not reusing the chroot" >&2
    return 1
  }
}

# Prints the key for the installed chroot: the bootstrap key plus every input of the
# package stage (all lb config files, package lists, apt sources, keys and pins,
# preseeds, local packages, the includes copied before packages). Hooks and the
# includes copied after packages are not in it: they run again on every build.
lb_cache_chroot_key() {
  local base path files=() dirs=()
  base=${LB_CACHE_KEY:-$(lb_cache_key)} || return 1
  [[ -n $base ]] || return 1
  for path in config/common config/bootstrap config/chroot config/binary config/source; do
    [[ -f $path ]] && files+=("$path")
  done
  for path in config/package-lists config/archives config/apt config/preseed \
      config/includes.chroot_before_packages config/packages.chroot config/packages; do
    [[ -e $path || -L $path ]] && dirs+=("$path")
  done
  {
    printf 'couchliteos-chroot-v1\n%s\n' "$base"
    printf '%s\n' "${files[@]}"
    ((${#files[@]} == 0)) || lb_cache_config "${files[@]}"
    if ((${#dirs[@]})); then
      find "${dirs[@]}" -printf '%y %m %p %l\n' | LC_ALL=C sort
      find "${dirs[@]}" -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
    fi
  } | md5sum | cut -d' ' -f1
}

lb_cache_chroot_cmd() {
  chroot chroot /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin "$@"
}

# Unmounts what the chroot steps mounted, as live-build's own exit handler does
# (build.sh calls it when a build fails).
lb_cache_chroot_unmount() {
  local mount
  awk -v dir="$PWD/chroot/" 'index($2, dir) == 1 { print $2 }' /proc/mounts | sort -r \
    | while read -r mount; do umount "$mount" >/dev/null 2>&1 || umount -l "$mount" >/dev/null 2>&1 || true; done
  rm -f .build/chroot_devpts .build/chroot_proc .build/chroot_selinuxfs .build/chroot_sysfs
}

# Replaces chroot/ with the saved snapshot. Returns 1 (chroot/ untouched) when there is none.
lb_cache_chroot_restore() {
  local snapshot=$1 tmp=chroot.lb-cache
  [[ -f $snapshot ]] || return 1
  # rm -rf must never walk into a mounted /proc, /sys or /dev/pts.
  if awk -v dir="$PWD/chroot/" 'index($2, dir) == 1 { found = 1 } END { exit !found }' /proc/mounts; then
    echo 'lb-cache: something is mounted inside chroot/, installing packages afresh' >&2
    return 1
  fi
  if [[ -z $(find "$snapshot" -mmin "-$((LB_CACHE_CHROOT_DAYS * 24 * 60))") ]]; then
    echo "lb-cache: the chroot snapshot is older than $LB_CACHE_CHROOT_DAYS days, installing packages afresh" >&2
    rm -f -- "$snapshot"
    return 1
  fi
  rm -rf "$tmp" && mkdir "$tmp"
  if zstd -q -d -c -- "$snapshot" \
      | tar -x -p -f - -C "$tmp" --numeric-owner --xattrs --xattrs-include='*' --acls \
      && lb_cache_set_build_id "$tmp"; then
    rm -rf chroot && mv "$tmp" chroot
    echo "lb-cache: restored the installed chroot $(basename "$snapshot")" >&2
    return 0
  fi
  rm -rf "$tmp"
  echo 'lb-cache: restoring the chroot snapshot failed, installing packages afresh' >&2
  return 1
}

# Never fails the build.
lb_cache_chroot_save() {
  local dir=$1 snapshot=$2 tmp
  tmp=$dir/.tmp-$(basename "$snapshot").$$
  if tar -c -f - -C chroot --one-file-system --numeric-owner --xattrs --xattrs-include='*' --acls \
        --exclude='./var/cache/apt/archives/*.deb' . \
      | zstd -q -T0 -6 -o "$tmp" && mv "$tmp" "$snapshot"; then
    echo "lb-cache: saved the installed chroot $(basename "$snapshot") ($(du -h "$snapshot" | cut -f1))" >&2
    # Keep the two newest snapshots (general and nvidia have different package lists).
    find "$dir" -maxdepth 1 -type f -name 'chroot-*.tar.zst' -printf '%T@ %p\n' \
      | sort -rn | tail -n +3 | cut -d' ' -f2- | xargs -r -d '\n' rm -f
  else
    rm -f -- "$tmp"
    echo 'lb-cache: saving the chroot snapshot failed (the build itself is fine)' >&2
  fi
  return 0
}

# Runs in build/work in place of `lb chroot`: the same live-build steps in the same
# order, plus the snapshot. With reuse=1 and a snapshot for this key, the chroot
# starts from it; the package steps still run (apt upgrades anything newer and
# installs anything missing, which with an unchanged key is nothing), then the
# includes and hooks run on top exactly as in a fresh build. Without a snapshot the
# packages are installed normally and the chroot is saved at that point: after
# chroot_prep remove, so the snapshot holds no mounts, build-time apt sources,
# policy-rc.d or diverted start-stop-daemon. Release builds pass reuse=0.
lb_cache_chroot_stage() {
  local dir=$1 reuse=$2 key='' snapshot='' restored=0 pass
  if lb_cache_chroot_supported; then
    key=$(lb_cache_chroot_key) || key=
    [[ -n $key ]] || echo 'lb-cache: package indices unavailable, not reusing or saving the chroot' >&2
  fi
  if [[ -z $key ]]; then
    lb chroot
    return
  fi
  snapshot=$dir/chroot-$key.tar.zst
  if [[ $reuse == 1 ]]; then
    if lb_cache_chroot_restore "$snapshot"; then
      restored=1
    elif [[ ! -f $snapshot ]]; then
      echo "lb-cache: no installed chroot for $key yet, installing packages" >&2
    fi
  else
    echo 'lb-cache: release build, installing packages afresh' >&2
  fi

  lb chroot_cache restore
  lb chroot_prep install all mode-archives-chroot
  lb chroot_linux-image
  lb chroot_firmware
  lb chroot_preseed
  lb chroot_includes_before_packages
  for pass in install live; do
    lb chroot_package-lists "$pass"
    lb chroot_install-packages "$pass"
    if [[ $pass == install ]]; then
      lb_cache_chroot_cmd dpkg-query -W > chroot.packages.install
    fi
  done
  if ((!restored)); then
    lb chroot_prep remove all mode-archives-chroot
    lb_cache_chroot_save "$dir" "$snapshot"
    lb chroot_prep install all mode-archives-chroot
  fi
  lb chroot_includes_after_packages
  lb chroot_hooks
  lb chroot_hacks
  lb chroot_interactive
  lb_cache_chroot_cmd dpkg-query -W > chroot.packages.live
  lb chroot_prep remove all mode-archives-chroot
  lb chroot_cache save
  lb_cache_chroot_cmd ls -lR > chroot.files
}
