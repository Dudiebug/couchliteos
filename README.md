# MoonlightOS v0.1.13

MoonlightOS is a Debian 13 (Trixie) x86_64 gaming-streaming appliance. It boots
directly into a small controller-friendly launcher for Moonlight, chiaki-ng,
Firefox ESR, official Google Chrome, and Remote Desktop, with an allowlist-only
Linux USB/IP server. It is not a general-purpose desktop.

## Which ISO?

| Your graphics | Download |
|---|---|
| Intel or AMD graphics, or no NVIDIA card | `moonlightos-<version>-amd64.iso` (general) |
| NVIDIA Maxwell or newer: GeForce GTX 745, 750 and 750 Ti, GTX 800M/900M laptops, GTX 900, GTX 10, GTX 16, RTX 20, 30 and 40 series | `moonlightos-<version>-nvidia-amd64.iso` |
| NVIDIA Kepler or older: GTX 600 series, GTX 760 to 780, and the mobile GT 750M/755M of the iMac Late 2013 | general; the NVIDIA ISO also works and falls back to the same open driver |
| NVIDIA RTX 50 series | not supported yet (see [known limitations](docs/KNOWN_LIMITATIONS.md)) |

Not sure about an NVIDIA card? Use the NVIDIA ISO: it loads NVIDIA's driver only
for GPUs the driver supports and uses the open nouveau driver for any other.
Names mislead: the desktop GTX 750 is Maxwell, the mobile GT 750M is Kepler.

Secure Boot: NVIDIA's driver and Broadcom's `wl` Wi-Fi driver are not signed, so
they do not load with Secure Boot on.
<!-- LEAD-CHECK secure-boot-fallback: assumes feat/bugfix4 (automatic nouveau fallback with Secure Boot on) is merged; see docs/INSTALL.md. -->
If Secure Boot is on, MoonlightOS falls back to the open driver (lower
performance); turn Secure Boot off in firmware setup to use the NVIDIA driver.
A Broadcom chip that needs `wl` has no Wi-Fi with Secure Boot on.

Both ISOs detect the machine on every boot: the same USB stick moves between
PCs. Details and the per-machine decisions are in [HARDWARE.md](docs/HARDWARE.md).

> Testing status: source/static and QEMU application tests are automated. No
> physical machine has run v0.1.13 yet; the hardware checklists in
> [TESTING.md](docs/TESTING.md) must pass before calling either ISO a
> production image.

## What v0.1.13 contains

- Debian standard kernel, systemd, NetworkManager, nftables, PipeWire, ALSA
- IPv4-only networking; IPv6 is disabled
- Two release ISOs built from profiles (`make build PROFILE=general|nvidia`):
  - general: Intel, AMD, and NVIDIA graphics through the open drivers (i915/xe,
    amdgpu/radeon, nouveau) and Mesa, curated firmware for common PC and Intel
    Mac hardware, VA-API for Intel and AMD, and Broadcom `wl` Wi-Fi built with
    DKMS for the image kernel
  - nvidia: everything in general plus NVIDIA's proprietary 550 driver (DKMS,
    GBM for Cage, NVDEC, VA-API through `nvidia-vaapi-driver`) and a
    **Start MoonlightOS (Basic Graphics)** boot entry that uses nouveau
- Boot-time hardware detection (`moonlightos-hwdetect`): per-machine kernel
  module policy under `/run` only, the proprietary NVIDIA driver only for GPUs
  on its supported list, nouveau for older cards, Broadcom `wl` only where it
  owns the Wi-Fi chip, `applesmc` on Apple hardware, and software H.264 for
  Moonlight where the GPU has no usable decoder (nouveau)
- Intel Mac support in both ISOs: tg3 Ethernet firmware, `hid_apple` function
  keys, `applesmc` sensors (the iMac Late 2013 profile's work, now general)
- Remote Desktop with FreeRDP 3's SDL client on Wayland: saved connections in
  Settings, password prompts through the on-screen keyboard, optional
  root-only saved passwords, certificate pinning on first use, and pinnable
  launcher buttons with controller shortcuts
- Named-device Bluetooth discovery and PipeWire output selection
- Home/Guide managed application resume and close controls
- Standard Firefox EME/Widevine readiness and safe diagnostics
- Mesa Vulkan and VA-API
- Cage as the direct DRM/KMS Wayland kiosk compositor; no desktop environment
- Moonlight Qt 6.1.0 and chiaki-ng 1.10.0 pinned to fixed release URLs
- Firefox ESR from Debian 13, running natively on Wayland with a persistent profile
- Official Google Chrome Stable from Google's signed Debian repository, with a persistent profile
- black-and-white full-screen terminal launcher with keyboard and common gamepad navigation
- matching high-contrast UEFI/BIOS boot menus and dark text installer
- controller-friendly Bluetooth management inside Settings, backed by BlueZ
- continuous Bluetooth discovery while its settings screen is open
- a first-boot Setup Wizard that can be rerun from Settings
- manifest-driven system, custom command, and custom web applications
- isolated full-screen Terminal, Tailscale, network, and diagnostics processes
- a full-screen buffered keyboard for launcher text fields (X on Xbox pads, Triangle on PlayStation pads, or F12)
- broader controller device rules from Debian's `steam-devices` package; Steam itself is not installed
- Bluetooth controller input through evdev and opt-in Bluetooth audio through PipeWire/WirePlumber
- controller-friendly display settings based only on modes advertised by Cage/wlroots
- 15-second display-mode preview with confirmation, rollback, and display-identity-safe persistence
- redacted support archives exported to validated writable removable media by a restricted system service
- systemd crash recovery for the launcher, streaming applications, Firefox, and Remote Desktop sessions
- explicit USB/IP allowlist, hotplug reconciliation, and fail-closed TCP/3240
- optional, unauthenticated-by-default Tailscale overlay and native Tailscale SSH
- settings and pairing data under `/var/lib/moonlightos` (kept on an installed
  system or a persistence stick, forgotten by a plain live USB; see
  [Live USB or installed?](#live-usb-or-installed))
- logs and diagnostic snapshots under `/var/log/moonlightos`
- Debian Installer integration for installation to an internal disk

## Exact build command

On Debian 13 x86_64:

```bash
sudo apt update
sudo apt install --yes make git live-build curl ca-certificates xorriso \
  squashfs-tools grub-pc-bin grub-efi-amd64-bin mtools dosfstools
sudo make build                    # general ISO
make configure PROFILE=nvidia && sudo make build PROFILE=nvidia   # NVIDIA ISO
```

`make` runs the build, `curl` downloads the pinned application payloads,
`squashfs-tools` (`unsquashfs`) extracts them, and `git` stamps the source
commit into the image (the build falls back to `unknown` without it) and is
needed by the release tools. The other make targets need more:

- `make test`: `python3` and `ripgrep` (`rg`).
- QEMU tests (`make qemu-smoke`, `qemu-persistence-smoke`, `qemu-install-smoke`):
  `qemu-system-x86`, `ovmf`, and `python3`; the install test also needs
  `qemu-utils` (`qemu-img`), and the persistence test needs `xorriso` (or
  `bsdtar`) and `mke2fs` from `e2fsprogs`.
- `tools/release-draft.sh`: the GitHub CLI (`gh`).

Output:

```text
build/out/moonlightos-0.1.13-amd64.iso
build/out/moonlightos-0.1.13-nvidia-amd64.iso
```

The legacy single-machine profiles `intel` (Dell OptiPlex DCC36X3) and
`imac2013` still build (`moonlightos-<version>-intel-amd64.iso`,
`-imac2013-amd64.iso`) but are no longer release assets.

Builds, tests, and releases run locally; nothing depends on hosted CI. Build
natively on Debian 13 x86_64 (the iMac itself works). `make release-assets`
writes `build/out/SHA256SUMS` and checks GitHub's 2 GiB asset limit, and
`tools/release-draft.sh` creates a draft release without publishing it.

`scripts/fetch-apps.sh` downloads only the fixed versions and HTTPS URLs in
`build/applications.lock`. `build/configure.sh`
extracts their pinned payloads into the read-only image so runtime FUSE is not
required. Firefox is installed from Debian's signed repositories and Google
Chrome Stable from Google's signed repository during the image build. No
application binary is committed to Git.

## First boot

1. Connect DisplayPort/HDMI, wired Ethernet, and a **keyboard**. The boot menu
   and the installer cannot be used with a controller; once the launcher is
   running, a controller is enough.
2. Open the firmware boot menu and select the USB device: `F12` on the Dell
   (the key differs by maker), or hold Option (⌥) on the iMac and choose **EFI
   Boot**. The MoonlightOS menu starts the live system by itself after three
   seconds; press an arrow key to stop the countdown and choose another entry
   such as **Install MoonlightOS**.
3. The launcher becomes ready even without network, then the first-boot Setup
   Wizard opens. Complete, skip, or exit it before choosing an application.
4. Pair Sunshine in Moonlight. Bluetooth devices are managed in Settings.
   Tailscale setup, when wanted, uses an on-screen QR code.

The USB stick is written with `dd`, Rufus (DD Image mode) or balenaEtcher, and
the download is checked against `SHA256SUMS`; the steps for Linux, Windows and
macOS are in [INSTALL.md](docs/INSTALL.md).

### Live USB or installed?

A live USB starts from the image on every boot, so on its own it forgets your
Moonlight pairing, Wi-Fi, Bluetooth pairings and Setup Wizard at power-off, and
you pair again each time. To keep them, install MoonlightOS to a disk
(recommended for daily use) or, for testing, add a persistence stick; both are
in [INSTALL.md](docs/INSTALL.md), which also explains how to update to a new
release without losing them.

The installed system preserves application configuration normally. A live USB
with persistence needs a separate stick holding `persistence.conf` with
distinct backing directories, including `/var/lib/bluetooth` for Bluetooth
pairings. UEFI installation and independent virtual-disk boot are automated;
Rufus, Ventoy, physical NVMe, and second-USB installation remain physical
validation items. Local LAN streaming and the launcher do not depend on
Tailscale or Bluetooth.

## Target hardware

Any x86-64 PC or Intel Mac from roughly 2012 onward with UEFI or legacy BIOS
boot, 4 GB of RAM (the automated live-boot test uses 3 GiB), and wired
Ethernet or supported Wi-Fi. Graphics support depends on the ISO (see
[Which ISO?](#which-iso) above).

Targets are 1080p60, 1080p120 where supported, 1440p60, and best-effort 4K60
SDR. 4K HDR is deliberately unclaimed until the exact TV, adapter, cable, and
display path are tested.

Two machines define the reference checklist in [TESTING.md](docs/TESTING.md):

- Dell OptiPlex 7010 Micro (Core i5-13500T, UHD 770): general ISO, VA-API
  hardware decode. A matched 2x8 GB dual-channel kit is preferred over one
  16 GB DIMM because the integrated GPU shares memory bandwidth.
- Apple iMac Late 2013 (NVIDIA Kepler): general ISO, or the NVIDIA ISO's
  nouveau fallback. It decodes Moonlight's H.264 in software and targets
  1080p60. See [iMac Late 2013](docs/IMAC-2013.md).

## Configuration map

| Purpose | Persistent path |
|---|---|
| Launcher/default profile | `/var/lib/moonlightos/config.ini` |
| Network, host profiles, and Tailscale | `/var/lib/moonlightos/config.ini` |
| Moonlight host list/pairing | `/var/lib/moonlightos/home/.config/` |
| chiaki-ng registration | `/var/lib/moonlightos/home/.config/` |
| Firefox profile, bookmarks, and settings | `/var/lib/moonlightos/home/.mozilla/` |
| Google Chrome profile, bookmarks, and settings | `/var/lib/moonlightos/home/.config/google-chrome/` |
| Launcher controller identity | `/var/lib/moonlightos/launcher-controller.id` |
| Bluetooth power preference | `/var/lib/moonlightos/bluetooth-enabled` |
| Application manifests and state | `/var/lib/moonlightos/apps.d/`, `/var/lib/moonlightos/apps-state.ini` |
| Remote Desktop connections and pinned certificates | `/var/lib/moonlightos/rdp/connections.ini` |
| Saved Remote Desktop passwords (root-only, unencrypted) | `/var/lib/moonlightos/rdp-secrets/` |
| Setup completion | `/var/lib/moonlightos/setup-complete` |
| BlueZ pairing state (contains secrets) | `/var/lib/bluetooth/` |
| USB/IP allowlist policy | `/etc/moonlightos/usbip-allowlist.conf` |
| Logs and diagnostics | `/var/log/moonlightos/` |

The Settings screen can generate a support archive on a mounted writable
removable filesystem, an explicitly labeled `MOONLIGHTOS_SUPPORT` partition,
or a writable live-USB persistence partition. It never writes to an internal
SATA/NVMe filesystem. See [Support export](docs/SUPPORT.md).

More documentation:

- [Installation](docs/INSTALL.md)
- [Hardware and performance](docs/HARDWARE.md)
- [iMac Late 2013](docs/IMAC-2013.md)
- [Remote Desktop](docs/REMOTE_DESKTOP.md)
- [Sunshine and Moonlight](docs/SUNSHINE.md)
- [chiaki-ng registration](docs/CHIAKI.md)
- [USB/IP server and Linux host client](docs/USBIP.md)
- [Optional Tailscale overlay](docs/networking/tailscale.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Support export](docs/SUPPORT.md)
- [Testing and QEMU](docs/TESTING.md)
- [Known limitations](docs/KNOWN_LIMITATIONS.md)
- [Roadmap](ROADMAP.md)

## License

Original MoonlightOS code is GPL-3.0-or-later. Bundled programs retain their
own licenses. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
