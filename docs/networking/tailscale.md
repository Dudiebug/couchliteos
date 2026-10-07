# Optional Tailscale overlay

Tailscale connects CouchLiteOS to the Linux Sunshine host when the two systems
are on different networks. It is optional: ordinary wired LAN Moonlight,
chiaki-ng, and LAN USB/IP continue to work when Tailscale is disabled, logged
out, or unavailable.

## Package provenance

v0.1.11 installs `tailscale` version `1.102.3` from Tailscale's official stable
Debian Trixie repository:

```text
deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian trixie main
```

The public archive key URL is fixed in `build/sources.lock`, and apt verifies
packages with that repository key. There is no `curl | sh`. The native `tailscaled.service` starts after
`network-online.target`, restarts on failure, stores identity under root-only
`/var/lib/tailscale`, and is not a launcher dependency.

Official references: [Linux install](https://tailscale.com/docs/install/linux),
[MagicDNS](https://tailscale.com/docs/features/magicdns),
[Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh), and
[connection types](https://tailscale.com/docs/reference/connection-types).

## Enrollment

Choose **TAILSCALE** in the local launcher. CouchLiteOS starts the native daemon,
generates `couchliteos-<first-eight-machine-id>` unless `node_name` is configured,
and displays the short-lived login URL as a scannable QR code plus text. The URL
is kept only under `/run`; it is not written to persistent logs.

For out-of-band provisioning, place a single-use key in
`/etc/couchliteos/tailscale-auth.key`, owned by root with mode `0600`. The
enrollment service uses Tailscale's `file:` auth-key form, so the key is not in
argv, shell history, or logs. Never put this file in Git or the image.
Remove the out-of-band file after a single-use enrollment succeeds.

```ini
[tailscale]
enabled = false
accept_dns = true
ssh_enabled = false
host_address_mode = auto
remote_usbip = false
allowed_usbip_peer =
node_name =
```

Incremental changes use `tailscale set`. `tailscale up` is reserved for the
initial unauthenticated enrollment. Key expiry is not automatically disabled;
an administrator may change it for a trusted always-on appliance in the
Tailscale admin console after weighing the security tradeoff.

CouchLiteOS never enables route acceptance, advertised routes, subnet routing,
exit-node behavior, Serve, Funnel, or public ingress.

## Sunshine host profile

Install Tailscale on the Linux Sunshine PC too. Configure the same existing INI:

```ini
[host:gaming-pc]
lan_address = 192.168.1.50
tailscale_hostname = gaming-pc.example-tailnet.ts.net
tailscale_ip = 100.64.10.20
address_mode = auto
app =
```

`auto` tries reachable LAN first, then MagicDNS, then the literal Tailscale IP.
It never waits for Tailscale before opening Moonlight. Run
`couchliteos-host-address` to see the selected address. Add that address
manually in Moonlight; LAN discovery and mDNS are not expected to cross the
overlay. If `app` is set to a paired Sunshine application such as `Desktop`,
CouchLiteOS uses Moonlight's direct `stream HOST APP` CLI.

The Sunshine host firewall must allow streaming on `tailscale0` for the
current Sunshine installation. Do not paste an old static port list. Verify
the current base port/configuration and active listeners on the host, then use
Sunshine's current official troubleshooting/configuration documentation:
[Sunshine troubleshooting](https://docs.lizardbyte.dev/projects/sunshine/latest/md_docs_2troubleshooting.html).
No router port forwards are required for the tailnet path.

## Tailscale SSH

Tailscale SSH is off by default and no OpenSSH server is installed. After
enrollment, turn it on from a console with `sudo couchliteos-tailscale ssh-enable`
(there is no launcher item for it). This runs the supported incremental command
`tailscale set --ssh=true`. Both a network grant and an SSH rule in the
tailnet policy remain mandatory. `couchliteos-tailscale logout` refuses to run
without `--confirm` because logout destroys the node identity.

Example least-privilege policy (replace the example administrator identity):

```json
{
  "tagOwners": {
    "tag:moonlight-client": ["admin@example.com"],
    "tag:gaming-host": ["admin@example.com"],
    "tag:moonlight-admin": ["admin@example.com"]
  },
  "grants": [
    {
      "src": ["tag:moonlight-client"],
      "dst": ["tag:gaming-host"],
      "ip": ["tcp:47984", "tcp:47989", "tcp:48010", "udp:47998", "udp:47999", "udp:48000"]
    },
    {
      "src": ["tag:moonlight-admin"],
      "dst": ["tag:moonlight-client"],
      "ip": ["tcp:22"]
    },
    {
      "src": ["tag:gaming-host"],
      "dst": ["tag:moonlight-client"],
      "ip": ["tcp:3240"]
    }
  ],
  "ssh": [
    {
      "action": "check",
      "src": ["tag:moonlight-admin"],
      "dst": ["tag:moonlight-client"],
      "users": ["couchliteos"],
      "checkPeriod": "1h"
    }
  ]
}
```

The Sunshine ports above were verified on 2026-08-20 against the current
upstream [`config.cpp`](https://github.com/LizardByte/Sunshine/blob/master/src/config.cpp)
default base port and the offsets in
[`nvhttp.h`](https://github.com/LizardByte/Sunshine/blob/master/src/nvhttp.h),
[`stream.h`](https://github.com/LizardByte/Sunshine/blob/master/src/stream.h),
and [`rtsp.h`](https://github.com/LizardByte/Sunshine/blob/master/src/rtsp.h);
verify them again against the installed Sunshine
version and adjust for a non-default base port before applying policy. The example does not
grant unrestricted `*:*` access. Tailnet policy is enforced in the Tailscale
admin console, not trusted to appliance-local nftables alone.

## Direct and relayed paths

Run `couchliteos-tailscale-diagnostics`. It shows backend state, node name,
Tailscale IPv4, MagicDNS name, status, ping, netcheck, selected host, approximate
latency, and whether the active path is direct, peer-relay, or DERP. Relays are
warned but never block launch. Direct is normally best for throughput/latency;
peer-relay adds a hop and DERP is usually slower.

Do not add an unconditional WAN UDP/41641 rule. Diagnose with `tailscale
netcheck` and `tailscale ping` first and follow Tailscale's
[device-connectivity guidance](https://tailscale.com/docs/reference/device-connectivity).

## USB/IP and PlayStation boundaries

USB/IP remains LAN-only unless both `remote_usbip = true` and one literal
`allowed_usbip_peer` Tailscale IPv4 are set. nftables separates LAN and
`tailscale0` chains, and the VID/PID/serial allowlist still applies. Relay paths
can be unsuitable for latency-sensitive devices.

chiaki-ng runs locally on CouchLiteOS and expects the PlayStation on the local
LAN. Tailscale does not turn a PlayStation into a tailnet node. v0.1.2 does not
enable a [subnet router](https://tailscale.com/docs/features/subnet-routers) or
an [exit node](https://tailscale.com/docs/features/exit-nodes); reaching a
console on another LAN is deferred to a separately reviewed future design.
