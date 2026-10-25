# Snapdragon X kernel (Surface Laptop 7)

The arm64 ISO needs a kernel that Debian does not ship: the Surface Laptop 7's
touchpad and touchscreen are HID devices on Qualcomm QSPI/I2C buses that upstream
Linux has no transport for yet, and its Wi-Fi needs an rfkill workaround. This
directory builds `linux-image-<version>-sl7` from upstream Linux plus the
community patch set, as Debian packages, on GitHub's arm64 runner
(`.github/workflows/kernel-arm64.yml`).

| | |
|---|---|
| Base | upstream `linux-7.3` from kernel.org (`version.conf`, SHA-256 pinned) |
| Config | Debian trixie-backports `linux-image-arm64` config + `config.fragment` |
| Patches | `patches/series`, applied with `patch --fuzz=0` (exact context, or the build fails) |
| Output | `kernel/out/linux-image-7.3.0-sl7_7.3-1~couchliteos_arm64.deb` and headers, with `SHA256SUMS` |
| DTBs | `/usr/lib/linux-image-7.3.0-sl7/qcom/x1e80100-microsoft-romulus13.dtb` (13.8", I2C touchscreen), `...-romulus13-spi-touch.dtb` (13.8", SPI touchscreen), `...-romulus15.dtb` (15") |

## What the patches do

| Patch | From | Purpose |
|---|---|---|
| 0001 | ItsLucas | Wi-Fi: skip the ath12k rfkill setup that reports a false hardware block on this laptop |
| 0002 | ItsLucas (from ELLX) | The HID-over-SPI v3 transport (`spi-hid`, Microsoft's Surface Duo driver), QSPI mode for the GENI SPI controller and GPI DMA, and the 15-inch touchpad wiring |
| 0003, 0005, 0017 | ItsLucas | The 15-inch GTCH touchscreen on spi10, its power and reset sequencing, pin bias |
| 0004, 0007-0015, 0021 | ItsLucas | spi-hid robustness: power lifecycle, fragment reassembly, device resets, IRQ handling |
| 0016 | ItsLucas | GENI SPI diagnostics |
| 0018, 0019, 0022 | ItsLucas | 15-inch: audio over DisplayPort, the Iris video decoder, the front camera |
| 0020 | ItsLucas | Surface EC posture events, so the touchpad comes back after a lid close |
| 0026, 0027 | Qualcomm (via ItsLucas) | Make the QCE crypto engine safe to enable |
| 0100 | fQwQf (linux-input, 2026-09-07), via 1Martin11 | 13.8-inch touchscreen (ITCH MSHW0468) as HID-over-I2C on i2c8 |
| 0101 | CouchLiteOS | 13.8-inch touchpad wiring, and the `romulus13-spi-touch` DTB for 13.8-inch units with the SPI panel |

Sources: [ItsLucas/surface-laptop-7-ubuntu-kernel](https://github.com/ItsLucas/surface-laptop-7-ubuntu-kernel)
(GPL-2.0, series r15 for Ubuntu 7.3), [1Martin11/surface-laptop-7-fedora](https://github.com/1Martin11/surface-laptop-7-fedora)
(`build/patches-upstream`), [ProgrammerIn-wonderland/ELLX-Kernel](https://github.com/ProgrammerIn-wonderland/ELLX-Kernel).
All kernel patches are GPL-2.0 like the kernel; the device tree sources are BSD-3-Clause.

The 13.8-inch model ships with one of two touch panels (the supplier decides): an I2C
one, which the default DTB describes, or an SPI one wired like the 15-inch model, which
`romulus13-spi-touch` describes. Both 13.8-inch device trees are untested by us so far.
The 15-inch tree is what ItsLucas's users run daily.

## Updating the patch set

1. Fetch the new series from ItsLucas (`patches/series` there) into `patches/`, keep
   our `0100` and `0101` at the end.
2. Check it against the pinned kernel: extract `linux-<version>` and run the loop from
   `build.sh` step 2 with `--dry-run`. Nothing is fuzzed; fix rejects by hand.
3. Bump `PKG_REVISION` (same kernel) or `KERNEL_VERSION` + `KERNEL_SHA256` (new kernel).
4. Push: the workflow builds on `arm64-sl7` and `main`. Run it by hand with `publish`
   to attach the packages to the `kernel-sl7-<version>-<revision>` release.

## Testing on a laptop that already runs Ubuntu

```
sudo apt install ./linux-image-7.3.0-sl7_7.3-1~couchliteos_arm64.deb
```

The packages install the DTBs under `/usr/lib/linux-image-7.3.0-sl7/qcom/`. Boot the
new kernel with its DTB (GRUB `devicetree`, or copy it where the existing setup loads
DTBs from). Then:

```
dmesg | grep -iE 'spi-hid|i2c_hid|hid-over|gtch|touch'
libinput list-devices
```

A 13.8-inch unit with no touch events on the default DTB tries
`x1e80100-microsoft-romulus13-spi-touch.dtb` next.
