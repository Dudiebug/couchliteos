# Hardware support

CouchLiteOS v0.1.13 ships two ISOs. Both detect the hardware on every boot
(`couchliteos-hwdetect`) and write that machine's driver policy under `/run`
only, so a USB stick moved between machines starts clean each time. Run
`/usr/libexec/couchliteos-hwdetect report`, or open System Diagnostics, to see
what it chose.

| Graphics | ISO | Driver | Moonlight video decode |
|---|---|---|---|
| Intel HD/UHD/Iris/Arc (Broadwell and newer) | general | `i915`/`xe`, Mesa | VA-API (`iHD`) |
| Intel Gen4 to Haswell | general | `i915`, Mesa | VA-API (`i965`) |
| AMD Radeon (GCN and newer; older via `radeon`) | general | `amdgpu`/`radeon`, Mesa | VA-API (`radeonsi`) |
| NVIDIA Maxwell to Ada: GTX 745/750/750 Ti, GTX 800M/900M, GTX 900, GTX 10, GTX 16, RTX 20/30/40 | nvidia | proprietary 550, GBM | NVDEC through CUDA |
| NVIDIA Maxwell to Ada (same cards) | general | `nouveau` | software H.264 |
| NVIDIA Kepler and older: GTX 600, GTX 760 to 780, mobile GT 750M/755M (iMac Late 2013) | general, or nvidia (falls back) | `nouveau` | software H.264 |
| NVIDIA RTX 50 (Blackwell) | not supported | none (firmware framebuffer at best) | - |
| Virtual machines (QEMU, Hyper-V, Proxmox) | general | `virtio-gpu`, `bochs`, `hyperv_drm` | software |

Decode columns describe the design. No row has a physical hardware result yet;
[TESTING.md](TESTING.md) lists the checklist that must pass per GPU class.

The NVIDIA ISO loads the proprietary driver only for GPUs on its supported
list (`/usr/share/nvidia/nvidia.ids`, from Debian's 550 driver, which starts at
Maxwell). For any other NVIDIA GPU, and when the
**Start CouchLiteOS (Basic Graphics)** boot entry is chosen, it blocks the
proprietary modules and loads nouveau. When the display driver is nouveau,
Moonlight starts with software H.264 decoding. The desktop GTX 750 is Maxwell
but the mobile GT 750M is Kepler, so check the exact chip when a name is
ambiguous; when in doubt, the NVIDIA ISO decides at boot from the GPU's PCI ID.

NVIDIA's driver and Broadcom's `wl` are DKMS modules that are not signed, so
they do not load with Secure Boot on.
<!-- LEAD-CHECK secure-boot-fallback: assumes feat/bugfix4 (automatic nouveau fallback with Secure Boot on) is merged; see docs/INSTALL.md. -->
If Secure Boot is on, CouchLiteOS falls back to the open driver (lower
performance); turn Secure Boot off in firmware setup to use the NVIDIA driver.

Broadcom Wi-Fi chips supported by Broadcom's `wl` driver (for example the
iMac's BCM4360) use `wl`, built by DKMS for the image kernel. Other Broadcom
chips keep the open drivers (`b43`, `brcmsmac`, `brcmfmac`). Apple machines
load `applesmc` for sensors, and Apple keyboards default to F1-F12
(`hid_apple fnmode=2`).

The firmware set is curated rather than Debian's install-everything default:
Intel, AMD, and NVIDIA (open) graphics; Intel SOF, Intel, and Cirrus sound;
Intel, Realtek, Atheros, MediaTek, and Broadcom Wi-Fi/Bluetooth/Ethernet; Intel
and AMD CPU microcode. The NVIDIA ISO adds the 550 driver's GSP firmware.

CouchLiteOS is IPv4-only. IPv6 is disabled on the live kernel command line, in
the installed GRUB configuration, in NetworkManager, and through sysctl.

## Reference machine: Dell OptiPlex 7010 Micro (general ISO)

| Component | Design |
|---|---|
| Core i5-13500T / UHD 770 | Standard Debian kernel `i915`, Mesa, Intel media driver |
| 16 GB 1x16 DDR4-3200 | Supported; 2x8 GB dual-channel preferred for iGPU bandwidth |
| 256 GB NVMe | Supported installation target |
| Gigabit Ethernet | NetworkManager DHCP, wired-first |
| DisplayPort 1.4a | Preferred for 4K60 SDR testing |
| HDMI 1.4b | Suitable for lower modes; 4K60 depends on the physical port/path |
| No factory Wi-Fi/Bluetooth | Expected; not required |
| USB controllers | Wired supported; wireless needs a USB Bluetooth adapter |

The UHD 770 generation can expose hardware H.264, HEVC, and AV1 decode through
VA-API, but the diagnostic output on the actual machine is the source of truth.
Run `vainfo` and confirm decoder profiles before accepting a test result.

4K60 SDR is best effort. Do not infer HDR support from EDID, Vulkan, or VA-API
alone; HDR requires an end-to-end physical test.

## Reference machine: Apple iMac Late 2013 (general ISO)

| Component | Design |
|---|---|
| iMac14,2 (27-inch) / iMac14,3 (21.5-inch) | General ISO; the NVIDIA ISO also works through its nouveau fallback |
| NVIDIA Kepler GT 750M / GT 755M / GTX 775M / GTX 780M | `nouveau` KMS with Mesa; never a proprietary driver (550 does not support Kepler, and the 470 branch has no GBM for Cage) |
| Video decode | Software H.264 in Moonlight; no HEVC/AV1 on Kepler, nouveau H.264 needs non-redistributable firmware |
| Haswell quad-core i5/i7 | Debian kernel defaults |
| 2560x1440 or 1920x1080 panel | Native (preferred) mode by default, marked in Settings → Display |
| Broadcom BCM4360 Wi-Fi | `broadcom-sta-dkms` (`wl`) built for the image kernel; the open Broadcom drivers are blacklisted for that boot only |
| Broadcom BCM57766 Ethernet | `tg3` with `firmware-misc-nonfree` |
| Cirrus Logic CS4208 audio | `snd-hda-intel` |
| Broadcom Bluetooth | `btusb` |
| Apple keyboard | `hid_apple fnmode=2` (F-keys first) |
| SMC sensors | `applesmc` (loaded on Apple hardware), shown in System Diagnostics |
| Boot | Apple EFI: hold Option, choose **EFI Boot**; no Secure Boot |

Details, the section 0 identification report, and limitations are in
[IMAC-2013.md](IMAC-2013.md). No iMac hardware result is recorded yet.
