#!/usr/bin/env python3
"""Draw CouchLiteOS's XMB icons: flat 2D silhouettes, rendered like the PS3's.

Each icon is a 24x24 silhouette (BODY below). The same silhouette is painted three times: a soft
shadow under it, a silver gradient body, and a gloss over its top half, so a flat shape reads as
a polished 3D one. Details "engraved" into a shape use the {ink} colour. No logo of anyone else's
is drawn here: a website gets a glyph for what it is (film, music, sport...), and the interface
writes the service's name under it.

    tools/make-icons.py            write overlay/usr/share/couchliteos/icons/*.svg
    tools/make-icons.py --check    exit 1 when a written icon differs from this script
"""

from __future__ import annotations

import math
import pathlib
import sys

OUT = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/icons"
INK = "#2b3850"
STYLE = 'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"'
HEAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="128" height="128">'
    "<defs>"
    '<linearGradient id="m" gradientUnits="userSpaceOnUse" x1="0" y1="2" x2="0" y2="22">'
    '<stop offset="0" stop-color="#ffffff"/><stop offset=".5" stop-color="#e2e7ee"/>'
    '<stop offset="1" stop-color="#97a3b5"/></linearGradient>'
    '<linearGradient id="g" gradientUnits="userSpaceOnUse" x1="0" y1="2" x2="0" y2="11.5">'
    '<stop offset="0" stop-color="#ffffff" stop-opacity=".85"/>'
    '<stop offset="1" stop-color="#ffffff" stop-opacity="0"/></linearGradient>'
    '<clipPath id="c"><rect x="0" y="0" width="24" height="11.2"/></clipPath>'
    '<filter id="s" x="-.2" y="-.2" width="1.4" height="1.4"><feGaussianBlur stdDeviation=".6"/></filter>'
    "</defs>"
)


def n(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def circle_path(cx: float, cy: float, r: float) -> str:
    return f"M{n(cx - r)} {n(cy)}a{n(r)} {n(r)} 0 1 0 {n(2 * r)} 0a{n(r)} {n(r)} 0 1 0 {n(-2 * r)} 0z"


def gear() -> str:
    points = []
    for tooth in range(8):
        centre = tooth * 45
        for angle, radius in ((-17, 7.2), (-10, 9.8), (10, 9.8), (17, 7.2)):
            a = math.radians(centre + angle)
            points.append(f"{n(12 + radius * math.cos(a))} {n(12 + radius * math.sin(a))}")
    return (f'<path stroke="none" fill-rule="evenodd" d="M{"L".join(points)}z{circle_path(12, 12, 3.2)}"/>')


def sparkle(cx: float, cy: float, r: float) -> str:
    c = f"{n(cx)} {n(cy)}"
    return (f"M{n(cx)} {n(cy - r)}Q{c} {n(cx + r)} {n(cy)}Q{c} {n(cx)} {n(cy + r)}"
            f"Q{c} {n(cx - r)} {n(cy)}Q{c} {n(cx)} {n(cy - r)}z")


def frame(x: float, y: float, w: float, h: float, r: float, inset: float, top: float | None = None) -> str:
    """A solid rounded frame with a see-through 'glass' inside (a screen, a window)."""
    top = inset if top is None else top
    outer = (f"M{n(x + r)} {n(y)}h{n(w - 2 * r)}a{n(r)} {n(r)} 0 0 1 {n(r)} {n(r)}v{n(h - 2 * r)}"
             f"a{n(r)} {n(r)} 0 0 1 {n(-r)} {n(r)}h{n(-(w - 2 * r))}a{n(r)} {n(r)} 0 0 1 {n(-r)} {n(-r)}"
             f"v{n(-(h - 2 * r))}a{n(r)} {n(r)} 0 0 1 {n(r)} {n(-r)}z")
    gx, gy, gw, gh = x + inset, y + top, w - 2 * inset, h - top - inset
    inner = f"M{n(gx)} {n(gy)}v{n(gh)}h{n(gw)}v{n(-gh)}z"
    return (f'<path stroke="none" fill-rule="evenodd" d="{outer}{inner}"/>'
            f'<rect stroke="none" opacity=".32" x="{n(gx)}" y="{n(gy)}" width="{n(gw)}" height="{n(gh)}"/>')


CONTROLLER = (
    '<path stroke="none" fill-rule="evenodd" d="M7 6.5h10a5.2 5.2 0 0 1 5.1 6.2l-.9 4.4a2.6 2.6 0 0 1-4.6 1.1'
    "L14.3 15.5H9.7l-2.3 2.7a2.6 2.6 0 0 1-4.6-1.1l-.9-4.4A5.2 5.2 0 0 1 7 6.5z"
    "M6.2 9.4h1.6V11h1.6v1.6H7.8v1.6H6.2v-1.6H4.6V11h1.6z"
    + circle_path(16.3, 10.4, 0.95) + circle_path(18.2, 12.4, 0.95) + '"/>'
)
MONITOR_STAND = '<path stroke="none" d="M10.4 17h3.2l.6 2.4H9.8z"/><rect stroke="none" x="7.5" y="19.2" width="9" height="1.7" rx=".85"/>'

BODY: dict[str, str] = {
    # ---- the cross: categories, power, settings
    "power": '<path fill="none" d="M12 3v8.5"/><path fill="none" d="M7 5.9a8 8 0 1 0 10 0"/>',
    "gear": gear(),
    "television": frame(2, 6, 20, 14, 2.4, 2.2)
    + '<path fill="none" stroke-width="1.7" d="M8.5 2.5 12 5.5l3.5-3"/>',
    "game-controller": CONTROLLER,
    "squares-four": "".join(f'<rect stroke="none" x="{x}" y="{y}" width="7.6" height="7.6" rx="1.8"/>'
                            for y in (3, 13.4) for x in (3, 13.4)),
    "globe": '<circle fill="none" cx="12" cy="12" r="8.8"/><path fill="none" stroke-width="1.6" d="M3.6 9h16.8'
             "M3.6 15h16.8M12 3.2c-2.6 2.4-3.9 5.4-3.9 8.8s1.3 6.4 3.9 8.8M12 3.2c2.6 2.4 3.9 5.4 3.9 8.8"
             's-1.3 6.4-3.9 8.8"/>',
    # ---- applications and settings entries
    "browser": frame(2, 3.5, 20, 17, 2.4, 1.8, 5)
    + '<circle fill="none" stroke-width="1.4" cx="12" cy="14.2" r="3.3"/>'
      '<path fill="none" stroke-width="1.2" d="M8.7 14.2h6.6M12 10.9c1 .9 1.4 2 1.4 3.3s-.4 2.4-1.4 3.3'
      'c-1-.9-1.4-2-1.4-3.3s.4-2.4 1.4-3.3z"/>'
      f'<path stroke="none" fill="{{ink}}" d="{circle_path(5, 6, 0.8)}{circle_path(7.6, 6, 0.8)}"/>',
    "app-window": frame(2, 3.5, 20, 17, 2.4, 1.8, 5)
    + f'<path stroke="none" fill="{{ink}}" d="{circle_path(5, 6, 0.8)}{circle_path(7.6, 6, 0.8)}"/>',
    "shield": '<path stroke="none" d="M12 2.5l8 3.2v5.8c0 4.8-3.4 8.6-8 10-4.6-1.4-8-5.2-8-10V5.7z"/>'
              '<path fill="none" stroke="{ink}" stroke-width="2" d="M8.5 12l2.4 2.4 4.6-4.8"/>',
    "wifi-high": '<path fill="none" stroke-width="2.4" d="M2.5 9.2a14 14 0 0 1 19 0M5.6 12.7a9.5 9.5 0 0 1 12.8 0'
                 'M8.7 16.1a5 5 0 0 1 6.6 0"/><circle stroke="none" cx="12" cy="19.3" r="1.6"/>',
    "terminal-window": '<rect stroke="none" x="2" y="3.5" width="20" height="17" rx="2.4"/>'
                       '<path fill="none" stroke="{ink}" stroke-width="2" d="M6.5 9l3.5 3-3.5 3M12.5 15.5h5"/>',
    "stethoscope": '<path fill="none" stroke-width="2" d="M5.5 3.5H4.5v5a4.5 4.5 0 0 0 9 0v-5h-1'
                   'M9 13v2.5a4.8 4.8 0 0 0 9.6 0V13"/><circle stroke="none" cx="18.6" cy="10.8" r="2.4"/>',
    "speaker-high": '<path stroke="none" d="M3 9h3.8L12 4.8v14.4L6.8 15H3a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1z"/>'
                    '<path fill="none" stroke-width="2" d="M15.5 9a4 4 0 0 1 0 6M18.2 6.4a7.6 7.6 0 0 1 0 11.2"/>',
    "monitor": frame(2, 3.5, 20, 13.8, 2.2, 1.8) + MONITOR_STAND,
    "desktop": frame(2, 3.5, 20, 13.8, 2.2, 1.8) + MONITOR_STAND
    + '<path fill="none" stroke-width="1.8" d="M7.5 10.4h8M13 7.9l2.5 2.5-2.5 2.5"/>',
    "desktop-tower": '<rect stroke="none" x="12.5" y="2.5" width="8.5" height="19" rx="1.6"/>'
                     '<path fill="none" stroke="{ink}" stroke-width="1.4" d="M15 6.5h3.5M15 9.5h3.5"/>'
                     f'<path stroke="none" fill="{{ink}}" d="{circle_path(16.75, 17.5, 1)}"/>'
                     + frame(2, 6, 9, 9, 1.4, 1.4)
                     + '<path stroke="none" d="M5.7 15h1.6v2.4H5.7z"/>'
                       '<rect stroke="none" x="3.8" y="17.2" width="5.4" height="1.5" rx=".75"/>',
    "paint-brush": '<path stroke="none" d="M20.8 2.8c-3.3 1-8.6 6-10.8 9.2l2 2c3.2-2.2 8.2-7.5 9.2-10.8z"/>'
                   '<path stroke="none" d="M9.3 12.7c-2.2 0-3.8 1.6-3.8 3.8 0 1.5-.9 2.6-3.4 3.1 2.2 1.7 6.6 1.7'
                   ' 8.3-.6 1.1-1.5 1-3.6.9-4.3z"/>',
    "bluetooth": '<path fill="none" stroke-width="2.2" d="M6.8 7.3 17 16.4l-5 4.6V3l5 4.6L6.8 16.7"/>',
    "moon": '<path stroke="none" d="M20.5 14.6A8.8 8.8 0 0 1 9.4 3.5 8.8 8.8 0 1 0 20.5 14.6z"/>',
    "broadcast": '<circle stroke="none" cx="12" cy="12" r="2.3"/><path fill="none" stroke-width="2"'
                 ' d="M8.1 8.1a5.5 5.5 0 0 0 0 7.8M15.9 8.1a5.5 5.5 0 0 1 0 7.8M5 5a10 10 0 0 0 0 14'
                 'M19 5a10 10 0 0 1 0 14"/>',
    "download": '<path stroke="none" d="M10.6 3h2.8v7.5h3.4L12 15.6l-4.8-5.1h3.4z"/>'
                '<path fill="none" d="M4 15.5v3.5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3.5"/>',
    "arrows-clockwise": '<path fill="none" d="M19.3 8.5A7.8 7.8 0 0 0 5.2 7.2M4.7 15.5a7.8 7.8 0 0 0 14.1 1.3"/>'
                        '<path stroke="none" d="M20.8 3.6v6.3h-6.3zM3.2 20.4v-6.3h6.3z"/>',
    "keyboard": '<rect stroke="none" x="1.5" y="5.5" width="21" height="13" rx="2.2"/>'
                '<path stroke="none" fill="{ink}" d="' + "".join(
                    f"M{n(x)} {n(y)}h1.8v1.8h-1.8z" for y in (8.3, 11.1) for x in (4, 6.9, 9.8, 12.7, 15.6, 18.5))
                + 'M7.3 14.2h9.4v1.8H7.3z"/>',
    "magic-wand": '<path stroke="none" d="M3.2 19.4 14.6 8l2 2L5.2 21.4a1.4 1.4 0 0 1-2-2z"/>'
                  f'<path stroke="none" d="{sparkle(18.5, 4.5, 2.6)}{sparkle(20.2, 11.3, 1.6)}{sparkle(11.2, 3.6, 1.6)}"/>',
    "lifebuoy": f'<path stroke="none" fill-rule="evenodd" d="{circle_path(12, 12, 9.2)}{circle_path(12, 12, 4.4)}"/>'
                '<path fill="none" stroke="{ink}" stroke-width="2.6" stroke-linecap="butt"'
                ' d="M8.9 8.9 5.5 5.5M15.1 15.1l3.4 3.4M15.1 8.9l3.4-3.4M8.9 15.1l-3.4 3.4"/>',
    "plus-circle": '<path stroke="none" fill-rule="evenodd" d="' + circle_path(12, 12, 9.2)
                   + 'M10.9 7.3h2.2v3.6h3.6v2.2h-3.6v3.6h-2.2v-3.6H7.3v-2.2h3.6z"/>',
    "stack": '<path stroke="none" d="M12 2.8l9.2 4.7-9.2 4.7-9.2-4.7z"/>'
             '<path fill="none" stroke-width="2" d="M3 12l9 4.6 9-4.6M3 16.4 12 21l9-4.6"/>',
    "play-circle": '<path stroke="none" fill-rule="evenodd" d="' + circle_path(12, 12, 9.2)
                   + 'M10 7.9v8.2l6.4-4.1z"/>',
    # ---- websites: what a service is, never its logo (the interface writes its name)
    "film": '<path stroke="none" d="M2.3 6.4 19.8 2.5l.7 3-17.5 3.9z"/>'
            '<rect stroke="none" x="2.5" y="10.2" width="19" height="10.6" rx="1.6"/>'
            '<path fill="none" stroke="{ink}" stroke-width="1.5" stroke-linecap="butt"'
            ' d="M6.3 5.5l2.1 2.6M10.8 4.5l2.1 2.6M15.3 3.5l2.1 2.6M2.5 13.2h19"/>',
    "music-note": '<path fill="none" stroke-width="2" d="M9.6 17V6.4l10.4-2.4V14.4"/>'
                  '<path stroke="none" d="M8.6 5.6l12.4-2.9v3.4L8.6 9z"/>'
                  '<ellipse stroke="none" cx="6.7" cy="17.4" rx="3.2" ry="2.4" transform="rotate(-20 6.7 17.4)"/>'
                  '<ellipse stroke="none" cx="17.1" cy="14.8" rx="3.2" ry="2.4" transform="rotate(-20 17.1 14.8)"/>',
    "trophy": '<path stroke="none" d="M7 3h10v5.6a5 5 0 0 1-10 0z"/>'
              '<path fill="none" stroke-width="1.9" d="M7 5H4.4v1.4A3.6 3.6 0 0 0 7.6 10M17 5h2.6v1.4a3.6 3.6 0 0 1-3.2 3.6"/>'
              '<path stroke="none" d="M10.9 13.4h2.2v3.8h-2.2zM7.6 17.2h8.8a1 1 0 0 1 1 1V21H6.6v-2.8a1 1 0 0 1 1-1z"/>',
    "newspaper": '<path stroke="none" d="M5 3.5h14.5a1.5 1.5 0 0 1 1.5 1.5v14a2.5 2.5 0 0 1-2.5 2.5H5'
                 'A2.5 2.5 0 0 1 2.5 19V8H5z"/>'
                 '<path stroke="none" fill="{ink}" d="M7.5 6.5h10v4.5h-10z"/>'
                 '<path fill="none" stroke="{ink}" stroke-width="1.4" d="M7.5 14h10M7.5 17h10"/>',
    "microphone": '<rect stroke="none" x="8.5" y="2.5" width="7" height="12" rx="3.5"/>'
                  '<path fill="none" stroke-width="2" d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5v3.5M8.5 21h7"/>',
    "cloud-game": '<path stroke="none" d="M7 19.5a5 5 0 0 1-.6-9.96A6.5 6.5 0 0 1 18.8 9a5.2 5.2 0 0 1-.8 10.5z"/>'
                  '<path fill="none" stroke="{ink}" stroke-width="1.6" d="M7.6 14.2h3.6M9.4 12.4V16"/>'
                  f'<path stroke="none" fill="{{ink}}" d="{circle_path(14.6, 13.4, 0.95)}{circle_path(16.6, 15.2, 0.95)}"/>',
    "image": '<rect stroke="none" x="2" y="4" width="20" height="16" rx="2.4"/>'
             f'<path stroke="none" fill="{{ink}}" d="M4.5 17.5l4.6-5.6 3.4 4 2.6-2.6 4.4 4.2z{circle_path(15.8, 8.6, 1.8)}"/>',
    "chat": '<path stroke="none" d="M4.5 3.5h15A2.5 2.5 0 0 1 22 6v9a2.5 2.5 0 0 1-2.5 2.5H10L5 21.5v-4H4.5'
            'A2.5 2.5 0 0 1 2 15V6a2.5 2.5 0 0 1 2.5-2.5z"/>'
            f'<path stroke="none" fill="{{ink}}" d="{circle_path(7.5, 10.5, 1.2)}{circle_path(12, 10.5, 1.2)}'
            f'{circle_path(16.5, 10.5, 1.2)}"/>',
    "envelope": '<rect stroke="none" x="2" y="4.5" width="20" height="15" rx="2.2"/>'
                '<path fill="none" stroke="{ink}" stroke-width="1.6" d="M3.5 6.5l8.5 6.5 8.5-6.5"/>',
    "bag": '<path fill="none" stroke-width="1.9" d="M8.5 8.5V6.5a3.5 3.5 0 0 1 7 0v2"/>'
           '<path stroke="none" d="M5 7.5h14l1.2 12a1.8 1.8 0 0 1-1.8 2H5.6a1.8 1.8 0 0 1-1.8-2z"/>',
    "video-camera": '<rect stroke="none" x="2" y="6" width="13.5" height="12" rx="2.2"/>'
                    '<path stroke="none" d="M16.8 10.4 22 7v10l-5.2-3.4z"/>',
    "document": '<path stroke="none" d="M6 2.5h8.5L19.5 7.5V19.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-15a2 2 0 0 1 2-2z"/>'
                '<path fill="none" stroke="{ink}" stroke-width="1.4" d="M14.2 2.8v4.9h4.9M7.5 12h9M7.5 15h9M7.5 18h6"/>',
}


def render(body: str) -> str:
    return (
        HEAD
        + f'<g fill="#000000" stroke="#000000" opacity=".5" filter="url(#s)" transform="translate(0 .7)" {STYLE}>'
        + body.format(ink="#000000") + "</g>"
        + f'<g fill="url(#m)" stroke="url(#m)" {STYLE}>' + body.format(ink=INK) + "</g>"
        + f'<g fill="url(#g)" stroke="url(#g)" clip-path="url(#c)" {STYLE}>' + body.format(ink="none") + "</g>"
        + "</svg>\n"
    )


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    if not check:
        OUT.mkdir(parents=True, exist_ok=True)
        for old in OUT.glob("*.svg"):
            if old.stem not in BODY:
                old.unlink()
    for name, body in sorted(BODY.items()):
        path = OUT / f"{name}.svg"
        text = render(body)
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
    extra = sorted(path.stem for path in OUT.glob("*.svg") if path.stem not in BODY)
    if stale or extra:
        print("icons out of date (run tools/make-icons.py):", " ".join(stale + extra), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
