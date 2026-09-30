# Support export

Open **Settings → Generate Support File** with a keyboard or controller. If
more than one safe destination is present, choose one with the D-pad and south
button. The launcher writes only
`/run/moonlightos/support-export.request`; the root-owned
`moonlightos-support-export.path` unit starts the narrowly scoped exporter.
The launcher has no sudo permission.

## Destination rules

The launcher offers a drive only when it is external (USB or removable), has a
writable filesystem, and is not the boot drive. It offers:

- an already mounted, writable partition on removable/USB storage. No special
  label is needed; or
- an unmounted removable partition or whole-disk filesystem. No special label
  is needed on the drive: the launcher marks the request with the internal name
  `MOONLIGHTOS_SUPPORT`, and the root exporter refuses to mount anything that
  does not carry that name. It mounts the drive temporarily, without SUID,
  device, or executable permissions, and unmounts it when it has finished.

The boot USB itself is never offered, including its `persistence` partition,
because the whole boot disk is excluded. With only the boot USB attached, the
launcher says to insert a second USB drive. The exporter also rejects ISO9660,
squashfs, UDF, read-only media, and internal SATA/NVMe filesystems. It does not
format, repartition, run fsck, or repair media. The selected device and mount
are checked again immediately before the archive is copied.

NTFS drives are mounted with the kernel `ntfs3` driver. If that fails, or the
drive still comes up read-only (Windows often leaves NTFS marked unclean after
a fast shutdown), the screen says `THIS DRIVE IS NTFS; USE A FAT32 OR EXFAT
DRIVE`. FAT32 and exFAT are the recommended formats.

If the export cannot finish, the screen gives a short reason such as
`USB DRIVE IS FULL`, `USB DRIVE IS READ-ONLY`, or
`USB DRIVE ERROR: TRY ANOTHER DRIVE`.

Keep at least 64 MiB free. A successful export creates exactly one file:

```text
moonlightos-support-YYYYMMDD-HHMMSSZ-<machine-id-prefix>.tar.gz
```

The screen reports the final path. For a temporarily mounted drive,
it reports the device and filename because the temporary mount is removed after
the export.

## Archive contents

The archive contains version/build time, date, uptime, kernel information, CPU,
memory, PCI/USB inventory, loaded modules, GPU/DRM/wlroots output state, Vulkan,
VA-API, PipeWire/WirePlumber, ALSA, NetworkManager, IPv4 addresses, routes, link
and DNS state, sanitized Tailscale state, USB/IP, nftables, relevant package
versions, MoonlightOS service status, failed units, the current-boot journal,
kernel log when permitted, regular text files from `/var/log/moonlightos`, and
a sanitized copy of `/var/lib/moonlightos/config.ini`.

Logs are kept bounded. When an application or the launcher starts, its log in
`/var/log/moonlightos` is cut down to its last 5 MiB if it has grown past that,
and one older copy (`.1`) is kept. Only the newest five saved System
Diagnostics reports are kept. A log larger than 8 MiB is still collected: the
archive holds its last 8 MiB, and the first line of that file says it was
truncated.

The exporter validates every archive member, rejects unsafe paths, links, and
special files, and fully reads each regular member to detect damaged or
truncated archives. It copies to a hidden `.partial`, flushes and validates the
copy, then atomically publishes the archive and syncs the destination.
Incomplete output is removed on failure. Media mounted by MoonlightOS is
reported as successful only after a checked unmount; already-mounted media must
still be safely ejected or unmounted before removal.

## Privacy boundary

The exporter omits browser/application profiles, pairing and registration
state, `/var/lib/moonlightos/home`, `/var/lib/tailscale`, BlueZ link-key
storage under `/var/lib/bluetooth`, saved Remote Desktop passwords under
`/var/lib/moonlightos/rdp-secrets`, and the Remote Desktop session password
handoff in `/run/moonlightos`. Its systemd unit also makes these
private-state paths inaccessible. Structured and plain
text redaction removes private keys, credentials, passwords (including FreeRDP
`/p:`, `/gp:`, and gateway `p:` arguments), cookies, bearer
tokens, auth/API tokens, Tailscale keys, and Tailscale enrollment URLs. Remote
Desktop connections are listed by host, port, and options only, without
usernames, domains, or passwords. LAN IP
addresses and hardware/service failures remain because they are needed for
diagnosis.

Before sharing an archive, inspect it on another system:

```bash
mkdir extracted-support
tar -xzf moonlightos-support-*.tar.gz -C extracted-support
find extracted-support/moonlightos-support -maxdepth 3 -type f -print
```
