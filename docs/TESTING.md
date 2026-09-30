# Testing

## Automated source tests

```bash
make test
```

These tests validate shell/Python syntax, service references, no obvious secret
patterns, fixed artifact versions/URLs, default-deny Tailscale/USB-IP settings, and a
fake-sysfs USB allowlist case. Focused tests also cover display-mode parsing,
valid resolution/refresh pairs, missing connectors/modes, changed display
identity, malformed configuration preservation, Settings navigation, controller
mapping, support redaction, removable-media policy, safe archive streaming,
atomic copy, checked unmount ordering, failed-copy cleanup, Bluetooth protocol
validation, BlueZ object parsing, pairing-agent callbacks, and Bluetooth UI
recovery. They do not require a tailnet or Bluetooth adapter.
The v0.1.11 cases also cover manifest isolation and atomic state, configured-app
argv/environment construction, separate Foot wrapping, setup completion, and
buffered-keyboard navigation and payload validation.

The v0.1.12 cases cover the build-profile split and Remote Desktop:

- profile layout, Intel-only packages confined to `intel`, no NVIDIA package in
  any list, the `imac2013` DKMS/blacklist/`hid_apple`/`applesmc` checks
- RDP connection storage: validation, 0640 atomic writes, invalid or
  password-bearing sections isolated, unreadable stores never overwritten,
  symlinks refused
- FreeRDP argv: the password is never an argument, `/from-stdin:force`, the
  pinned `/cert:deny,fingerprint:sha256:` value, resolution/fullscreen/audio/
  clipboard options, Wayland-only client environment
- certificate probe against a real local TLS listener, negotiation parsing,
  trust on first use, and a blocked changed fingerprint
- password handoff (0600, owner-checked, symlink-safe), the root helper's
  save/delete/stage requests, and a root-owned 0600 secret store
- session runner: password written to stdin only, crash restart keeps the
  session, authentication failures are final and forget the password, close
  requests, OnFailure cleanup
- support-archive redaction of FreeRDP `/p:`, `/gp:`, gateway, and JSON
  passwords
- launcher button persistence (label, position, shortcut) across a fresh load,
  controller shortcuts, and the spare face button opening the keyboard

The v0.1.13 cases cover the two release profiles and hardware detection:

- profile layout and inheritance: `nvidia` builds on `general`
  (`PROFILE_BASE`), only `nvidia` may list proprietary NVIDIA packages, and
  each release profile's required and forbidden image paths
- `couchliteos-hwdetect` against a fake sysfs (`tests/test_hwdetect.py`):
  supported and unsupported NVIDIA GPUs on the NVIDIA ISO, the Basic Graphics
  option, Kepler on both ISOs, Intel, AMD, no GPU, Broadcom chips on and off
  `wl`'s list, Apple DMI, the nouveau fallback load, decode hints, and
  compositor environment
- the Basic Graphics GRUB and isolinux entries, and software H.264 on nouveau
  in the application runner
- every negative check in the static suite now fails the suite; a negated
  command (`! cmd`) never trips `set -e`, so earlier versions could not fail on
  them

`make test` must run as root on a machine that already has a `couchliteos`
account (for example the lab build VM), because the atomic writers assign that
account. `python3 tools/mutants.py` also checks that the tests catch removing
`/from-stdin:force`, accepting any certificate, allowing IPv6 literals,
retrying authentication failures, keeping the password after a final failure,
connecting despite a changed certificate, and dropping the FreeRDP redaction.

## QEMU ISO smoke test

```bash
sudo apt install qemu-system-x86 ovmf
make qemu-smoke                    # general ISO
make qemu-smoke PROFILE=nvidia     # NVIDIA ISO
```

Every QEMU target takes `PROFILE=` (or an explicit `ISO=`). Without KVM (for
example inside a software-emulated build VM), set
`COUCHLITEOS_QEMU_TIMEOUT_SCALE=N` (1-99, default 1) to stretch every host-side
wait, the installer keystroke pauses, and the in-guest waits by the same factor;
the guest receives the factor through a QEMU fw_cfg entry.

The QEMU tests also run from Git Bash on a Windows host with QEMU for Windows
(no KVM). With Windows Hypervisor Platform enabled, set
`COUCHLITEOS_QEMU_ACCEL_ARGS="-accel whpx,kernel-irqchip=off -cpu max"`;
without it, QEMU emulates in software. Also set
`COUCHLITEOS_OVMF_CODE`/`COUCHLITEOS_OVMF_VARS` to QEMU's
`edk2-x86_64-code.fd`/`edk2-i386-vars.fd`, `TMPDIR` to a Windows-style
path such as `C:/qemu-tmp`, and `COUCHLITEOS_QEMU_MONITOR_PORT` to a free local
TCP port (Windows Python has no Unix-domain sockets). The persistence test
accepts `COUCHLITEOS_QEMU_PERSISTENCE_IMAGE` (a pristine image made by its own
`mke2fs` command on a Linux host) and `COUCHLITEOS_BSDTAR` (Windows'
`C:/Windows/System32/tar.exe`) when `mke2fs` and `xorriso` are unavailable.

The script boots the profile's ISO (for example
`build/out/couchliteos-0.1.13-amd64.iso`) with serial
output, a virtual Ethernet NIC, and UEFI when OVMF is available. Success means
the boot reached the CouchLiteOS launcher service marker. QEMU does not prove
Intel VA-API, physical Bluetooth behavior, display audio, gamepad, USB/IP
hardware, or streaming.

The test boots through UEFI rather than injecting the kernel directly. It must
reach the launcher marker without depending on DHCP. GRUB also mirrors output
to a 115200-baud serial console so bootloader failures appear in CI logs. The
test creates a disposable writable copy of `OVMF_VARS`; supplying OVMF code
without its variable store is not a valid UEFI test setup.
On systems where OVMF is unpacked outside the standard locations, set both
`COUCHLITEOS_OVMF_CODE` and `COUCHLITEOS_OVMF_VARS` to readable firmware files.

On failure, CI retains the serial log and a QEMU framebuffer screenshot. The
test also extracts `/boot/grub/grub.cfg` from the ISO and verifies the appliance
timeout and serial settings before starting the VM.

Success requires `COUCHLITEOS_LAUNCHER_READY`, emitted only after foot presents
the full-screen curses launcher. A QEMU-only firmware flag then starts the three
production application services and requires `COUCHLITEOS_APP_STARTED moonlight`,
`COUCHLITEOS_APP_STARTED chiaki-ng`, `COUCHLITEOS_APP_STARTED firefox`, and a
five-second `google-chrome-ready` marker from the generic application runner
after each real application remains alive for five seconds. It then starts a
Remote Desktop session through the real path unit against a local TLS endpoint:
`sdl-freerdp3` must start under Cage with `SDL_VIDEO_DRIVER=wayland`, the session
must fail through the systemd restart policy into the OnFailure cleanup, and the
test password must not appear in the journal or `/var/log/couchliteos`. It also
saves a password through the root helper and checks its root-only 0600 file
(`COUCHLITEOS_SMOKE_RDP_READY`). It also proves the
Bluetooth control service handles an absent adapter and survives restarts of
BlueZ and its own service without changing the launcher or audio-session PID
and restart counts.
It checks that both hardware detection stages ran, that
`systemd-modules-load` did not fail, and that the GPU driver and decode hints
exist (`COUCHLITEOS_SMOKE_HWDETECT_READY`); on the NVIDIA ISO, where QEMU has
no NVIDIA GPU, the proprietary modules must be blacklisted and unloaded.
Launcher output is
mirrored to the boot console and remains available in
`/var/log/couchliteos/launcher.log`.

## Installed-system QEMU smoke test

```bash
make qemu-install-smoke
```

This test starts in OVMF, boots the built ISO as removable media, selects the
ISO's visible `Install CouchLiteOS` GRUB entry, and adds only a test-preseed URL
to that entry. It installs to a disposable 32 GiB UEFI virtual disk, boots that
disk without the ISO, writes a configuration marker, cold-boots the disk again,
and requires the marker and `COUCHLITEOS_LAUNCHER_READY`. Passing an extracted
kernel or initrd directly to QEMU is forbidden by the static suite.
The install and installed-boot logs default to
`/tmp/couchliteos-qemu-install.log` and
`/tmp/couchliteos-qemu-installed-boot.log`; the selected menu, edited kernel
line, dark installer, installed launcher, and exact VM command are captured
beside them.

For an interactive install, create a disposable disk, boot the ISO with a
graphical QEMU display, and complete Debian Installer manually:

```bash
qemu-img create -f qcow2 build/out/couchliteos-install-test.qcow2 32G
qemu-system-x86_64 -enable-kvm -m 4096 -cpu host \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE.fd \
  -cdrom build/out/couchliteos-0.1.13-amd64.iso \
  -drive file=build/out/couchliteos-install-test.qcow2,if=virtio \
  -device virtio-vga -display gtk \
  -netdev user,id=net0 -device virtio-net-pci,netdev=net0
```

After reboot, verify launcher/app exit/crash recovery and confirm the saved
`/var/lib/couchliteos/config.ini` values remain unchanged.

The automated install test proves the public UEFI ISO/menu installation path,
an independent installed boot, and configuration persistence across a cold
reboot. It does not prove
physical NVMe, USB destination selection, Rufus, or Ventoy behavior.

## Live-persistence QEMU smoke test

```bash
make qemu-persistence-smoke
```

This test creates an ext4 backend with the documented `persistence.conf`, boots
the release ISO twice, and verifies CouchLiteOS, Moonlight, chiaki-ng,
Tailscale, BlueZ, and log state across reboot. The first boot also saves a
Remote Desktop connection, pins it as a launcher button with a label, position,
and shortcut, and saves its password through the root helper; the second boot
verifies all of it. A third boot passes
`nopersistence` while the same backend remains attached and verifies that none
of its test state is visible. The installed-disk test performs the same Remote
Desktop check across its cold reboot.

## Remote Desktop lab test (xrdp)

The RDP path was also exercised end to end in the Debian 13 lab build VM: xrdp,
a headless Cage compositor, and CouchLiteOS's runner starting `sdl-freerdp3` as
the `couchliteos` user with a generated test password. Results on 2026-09-30:

- `probe_certificate` returned the same SHA-256 value as
  `openssl x509 -in /etc/xrdp/cert.pem -outform der | sha256sum`.
- With the correct pin, the client connected over TLS 1.3, stayed connected,
  and the close request ended it with exit 0 (`exited: disconnected`) and no
  runtime state left behind. xrdp logged the auto-logon request, the username,
  and a supplied password.
- With a different pin, the client stopped in under two seconds with exit 65
  (`the TLS connection failed; the server certificate may have changed`) and no
  sign-in reached xrdp.
- The password never appeared in any `/proc/*/cmdline` while connected, or in
  the client or compositor logs.
- xrdp 0.10.1 then showed its own login dialog rather than starting the session
  (see [Remote Desktop](REMOTE_DESKTOP.md#known-behavior)).

## v0.1.12 local results (2026-09-30)

Both release ISOs were built from commit `dc1de54` in the lab's Debian 13 build
VM (QEMU software emulation, 14 vCPUs, 6 GiB RAM); nothing ran on GitHub
Actions. Later commits change only documentation, tests, and tools. Each build
passed its profile's required/forbidden image-path checks; the iMac build
reported `wl.ko.xz built for 6.12.111+deb13-amd64`.

| ISO | Bytes | Build time | SHA-256 |
|---|---|---|---|
| `moonlightos-0.1.12-amd64.iso` | 1,890,271,232 | 63 min | `0dd5ae5c87fd88059481f61fc86c650bab5821bfc436d9ffa8f7f612c8c98a97` |
| `moonlightos-0.1.12-imac2013-amd64.iso` | 1,629,716,480 | 59 min | `64d6617b9fe2802315620f07c0e193f7df5381990e181820f41bee94b3ee0def` |

Both are under GitHub's 2 GiB (2,147,483,648-byte) asset limit.

QEMU tests on those exact ISOs, run from Git Bash on the Windows host (QEMU
11.1.0, software emulation, `MOONLIGHTOS_QEMU_TIMEOUT_SCALE=3`):

| Test | intel | imac2013 |
|---|---|---|
| `qemu-smoke.sh` (live boot, apps, OSK, Bluetooth, USB/IP, Remote Desktop) | pass, 3 min | pass, 3 min |
| `qemu-persistence-smoke.sh` (persistent reboot, `nopersistence`) | pass, 4 min | pass, 4 min |
| `qemu-install-smoke.sh` (install, disk boot, cold-reboot persistence) | pass, 18 min | pass, 18 min |

Notes:

- An earlier imac2013 test ISO failed the install test once at the installer's
  "Install the system" step while the host was heavily loaded. No log was
  captured; the same ISO passed when rerun, and the final ISO passed.
- Nested KVM inside the software-emulated build VM crashed the outer QEMU
  (`bql_locked` assertion). The build VM now hides SVM (`-cpu max,svm=off`), and
  the QEMU tests run on the Windows host instead.
- Both ISOs boot through Debian's shim (`EFI/boot/bootx64.efi`, signed by the
  Microsoft UEFI CA). No boot with Secure Boot enabled has been tested.
- Not run: the Hyper-V and Proxmox VE scripts (syntax-checked only), a Windows
  RDP host, the physical OptiPlex, and the physical iMac.

## v0.1.13 local results (2026-09-30)

Both release ISOs were built from commit `ae118b7` in the lab's Debian 13 build
VM (QEMU with the Windows Hypervisor Platform accelerator, 14 vCPUs, 6 GiB
RAM); nothing ran on GitHub Actions. Later commits change only documentation.
Both builds reported `wl.ko.xz built for 6.12.111+deb13-amd64`, and the nvidia
image contains the `nvidia-current` DKMS modules for the same kernel.

| ISO | Bytes | Build time | SHA-256 |
|---|---|---|---|
| `moonlightos-0.1.13-amd64.iso` | 1,838,284,800 | 16 min | `f0d53baa6b7288e48ef3dbfbbabba8a250193829bb7002f61fad48c7f7da0471` |
| `moonlightos-0.1.13-nvidia-amd64.iso` | 2,049,490,944 | 18 min | `f3d46e3bd70eec4c28cadab2c91894afa9090fbb6c960d23b49e2f423526ce65` |

Both are under GitHub's 2 GiB (2,147,483,648-byte) asset limit.

QEMU tests on those exact ISOs, run from Git Bash on the Windows host (QEMU
11.1.0, software emulation, `MOONLIGHTOS_QEMU_TIMEOUT_SCALE=3`):

| Test | general | nvidia |
|---|---|---|
| `qemu-smoke.sh` (live boot, apps, OSK, Bluetooth, USB/IP, Remote Desktop) | pass, 4 min | pass, 4 min |
| `qemu-persistence-smoke.sh` (persistent reboot, `nopersistence`) | pass, 5 min | pass, 6 min |
| `qemu-install-smoke.sh` (install, disk boot, cold-reboot persistence) | pass, 26 min | not completed: stalled after the installer came up (log unchanged for 38 min), stopped |

Notes:

- QEMU has no NVIDIA GPU, so the nvidia ISO's tests exercise its open-driver
  fallback only. NVIDIA's driver has not run on any GPU yet.
- The install tests took longer than for v0.1.12 because the host was also
  running unit tests in the build VM at the same time.
- No boot with Secure Boot enabled has been tested.
- Not run: the Hyper-V and Proxmox VE scripts, a Windows RDP host, and every
  row of the physical checklists below (OptiPlex, iMac, GPU classes).

## VM testing: Hyper-V and Proxmox VE

VMs cannot emulate an NVIDIA GPU, the iMac's Broadcom Wi-Fi, or its audio, so
they cover boot, launcher, Settings, persistence, and Remote Desktop only.

Hyper-V (Generation 2; run as Administrator on the Windows host):

```powershell
.\tools\hyperv-create-test-vm.ps1 -IsoPath D:\iso\couchliteos-0.1.13-amd64.iso -Start
```

The ISO ships Debian's Microsoft-signed shim, so Secure Boot stays on with the
Microsoft UEFI Certificate Authority template; pass `-SecureBoot Off` to test
without it. The script refuses to replace an existing VM or disk and connects
COM1 to a named pipe for the serial console.

Proxmox VE (on the Proxmox host, after uploading the ISO to an ISO storage):

```bash
./tools/proxmox-create-test-vm.sh --iso local:iso/couchliteos-0.1.13-amd64.iso --start
```

In the VM: complete the setup wizard, check Settings → Display, add, edit, pin,
and delete an RDP connection with the on-screen keyboard, connect to an RDP
host reachable from the VM, close it with Home/Guide → X, then install to the
virtual disk and confirm connections, buttons, and saved passwords survive a
reboot.

## Deployment-mode checklist

- [x] One `iso-hybrid` artifact contains live and Debian Installer paths
- [x] UEFI QEMU live boot reaches the launcher
- [x] UEFI QEMU installs to a virtual disk
- [x] Installed virtual disk boots without the ISO and reaches the launcher
- [x] Installed virtual-disk configuration survives a cold reboot
- [x] Live boot without a persistence device
- [x] Live persistence survives reboot for Moonlight, chiaki-ng, Tailscale,
      and BlueZ state
- [x] `No Persistence` ignores an existing persistence backend
- [ ] Invalid and full persistence backends fail diagnostically
- [ ] Rufus DD/Image-mode USB boot on physical UEFI hardware
- [ ] Ventoy normal-mode live boot and installer entry
- [ ] Ventoy persistence backend across reboot
- [ ] Install to physical internal NVMe/SATA
- [ ] Install from USB #1 to USB #2 and boot USB #2 independently

## Physical OptiPlex 7010 Micro checklist, general ISO (must be recorded, never inferred)

Display settings:

- [ ] Change to another advertised resolution and confirm within 15 seconds
- [ ] Change refresh rate to another advertised rate and confirm
- [ ] Let a preview time out and verify automatic rollback
- [ ] Cancel a preview with Escape/controller east and verify rollback
- [ ] Confirm a non-default mode, reboot, and verify persistence
- [ ] Change the monitor/adapter and verify compositor-default fallback/no black screen
- [ ] Navigate every Settings action using the intended controller

Applications and acceleration:

- [ ] 1080p60 Moonlight stream for 30 minutes
- [ ] 1080p120 on a compatible display
- [ ] 1440p60 on a compatible display
- [ ] 4K60 SDR best effort over DisplayPort
- [ ] chiaki-ng registration, connection, and gameplay
- [ ] HDMI/DP audio and wired controller input
- [ ] One exact allowlisted specialty USB/IP device
- [ ] Ethernet interruption and recovery
- [ ] Pairing/configuration survives cold reboot
- [ ] Closing Moonlight returns to the launcher
- [ ] Closing chiaki-ng returns to the launcher
- [ ] Closing Firefox returns to the launcher
- [ ] Closing Google Chrome returns to the launcher
- [ ] Confirm Widevine protected playback in Google Chrome and record Disney+ behavior separately
- [ ] Exit and Ctrl+D close Terminal and return to the launcher
- [ ] Ctrl+C in Terminal, nmtui, diagnostics, and Tailscale does not interrupt the launcher
- [ ] X (Xbox) / Triangle (PlayStation) or F12 opens the buffered keyboard in launcher text fields
- [ ] TYPE and TYPE + ENTER inject expected text after focus returns
- [ ] Record `vainfo`, `vulkaninfo --summary`, `wpctl status`, and `aplay -l`

Support export:

- [ ] Export to a second writable removable USB and verify exactly one readable archive
- [ ] Inspect the extracted archive for secrets
- [ ] With only the boot USB attached, Generate Support File says to insert a second USB drive (the boot drive and its persistence partition are never offered)
- [ ] Export to an unlabeled, unmounted USB stick (the launcher assigns the internal `COUCHLITEOS_SUPPORT` mount name; no drive label is required)
- [ ] A full, a write-protected, and an unplugged-mid-write USB drive each show a short plain message (`USB DRIVE IS FULL`, `USB DRIVE IS READ-ONLY`, `USB DRIVE ERROR: TRY ANOTHER DRIVE`)
- [ ] Export to an NTFS USB stick: it either works (mounted with ntfs3) or shows `THIS DRIVE IS NTFS; USE A FAT32 OR EXFAT DRIVE`, never "mounted read-only"
- [ ] Confirm the ISO9660 boot filesystem is not offered
- [ ] Attach two writable removable targets and use the controller selector
- [ ] Confirm the internal NVMe is never offered, without writing test data to it

Bluetooth (record every unperformed item as untested):

- [ ] Detect the Bluetooth adapter and turn Bluetooth off/on
- [ ] Scan, cancel scanning, and confirm discovery stops
- [ ] Pair an Xbox Wireless Controller, if available
- [ ] Pair a DualSense controller, if available
- [ ] Pair a Switch Pro Controller, if available
- [ ] Pair a second controller while the first continues navigating the launcher
- [ ] Pair a Bluetooth keyboard
- [ ] Complete numeric-confirmation and PIN/passkey pairing
- [ ] Pair Bluetooth headphones or a speaker and select its audio sink
- [ ] Confirm HDMI/DP audio remains selectable
- [ ] Disconnect, reconnect, forget, and re-pair a device
- [ ] Reboot and confirm pairing state and automatic reconnection survive
- [ ] Confirm Bluetooth audio reconnects without forcibly becoming the default
- [ ] Restart `bluetooth.service` and `couchliteos-bluetooth.service`
- [ ] Unplug the adapter while scanning, reinsert it, and recover
- [ ] Unplug the adapter while pairing, reinsert it, and recover
- [ ] Confirm the launcher PID and restart count do not change
- [ ] Launch and exit Moonlight after Bluetooth configuration
- [ ] Launch and exit chiaki-ng after Bluetooth configuration
- [ ] Launch and exit Firefox after Bluetooth configuration

Installed systems retain BlueZ state under `/var/lib/bluetooth`. Live USB
persistence must include that directory for pairings to survive reboot. Never
copy its contents into a support archive because it contains link keys.

## iMac Late 2013 hardware checklist

Boot the **general** ISO and record every result on the iMac itself; nothing
here may be inferred from a VM. Save the output of
`sudo couchliteos-hardware-report` in [IMAC-2013.md](IMAC-2013.md#recorded-report).

| # | Check | Result |
|---|---|---|
| 1 | Section 0 identification matches the expected model (`iMac14,2` or `iMac14,3`) and serial `DCPM5026FQPG` | untested |
| 2 | Boots via Option → EFI Boot | untested |
| 3 | Launcher runs at the panel's native resolution (Settings → Display shows `(NATIVE)` as current) | untested |
| 4 | `lsmod` shows `nouveau` and no `nvidia` module | untested |
| 5 | Ethernet connects (`tg3`), and Wi-Fi connects using `wl` | untested |
| 6 | Audio output works (Settings → Audio, AUDIO TEST) | untested |
| 7 | A Bluetooth controller pairs | untested |
| 8 | Moonlight streams 1080p60 with software H.264; record the stats overlay (Ctrl+Alt+Shift+S): decoder, codec, FPS, frame drops, network/decode/render latency | untested |
| 9 | RDP connects to a Windows host and to an xrdp host, and Home/Guide → X disconnects each | untested |
| 10 | Saved RDP connections and launcher buttons (label, position, shortcut) survive a reboot | untested |
| 11 | `/usr/libexec/couchliteos-hwdetect report` shows `wl` for the BCM4360, `applesmc` loaded, display driver `nouveau`, and software H.264 | untested |
| 12 | The NVIDIA ISO boots to the launcher on the iMac through its nouveau fallback (report: "not supported by the installed NVIDIA driver") | untested |

## GPU class checklist (v0.1.13)

Record one machine per row before publishing. Each needs: the launcher at the
native resolution, open and close Moonlight, Firefox, and Terminal, a 10-minute
1080p60 Moonlight stream with the stats overlay (decoder and frame drops), and
`/usr/libexec/couchliteos-hwdetect report` saved with the result.

| GPU class | ISO / boot entry | Expected driver and decoder | Result |
|---|---|---|---|
| Intel UHD 770 (OptiPlex 7010 Micro) | general | `i915`, VA-API hardware decode | untested |
| AMD Radeon (any GCN or newer) | general | `amdgpu`, VA-API hardware decode | untested |
| NVIDIA Maxwell to Ada (GTX 745/750, GTX 900 to RTX 40) | nvidia, Start CouchLiteOS | `nvidia-drm`, no `nouveau`; hardware decode | untested |
| Same NVIDIA card | nvidia, Basic Graphics | `nouveau`, no `nvidia` module; software H.264 | untested |
| Same NVIDIA card | general | `nouveau`; software H.264 | untested |
| NVIDIA Kepler (iMac Late 2013) | general, and nvidia (fallback) | `nouveau`; software H.264 | untested |
| Broadcom Wi-Fi chip not on `wl`'s list (for example BCM4306 or BCM4350) | general | open driver (`b43` or `brcmfmac`), no `wl` | untested |

## Opt-in live Tailscale checklist

- [ ] Moonlight launches normally with Tailscale disabled
- [ ] Local enrollment displays a URL and state survives reboot
- [ ] MagicDNS resolves when `accept_dns = true`
- [ ] Tailscale IP fallback works without MagicDNS
- [ ] Sunshine streams over a direct Tailscale path
- [ ] Relay warning appears for peer-relay or DERP
- [ ] Tailscale SSH is unavailable before opt-in
- [ ] Authorized admin SSH works; unauthorized identity is denied by policy
- [ ] Unauthorized peer cannot reach TCP/3240
- [ ] USB/IP stays LAN-only until both remote guards are set
- [ ] Tailscale/tailscaled failure does not stop local streaming
- [ ] chiaki-ng still reaches the local PlayStation
- [ ] No auth key, login URL, or machine key appears in persistent logs/artifacts

Live tailnet tests are intentionally absent from CI and must be invoked only in
a disposable, explicitly authorized test tailnet.
