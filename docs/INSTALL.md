# Installation

You need a USB stick, a **keyboard**, and a wired network connection. The boot
menu and the installer cannot be operated with a controller; a controller is
enough only once the launcher is running.

## Choose the ISO

- `couchliteos-0.1.13-nvidia-amd64.iso` for an NVIDIA card from Maxwell on:
  GeForce GTX 745, 750 and 750 Ti, the GTX 800M and 900M laptop GPUs, and every
  GTX 900, GTX 10, GTX 16, RTX 20, RTX 30 and RTX 40 card.
- `couchliteos-0.1.13-amd64.iso` (general) for everything else: Intel and AMD
  graphics, NVIDIA Kepler and older (GTX 600, GTX 760 to 780, and the GT 750M
  and GT 755M of the iMac Late 2013), Intel Macs, and virtual machines. Names
  mislead here: the desktop GTX 750 is Maxwell, but the mobile GT 750M is
  Kepler.
- Not sure about an NVIDIA card? Use the NVIDIA ISO. It loads NVIDIA's driver
  only for GPUs on the driver's supported list (Maxwell and newer) and uses
  nouveau for any other NVIDIA GPU. RTX 50 cards are not supported yet.

[HARDWARE.md](HARDWARE.md) has the full table.

## Check the download

`SHA256SUMS` from the same release lists the hash of each ISO. Compare it with
the hash of the file you downloaded.

Linux (run in the folder that holds the ISO; `--ignore-missing` skips the ISO
you did not download):

```bash
sha256sum -c --ignore-missing SHA256SUMS
```

Windows (PowerShell, or `certutil` in a Command Prompt):

```powershell
Get-FileHash .\couchliteos-0.1.13-amd64.iso -Algorithm SHA256
certutil -hashfile couchliteos-0.1.13-amd64.iso SHA256
```

macOS:

```bash
shasum -a 256 couchliteos-0.1.13-amd64.iso
```

On Windows and macOS compare the printed hash with the matching line of
`SHA256SUMS` yourself (upper or lower case does not matter).

## Write the USB stick

Write the hybrid ISO to the whole USB device, as a raw image. **The selected
device is erased**, so identify it first. Do not copy the ISO onto a stick as a
file.

Linux: find the device with `lsblk`, then

```bash
sudo dd if=couchliteos-0.1.13-amd64.iso of=/dev/sdX bs=4M \
  status=progress conv=fsync
```

macOS:

```bash
diskutil list                      # find the stick, for example /dev/disk4
diskutil unmountDisk /dev/disk4
sudo dd if=couchliteos-0.1.13-amd64.iso of=/dev/rdisk4 bs=4m
diskutil eject /dev/disk4
```

(macOS `dd` prints no progress; press `Ctrl+T` to see it.)

Windows: use Rufus and choose **DD Image mode** when it asks how to write the
ISO, or balenaEtcher, which writes the ISO as a raw image. Rufus, balenaEtcher,
macOS `dd` and physical USB boot remain unverified; the automated test covers
the SHA-identical ISO in UEFI QEMU.

If the stick does not show up in the firmware boot menu, rewrite it to the
whole device (not a partition) and try another USB port.

### Secure Boot

The image boots through Debian's signed shim, but NVIDIA's driver (NVIDIA ISO)
and Broadcom's `wl` Wi-Fi driver (both ISOs) are DKMS modules that are not
signed, so they do not load with Secure Boot on.

<!-- LEAD-CHECK secure-boot-fallback: the next paragraph assumes feat/bugfix4 (automatic nouveau fallback when Secure Boot is on) is merged. If it is not, say instead that the NVIDIA driver does not load and the screen can stay black: turn Secure Boot off or use Basic Graphics. -->
If Secure Boot is on, CouchLiteOS falls back to the open driver (lower
performance); turn Secure Boot off in firmware setup to use the NVIDIA driver.
A Broadcom Wi-Fi chip that needs `wl` has no Wi-Fi with Secure Boot on; use
wired Ethernet, a supported adapter, or turn Secure Boot off. On a UEFI PC the
boot menu's Utilities submenu has **UEFI Firmware Settings**, which opens the
firmware setup. Apple firmware from 2013 has no Secure Boot.

## Start it

1. Connect the display, wired Ethernet and a keyboard, and plug in the stick.
2. Power on and open the firmware boot menu. The key depends on the maker: F12
   on the reference Dell OptiPlex (choose the entry beginning with `UEFI`),
   often F10, F11, F9 or Esc elsewhere; on an Intel Mac hold Option (⌥) and
   choose **EFI Boot** (see [IMAC-2013.md](IMAC-2013.md)).
3. The CouchLiteOS menu starts **Start CouchLiteOS** by itself after three
   seconds. Press an arrow key as soon as the menu appears to stop the
   countdown, then pick an entry with the arrow keys and Enter. The menu ignores
   controllers, so this is the only way to reach the other entries.

The menu provides:

- `Start CouchLiteOS`: uses a valid persistence backend when present.
- `Start CouchLiteOS (No Persistence)`: passes Debian Live's `nopersistence`
  option, so nothing saved on a persistence stick is used. Use it if a saved
  setting (for example a display mode your screen cannot show) keeps the
  screen black.
- `Start CouchLiteOS (Basic Graphics)` (NVIDIA ISO only): blocks the
  proprietary driver and uses nouveau. Use it if the screen stays black or the
  launcher misdraws on the NVIDIA driver.
- `Install CouchLiteOS`: starts the text Debian Installer (next section).

<!-- LEAD-CHECK fail-safe-entry: cleanup of the stock boot-menu entries is deferred; update this paragraph if it lands. -->
The menu also carries Debian's stock `Live system (amd64 fail-safe mode)`,
`Advanced install options` and `Utilities` entries. They are not part of
CouchLiteOS's tested paths. In particular, the fail-safe entry uses different
boot options and is not the Basic Graphics entry; do not use it to fix a black
screen.

An installed system has no Basic Graphics entry of its own. To get the same
effect once, press `e` on the GRUB entry, append `couchliteos.gpu=basic` to the
line starting with `linux`, and press `Ctrl+X`.

## Install to a disk

Installing is the normal way to keep pairings and settings. The Debian text
installer copies the CouchLiteOS system to the disk you choose. The screens are
Debian's own and their wording can differ slightly; the answers below are the
ones CouchLiteOS recommends (the automated test answers the same questions from
`tests/installer-preseed.cfg`). They are the same on any PC. On the Dell
OptiPlex 7010 Micro reference machine the target is the 256 GB NVMe.

1. Back up the target disk and unplug other writable drives where practical.
2. Boot the stick, press an arrow key to stop the countdown, and choose
   `Install CouchLiteOS`.
3. **Language, location, keyboard:** choose yours. The keyboard layout applies
   to the installer and the installed system's text console. CouchLiteOS sets no
   layout for the launcher session, so expect US key mapping there (not
   verified on an installed system).
4. **Network:** wired DHCP is configured automatically. If it fails, or there is
   no network, choose *Do not configure the network at this time*; the installed
   system uses NetworkManager and the first-boot wizard can open `nmtui`. If
   asked for missing firmware files, continue without them: the ISO carries no
   extra firmware for the installer itself, and the installed system keeps the
   firmware of the live system.
5. **Hostname and domain:** `couchliteos` and an empty domain. Any name works.
6. **Root password:** leave it empty to disable the root login (the new user
   then gets `sudo`). **New user:** any name except `couchliteos` (that account
   already exists in the image), and a password you will remember. The launcher
   runs as the `couchliteos` account; this extra user is only for maintenance.
7. **Clock:** choose your time zone.
8. **Partition disks:** choose *Guided - use entire disk* (the plain option, not
   LVM or encrypted), then pick the target disk. Check its size and model
   against your backup: the next steps erase it. Choose *All files in one
   partition*. On a UEFI PC the guided layout creates an EFI System Partition
   and an ext4 root, the reference layout. Choose *Finish partitioning and write
   changes to disk*. The installer asks *Write the changes to disks?* and
   defaults to No; answer Yes only when the disk is right.
9. **Network mirror:** if asked, No. The system is copied from the ISO and needs
   no downloads (the tested answer).
10. **Software selection and popularity contest:** if asked, select nothing and
    answer No. CouchLiteOS is a kiosk; it does not use a desktop environment.
11. **GRUB:** on a UEFI PC it installs itself to the EFI System Partition. On a
    legacy BIOS PC it asks for a target: install to the primary drive and choose
    the disk you just installed to (for example `/dev/nvme0n1` or `/dev/sda`),
    never a partition and never the USB stick.
12. When the installer says it is finished, remove the USB stick and continue.

Then confirm the CouchLiteOS launcher appears, open SYSTEM DIAGNOSTICS, pair
your applications, reboot, and confirm the host lists remain.

The first-boot wizard can open the existing `nmtui` network setup; wired DHCP
remains automatic and IPv6 is disabled. The launcher is deliberately not held
behind network-online, so it must appear even with Ethernet unplugged. Streaming
applications wait briefly for IPv4 but remain launchable after a DHCP timeout.

If the boot menu appears but the launcher does not, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-screen-stays-black-or-the-launcher-never-appears).

The installed root filesystem is writable. Pairings, settings, and logs live
beneath `/var/lib/couchliteos` and `/var/log/couchliteos`.

The install smoke test (`make qemu-install-smoke`) performs a complete UEFI
installation to a disposable 32 GB virtual disk, removes the ISO, boots that
disk independently, and waits for the real launcher-ready marker. Physical
NVMe and second-USB installation still require the checklist in `TESTING.md`.

## Optional live-USB persistence

A live USB starts from the image every boot, so Moonlight pairing, Wi-Fi,
Bluetooth pairings and the Setup Wizard's completion are forgotten at power-off
unless something keeps them. Installation (above) is preferred. For testing,
persistence keeps them on a **second USB stick**.

The layout matches `tests/qemu-persistence-smoke.sh`: one ext4 filesystem on the
whole second device, labeled `persistence`, holding a `persistence.conf` at its
root. The ISO stick stays untouched. A persistence partition on the same stick
as the ISO has not been tested and is not documented.

On Linux, plug in the second stick, find it with `lsblk` (it must not be the ISO
stick), and run the commands below, replacing `/dev/sdX` with the whole device
(not a partition). `mkfs.ext4` **erases the stick** and may warn about an
existing partition table or filesystem; answer `y` only when `lsblk` confirmed
the device.

```bash
lsblk -o NAME,SIZE,MODEL,FSTYPE,LABEL,MOUNTPOINT
sudo mkfs.ext4 -L persistence /dev/sdX
sudo mkdir -p /mnt/persistence
sudo mount /dev/sdX /mnt/persistence
sudo tee /mnt/persistence/persistence.conf <<'EOF'
/var/lib/couchliteos source=couchliteos-state
/var/log/couchliteos source=couchliteos-logs
/var/lib/tailscale source=tailscale-state
/var/lib/bluetooth source=bluetooth-state
/etc/NetworkManager/system-connections source=nm-connections
EOF
sudo umount /mnt/persistence
```

Windows and macOS cannot create ext4 themselves, and this has not been tested
from either. The practical route is any Linux system for the commands above,
including a Linux virtual machine with USB access. CouchLiteOS's own live
session also works: boot it from the ISO stick, plug in the second stick, open
TERMINAL, and run the same commands (`sudo` needs no password in the live
session; the stick mounted at `/run/live/medium` is the ISO stick, so never
format that one).

Sticks made for MoonlightOS keep their settings automatically (the image adopts their old `/var/lib/moonlightos` and `/var/log/moonlightos`), and new sticks use the `persistence.conf` lines above. <!-- rename:keep -->

Then boot the ISO stick with the persistence stick still plugged in and choose
`Start CouchLiteOS`. Do not pre-create those four source directories empty. On the first persistent
boot, `live-boot` creates each directory and bootstraps it from the matching
image directory with matching ownership and permissions. After boot, verify:

```bash
findmnt /var/lib/couchliteos /var/log/couchliteos \
  /var/lib/tailscale /var/lib/bluetooth /etc/NetworkManager/system-connections
stat -c '%U:%G %a %n' /var/lib/couchliteos /var/log/couchliteos \
  /var/lib/tailscale /var/lib/bluetooth /etc/NetworkManager/system-connections
```

The NetworkManager line makes saved Wi-Fi networks survive a reboot. Its
directory must show `root:root 700` so the unprivileged launcher user cannot
read the saved passwords. A persistence stick made before this line existed
keeps working, but forgets Wi-Fi at every reboot until you add the line to its
`persistence.conf` (mount the stick, append the line, and let the next boot
create the source directory). Installed systems need no change: their `/etc`
already persists.

The ISO's default entry already passes `persistence`; the explicit `No
Persistence` entry ignores the backend. Do not use an unencrypted persistent USB
for sensitive pairing data outside a trusted lab. Persistence on physical USB
media is unverified; only the QEMU test exercises this layout.

## Updating to a new release

What you do depends on where CouchLiteOS lives:

| Your setup | To update | Pairings and settings |
|---|---|---|
| Installed to a disk, CouchLiteOS 0.2.1 or newer | **Settings > SOFTWARE UPDATE** (below) | **Kept**: pairings, Wi-Fi, Bluetooth, Remote Desktop connections, Tailscale, the user accounts you made, and settings. |
| Installed to a disk, CouchLiteOS 0.2.0 or older | Run the installer again from the new ISO, once | **Lost.** Guided partitioning erases the disk, including `/var/lib/couchliteos`. Pair again. Those releases have no updater, so this is the last time you reinstall to update. |
| Live USB with a persistence stick | Write the new ISO to the ISO stick only; keep the persistence stick | **Kept** on the persistence stick. Carrying one across releases is not tested; if a release misbehaves, boot once with `No Persistence` to compare. |
| Live USB without persistence | Write the new ISO | Nothing was kept; you pair again at every boot. |
| Any stick you write an ISO to | - | **Wiped.** Writing an ISO replaces everything on that stick, including a persistence partition you put on it. |

A live USB cannot update itself; the SOFTWARE UPDATE screen says so and shows
where to get the new ISO.

### Settings > SOFTWARE UPDATE (installed systems)

1. Connect the box to the network (Settings > NETWORK) and plug it in. Close any
   running app; the update refuses to start while one is open.
2. Open **Settings > SOFTWARE UPDATE**. The screen shows the installed
   version and profile. Choose **CHECK FOR UPDATES**.
3. If a newer release has a file for your box (the general or the NVIDIA ISO,
   the same kind you installed), choose **INSTALL UPDATE** and confirm.
4. The box downloads the ISO (about 2 GB; it needs that much free space plus
   4 GiB of room), checks it against the release's `SHA256SUMS`, installs it,
   and restarts. B cancels while it is downloading; once it says INSTALLING it
   cannot be stopped. **Keep the box plugged in until it restarts.**
5. After the restart the launcher shows the "what's new" screen.

A download that was interrupted (the network dropped, or you cancelled) resumes
from where it stopped the next time. The ISO is kept in
`/var/cache/couchliteos/update` until the next update and then removed.

The update replaces the system with the new release's system and keeps your
settings, accounts, pairings, Wi-Fi and Bluetooth state, the bootloader, and
the firmware the installer added. Packages you installed yourself with `apt`
stay on the disk, but they are not rebuilt for the new release, so one that
depends on libraries the release replaced may stop working. There is no way
back to the previous release: see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

For an offline update, copy the ISO onto the box and run, as root:

```bash
sudo /usr/libexec/couchliteos-updater apply-iso /path/to/couchliteos-X.Y.Z-amd64.iso
```

The same checks and the same install run; `--no-reboot` stops before the
restart. The SOFTWARE UPDATE screen itself only downloads from GitHub.

## Optional Ventoy layout

Ventoy persistence is not yet a supported or physically verified path. For
testing, keep the ISO and an ext4 backend labeled `persistence` on Ventoy's
first partition:

```text
ISO/couchliteos-0.1.13-amd64.iso
persistence/couchliteos.dat
ventoy/ventoy.json
```

`ventoy/ventoy.json` can associate them with Ventoy's persistence plugin:

```json
{
  "persistence": [
    {
      "image": "/ISO/couchliteos-0.1.13-amd64.iso",
      "backend": "/persistence/couchliteos.dat",
      "timeout": 0
    }
  ]
}
```

The backend must contain the same `persistence.conf` shown above. Record the
Ventoy version and test normal UEFI mode, launcher readiness, installer entry,
and persistence across reboot before treating this path as supported.
