#!/bin/bash
# Sets up apt-cacher-ng on a Debian build host so `sudo make build` fetches Debian
# packages from local disk after the first build (docs/BUILDING.md). Safe to run again.
set -Eeuo pipefail

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'Run with sudo: sudo tools/setup-apt-cacher-ng.sh' >&2; exit 1; }

echo 'apt-cacher-ng apt-cacher-ng/tunnelenable boolean false' | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends apt-cacher-ng zstd curl

cat > /etc/apt-cacher-ng/zz-couchliteos.conf <<'EOF'
# CouchLiteOS build host (tools/setup-apt-cacher-ng.sh).
# Only builds on this machine use the cache.
BindAddress: 127.0.0.1
Port: 3142
# The HTTPS sources (Tailscale, Google Chrome) cannot be cached. apt normally fetches
# them directly; if it goes through the proxy, let it tunnel to these two hosts only.
PassThroughPattern: ^(pkgs\.tailscale\.com|dl\.google\.com):443$
EOF
systemctl enable apt-cacher-ng >/dev/null
systemctl restart apt-cacher-ng

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 15 --proxy http://127.0.0.1:3142 -o /dev/null \
      http://deb.debian.org/debian/dists/trixie/InRelease; then
    echo 'apt-cacher-ng answers on http://127.0.0.1:3142; sudo make build uses it automatically.'
    exit 0
  fi
  sleep 1
done
echo 'apt-cacher-ng does not answer on http://127.0.0.1:3142; see journalctl -u apt-cacher-ng' >&2
exit 1
