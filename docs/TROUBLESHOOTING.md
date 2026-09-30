# Troubleshooting

Run `moonlightos-diagnostics`. It prints and saves OS, kernel, CPU/GPU,
Vulkan, VA-API, audio, network, USB/IP, Tailscale, USB, and display-mode data.

| Symptom | Check |
|---|---|
| Launcher does not appear | `systemctl status moonlightos-launcher seatd`; inspect `/var/log/moonlightos/launcher.log` |
| Moonlight returns immediately | `/var/log/moonlightos/moonlight.log`; verify XWayland and VA-API output |
| chiaki-ng black screen | Try Vulkan then OpenGL; optionally set `gamescope = true`; keep HDR off |
| No HDMI/DP audio | `wpctl status`, `aplay -l`; select the display sink in application settings |
| No DHCP | `nmcli device`, `ip route`, cable/switch link, `/var/log/moonlightos/network.log` |
| USB/IP refused | `moonlightos-usbip list`; confirm exact serial and risky-class policy |
| Tailscale is offline | `moonlightos-tailscale-diagnostics`; local LAN remains usable |
| Tailscale stream is slow | Check direct/peer-relay/DERP result and approximate latency; reduce bitrate before changing router policy |
| Saved display mode is not restored | The output, advertised mode, or encoded display identity changed; inspect `/var/log/moonlightos/display.log` |
| Display preview is unusable | Wait 15 seconds or press Escape/controller east; MoonlightOS attempts to restore the previous advertised mode |
| Support USB is not shown | Use mounted writable removable media, a removable `MOONLIGHTOS_SUPPORT` partition, or writable live persistence; ISO9660 and internal disks are intentionally rejected |
| Support export fails | `systemctl status moonlightos-support-export.service`; retry after checking free space and write protection |

Do not open UDP/41641 unconditionally. Run `tailscale netcheck` and
`tailscale ping` first. Do not port-forward Sunshine or USB/IP to the public
internet.

## USB does not boot

1. Write the ISO to the whole USB device with the `dd` command in `INSTALL.md`.
2. Use Dell `F12` and choose the `UEFI` USB entry. The image requires x86_64
   UEFI and has Secure Boot support enabled.
3. Wait for the three-second GRUB timeout. The launcher is not allowed to wait
   for DHCP, so unplugged Ethernet must not prevent it from appearing.
4. If it still stops, capture the exact last message or a photo. Serial boot
   output is available at 115200 8N1 for development builds.

IPv6 is intentionally unavailable in v0.1.12. Use `ip -4 address` and `ip -4
route` when troubleshooting networking.

Support export details and verification commands are in
[SUPPORT.md](SUPPORT.md).

## iMac Late 2013 shows a black screen

The `imac2013` image drives the NVIDIA Kepler GPU with nouveau and needs KMS.
Do not add `nomodeset`, and never install a proprietary NVIDIA driver (the last
Kepler branch has no GBM, so Cage cannot start). Reboot with **Start
MoonlightOS (No Persistence)**, then check `lsmod` for `nouveau` and the
journal for `nouveau` or `cage` errors. Confirm the machine with
`sudo moonlightos-hardware-report`; an `iMac14,1` has Intel graphics and does
not match this profile.

## iMac Wi-Fi is missing

Check `lsmod | grep '^wl '` and `modinfo wl`. If `wl` is missing on an installed
system after a kernel update, make sure `linux-headers-amd64`, `dkms`, and
`broadcom-sta-dkms` are installed so DKMS can rebuild it. `b43`, `bcma`, `ssb`,
and `brcmsmac` must stay blacklisted.

## Remote Desktop does not connect

- `COULD NOT REACH`: check the host, port, and firewall; MoonlightOS is
  IPv4-only.
- `CERTIFICATE CHANGED - CONNECTION BLOCKED`: the server presented a different
  certificate. Verify the new fingerprint on the server before using Settings →
  Remote Desktop → Forget certificate.
- `authentication failed`, `the password is wrong`, `the account is locked out`:
  these are never retried automatically. Check the username, domain, and
  password, then connect again.
- `the saved password is missing`: save it again under Save password.
- xrdp shows its own login dialog: see [Remote Desktop](REMOTE_DESKTOP.md#known-behavior).
- Client output: `journalctl -u moonlightos-rdp.service`; certificate decisions:
  `/var/log/moonlightos/rdp.log`.
