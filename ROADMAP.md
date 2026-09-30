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
