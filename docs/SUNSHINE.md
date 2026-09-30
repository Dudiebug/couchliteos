# Sunshine and Moonlight

1. Install and configure Sunshine on the gaming PC (the reference host is a
   Ryzen 5 7600X / RTX 5070 Linux PC) using Sunshine's official documentation.
2. Keep the host and appliance on wired gigabit Ethernet where possible.
3. Start Moonlight, add the host by name or address, and complete the PIN on the
   Sunshine web UI. The host address is never built into CouchLiteOS. On a live
   USB without persistence the pairing is forgotten at power-off; install
   CouchLiteOS or add a persistence stick to keep it (see
   [INSTALL.md](INSTALL.md)).
4. Start at 1920x1080, 60 FPS, automatic codec, and fullscreen. Confirm hardware
   decoding in the Moonlight statistics overlay. When the display driver is
   nouveau, Moonlight starts with software H.264 instead (see
   [HARDWARE.md](HARDWARE.md)).
5. Then test 1080p120, 1440p60, and finally 4K60 SDR if the display path allows.

Moonlight Qt stores its host list, last-used host, pairing material, and stream
settings below `/var/lib/couchliteos/home/.config`. Exiting Moonlight returns to
the launcher. Non-zero exits are logged and retried by systemd up to three
times. NetworkManager restores DHCP after link loss; Moonlight's own reconnect
UI handles an interrupted stream.

Moonlight Qt runs through XWayland (`QT_QPA_PLATFORM=xcb`) under Cage, because
the pinned Moonlight Qt build includes the X11 (XCB) Qt platform plugin but no
Wayland one. A direct-KMS Moonlight client is not enabled because feature parity
and reliability have not been verified on physical hardware; this avoids falsely
claiming that path is ready.
