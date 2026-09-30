# Known limitations

- No physical DCC36X3 test has been recorded in this repository yet.
- No physical iMac Late 2013 test has been recorded yet, including the section 0
  identification (model, serial, GPU, Wi-Fi). See [IMAC-2013.md](IMAC-2013.md).
- The iMac profile decodes Moonlight video in software (H.264 only): Kepler has
  no HEVC decoder and nouveau's H.264 decoder needs non-redistributable NVIDIA
  firmware. nouveau also leaves Kepler at its boot clocks. The proprietary
  NVIDIA driver cannot be used (no GBM for Cage).
- iMac Wi-Fi uses Broadcom's proprietary `wl` driver built with DKMS; it loads
  only with Secure Boot off (always true on 2013 Apple firmware).
- Remote Desktop uses FreeRDP 3.15's SDL client, which upstream marks
  experimental. One RDP session runs at a time. Saved passwords are stored
  unencrypted (root-only 0600). Debian 13's xrdp 0.10.1 showed its own login
  dialog in the lab even though MoonlightOS sent the credentials. No Windows RDP
  host has been tested yet.
- UEFI installation, independent virtual-disk boot, and live persistence are
  automated in QEMU. Physical NVMe/SATA installation, second-USB installation,
  Rufus, Ventoy, and persistence on physical media remain unverified.
- Moonlight uses XWayland under Cage because the pinned Moonlight Qt build does
  not include a Wayland Qt platform plugin. Cage itself owns DRM/KMS directly.
- Moonlight and chiaki-ng come from hash-pinned upstream AppImages, but their
  payloads are extracted into the image at build time. Native source-built
  Debian packages remain a future goal.
- 4K60 SDR is best effort. HDR is unverified and unsupported for acceptance.
- The installed root filesystem is writable; A/B read-only updates are deferred.
- Steam is not installed. `steam-devices` supplies controller device rules only;
  the documented Steam manifest is groundwork for a future supported install.
- The controller keyboard is a full-screen buffered utility, not a compositor overlay.
  From a controller it opens in launcher text fields (X on Xbox pads, Triangle
  on PlayStation pads); the global Guide+X chord was removed in v0.1.10.
  Physical controller and text-injection validation remains required.
- Custom command applications must already exist on the filesystem.
- Google Chrome is tested for startup in QEMU, but Disney+ does not officially
  support ordinary Linux distributions; Chrome therefore does not guarantee playback.
- Bluetooth depends on kernel and firmware support for the installed adapter.
  QEMU verifies only clean no-adapter behavior; no physical Bluetooth result is
  claimed. MoonlightOS provides no desktop Bluetooth application.
- Installed systems retain BlueZ pairing state normally. A live-persistent USB
  must persist `/var/lib/bluetooth` separately for pairings to survive reboot.
- Tailscale is installed but unauthenticated and optional. No subnet router,
  exit node, Serve, Funnel, public ingress, route acceptance, or advertised
  route is configured.
- Moonlight Qt cannot open an arbitrary paired host's app grid from a host-only
  CLI argument. Manual host entry is documented; setting a profile `app`
  enables direct CLI streaming.
- Tailscale relay paths may not sustain game streaming or USB/IP latency.
- A remote PlayStation is not reached through Tailscale in v0.1.2; a deliberately
  designed subnet-router deployment is deferred.
