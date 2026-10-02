# Building CouchLiteOS

The [README](../README.md#exact-build-command) lists the packages a Debian 13 x86_64
build host needs. This page covers the two kinds of build, the stage timings and the
caches that make repeat builds faster.

## Test builds and release builds

```bash
sudo make build                      # test ISO
sudo make build RELEASE=1            # release ISO
sudo make build PROFILE=nvidia RELEASE=1
```

`make build` runs these steps:

1. Runs `make test`, unless `make test` already passed for this exact source tree
   (see [Tests run once](#tests-run-once)).
2. Runs `make configure`.
3. Builds with `build/build.sh` (`build/build.sh --release` for `RELEASE=1`).

| | Test build | Release build (`RELEASE=1`) |
|---|---|---|
| squashfs compression | zstd level 9: about 9 times faster to make, about 13% larger | xz: smallest |
| Reuses an installed chroot (`COUCHLITEOS_LB_CACHE`) | yes | never: packages are always installed afresh |

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
  includes are copied and the hooks run. A test build starts from it when the key
  matches. The key covers:
  - everything in the bootstrap key;
  - every lb config file;
  - the package lists;
  - the apt sources, keys and pins;
  - the preseeds;
  - local packages;
  - the files copied before packages.

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
packages afresh. Release builds never reuse the chroot, but they save one for later test
builds. Do not run two builds against one cache directory at the same time.

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
