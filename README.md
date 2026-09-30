# MoonlightOS v0.1.12

MoonlightOS is a Debian 13 (Trixie) x86_64 gaming-streaming appliance. It boots
directly into a small controller-friendly launcher for Moonlight, chiaki-ng,
Firefox ESR, official Google Chrome, and Remote Desktop, with an allowlist-only
Linux USB/IP server. It is not a general-purpose desktop.

Two build profiles share everything except hardware support:

| Profile | Target | ISO |
|---|---|---|
| `intel` (default) | Dell OptiPlex 7010 Micro DCC36X3, Intel UHD 770 | `moonlightos-<version>-amd64.iso` |
| `imac2013` | Apple iMac Late 2013, NVIDIA Kepler via nouveau ([details](docs/IMAC-2013.md)) | `moonlightos-<version>-imac2013-amd64.iso` |

> Testing status: source/static and QEMU application tests are automated. The
> physical Dell OptiPlex DCC36X3 matrix and the iMac Late 2013 hardware
> checklist must be completed before calling either a production image. See
> [TESTING.md](docs/TESTING.md).

## What v0.1.12 contains

- Debian standard kernel, systemd, NetworkManager, nftables, PipeWire, ALSA
- IPv4-only networking; IPv6 is disabled in v0.1.12
- Build profiles (`make build PROFILE=intel|imac2013`) with profile-specific
  packages, build checks, and files
- iMac Late 2013 profile: nouveau and Mesa only (never the proprietary NVIDIA
  driver), Broadcom `wl` Wi-Fi built with DKMS for the image kernel, tg3
  Ethernet firmware, `hid_apple` function keys, `applesmc` sensors, and software
  H.264 Moonlight defaults
- Remote Desktop with FreeRDP 3's SDL client on Wayland: saved connections in
  Settings, password prompts through the on-screen keyboard, optional
  root-only saved passwords, certificate pinning on first use, and pinnable
  launcher buttons with controller shortcuts
- Named-device Bluetooth discovery and PipeWire output selection
- Home/Guide managed application resume and close controls
- Standard Firefox EME/Widevine readiness and safe diagnostics
- Mesa Vulkan and VA-API; the Intel profile adds i915 firmware and the Intel media driver
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
- persistent settings and pairing data under `/var/lib/moonlightos`
- logs and diagnostic snapshots under `/var/log/moonlightos`
- Debian Installer integration for installation to an internal SSD

## Exact build command

On Debian 13 x86_64:

```bash
sudo apt update
sudo apt install --yes live-build curl ca-certificates xorriso squashfs-tools \
  grub-pc-bin grub-efi-amd64-bin mtools dosfstools ripgrep
sudo make build                    # Intel profile
sudo make build PROFILE=imac2013   # iMac Late 2013 profile
```

Output:

```text
build/out/moonlightos-0.1.12-amd64.iso
build/out/moonlightos-0.1.12-imac2013-amd64.iso
```

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

1. Connect DisplayPort/HDMI, wired Ethernet, and a controller or keyboard.
2. Press `F12` on the Dell and select the UEFI USB device, or hold Option (⌥)
   on the iMac and choose **EFI Boot**. Wait three seconds.
3. The launcher becomes ready even without network, then the first-boot Setup
   Wizard opens. Complete, skip, or exit it before choosing an application.
4. Pair Sunshine once in Moonlight. Bluetooth devices are managed in Settings.
   Tailscale setup, when wanted, uses an
   on-screen QR code.

That is the complete live-image setup. The same hybrid ISO offers persistent
live boot, an explicit `No Persistence` recovery entry, and `Install
MoonlightOS` for installation to another disk. For durable settings, select
the installer entry and follow [INSTALL.md](docs/INSTALL.md). Installation keeps
the disk-selection confirmation because silently erasing a disk is unsafe.

The installed system preserves application configuration normally. A live USB
needs a separate persistence partition containing `persistence.conf` with
distinct backing directories. A live USB must also persist
`/var/lib/bluetooth` for Bluetooth pairings to survive reboot. UEFI installation
and independent virtual-disk boot are automated; Rufus, Ventoy, physical NVMe,
and second-USB installation remain physical validation items. Local LAN
streaming and the launcher do not depend on Tailscale or Bluetooth.

## Target hardware

The first target is Dell OptiPlex 7010 Micro, service tag DCC36X3: Core
i5-13500T, UHD 770, 16 GB DDR4-3200, 256 GB NVMe, gigabit Ethernet, and wired
DisplayPort/HDMI. The image works with the current 1x16 GB DIMM. A matched 2x8
GB dual-channel kit is preferred because the integrated GPU shares system
memory bandwidth.

Targets are 1080p60, 1080p120 where supported, 1440p60, and best-effort 4K60
SDR. 4K HDR is deliberately unclaimed until the exact TV, adapter, cable, and
display path are tested.

The second target is an Apple iMac Late 2013 (iMac14,2 or iMac14,3, serial
DCPM5026FQPG) with an NVIDIA Kepler GPU. It decodes Moonlight's H.264 in
software and targets 1080p60. See [iMac Late 2013](docs/IMAC-2013.md).

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
- [iMac Late 2013 profile](docs/IMAC-2013.md)
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
