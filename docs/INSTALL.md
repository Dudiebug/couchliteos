# Installation

## Choose the ISO

- `moonlightos-0.1.13-nvidia-amd64.iso` for a GeForce GTX 900 to RTX 40 card.
- `moonlightos-0.1.13-amd64.iso` (general) for everything else: Intel and AMD
  graphics, older NVIDIA cards, Intel Macs including the iMac Late 2013, and
  virtual machines.

[HARDWARE.md](HARDWARE.md) has the full table. Check the download against
`SHA256SUMS` from the same release:

```bash
sha256sum -c --ignore-missing SHA256SUMS
```

## Create the USB installer

Write the hybrid ISO to a whole USB device. **The selected device is erased.**
Resolve the exact target with `lsblk` before running this example:

```bash
sudo dd if=moonlightos-0.1.13-amd64.iso of=/dev/sdX bs=4M \
  status=progress conv=fsync
```

On an Intel Mac, boot it by holding Option (⌥) and choosing **EFI Boot**; see
[IMAC-2013.md](IMAC-2013.md). Both ISOs use DKMS drivers (NVIDIA, Broadcom
`wl`) that do not load with Secure Boot on; turn it off in the firmware
settings on PCs that have it.

Rufus users should select the same ISO and write it in DD/Image mode so the
hybrid disk layout is preserved. Rufus and physical USB boot remain unverified;
the automated test covers the SHA-identical ISO in UEFI QEMU.

The boot menu provides:

- `Start MoonlightOS` — uses a valid persistence backend when present.
- `Start MoonlightOS (No Persistence)` — passes Debian Live's
  `nopersistence` recovery option.
- `Start MoonlightOS (Basic Graphics)` (NVIDIA ISO only) — blocks the
  proprietary driver and uses nouveau. Use it if the screen stays black or the
  launcher misdraws on the NVIDIA driver.
- `Install MoonlightOS` — starts the text Debian Installer.

An installed system has no Basic Graphics entry of its own. To get the same
effect once, press `e` on the GRUB entry, append `moonlightos.gpu=basic` to the
line starting with `linux`, and press `Ctrl+X`.

## Install to the OptiPlex SSD

1. Back up the SSD. Disconnect other writable drives where practical.
2. Press `F12` at power-on and select the entry beginning with `UEFI` for the
   USB device. The live appliance starts automatically after three seconds.
3. Choose `Install MoonlightOS` from the boot menu. The image includes Debian
   Installer in live mode; it copies the configured appliance system to the SSD.
4. Select the 256 GB NVMe only. Guided partitioning with an EFI System
   Partition and ext4 root is the reference layout.
5. Reboot, remove the USB, and confirm the MoonlightOS launcher appears.
6. Run `moonlightos-diagnostics`, pair applications, reboot, and confirm the
   host lists remain.

The first-boot wizard can open the existing `nmtui` network setup; wired DHCP
remains automatic and IPv6 is disabled.
The launcher is deliberately not held behind network-online, so it must appear
even with Ethernet unplugged. Streaming applications wait briefly for IPv4 but
remain launchable after a DHCP timeout.

If the USB is absent from the `F12` menu, rewrite the ISO directly to the whole
USB device (not a partition), and try another USB port.
Do not use a file-copy operation. If the boot menu appears but the launcher does
not, photograph the last screen and include it in an issue.

The installed root filesystem is writable. Pairings, settings, and
logs live beneath `/var/lib/moonlightos` and `/var/log/moonlightos`.

The install smoke test (`make qemu-install-smoke`) performs a complete UEFI installation to a disposable
24 GB virtual disk, removes the ISO, boots that disk independently, and waits
for the real launcher-ready marker. Physical NVMe and second-USB installation
still require the checklist in `TESTING.md`.

## Optional live-USB persistence

Installation is preferred. For testing, create an ext4 partition labeled
`persistence` in the USB's remaining space, mount it, and create a file named
`persistence.conf` at the filesystem root with these contents:

```text
/var/lib/moonlightos source=moonlightos-state
/var/log/moonlightos source=moonlightos-logs
/var/lib/tailscale source=tailscale-state
/var/lib/bluetooth source=bluetooth-state
/etc/NetworkManager/system-connections source=nm-connections
```

Do not pre-create those five source directories empty. On the first persistent
boot, `live-boot` creates each directory and bootstraps it from the matching
image directory with matching ownership and permissions. After boot, verify:

```bash
findmnt /var/lib/moonlightos /var/log/moonlightos \
  /var/lib/tailscale /var/lib/bluetooth /etc/NetworkManager/system-connections
stat -c '%U:%G %a %n' /var/lib/moonlightos /var/log/moonlightos \
  /var/lib/tailscale /var/lib/bluetooth /etc/NetworkManager/system-connections
```

The NetworkManager line makes saved Wi-Fi networks survive a reboot. Its
directory must show `root:root 700` so the unprivileged launcher user cannot
read the saved passwords. A persistence partition made before this line
existed keeps working, but forgets Wi-Fi at every reboot until you add the
line to its `persistence.conf` (mount the partition, append the line, and let
the next boot create the source directory). Installed systems need no change:
their `/etc` already persists.

The ISO's default entry already passes `persistence`; the explicit
`No Persistence` entry ignores the backend. Do not use an
unencrypted persistent USB for sensitive pairing data outside a trusted lab.

## Optional Ventoy layout

Ventoy persistence is not yet a supported or physically verified path. For
testing, keep the ISO and an ext4 backend labeled `persistence` on Ventoy's
first partition:

```text
ISO/moonlightos-0.1.13-amd64.iso
persistence/moonlightos.dat
ventoy/ventoy.json
```

`ventoy/ventoy.json` can associate them with Ventoy's persistence plugin:

```json
{
  "persistence": [
    {
      "image": "/ISO/moonlightos-0.1.13-amd64.iso",
      "backend": "/persistence/moonlightos.dat",
      "timeout": 0
    }
  ]
}
```

The backend must contain the same `persistence.conf` shown above. Record the
Ventoy version and test normal UEFI mode, launcher readiness, installer entry,
and persistence across reboot before treating this path as supported.
