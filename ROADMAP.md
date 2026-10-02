# Roadmap

CouchLiteOS should boot straight to the couch on any common x86-64 PC or Intel
Mac from about 2012 on, with no per-machine setup: from the power button to a
game with only a controller or the TV remote.

Plans change; the [releases](https://github.com/Dudiebug/couchliteos/releases)
say what actually shipped. Ideas and votes are welcome as
[feature requests](https://github.com/Dudiebug/couchliteos/issues/new?template=feature_request.yml).

## Next

- **A TV interface.** Your games first, with cover art, a bar along the bottom
  that always shows what each button does, sounds and small notifications (a
  controller connected, an update ready, the gaming PC online), and a return to
  the game you just played.
- **Themes**, including your own colors.
- **Faster boot** on installed systems.
- **Undo an update.** Every update saves the current system first, so you can go
  back with the controller if the new version misbehaves.
- **Type from your phone.** Scan a QR code and type passwords and searches on
  the phone instead of the on-screen keyboard.
- **Controller management**: every connected pad with its battery, player order,
  identify and test buttons, and an A/B and X/Y swap for Nintendo layouts.
- **Per-game stream settings**: performance, balanced or quality per game.
- **Keyboard tips** next to the controller hints on every screen.

## Later

- Profiles: each person gets their own recent games, theme and controller layout.
- Parental controls: a PIN for Settings or chosen games, and play-time limits.
- One-press play: wake a sleeping gaming PC, wait for it, then start the game.
- A connection test that suggests stream settings.
- A screenshot button.

## Hardware

- A compatibility table built from user reports (PC, graphics, ISO, result).
- Hardware video decoding checked per graphics family, so each PC gets the best
  decoder by default.
- Secure Boot with the NVIDIA and Broadcom Wi-Fi drivers.
- NVIDIA RTX 50 series once Debian ships a driver that supports it.
- Laptops with two graphics chips: choose which one drives the screen.
