# Installation

## Create the USB installer

Write the hybrid ISO to a whole USB device. **The selected device is erased.**
Resolve the exact target with `lsblk` before running this example:

```bash
sudo dd if=moonlightos-0.1.12-amd64.iso of=/dev/sdX bs=4M \
  status=progress conv=fsync
```

For the iMac Late 2013, write `moonlightos-0.1.12-imac2013-amd64.iso` instead
and boot it by holding Option (⌥) and choosing **EFI Boot**; see
[IMAC-2013.md](IMAC-2013.md).

Rufus users should select the same ISO and write it in DD/Image mode so the
hybrid disk layout is preserved. Rufus and physical USB boot remain unverified;
the automated test covers the SHA-identical ISO in UEFI QEMU.

The boot menu provides:

- `Start MoonlightOS` — uses a valid persistence backend when present.
- `Start MoonlightOS (No Persistence)` — passes Debian Live's
  `nopersistence` recovery option.
- `Install MoonlightOS` — starts the text Debian Installer.

## Install to the OptiPlex SSD

1. Back up the SSD. Disconnect other writable drives where practical.
2. Press `F12` at power-on and select the entry beginning with `UEFI` for the
   USB device. The live appliance starts automatically after three seconds.
3. Choose `Install MoonlightOS` from the boot menu. The image includes Debian
   Installer in live mode; it copies the configured appliance system to the SSD.
4. Select the 256 GB NVMe only. Guided partitioning with an EFI System
   Partition and ext4 root is the v0.1.12 reference layout.
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

The installed root filesystem is writable in v0.1.12. Pairings, settings, and
logs live beneath `/var/lib/moonlightos` and `/var/log/moonlightos`.

The CI install smoke test performs a complete UEFI installation to a disposable
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
```

Do not pre-create those four source directories empty. On the first persistent
boot, `live-boot` creates each directory and bootstraps it from the matching
image directory with matching ownership and permissions. After boot, verify:

```bash
findmnt /var/lib/moonlightos /var/log/moonlightos \
  /var/lib/tailscale /var/lib/bluetooth
stat -c '%U:%G %a %n' /var/lib/moonlightos /var/log/moonlightos \
  /var/lib/tailscale /var/lib/bluetooth
```

The ISO's default entry already passes `persistence`; the explicit
`No Persistence` entry ignores the backend. Do not use an
unencrypted persistent USB for sensitive pairing data outside a trusted lab.

## Optional Ventoy layout

Ventoy persistence is not yet a supported or physically verified path. For
testing, keep the ISO and an ext4 backend labeled `persistence` on Ventoy's
first partition:

```text
ISO/moonlightos-0.1.12-amd64.iso
persistence/moonlightos.dat
ventoy/ventoy.json
```

`ventoy/ventoy.json` can associate them with Ventoy's persistence plugin:

```json
{
  "persistence": [
    {
      "image": "/ISO/moonlightos-0.1.12-amd64.iso",
      "backend": "/persistence/moonlightos.dat",
      "timeout": 0
    }
  ]
}
```

The backend must contain the same `persistence.conf` shown above. Record the
Ventoy version and test normal UEFI mode, launcher readiness, installer entry,
and persistence across reboot before treating this path as supported.
