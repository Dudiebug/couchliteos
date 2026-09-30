# Hardware support

## Dell OptiPlex 7010 Micro DCC36X3

| Component | v0.1.2 design |
|---|---|
| Core i5-13500T / UHD 770 | Standard Debian kernel `i915`, Mesa, Intel media driver |
| 16 GB 1x16 DDR4-3200 | Supported; 2x8 GB dual-channel preferred for iGPU bandwidth |
| 256 GB NVMe | Supported installation target |
| Gigabit Ethernet | NetworkManager DHCP, wired-first |
| DisplayPort 1.4a | Preferred for 4K60 SDR testing |
| HDMI 1.4b | Suitable for lower modes; 4K60 depends on the physical port/path |
| No factory Wi-Fi/Bluetooth | Expected; not required in v0.1.2 |
| USB controllers | Wired supported; wireless needs a USB Bluetooth adapter |

The UHD 770 generation can expose hardware H.264, HEVC, and AV1 decode through
VA-API, but the diagnostic output on the actual machine is the source of truth.
Run `vainfo` and confirm decoder profiles before accepting a test result.

4K60 SDR is best effort. Do not infer HDR support from EDID, Vulkan, or VA-API
alone; HDR requires an end-to-end physical test.

MoonlightOS v0.1.2 is IPv4-only. IPv6 is disabled on the live kernel command line,
in the installed GRUB configuration, in NetworkManager, and through sysctl.

## Apple iMac Late 2013 (`imac2013` profile)

| Component | Design |
|---|---|
| iMac14,2 (27-inch) / iMac14,3 (21.5-inch) | Separate ISO built with `PROFILE=imac2013` |
| NVIDIA Kepler GT 750M / GT 755M / GTX 775M / GTX 780M | `nouveau` KMS with Mesa; never the proprietary driver (the 470 branch has no GBM for Cage) |
| Video decode | Software H.264 in Moonlight; no HEVC/AV1 on Kepler, nouveau H.264 needs non-redistributable firmware |
| Haswell quad-core i5/i7 | Debian kernel defaults |
| 2560x1440 or 1920x1080 panel | Native (preferred) mode by default, marked in Settings → Display |
| Broadcom BCM4360 Wi-Fi | `broadcom-sta-dkms` (`wl`) built for the image kernel; `b43`/`bcma`/`ssb`/`brcmsmac` blacklisted |
| Broadcom BCM57766 Ethernet | `tg3` with `firmware-misc-nonfree` |
| Cirrus Logic CS4208 audio | `snd-hda-intel` |
| Broadcom Bluetooth | `btusb` |
| Apple keyboard | `hid_apple fnmode=2` (F-keys first) |
| SMC sensors | `applesmc`, shown in System Diagnostics |
| Boot | Apple EFI: hold Option, choose **EFI Boot**; no Secure Boot |

Details, the section 0 identification report, and limitations are in
[IMAC-2013.md](IMAC-2013.md). No iMac hardware result is recorded yet.
