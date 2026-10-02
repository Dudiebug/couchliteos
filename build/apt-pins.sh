#!/bin/bash
# Prints apt preferences that hold every `package=version` entry of the given
# live-build package lists at exactly that version.
#
# `package=version` only applies to the apt-get install that live-build runs from the
# lists. live-build also runs apt-get upgrade and dist-upgrade each time it sets up the
# chroot's archives again: the binary stage does so on the finished chroot after the
# squashfs is made, which moved tailscale=1.102.3 to the repository's newer 1.102.4 in
# the 0.2.2 and 0.2.3 build logs and work trees (the squashfs itself kept 1.102.3).
# A reused chroot (build/lb-cache.sh) is upgraded the same way. configure.sh installs this
# output as config/archives/couchliteos-pins.pref.{chroot,binary}: live-build puts
# those in /etc/apt/preferences.d only while it builds and removes them afterwards,
# so the pin never ships in the image.
set -Eeuo pipefail

sed -n -E 's/^[[:space:]]*([a-z0-9][a-z0-9+.-]+)=([0-9A-Za-z.+~:-]+)[[:space:]]*(#.*)?$/\1 \2/p' "$@" \
  | LC_ALL=C sort -u \
  | while read -r package version; do
      printf 'Package: %s\nPin: version %s\nPin-Priority: 1001\n\n' "$package" "$version"
    done
