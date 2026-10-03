# Building CouchLiteOS

This page is for people who want to build the ISO themselves or work on CouchLiteOS.
To install it, download an ISO from the
[latest release](https://github.com/Dudiebug/couchliteos/releases/latest) and follow the
[wiki](https://github.com/Dudiebug/couchliteos/wiki).

## Build host

Build on Debian 13 x86_64 (a VM works, and so does the iMac Late 2013 itself):

```bash
sudo apt update
sudo apt install --yes make git live-build curl ca-certificates xorriso   squashfs-tools zstd apt-utils grub-pc-bin grub-efi-amd64-bin mtools dosfstools
sudo make build RELEASE=1                  # general ISO
sudo make build PROFILE=nvidia RELEASE=1   # NVIDIA ISO
```

The ISOs are written to `build/out/couchliteos-<version>-amd64.iso` and
`build/out/couchliteos-<version>-nvidia-amd64.iso`.

`curl` downloads the pinned application payloads, `squashfs-tools` (`unsquashfs`)
extracts them, and `git` stamps the source commit into the image (the build falls back
to `unknown` without it). The other make targets need more:

- `make test`: `python3` and `ripgrep` (`rg`).
- QEMU tests (`make qemu-smoke`, `qemu-persistence-smoke`, `qemu-install-smoke`):
  `qemu-system-x86`, `ovmf` and `python3`; the install test also needs `qemu-utils`
  (`qemu-img`), and the persistence test needs `xorriso` (or `bsdtar`) and `mke2fs`
  from `e2fsprogs`.
- `make release-check`: `python3` and `git`.

`scripts/fetch-apps.sh` downloads only the fixed versions and HTTPS URLs in
`build/applications.lock`, and `build/configure.sh` extracts their pinned payloads into
the read-only image, so runtime FUSE is not needed. Firefox comes from Debian's signed
repositories and Google Chrome Stable from Google's signed repository. No application
binary is committed to Git.

The single-machine profiles `intel` and `imac2013` still build
(`couchliteos-<version>-intel-amd64.iso`, `-imac2013-amd64.iso`) but are not release
assets.

## Test builds and release builds

```bash
sudo make build                      # test ISO
sudo make build RELEASE=1            # release ISO
sudo make build PROFILE=nvidia RELEASE=1
sudo make build RELEASE=1 FRESH=1    # release ISO, every package installed afresh
```

`make build` runs these steps:

1. Runs `make test`, unless `make test` already passed for this exact source tree
   (see [Tests run once](#tests-run-once)).
2. Runs `make configure`.
3. Builds with `build/build.sh` (`--release` for `RELEASE=1`, `--fresh` for `FRESH=1`).

| | Test build | Release build (`RELEASE=1`) |
|---|---|---|
| squashfs compression | zstd level 9: about 9 times faster to make, about 13% larger | xz: smallest |
| Reuses an installed chroot (`COUCHLITEOS_LB_CACHE`) | yes, unless `FRESH=1` | yes, unless `FRESH=1` |

Publish only release builds: `make release-assets` refuses an ISO whose squashfs is not
xz. Test builds are for checking a change. The image kernel
reads zstd squashfs (`CONFIG_SQUASHFS_ZSTD=y` in Debian 13's 6.12 kernel), so a test ISO
boots and installs the same way as a release ISO.

## Stage times

Each build prints the time of every stage and writes them to `build/out/build-times.txt`:

```text
Build stage times:
  tests                         0s  0m00s
  configure                    38s  0m38s
  bootstrap                    21s  0m21s
  chroot                      152s  2m32s
  installer                   178s  2m58s
  binary                      402s  6m42s
    squashfs                  104s  1m44s
    checksums                  76s  1m16s
    iso                        92s  1m32s
  source                        0s  0m00s
  checks                       25s  0m25s
  total                       816s 13m36s
```

The indented lines are part of `binary` and are not added to the total. The log also has
one `couchliteos-time: NAME SECONDS` line per stage.

## apt-cacher-ng

Set it up once on the build host:

```bash
sudo tools/setup-apt-cacher-ng.sh
```

The script installs apt-cacher-ng and binds it to 127.0.0.1:3142, so only builds on that
machine use it. From then on, `make build` finds the proxy on its own and prints
`Package downloads: through http://127.0.0.1:3142`. Debian packages, package indices and
the installer files are downloaded once and then read from the local disk.

How the build uses the proxy:

- **Integrity.** apt checks every file the proxy serves against the signed package
  indices, so a damaged cached file fails the download instead of reaching dpkg.
- **No second package cache.** With the proxy, live-build keeps no copy of the `.deb`
  files of its own (`--cache-packages false`).
- **HTTPS sources go direct.** The Tailscale and Google Chrome repositories are fetched
  directly and are not cached.
- **Nothing reaches the image.** The proxy is passed to debootstrap, apt and the
  installer download only through the `http_proxy` environment variable. It is never
  written to a file in the image. After every build, `build/build.sh` checks the image's
  `/etc/apt` and `/etc/environment` and fails the build if any of them names a proxy.

`COUCHLITEOS_APT_PROXY` controls the proxy:

| Value | Effect |
|---|---|
| `auto` (default) | Uses `http://127.0.0.1:3142` when it answers; otherwise downloads directly. |
| `off` | Always downloads directly. |
| `http://host:port` | Uses that proxy. The build fails if it does not answer. |

If the chroot stage fails with many `503  Connection closed, check DlMaxRetries
[IP: 127.0.0.1 3142]` lines, apt-cacher-ng lost its connection to the Debian mirror. This
happened once during the 0.2.4 release build (682 packages failed). The mirror is
probably reachable again. Check with `curl -I --proxy http://127.0.0.1:3142
http://deb.debian.org/debian/dists/trixie/InRelease`, then run the build again. If it
fails the same way twice, build with `COUCHLITEOS_APT_PROXY=off`.

Without a proxy, live-build caches the downloaded packages itself. Before each build,
every cached `.deb` is unpacked (`dpkg-deb --fsys-tarfile`, in parallel), and any that do
not unpack are deleted and reported. A truncated download then cannot stop the build at
dpkg time, as it did twice while building 0.2.3.

## Shared cache: COUCHLITEOS_LB_CACHE

`make configure` empties `build/work`, so live-build's own cache does not survive from
one build to the next. To keep a cache across builds, give it a directory outside the
work tree:

```bash
sudo COUCHLITEOS_LB_CACHE=/var/cache/couchliteos-lb make build
```

The directory holds three things:

- **`bootstrap-<key>`: the debootstrapped root.** The key covers the lb configuration,
  the package indices the Debian mirror serves at that moment, and the debootstrap and
  live-build versions.
- **`chroot-<key>.tar.zst`: the chroot with every package installed,** taken before the
  includes are copied and the hooks run. A build starts from it when the key matches,
  release builds included. The key covers:
  - everything in the bootstrap key;
  - every lb config file;
  - the package lists;
  - the apt sources, keys and pins;
  - the preseeds;
  - local packages;
  - the files copied before packages;
  - the hooks that install or remove packages, and the sources they build: the Cage hook
    installs its build dependencies and purges them again, so a change to it or to
    `config/cage/` is a new key.

  A snapshot older than 7 days is deleted and rebuilt
  (`COUCHLITEOS_CHROOT_CACHE_DAYS` changes the limit). The newest two snapshots are kept,
  one each for general and nvidia.
- **`packages.*`: the live-build package caches,** used only without apt-cacher-ng.

What still runs when the chroot is reused:

- **Every live-build chroot step**, in live-build's order: the apt sources are set up
  again, apt upgrades anything newer in the HTTPS repositories, and every package list is
  installed (with an unchanged key, nothing is left to install).
- **The includes and every hook.** A change to the launcher, a script, an overlay file or
  a hook therefore lands in the next test build without a full rebuild.
- **The chroot steps are replicated only for one live-build version:** the 1:20250505+deb13u1
  `chroot` script, checked by its md5. With any other live-build version, the build runs
  plain `lb chroot` and installs everything.

A changed package list, apt source, pin or Debian archive is a new key, so it installs
packages afresh, and so does a snapshot older than 7 days. `FRESH=1` (`build/build.sh
--fresh`) always installs every package, and saves a new snapshot for the builds after
it. Do not run two builds against one cache directory at the same time.

## Repeat release builds

A repeat build is fast only if it finds what the last build left behind. Build every
release from one kept build directory:

- **Keep the checkout.** Update the source in place (`git pull`, or a sync that does not
  delete files). Do not delete `build/` between builds: `build/.tests-passed` lets
  `make build` skip the tests it already ran (see [Tests run once](#tests-run-once)), and
  `build/downloads` holds the application images and the Cage tarball.
- **Keep the cache outside the checkout** and pass it to every build, test or release:

  ```bash
  sudo COUCHLITEOS_LB_CACHE=/var/cache/couchliteos-lb make build RELEASE=1
  sudo COUCHLITEOS_LB_CACHE=/var/cache/couchliteos-lb make build PROFILE=nvidia RELEASE=1
  ```

  Without `COUCHLITEOS_LB_CACHE` every build bootstraps and installs everything.
- **Use `FRESH=1`** when a package came from somewhere the key does not see, or to check
  that a release still builds from nothing.

The build log says which way the chroot stage went: `lb-cache: restored the installed
chroot ...` for a reuse, `lb-cache: fresh build, installing packages afresh` for
`FRESH=1`.

## Tests run once

The last step of `make test` writes `build/.tests-passed`, a hash of the host name, the
Python version, and every source file's path, mode and content. `.git`, build output,
downloads and Python caches are left out. `make build` skips the unit and static tests only
when the hash of the tree matches. Any edit, added or removed file, or a different machine
runs them again.

## Package versions

A `package=version` line in a package list only applies to the install pass. live-build
also runs `apt-get upgrade` and `dist-upgrade` each time it sets up the archives again,
including once more in the binary stage after the squashfs is made. That is why the 0.2.2
and 0.2.3 build logs show `tailscale 1.102.4`, while the images shipped the listed 1.102.3.

`build/configure.sh` turns every `package=version` line into an apt pin with priority 1001
(`build/apt-pins.sh`). The pin applies only while the chroot is built: it is installed as a
`.pref.chroot` file, which live-build removes afterwards, and the image check fails the
build if `/etc/apt/preferences.d/couchliteos-pins` is in the image. The installed system
follows Tailscale's repository as before.

## Testing

```bash
make test                          # unit and static tests
make qemu-smoke                    # boots the ISO in QEMU (UEFI) and waits for the launcher
make qemu-install-smoke            # installs to a virtual disk and boots it twice
make qemu-persistence-smoke        # checks live persistence across reboots
```

Every QEMU target takes `PROFILE=` (for example `PROFILE=nvidia`) or an explicit `ISO=`.
Without KVM, set `COUCHLITEOS_QEMU_TIMEOUT_SCALE=N` (1-99) to stretch every wait by the
same factor. `make release-gauntlet` runs all three QEMU tests on a release ISO.

To try an ISO in a full VM:

```powershell
# Hyper-V (Generation 2), as Administrator on the Windows host
.\tools\hyperv-create-test-vm.ps1 -IsoPath D:\iso\couchliteos-<version>-amd64.iso -Start
```

```bash
# Proxmox VE, after uploading the ISO to an ISO storage
./tools/proxmox-create-test-vm.sh --iso local:iso/couchliteos-<version>-amd64.iso --start
```

The ISO ships Debian's Microsoft-signed shim, so the Hyper-V VM keeps Secure Boot on;
pass `-SecureBoot Off` to test without it. A VM cannot emulate an NVIDIA GPU, the iMac's
Broadcom Wi-Fi or its audio, so test those on the hardware.
