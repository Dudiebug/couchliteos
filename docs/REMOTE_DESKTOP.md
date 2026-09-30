# Remote Desktop (RDP)

CouchLiteOS includes FreeRDP 3's SDL client (`sdl-freerdp3`, Debian package
`freerdp3-sdl`). It runs natively on Cage's Wayland socket with SDL3; it never
uses Xwayland. Both build profiles include it. Remmina and other desktop
clients are not installed.

## Saved connections

Open **Settings → Remote Desktop**. Every field works with a controller:

| Controller | Keyboard | Action |
|---|---|---|
| D-pad | arrows | move |
| A / Cross | Enter | select, edit, toggle |
| B / Circle | Esc | back or cancel |
| X on Xbox pads, Triangle on PlayStation pads | F12 | open the on-screen keyboard in a text field |
| Left/right on a toggle | Left/Right | switch ON/OFF |

Text fields (display name, host, port, username, domain, custom resolution,
password) accept the buffered on-screen keyboard: open it, type, then choose
**TYPE + ENTER**. Password fields show `*` and open the keyboard masked.

| Field | Values |
|---|---|
| Display name | required |
| Host | IPv4 address or DNS name (CouchLiteOS is IPv4-only) |
| Port | default 3389 |
| Username | required; `DOMAIN\user` and `user@domain` also work |
| Domain | optional |
| Resolution | **Native** (this display's current mode), **Fit** (the session follows the window size), or **Custom** (a preset such as 1920x1080 or a typed `WIDTHxHEIGHT`, scaled to fit) |
| Fullscreen | ON/OFF |
| Audio | ON plays remote audio through PipeWire; OFF leaves audio on the remote computer |
| Clipboard | ON shares text in both directions (file transfer is off) |
| Save password | OFF (default) asks at every connection; ON stores the password **unencrypted** |

Changing the host or port forgets the pinned certificate so the new server must
be verified.

## Passwords

- By default the password is typed at connect time with the on-screen
  keyboard.
- The launcher hands it to the session runner in `/run/couchliteos` (RAM-backed,
  mode 0600, appliance user only). The runner writes it to `sdl-freerdp3`'s
  standard input, and FreeRDP reads it with `/from-stdin:force`. It never
  appears on a command line, in the environment of the client, or in a log. The
  handoff file is removed when the session ends; it survives only while systemd
  restarts a crashed session.
- **Save password** stores it in `/var/lib/couchliteos/rdp-secrets/<id>`,
  owned by root with mode 0600, in a root-only 0700 directory, together with the
  host, port, username, and domain it was saved for. It is **not encrypted**;
  anyone with root access or physical access to the disk can read it, and so
  can software running as the appliance account, because the RDP client itself
  must receive it. A sandboxed root helper (`couchliteos-rdp-secret`, started
  by a path unit, with none of the appliance account's environment) saves,
  deletes, and hands it to a session. It refuses to hand it over if the
  connection's host, port, username, or domain changed since it was saved;
  editing any of those turns **Save password** off until you save it again.
- Support archives never read the saved-password directory or the handoff file
  (the export service cannot access them), list connections without usernames,
  domains, or passwords, and redact `/p:`-style FreeRDP arguments and
  `password` values from every collected log.
- Authentication failures are never retried automatically, so a wrong password
  cannot lock the account out through restarts.

## Certificates

CouchLiteOS uses trust on first use:

1. Before connecting, the launcher opens a TLS connection to the server and
   shows its SHA-256 certificate fingerprint. Compare it with the server:
   - Windows (PowerShell): `Get-ChildItem 'Cert:\LocalMachine\Remote Desktop' | ForEach-Object { [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($_.RawData)) }`
     (the Thumbprint column is SHA-1, not the value shown here)
   - xrdp: `openssl x509 -in /etc/xrdp/cert.pem -noout -fingerprint -sha256`
2. **TRUST AND CONNECT** pins the fingerprint to the saved connection.
3. Every later connection is checked first. If the fingerprint changed, the
   launcher shows the old and new fingerprints, logs the event to
   `/var/log/couchliteos/rdp.log`, and blocks the connection.
4. FreeRDP itself runs with `/cert:deny,fingerprint:sha256:<pin>`, so it never
   prompts and aborts on any other certificate. Legacy RDP security (no TLS) is
   disabled with `/sec:rdp:off`.

If a server's certificate was replaced on purpose, verify the new fingerprint on
the server, then use **Settings → Remote Desktop → <connection> → Forget
certificate** and connect again.

## Launcher buttons

Any saved connection can be pinned as a launcher button (text only):

- **Pin to launcher** adds the button; **Unpin** removes it.
- **Button label** edits the text shown in the launcher.
- **Button position** moves it up or down among the launcher buttons.
- **Controller shortcut** assigns LB/L1, RB/R1, View/Select, or Menu/Start. On
  the main launcher screen that button (or F5–F8 on a keyboard) starts the
  connection. A shortcut belongs to one button; assigning it again moves it.

A pinned button is an ordinary user application manifest
(`/var/lib/couchliteos/apps.d/rdp-<name>.ini`, `kind = rdp`); its position is
stored in `/var/lib/couchliteos/apps-state.ini`. Settings → Applications can
also enable, disable, reorder, or delete it.

## Sessions

`couchliteos-rdp.path` starts `couchliteos-rdp.service` when the launcher
requests a connection. The session behaves like the other applications:

- **Home/Guide** opens Active Applications; **A** resumes the session and
  **Y** (Xbox) / **Square** (PlayStation) closes it. X/Triangle open the
  on-screen keyboard instead.
- If the client crashes or the connection drops, systemd restarts it (up to
  three starts per minute) and it reconnects with the same credentials. When
  the restarts are exhausted, `couchliteos-rdp-cleanup.service` removes the
  session state, clears the start limit, and re-arms the path unit so the next
  connection can start.
- If Cage restarts underneath a session, the runner notices that the Wayland
  socket was replaced, ends the client, and systemd reconnects it.
- Stopping the unit (for example at shutdown) ends the session normally; the
  client ignores SIGTERM, so the runner follows up with SIGKILL after 5 s.
- Authentication, certificate, and configuration failures stop immediately
  with a message in the launcher.
- One Remote Desktop session runs at a time.

## Files

| Purpose | Path |
|---|---|
| Saved connections and pinned fingerprints (no secrets) | `/var/lib/couchliteos/rdp/connections.ini` |
| Saved passwords (root, 0600, unencrypted) | `/var/lib/couchliteos/rdp-secrets/` |
| Launcher buttons | `/var/lib/couchliteos/apps.d/rdp-*.ini`, `/var/lib/couchliteos/apps-state.ini` |
| Certificate trust and change events | `/var/log/couchliteos/rdp.log` |
| Client output | `journalctl -u couchliteos-rdp.service` |

A live USB needs `/var/lib/couchliteos` in its persistence configuration for
connections, buttons, and saved passwords to survive a reboot.

## Known behavior

- In the lab, Debian 13's xrdp 0.10.1 received the username, the password, and
  the auto-logon request, then showed its own login dialog instead of starting
  the session immediately (it waits for a graphics-pipeline handshake before
  auto-logon). Sign in again in xrdp's dialog if this happens. Windows hosts
  authenticate with NLA before the session starts; no Windows host has been
  tested yet. The hardware checklist records both.
- FreeRDP's SDL client in Debian 13 (3.15) is marked experimental by upstream.
