# TV interface guidelines

How screens in CouchLiteOS look and behave. The TV interface (`launcher/couchliteos-tv.py`,
GTK 4) draws every screen; new screens follow these rules so the box feels like one product.

## Every screen is drawn by the TV interface

- New screens are written once, against curses, in `couchliteos-launcher.py` (`--screen NAME`).
  The TV runs them with `--bridge` and draws their frames itself (`couchliteos_uibridge`,
  `couchliteos_gtk_screen`). Do not open a terminal (foot) window for anything new.
- Foot is only the safety net: a screen that exits before drawing its first frame reopens there.
- Lists are drawn as `ListView` rows; write a setting as `NAME: VALUE` (value at most 24
  characters, no full stop) and the TV shows the value on the right in the accent colour.
- The on-screen keyboard is GTK too (`couchliteos_gtk_osk`), docked along the bottom 40%.

## Look

- Colours come only from the theme (`couchliteos_theme`): background, surface, text, muted,
  accent, focus. Never hard-code a colour; HIGH CONTRAST and TERMINAL must still work.
- Text is upper case, plain words, no jargon. Say where things are as `SETTINGS > PAGE > ROW`.
- Sizes scale from the picture height (`tvlayout.Layout.px`). Text is at least 28 px at 1080p
  and nothing may be cut off at 720p; `tests/tv-headless.sh` checks both.
- One focused item at a time, filled with the accent colour. The bottom hint says what A / B
  (and any other button) does, with the pad's own letters (`couchliteos_quick.prompt`).
- Keep the PS3 home (XMB): categories across, items down, the wave behind. Do not redraw brand
  logos; app icons come from their own packages.

## Space

- Do not cram. A screen holds what fits at 720p; longer content goes in pages (WHAT'S NEW),
  scrolls with ▲ / ▼ markers, or moves to a sub-page.
- A status line is one line. Adding a line to the home screen means reserving it in the layout,
  or the window outgrows the screen.

## Motion and cost

- Animations are short (140–220 ms), retarget instead of queueing, and respect Settings >
  APPEARANCE > MOTION and the automatic REDUCED mode on slow PCs.
- Nothing draws while an app or stream is in front, the screen is blank, or the box rests.
- Slow work (disk, network, D-Bus, `settings.refresh`) runs off the main thread; the screen
  draws the last known values first.

## Controls

- A / Cross / Enter selects, B / Circle / Esc goes back, Y / Square deletes in text, Home goes
  home from anywhere and never leaves the user stuck.
- Every screen can be left with B. A screen showing an error needs at most one more B.
