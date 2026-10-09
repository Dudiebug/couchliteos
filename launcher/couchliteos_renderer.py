"""Which GTK renderer the TV interface asks for, before GTK starts (plain Python, no GTK).

GTK 4.16+ under Wayland prefers its Vulkan renderer. On a PC whose graphics have no Vulkan
driver (nouveau: Mesa 25.0 has no NVK for older NVIDIA cards; i915 before Broadwell; the old
radeon kernel driver) the only Vulkan device is llvmpipe, so the whole screen would be drawn
by the CPU while the same card's OpenGL driver is fast. There the TV asks for GSK_RENDERER=ngl
(OpenGL). A PC whose card has a real Vulkan driver keeps GTK's own choice.

i915 always takes OpenGL: the Mesa Vulkan driver for it (anv) starts at Broadwell, the one for
older chips (hasvk) is incomplete, and both ICD files are installed on every PC, so their
presence tells nothing about this one. OpenGL is hardware on every i915 chip, and it was
GTK's own default renderer until 4.14. nouveau takes OpenGL even with NVK installed (it does
not drive the older cards).

A GSK_RENDERER that is already set (couchliteos-session's cairo retry, a test) is never changed.
The choice and why are written to $RUN_DIR/renderer for the support export.
"""

from __future__ import annotations

import pathlib
from collections.abc import MutableMapping

DRM = pathlib.Path("/sys/class/drm")
ICD_DIRS = (pathlib.Path("/usr/share/vulkan/icd.d"), pathlib.Path("/etc/vulkan/icd.d"))
GL = "ngl"
# DRM driver -> the Vulkan ICD file names (before the first ".") that drive it on the GPU.
VULKAN_ICDS = {
    "amdgpu": ("radeon_icd", "amd_icd"),
    "xe": ("intel_icd",),
    "nvidia": ("nvidia_icd",),
}
# Drivers with a hardware OpenGL driver in Mesa (or NVIDIA's own): OpenGL there is worth asking for.
GL_DRIVERS = frozenset({"amdgpu", "radeon", "nouveau", "i915", "xe", "nvidia"})


def drm_drivers(root: pathlib.Path = DRM) -> tuple[str, ...]:
    """The kernel drivers of the cards in /sys/class/drm (card0, card1; not their outputs)."""
    found = []
    try:
        cards = sorted(root.glob("card[0-9]*"))
    except OSError:
        return ()
    for card in cards:
        if "-" in card.name:  # card0-HDMI-A-1: an output
            continue
        try:  # device/uevent: DRIVER=nouveau (a plain file, unlike the device/driver link)
            lines = (card / "device" / "uevent").read_text(errors="replace").splitlines()
        except OSError:
            continue
        name = next((line[7:].strip() for line in lines if line.startswith("DRIVER=")), "")
        if name and name not in found:
            found.append(name)
    return tuple(found)


def icd_names(dirs: tuple[pathlib.Path, ...] = ICD_DIRS) -> frozenset[str]:
    """The installed Vulkan ICDs, as their file names before the first "." (radeon_icd)."""
    names = set()
    for directory in dirs:
        try:
            for item in directory.iterdir():
                if item.name.endswith(".json"):
                    names.add(item.name.split(".", 1)[0])
        except OSError:
            continue
    return frozenset(names)


def choose(drivers: tuple[str, ...], icds: frozenset[str]) -> tuple[str | None, str]:
    """(GSK_RENDERER to set or None for GTK's own choice, why)."""
    if not drivers:
        return None, "no graphics card found"
    for driver in drivers:
        if any(icd in icds for icd in VULKAN_ICDS.get(driver, ())):
            return None, f"{driver} has a Vulkan driver"
    gl = [driver for driver in drivers if driver in GL_DRIVERS]
    if gl:
        return GL, f"no GPU Vulkan driver for {', '.join(drivers)}: OpenGL on {gl[0]}"
    return None, f"no hardware OpenGL driver for {', '.join(drivers)}"


def prepare(environ: MutableMapping[str, str], run: pathlib.Path | None = None,
            drm: pathlib.Path = DRM, icd_dirs: tuple[pathlib.Path, ...] = ICD_DIRS) -> str:
    """Set GSK_RENDERER in `environ` when it is unset and OpenGL is the better choice; write
    `run`/renderer (best effort). Returns the renderer GTK will be asked for ("" = GTK's own)."""
    preset = environ.get("GSK_RENDERER", "").strip()
    if preset:
        chosen, why = preset, "GSK_RENDERER set before the start"
    else:
        picked, why = choose(drm_drivers(drm), icd_names(icd_dirs))
        chosen = picked or ""
        if picked:
            environ["GSK_RENDERER"] = picked
    if run is not None:
        try:
            temporary = run / "renderer.tmp"
            temporary.write_text(f"{chosen or 'default'}\n{why}\n")
            temporary.replace(run / "renderer")
        except OSError:
            pass
    return chosen
