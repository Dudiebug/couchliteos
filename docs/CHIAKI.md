# chiaki-ng registration

1. Enable Remote Play on the PS4 or PS5 and keep the console on the same trusted
   network for initial registration.
2. Start chiaki-ng from the launcher and follow its upstream registration flow
   for the console generation and PlayStation account identifier.
3. Select Vulkan rendering and PipeWire audio. Test controller input and audio.
4. Exit and reboot; confirm the registered console remains visible.

chiaki-ng data persists below `/var/lib/couchliteos/home/.config`. Its pinned
upstream AppImage payload is extracted during the ISO build and runs on native
Wayland without runtime FUSE. System libva, OpenGL, Vulkan, hidraw, and PipeWire
support are included in the image.
Gamescope is installed from Debian 13 backports but disabled by default. Set
`gamescope = true` under `[chiaki-ng]` in
`/var/lib/couchliteos/config.ini` only when a display/compositor issue requires
it. HDR remains experimental and unclaimed.

The PlayStation is expected to remain directly reachable on the CouchLiteOS
local LAN. Installing Tailscale on CouchLiteOS does not make the console a
tailnet node or extend discovery to another site. No subnet routes are accepted
or advertised in v0.1.2; remote-console routing is a separately reviewed future
feature.
