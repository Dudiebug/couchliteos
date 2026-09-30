# Roadmap

## Vision: pick one of two ISOs, and it just works

CouchLiteOS should boot straight to the launcher on any common x86-64 PC or
Intel Mac from roughly 2012 onward, with no per-machine build and no manual
driver setup. A user answers one question, "does this PC have a GeForce GTX
900-series or newer card?", and downloads one of two images:

| ISO | For | Graphics |
|---|---|---|
| `couchliteos-<version>-amd64.iso` (**general**) | Intel and AMD graphics, older NVIDIA cards, Intel Macs, VMs | Open drivers only: i915/xe, amdgpu/radeon, nouveau, Mesa |
| `couchliteos-<version>-nvidia-amd64.iso` (**NVIDIA**) | GeForce GTX 900 through RTX 40 (Maxwell to Ada) | NVIDIA's proprietary 550 driver with GBM; falls back to nouveau on anything else |

Everything else is decided on the machine at boot, not at build time:

- **Hardware detection** (`couchliteos-hwdetect`) runs before udev loads
  drivers. It reads PCI and DMI identifiers and writes module policy to `/run`
  only, so nothing persists across machines when a USB stick moves:
  - NVIDIA ISO: loads `nvidia-drm` only when the GPU is on the 550 driver's
    supported list; otherwise it blocks the proprietary modules and loads
    nouveau instead. The **Basic Graphics** boot entry forces nouveau.
  - Broadcom Wi-Fi: uses the `wl` driver only for chips on its list (for
    example the iMac's BCM4360) and leaves the open b43/brcmsmac drivers alone
    everywhere else.
  - Apple machines: loads the SMC sensor driver; Apple keyboards default to
    F1-F12.
- **Decode hints**: when the active GPU driver has no usable video decoder
  (nouveau), Moonlight defaults to software H.264 instead of failing or
  showing a black stream. Users can still change it in Moonlight's settings.
- **One firmware set**: a curated list covering Intel, AMD, and NVIDIA (open)
  graphics, common Wi-Fi/Bluetooth/Ethernet chips, sound (SOF), and CPU
  microcode, sized to keep each ISO under GitHub's 2 GiB asset limit.

Why two images and not one: the NVIDIA driver, its GSP firmware, and its
libraries add roughly 350 MB and a non-free license that not everyone wants,
and it must never load on the GPUs it does not support. A single image would
exceed or crowd the 2 GiB limit and would still need the same boot-time
switch. Two images keep the general one lean and fully open-driver.

Why not a single NVIDIA image for every card: NVIDIA's current branch dropped
Kepler (GTX 600/700, the 2013 iMac) and older; their last driver for those has
no GBM, which Cage/wlroots require. nouveau is the only working path there.

## Plan

### Phase 1 (v0.1.13): two release ISOs

- [x] Vision and plan (this file).
- [x] `general` profile replaces the per-machine `intel` and `imac2013` release
  images. The OptiPlex (UHD 770) and iMac Late 2013 are supported by it.
- [x] `nvidia` profile builds on `general` (`PROFILE_BASE=general`) and adds
  the 550 driver built with DKMS for the image kernel, GBM backend, Vulkan,
  CUDA/NVDEC libraries, and the Basic Graphics boot entry.
- [x] `couchliteos-hwdetect` early (module policy) and late (nouveau fallback,
  GPU driver and decode hints, compositor environment) stages, with unit tests
  against a fake sysfs.
- [x] `intel` and `imac2013` stay buildable as legacy profiles but are no
  longer release assets.
- [ ] Build both ISOs locally, run the QEMU live, persistence, and install
  tests on both, and create a **draft** release.
- [ ] Physical hardware checklist for each GPU class before publishing (see
  `docs/TESTING.md`): Intel (OptiPlex UHD 770), AMD, NVIDIA Maxwell-Ada on the
  NVIDIA ISO, NVIDIA Kepler (iMac) on the general ISO and on the NVIDIA ISO's
  fallback.

### Phase 2: confidence on real hardware

- Community hardware reports: `sudo couchliteos-hardware-report` plus the
  hardware detection summary, collected into `docs/HARDWARE.md` as a
  compatibility table (machine, GPU, ISO, result, date, version).
- Per-GPU decode verification: VA-API on Intel/AMD, NVDEC/CUDA on NVIDIA; set
  decode hints from measured results instead of driver names.
- A quirks file for machine-specific fixes (DMI or PCI match to module
  options), so fixes ship as data instead of new profiles.

### Phase 3: fewer caveats

- Secure Boot with DKMS modules: sign `nvidia` and `wl` with a per-install MOK
  on installed systems, with an enrollment flow in Settings.
- RTX 50 (Blackwell) once Debian ships a 570+ driver; until then those cards
  get basic display only.
- Evaluate NVIDIA's open kernel modules for Turing and newer.
- Hybrid-graphics laptops: pick the output GPU explicitly.

## Next: the couch experience

Goal: from the power button to a game with only a controller or the TV
remote, without a keyboard and without visiting Settings. In priority order.

### A real Setup Wizard

Today's wizard is a checklist: each step shows one status line and opens the
matching Settings screen. The new one does each step in place and checks the
result before moving on.

- [ ] Network: wired is detected on its own; Wi-Fi shows a network list and
  takes the password on the on-screen keyboard; the step confirms that the
  LAN and the internet are reachable.
- [ ] Controller: pair Bluetooth controllers with instructions per model
  (Xbox, DualShock 4, DualSense, Switch Pro), confirm with a button press,
  and show the battery level.
- [ ] Display and sound: choose a mode from the advertised list with the
  existing 15-second preview; sound follows the display (HDMI or DisplayPort
  audio) with a test tone.
- [ ] Streaming host: find Sunshine hosts on the LAN, pair through Moonlight
  with the PIN shown large, remember the host's MAC address for Wake-on-LAN,
  and pick stream settings from the display mode and a short network test.
- [ ] Optional steps: TV control (CEC), Tailscale, chiaki-ng, more apps.
- [ ] A half-finished setup resumes where it stopped; every step can be
  skipped and rerun from Settings.

### Easier everyday use

- [ ] Wake the gaming PC with Wake-on-LAN when it is offline, and offer
  **Wake PC** wherever "host unreachable" appears.
- [ ] Optional: start streaming to the default host right after boot or wake.
- [ ] Controller battery levels in the launcher.
- [ ] Sound switches to the TV's HDMI or DisplayPort output when a TV is
  connected.
- [ ] Every error says what to do next and offers a button for it.
- [ ] Optional notice when a newer CouchLiteOS release exists (A/B updates stay
  a v0.2 candidate).

### TV control (HDMI-CEC)

- [ ] Use the kernel CEC framework (`/dev/cec*`, `cec-ctl` from v4l-utils):
  the TV remote moves through the launcher, the TV turns on and switches to
  CouchLiteOS when it starts or wakes, and (optionally) CouchLiteOS sleeps
  when the TV turns off.
- [ ] Settings shows whether a CEC device exists and what it controls.
- Hardware: PC graphics outputs usually carry no CEC, so check each reference
  machine with `ls /dev/cec*` before promising it. The two known paths are a
  Pulse-Eight USB-CEC adapter (kernel driver `pulse8-cec`) and a
  DisplayPort-to-HDMI adapter with CEC-Tunneling-over-AUX (kernel support in
  i915, amdgpu, and nouveau; adapter support varies).

### Sleep and wake

- [ ] Sleep from the power menu and from a long press of Home/Guide.
- [ ] Wake from a paired Bluetooth controller (wakeup enabled on the Bluetooth
  adapter), a USB keyboard, Wake-on-LAN, and the TV through CEC.
- [ ] Clean resume of display, network, sound, and the launcher, including
  NVIDIA's suspend helpers on the NVIDIA ISO.
- [ ] Blank the screen when idle and sleep later (protects OLED TVs from the
  launcher's static high-contrast screen).
- Whether USB stays powered in sleep differs by machine; record it per machine.

### Faster

- [ ] Measure boot with `systemd-analyze` on the OptiPlex and the iMac, then
  shorten the critical path.
- [ ] Installed systems hide the 3-second boot menu (Shift or Esc shows it).
- [ ] Build the nvidia profile on top of the general image instead of from
  scratch.

## v0.1.0-alpha acceptance

- Reproducible Debian 13 ISO source and pinned application downloads
- Boot-to-launcher flow, persistent application state, logs, diagnostics
- Wayland hardware-decoding path, wired DHCP, fail-closed USB/IP
- QEMU boot smoke test plus the physical DCC36X3 checklist

## v0.2 candidates

1. Replace the AppImage delivery path with reproducible native Debian packages
   built from pinned upstream source tags.
2. Add a settings screen for gaming-host IP, Moonlight stream presets, audio
   sink, and controller identity without editing files.
3. Validate and selectively enable a direct-KMS Moonlight implementation if it
   matches Moonlight Qt features and proves more reliable on UHD 770.
4. Add EDID-aware display selection, refresh-rate switching, and tested
   1080p120/1440p60 presets.
5. Add opt-in USB Bluetooth support and controller pairing UI.
6. Add A/B system updates with rollback; keep user data on a separate durable
   partition.
7. Evaluate HDR only after SDR reliability and the physical hardware matrix
   are complete.
8. Evaluate a narrowly scoped PlayStation subnet-router design only as an
   explicit opt-in; never enable route advertisement as a side effect.
