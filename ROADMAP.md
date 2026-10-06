# Roadmap

CouchLiteOS should boot straight to the couch on any common x86-64 PC or Intel
Mac from about 2012 on, with no per-machine setup: from the power button to a
game with only a controller or the TV remote.

Plans change; the [releases](https://github.com/Dudiebug/couchliteos/releases)
say what actually shipped. Ideas and votes are welcome as
[feature requests](https://github.com/Dudiebug/couchliteos/issues/new?template=feature_request.yml).

## Next

0.3.0 brought the smaller download, the web browser picked at setup, the TV
interface, themes, a faster start, undoing an update, typing from your phone,
controller management and per-game stream settings; see its
[release notes](https://github.com/Dudiebug/couchliteos/releases/tag/v0.3.0).
Still to come from that work:

- **Keyboard tips** next to the controller hints on the remaining classic
  screens (the TV interface already shows both).
- **Per-game stream settings in the classic interface**, which today uses the
  settings chosen in the TV interface but cannot change them.
- **PlayStation light bar** in each player's colour.

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
