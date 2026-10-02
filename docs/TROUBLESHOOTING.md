# Troubleshooting

Open SYSTEM DIAGNOSTICS in the launcher, or run `couchliteos-diagnostics`. It
prints and saves OS, kernel, CPU/GPU, Vulkan, VA-API, audio, network, USB/IP,
Tailscale, USB, and display-mode data.

The commands on this page run in the launcher's TERMINAL application (in the
live session `sudo` needs no password). They need a working display: no other
console login is documented, so when the screen stays black use
[the black-screen section](#the-screen-stays-black-or-the-launcher-never-appears)
instead.

| Symptom | Check |
|---|---|
| Launcher does not appear | See [the black-screen section](#the-screen-stays-black-or-the-launcher-never-appears) |
| Moonlight returns immediately | `/var/log/couchliteos/moonlight.log`; verify XWayland and VA-API output |
| chiaki-ng black screen | Try Vulkan then OpenGL; optionally set `gamescope = true`; keep HDR off |
| No HDMI/DP audio | `wpctl status`, `aplay -l`; select the display sink in Settings > AUDIO OUTPUT. If `aplay -l` lists the HDMI device but `wpctl status` has no HDMI sink, update to 0.2.2 (0.2.1 kept HDA cards on their analog profile); `audio.log` says which HDMI/DP profile was chosen, and the support file has `audio/alsa-cards.txt` and `audio/eld/` |
| No DHCP | `nmcli device`, `ip route`, cable/switch link, `/var/log/couchliteos/network.log` |
| USB/IP refused | `couchliteos-usbip list`; confirm exact serial and risky-class policy |
| Tailscale is offline | `couchliteos-tailscale-diagnostics`; local LAN remains usable |
| Tailscale stream is slow | Check direct/peer-relay/DERP result and approximate latency; reduce bitrate before changing router policy |
| Saved display mode is not restored | The output, advertised mode, or encoded display identity changed; inspect `/var/log/couchliteos/display.log` |
| Display preview is unusable | Wait 15 seconds or press Escape/controller east; CouchLiteOS attempts to restore the previous advertised mode |
| Support USB is not shown | Use mounted writable removable media, a removable `COUCHLITEOS_SUPPORT` partition, or writable live persistence; ISO9660 and internal disks are intentionally rejected |
| Support export fails | `systemctl status couchliteos-support-export.service`; retry after checking free space and write protection |

Do not open UDP/41641 unconditionally. Run `tailscale netcheck` and
`tailscale ping` first. Do not port-forward Sunshine or USB/IP to the public
internet.

## USB does not boot

1. Write the ISO to the whole USB device as a raw image (`dd`, Rufus in DD
   Image mode, or balenaEtcher; see [INSTALL.md](INSTALL.md)), not as a file
   copy. Check the download against `SHA256SUMS` first.
2. Use the firmware boot menu (Dell: `F12`; Mac: hold Option) and choose the
   `UEFI` USB entry. The image boots through Debian's signed shim; Secure Boot
   only affects the NVIDIA and Broadcom `wl` drivers (see
   [INSTALL.md](INSTALL.md#secure-boot)).
3. Wait for the three-second boot-menu timeout, or press an arrow key to stop it
   and choose an entry (a keyboard is required; the menu ignores controllers).
   The launcher is not allowed to wait for DHCP, so unplugged Ethernet must not
   prevent it from appearing.
4. If it still stops, capture the exact last message or a photo. Every live ISO
   also writes boot messages to the first serial port at 115200 8N1.

IPv6 is intentionally unavailable. Use `ip -4 address` and `ip -4
route` when troubleshooting networking.

Support export details and verification commands are in
[SUPPORT.md](SUPPORT.md).

## The screen stays black or the launcher never appears

If the launcher does not start, the screen can stay on boot text or go black,
and there is no shell to run commands in (the launcher's TERMINAL needs a
working display; on the live image tty1 has no login prompt and the
`couchliteos` account has no password). What you can do is reachable from the
boot menu, which needs a keyboard. On the live USB, press an arrow key within
three seconds of the menu appearing. An installed system hides the menu while
boots succeed: hold Shift (BIOS) or press Esc (UEFI) while it starts. After a
failed or interrupted boot, it shows the menu for 5 s by itself.

1. **NVIDIA ISO:** reboot and choose **Start CouchLiteOS (Basic Graphics)**. It
   blocks the proprietary driver and uses nouveau. On an installed system, press
   `e` in GRUB, append `couchliteos.gpu=basic` to the line starting with
   `linux`, and press `Ctrl+X`.
2. **Secure Boot on:** NVIDIA's driver and Broadcom's `wl` do not load with it.
   <!-- LEAD-CHECK secure-boot-fallback: assumes feat/bugfix4 (automatic nouveau fallback with Secure Boot on) is merged; see docs/INSTALL.md. -->
   CouchLiteOS falls back to the open driver (lower performance); turn Secure
   Boot off in firmware setup to use the NVIDIA driver. On a UEFI PC the boot
   menu's Utilities submenu has **UEFI Firmware Settings**.
3. **Persistence or saved settings:** choose **Start CouchLiteOS (No
   Persistence)**. It ignores everything saved on a persistence stick, such as a
   display mode your screen cannot show. An installed system has no such entry.
4. Try another display cable or port.
5. Never add `nomodeset`: Cage needs KMS.
   <!-- LEAD-CHECK fail-safe-entry: cleanup of the stock boot-menu entries is deferred; update this line if it lands. -->
   The stock `fail-safe mode` menu entry is not Basic Graphics: it does not
   block the proprietary NVIDIA driver.
6. If none of that works, photograph the screen and report the PC model and GPU
   (read them from the machine's label or another operating system). The
   launcher's Settings → Generate Support File helps only when the launcher
   starts.

If the launcher does start but is garbled, open TERMINAL and run
`/usr/libexec/couchliteos-hwdetect report` to see which driver was chosen and
why. On the NVIDIA ISO, reboot with Basic Graphics; if Basic Graphics works and
the normal entry does not, report the GPU model (`lspci -nn | grep -i nvidia`)
and the hwdetect report from the Basic Graphics boot.

RTX 50 (Blackwell) cards are not supported by either ISO yet.

## iMac Late 2013 shows a black screen

The general ISO drives the iMac's NVIDIA Kepler GPU with nouveau and needs KMS.
Do not add `nomodeset`, and never install a proprietary NVIDIA driver (the last
Kepler branch has no GBM, so Cage cannot start). Reboot and choose **Start
CouchLiteOS (No Persistence)**, which ignores anything saved on a persistence
stick. If it is still black there is no console to inspect it (see the section
above): photograph the screen and report the model. To tell the models apart
without a shell, use About This Mac in macOS or the label: an `iMac14,1` has
Intel graphics and is not the machine [IMAC-2013.md](IMAC-2013.md) describes.
When the launcher does appear, `sudo couchliteos-hardware-report` in TERMINAL
confirms the machine, and `lsmod | grep nouveau` shows the display driver.

## iMac Wi-Fi is missing

Check `lsmod | grep '^wl '`, `modinfo wl`, and the Broadcom line of
`/usr/libexec/couchliteos-hwdetect report`. If `wl` is missing on an installed
system after a kernel update, make sure `linux-headers-amd64`, `dkms`, and
`broadcom-sta-dkms` are installed so DKMS can rebuild it. When `wl` owns the
chip, hardware detection blacklists `b43`, `bcma`, `ssb`, `brcmsmac`, and the
other open Broadcom drivers for that boot; `wl` needs Secure Boot off.

## Remote Desktop does not connect

- `COULD NOT REACH`: check the host, port, and firewall; CouchLiteOS is
  IPv4-only.
- `CERTIFICATE CHANGED - CONNECTION BLOCKED`: the server presented a different
  certificate. Verify the new fingerprint on the server before using Settings →
  Remote Desktop → Forget certificate.
- `authentication failed`, `the password is wrong`, `the account is locked out`:
  these are never retried automatically. Check the username, domain, and
  password, then connect again.
- `the saved password is missing`: save it again under Save password.
- xrdp shows its own login dialog: see [Remote Desktop](REMOTE_DESKTOP.md#known-behavior).
- Client output: `journalctl -u couchliteos-rdp.service`; certificate decisions:
  `/var/log/couchliteos/rdp.log`.
