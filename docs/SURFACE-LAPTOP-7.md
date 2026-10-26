# Surface Laptop 7 (Snapdragon X)

`couchliteos-<version>-snapdragon-arm64.iso` is an experimental arm64 live image for the
Microsoft Surface Laptop 7 (Snapdragon X, 13.8 inch and 15 inch). It runs from a USB stick
with persistence; it does not install to the internal disk, and Windows stays as it is.

It is a test image: not every part of the laptop works yet, and it is not a release asset.

## Before the first boot

1. **Have your BitLocker recovery key.** Windows on the Surface usually encrypts the disk,
   and changing the Secure Boot setting makes Windows ask for the recovery key at its next
   start. Find it at https://account.microsoft.com/devices/recoverykey (or wherever your
   organisation keeps it) and keep it on another device.
2. **Turn Secure Boot off** in the Surface UEFI (shut down, hold Volume Up, press Power,
   release Volume Up when the Surface logo shows). The image's kernel is not signed, and
   GRUB does not load a device tree with Secure Boot on.
3. **Write the ISO to a USB stick**: Rufus in DD image mode, `dd`, or Ventoy.
4. Boot from the stick: shut down, hold Volume Down, press Power.

GRUB has no keyboard on this laptop (Ventoy's menu does), so the first entry starts by
itself after 3 seconds. It picks the device tree by the laptop's SMBIOS SKU:

| SKU | Device tree |
|---|---|
| `Surface_Laptop_7th_Edition_2037` (and `_For_Business_2037`): 15 inch | `x1e80100-microsoft-romulus15.dtb` |
| anything else, `Surface_Laptop_7th_Edition_2036`: 13.8 inch | `x1e80100-microsoft-romulus13-spi-touch.dtb` |

13.8-inch laptops come with one of two touchscreens. The default tree is for the SPI one;
a laptop with the I2C touchscreen can pick "13.8-inch, I2C touchscreen" from the menu
(through Ventoy, or with a USB keyboard). Windows shows which one you have in Device
Manager: `MSHW0460` (SPI) or `MSHW0468` (I2C) under Human Interface Devices.

## Firmware

Debian ships the firmware for the GPU (Adreno X1-85), the Wi-Fi (WCN7850) and the audio
topology. The laptop's own signed firmware (audio and compute DSPs, the GPU's zap shader,
video decoding, battery) is Microsoft's and cannot be shipped in the image; a later version
will copy it from the Windows drivers on the laptop. Until then audio, battery reporting
and hardware video decoding may not work.

## What to expect

Not yet in the arm64 image: Moonlight (no Linux arm64 build is published yet; chiaki-ng
and Remote Desktop are there), Google Chrome (no arm64 Linux build; Firefox is there),
automatic app updates.

## Credits

The kernel patches come from ItsLucas (surface-laptop-7-ubuntu-kernel), fQwQf and
1Martin11 (the 13.8-inch touchscreen), and ELLX; the Ubuntu setup notes from
bryce-hoehn (linux-surface-laptop-7). See [kernel/sl7/README.md](../kernel/sl7/README.md).
