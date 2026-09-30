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
| Broadcom 802.11 Linux STA driver (`broadcom-sta-dkms`, `wl`), `imac2013` ISO only | https://www.broadcom.com/ | Proprietary Broadcom Software License Agreement (Debian non-free). The image contains `wl.ko` built by DKMS from Debian's unmodified package for the image kernel; it may be redistributed only unmodified, for use with Broadcom hardware, and with the agreement, which ships at `/usr/share/doc/broadcom-sta-dkms/copyright` |
| Linux firmware (`firmware-misc-nonfree`), `imac2013` ISO only | https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git | Per-file redistributable firmware licenses (Debian non-free-firmware), including Broadcom `tigon/tg357766.bin` |
| lm-sensors, dmidecode, DKMS, Linux headers (`imac2013` ISO only) | https://www.debian.org/ | GPL-2.0-or-later and per-package terms; Debian copyright files are authoritative |

Moonlight and chiaki-ng binaries are fetched from their official GitHub
releases by `scripts/fetch-apps.sh` and verified against
`build/applications.lock`. Their corresponding source is available at the
tagged upstream repositories. Firefox ESR is installed as a Debian package, and
its Debian copyright and source records remain in the image. Google Chrome is
installed from Google's signed stable Debian repository. MoonlightOS does
not bundle Sony, NVIDIA, Steam, or other proprietary game/service binaries,
credentials, keys, firmware from unapproved sources, or user pairing material.
Neither image contains a proprietary NVIDIA driver; the `imac2013` build fails
if an NVIDIA driver package or module is present. Like earlier releases, both
images include every redistributable firmware package from Debian's
`non-free-firmware` area (live-build's default), which includes
`firmware-nvidia-graphics`, the signed firmware nouveau uses on Maxwell and
newer GPUs; Kepler does not load it. The DKMS module-signing key generated
while building `wl.ko` is deleted before the image is sealed.

Names such as Moonlight, Firefox, Mozilla, Google Chrome, PlayStation, Sunshine,
Intel, Dell, Apple, iMac, NVIDIA, Broadcom, FreeRDP, Microsoft, Windows, and
Gamescope are the property of their respective owners. This project is not
endorsed by Sony Interactive Entertainment, Mozilla, Google, Moonlight, Dell,
Intel, Apple, NVIDIA, Broadcom, Microsoft, the FreeRDP project, or Valve.
