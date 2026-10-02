# Known limitations

- No physical machine has run v0.1.13 yet. Every GPU class in
  [HARDWARE.md](HARDWARE.md) needs a recorded result from the checklist in
  [TESTING.md](TESTING.md) before the release is published; that includes the
  Dell OptiPlex 7010 Micro and the iMac Late 2013 section 0 identification
  (model, serial, GPU, Wi-Fi). See [IMAC-2013.md](IMAC-2013.md).
- The NVIDIA ISO has only run in virtual machines, which have no NVIDIA GPU.
  The proprietary driver loading, GBM under Cage, NVDEC decode, and the
  nouveau fallback on a real Kepler card are all unverified.
- NVIDIA RTX 50 (Blackwell) cards are not supported: Debian 13's 550 driver
  predates them, and so does nouveau in Debian 13's kernel. Expect at most the
  firmware framebuffer. Support waits for a 570 or newer Debian driver.
- The proprietary NVIDIA driver (NVIDIA ISO) and Broadcom `wl` (both ISOs) are
  DKMS modules signed with a key that is deleted before the image is sealed, so
  they do not load with Secure Boot on.
  <!-- LEAD-CHECK secure-boot-fallback: assumes feat/bugfix4 (automatic nouveau fallback with Secure Boot on) is merged; see docs/INSTALL.md. -->
  If Secure Boot is on, CouchLiteOS falls back to the open NVIDIA driver (lower
  performance); turn Secure Boot off in firmware setup to use the NVIDIA driver.
  A Broadcom chip that needs `wl` has no Wi-Fi with Secure Boot on: turn Secure
  Boot off, or use wired Ethernet or a supported USB or PCIe Wi-Fi adapter.
  Apple firmware from 2013 has no Secure Boot.
- XWayland applications (Moonlight among them) may flicker or show stale frames
  on the proprietary 550 driver, because it lacks explicit sync. Use the Basic
  Graphics entry if that happens and report the GPU model.
- Hybrid-graphics laptops (Intel or AMD plus NVIDIA): the launcher uses the
  GPU the firmware booted with, usually the integrated one. CouchLiteOS does
  not switch outputs or offload rendering to the discrete GPU yet.
- When the `wl` driver owns a Broadcom Wi-Fi chip, the open Broadcom drivers
  are blacklisted for that boot, as Debian's package does. That includes `b44`,
  the Ethernet driver of some older Broadcom laptops; those machines lose wired
  Ethernet while `wl` is active.
- Moonlight decodes H.264 in software on nouveau (Kepler has no HEVC decoder,
  nouveau's H.264 decoder needs non-redistributable NVIDIA firmware, and newer
  cards have no nouveau decoder), and CouchLiteOS holds Moonlight's decoder at
  software there so it does not warn about a missing hardware decoder on every
  stream. On Kepler (and GF117/GF119) Settings > STREAMING > VIDEO DECODER
  FIRMWARE can fetch NVIDIA's 325.15 driver from nvidia.com and extract the
  decoder firmware onto the stick; it is experimental, untested on real
  hardware, and switches Moonlight to automatic decoding only after `vainfo`
  showed H.264 decoding on that GPU. nouveau also leaves Kepler at its boot
  clocks, so the iMac targets 1080p60.
- To keep each ISO under GitHub's 2 GiB release-asset limit, neither ISO
  copies firmware packages for the text installer. The live system and the
  system it installs keep their firmware; only network hardware that needs
  firmware is unavailable inside the installer itself, which the live-copy
  installation does not require.
- Remote Desktop uses FreeRDP 3.15's SDL client, which upstream marks
  experimental. One RDP session runs at a time. Saved passwords are stored
  unencrypted (root-only 0600). Debian 13's xrdp 0.10.1 showed its own login
  dialog in the lab even though CouchLiteOS sent the credentials. No Windows RDP
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
  Installed boxes (0.2.1 and newer) update in place from Settings > SOFTWARE
  UPDATE, but there is no rollback: the update replaces the running system, and
  the previous release is not kept. There is no backup or restore of pairings
  either. 0.2.0 and older have no updater: reinstall once (which erases the
  disk). See [Updating to a new release](INSTALL.md#updating-to-a-new-release).
- Power loss while SOFTWARE UPDATE shows INSTALLING can leave the box unable to
  boot. The only recovery is to reinstall from the ISO, which erases the disk.
  A download that was interrupted is safe and resumes.
- SOFTWARE UPDATE trusts HTTPS to github.com plus the `SHA256SUMS` file of the
  same release. Neither the ISO nor `SHA256SUMS` is signed, so someone who can
  publish to the GitHub project could publish a bad update.
- A live USB cannot update itself; write the new ISO to the stick. Updates do
  not download only the changed parts (every update fetches the whole ISO, about
  2 GB), and the SOFTWARE UPDATE screen cannot install from a file on a USB
  stick; the command line `couchliteos-updater apply-iso` can.
- The updater has run only in virtual machines and in unit tests, never on
  physical hardware. See the update items in [TESTING.md](TESTING.md).
- A live USB without persistence forgets pairings, Wi-Fi, Bluetooth and the
  Setup Wizard at every power-off. Persistence needs a second USB stick made on
  Linux (see [INSTALL.md](INSTALL.md)); Windows and macOS have no tested way to
  make it.
- The boot menu and the installer need a keyboard; they ignore controllers.
- Steam is not installed. `steam-devices` supplies controller device rules only;
  the documented Steam manifest is groundwork for a future supported install.
- The controller keyboard is a full-screen buffered utility, not a compositor overlay.
  From a controller it opens in launcher text fields (X on Xbox pads, Triangle
  on PlayStation pads). To type into a running app, press Guide, pick the app
  and press X / Triangle (or press X / Triangle while the controller mouse is
  on); the text is typed into the app after the keyboard closes. Physical
  controller and text-injection validation remains required.
- The controller mouse (browsers and web apps) has no on-screen speed setting,
  and the ON/OFF choice made in the Guide menu lasts until the launcher restarts.
- The image carries Debian's Cage 0.2.0 rebuilt with a backported patch for
  window switching (wlr-foreign-toplevel-management, upstream in Cage 0.3.0);
  Debian's binary is kept as `/usr/bin/cage.debian`.
- Custom command applications must already exist on the filesystem.
- Google Chrome is tested for startup in QEMU, but Disney+ does not officially
  support ordinary Linux distributions; Chrome therefore does not guarantee playback.
- Bluetooth depends on kernel and firmware support for the installed adapter.
  QEMU verifies only clean no-adapter behavior; no physical Bluetooth result is
  claimed. CouchLiteOS provides no desktop Bluetooth application.
- Installed systems retain BlueZ pairing state normally. A live-persistent USB
  must persist `/var/lib/bluetooth` separately for pairings to survive reboot.
- Tailscale is installed but unauthenticated and optional. No subnet router,
  exit node, Serve, Funnel, public ingress, route acceptance, or advertised
  route is configured.
- Moonlight Qt cannot open an arbitrary paired host's app grid from a host-only
  CLI argument. Manual host entry is documented; setting a profile `app`
  enables direct CLI streaming.
- Tailscale relay paths may not sustain game streaming or USB/IP latency.
- A remote PlayStation is not reached through Tailscale; a deliberately
  designed subnet-router deployment is deferred.
