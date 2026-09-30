# Third-party notices

MoonlightOS aggregates unmodified third-party software. It does not claim
ownership of third-party names, trademarks, or code. Debian package copyright
and source records remain installed under `/usr/share/doc/*/copyright`.

| Component | Upstream | License / source obligation |
|---|---|---|
| Debian 13 and packages | https://www.debian.org/ | Per-package licenses; corresponding source is available from Debian repositories configured for the image |
| Linux kernel | https://kernel.org/ | GPL-2.0-only |
| Moonlight Qt | https://github.com/moonlight-stream/moonlight-qt | GPL-3.0-only; v6.1.0 unmodified payload extracted from pinned AppImage |
| chiaki-ng | https://github.com/streetpea/chiaki-ng | AGPL-3.0-only; v1.10.0 unmodified payload extracted from pinned AppImage |
| Mozilla Firefox ESR | https://www.mozilla.org/firefox/ | MPL-2.0 and component-specific terms; installed unmodified from Debian repositories |
| Google Chrome | https://www.google.com/chrome/ | Proprietary Google Chrome and ChromeOS Additional Terms; installed unmodified from Google's signed stable Debian repository |
| Mesa | https://mesa3d.org/ | Primarily MIT and other permissive licenses; Debian copyright file is authoritative |
| Gamescope | https://github.com/ValveSoftware/gamescope | BSD-2-Clause |
| Cage | https://www.hjdskes.nl/projects/cage/ | MIT |
| PipeWire | https://pipewire.org/ | MIT/LGPL-2.1-or-later, by component |
| USB/IP tools and kernel support | https://www.kernel.org/ | GPL-2.0-only and Debian package terms |
| Tailscale | https://github.com/tailscale/tailscale | BSD-3-Clause; official stable Debian package version 1.102.3 |
| FreeRDP 3 (`freerdp3-sdl`, `sdl-freerdp3`) | https://www.freerdp.com/ | Apache-2.0; installed unmodified from Debian 13 (3.15.0); Debian copyright file is authoritative |
| SDL3, SDL3_image, SDL3_ttf | https://www.libsdl.org/ | Zlib; Debian 13 packages pulled in by FreeRDP's SDL client |
| Broadcom 802.11 Linux STA driver (`broadcom-sta-dkms`, `wl`), both ISOs | https://www.broadcom.com/ | Proprietary Broadcom Software License Agreement (Debian non-free). The image contains `wl.ko` built by DKMS from Debian's unmodified package for the image kernel; it may be redistributed only unmodified, for use with Broadcom hardware, and with the agreement, which ships at `/usr/share/doc/broadcom-sta-dkms/copyright` |
| NVIDIA driver 550 (`nvidia-kernel-dkms`, `nvidia-driver-libs`, `libcuda1`, `libnvcuvid1`, `nvidia-vulkan-icd`, `nvidia-smi`, and related libraries), NVIDIA ISO only | https://www.nvidia.com/ | Proprietary NVIDIA Software License (Debian non-free), installed unmodified from Debian 13. The image contains the `nvidia-current*` kernel modules built by DKMS from Debian's unmodified package for the image kernel; the kernel interface layer is dual MIT/GPL-2.0 and the binary core is NVIDIA's. The license permits redistribution of the unmodified software and ships at `/usr/share/doc/nvidia-kernel-dkms/copyright` and the other packages' copyright files |
| NVIDIA GSP firmware (`firmware-nvidia-gsp`), NVIDIA ISO only | https://www.nvidia.com/ | Proprietary NVIDIA redistributable firmware license (Debian non-free-firmware); copyright file is authoritative |
| NVIDIA EGL platform libraries (`libnvidia-egl-gbm1`, `libnvidia-egl-wayland1`), NVIDIA ISO only | https://github.com/NVIDIA/egl-wayland | MIT; installed unmodified from Debian 13 |
| nvidia-vaapi-driver, NVIDIA ISO only | https://github.com/elFarto/nvidia-vaapi-driver | MIT; installed unmodified from Debian 13 |
| Linux firmware (`firmware-misc-nonfree`, `firmware-intel-graphics`, `firmware-amd-graphics`, `firmware-nvidia-graphics`, `firmware-iwlwifi`, `firmware-realtek`, `firmware-atheros`, `firmware-mediatek`, `firmware-brcm80211`, `firmware-sof-signed`, `firmware-intel-sound`, `firmware-intel-misc`, `firmware-cirrus`, `bluez-firmware`, `intel-microcode`, `amd64-microcode`) | https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git | Per-file redistributable firmware licenses (Debian non-free-firmware), including Broadcom `tigon/tg357766.bin`; copyright files are authoritative |
| lm-sensors, dmidecode, DKMS, Linux headers, VA-API drivers (`intel-media-va-driver`, `i965-va-driver`, `mesa-va-drivers`) | https://www.debian.org/ | GPL-2.0-or-later, MIT, and per-package terms; Debian copyright files are authoritative |

Moonlight and chiaki-ng binaries are fetched from their official GitHub
releases by `scripts/fetch-apps.sh` and verified against
`build/applications.lock`. Their corresponding source is available at the
tagged upstream repositories. Firefox ESR is installed as a Debian package, and
its Debian copyright and source records remain in the image. Google Chrome is
installed from Google's signed stable Debian repository. MoonlightOS does
not bundle Sony, Steam, or other proprietary game/service binaries,
credentials, keys, firmware from unapproved sources, or user pairing material.
The general ISO contains no proprietary NVIDIA driver, library, or driver GSP
firmware (`firmware-nvidia-gsp`), and its build fails if one appears; its only
NVIDIA files are the linux-firmware files in `firmware-nvidia-graphics` that
nouveau loads. The NVIDIA ISO adds NVIDIA's 550 driver and GSP firmware from
Debian 13's `non-free` and `non-free-firmware` areas, unmodified apart from
the DKMS build of the kernel module for the image kernel. Both ISOs install a curated firmware list instead
of live-build's install-everything default, and neither carries the separate
firmware package copies for the text installer. The DKMS module-signing key
generated while building `wl.ko` and the NVIDIA modules is deleted before the
image is sealed.

Names such as Moonlight, Firefox, Mozilla, Google Chrome, PlayStation, Sunshine,
Intel, Dell, Apple, iMac, NVIDIA, Broadcom, FreeRDP, Microsoft, Windows, and
Gamescope are the property of their respective owners. This project is not
endorsed by Sony Interactive Entertainment, Mozilla, Google, Moonlight, Dell,
Intel, Apple, NVIDIA, Broadcom, Microsoft, the FreeRDP project, or Valve.
