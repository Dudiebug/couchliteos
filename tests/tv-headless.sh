#!/bin/bash
# The TV interface (launcher/couchliteos-tv.py) in a headless Cage, with a fixture home.
# Its --script opens every screen (home, settings, power, the quick menu, change artwork,
# stream settings, what's new, the update progress, the tour, HELP, the loading screen) and
# --dump-layout records every label on each one.
# Checks: each screen opened; no text under 28 px at 1920x1080; no label shortened or cut
# at 1280x720 unless it is shortened on purpose (a tile's title); a live switch to each
# built-in theme; on the XMB, a live switch to each BACKGROUND (WAVE AND SPARKLES, WAVE, PLAIN)
# and to ACCENT > BY MONTH; no Python traceback in any run's log. A first-boot run (setup not complete)
# starts the setup wizard on top (a fake foot records it), then the tour, and still writes
# launcher-ready.
# The XMB (the default home) and the rows ([appearance] home = rows) each get their runs.
# Screenshots (1920x1080) go to build/out/screenshots/ (the XMB's to its xmb/ folder).
#
# Test-only tools, not in the image: cage, grim, and a Python with GTK 4 (PyGObject).
# COUCHLITEOS_TV_PYTHON picks the Python (default: the first of python3, python3.13 and
# python3.12 that has GTK 4); COUCHLITEOS_SCREENSHOTS the folder. With --if-available (make
# test) a missing tool skips the run instead of failing it.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SHOTS=${COUCHLITEOS_SCREENSHOTS:-$ROOT/build/out/screenshots}
THEMES=(midnight slate daylight ocean crimson emerald amethyst sunset rose gold graphite arctic high-contrast terminal)

missing() {
  if [[ ${1-} == --if-available ]]; then
    echo "tv-headless: skipped: $2"
    exit 0
  fi
  echo "tv-headless: $2" >&2
  exit 127
}
PYTHON=''
for candidate in ${COUCHLITEOS_TV_PYTHON:-python3 python3.13 python3.12}; do
  if command -v "$candidate" >/dev/null \
      && "$candidate" -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' 2>/dev/null; then
    PYTHON=$candidate
    break
  fi
done
[[ -n $PYTHON ]] || missing "${1-}" "no Python with GTK 4 (PyGObject) found"
for tool in cage grim; do
  command -v "$tool" >/dev/null || missing "${1-}" "$tool is not installed"
done

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# The foot window a classic screen opens in: records its arguments; as setup would, it marks
# setup complete.
cat > "$work/foot" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$COUCHLITEOS_RUN_DIR/foot.log"
touch "$COUCHLITEOS_STATE_DIR/setup-complete"
EOF
chmod 755 "$work/foot"
# The volume the quick menu shows: PipeWire is not running here.
mkdir -p "$work/bin"
cat > "$work/bin/wpctl" <<'EOF'
#!/bin/sh
[ "$1" = get-volume ] && { echo 'Volume: 0.60'; exit 0; }
exit 1
EOF
chmod 755 "$work/bin/wpctl"

# A fresh fixture per run: setup done, update checks and artwork lookup off (nothing leaves
# the machine), one gaming PC with three games (one with a long name) and two covers.
fixture() {
  local base=$1 state=$1/state
  rm -rf -- "$base"
  mkdir -p "$base/run" "$state" "$base/home" "$base/xdg"
  chmod 700 "$base/xdg"
  touch "$state/setup-complete" "$state/controls-shown" "$state/tutorial-seen"  # the script opens the tour
  printf '999.0.0\n' | tee "$state/whatsnew-seen" > "$base/run/whatsnew-seen"  # opened by the script
  printf '[updates]\nenabled = false\n' > "$state/update-check.ini"
  printf '{"lookup": false}\n' > "$state/artwork.json"
  printf '[appearance]\ntheme = midnight\naccent = \nsounds = off\nmusic = off\nhome = %s\n' "$home_layout" > "$state/config.ini"
  local conf="$state/home/.config/Moonlight Game Streaming Project"
  local art="$state/home/.cache/Moonlight Game Streaming Project/Moonlight/boxart/FIXTURE-UUID"
  mkdir -p "$conf" "$art"
  printf '%s\n' '[hosts]' '1\hostname=GAMING-PC' '1\uuid=FIXTURE-UUID' '1\localaddress=192.0.2.10' \
    '1\apps\size=3' '1\apps\1\name=Desktop' '1\apps\1\id=101' \
    '1\apps\2\name=Lanterns Over the Harbor: Definitive Collectors Edition Remastered' '1\apps\2\id=102' \
    '1\apps\3\name=Paper Kite Racers' '1\apps\3\id=103' 'size=1' > "$conf/Moonlight.conf"
  "$PYTHON" - "$art" <<'EOF'
import pathlib, struct, sys, zlib

def png(path, top, bottom, width=200, height=300):
    rows = b"".join(
        b"\0" + bytes(round(a + (b - a) * y / height) for a, b in zip(top, bottom)) * width
        for y in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

folder = pathlib.Path(sys.argv[1])
png(folder / "101.png", (220, 60, 40), (40, 30, 120))
png(folder / "102.png", (30, 160, 90), (240, 200, 60))
EOF
}

# run_tv <name> <width> <height> <script> [screenshots] [firstboot]: the TV interface at that
# size until the script's `quit`; its dumps land in $work/<name>/dump. With firstboot, setup is
# not complete and the tour not seen.
run_tv() {
  local name=$1 width=$2 height=$3 script=$4 shots=${5:-} firstboot=${6:-}
  local base=$work/$name
  fixture "$base"
  [[ -z $firstboot ]] || rm -f -- "$base/state/setup-complete" "$base/state/tutorial-seen"
  local status=0
  COUCHLITEOS_RUN_DIR=$base/run COUCHLITEOS_STATE_DIR=$base/state HOME=$base/home \
    XDG_RUNTIME_DIR=$base/xdg XDG_CONFIG_HOME=$base/home/.config COUCHLITEOS_FOOT=$work/foot PATH=$work/bin:$PATH     COUCHLITEOS_LAUNCHER=$ROOT/launcher/couchliteos-launcher.py COUCHLITEOS_TV_FOOT=${firstboot:+1} \
    WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 \
    TV_PYTHON=$PYTHON TV_ROOT=$ROOT TV_STATUS=$base/status TV_SIZE="$width $height" \
    TV_SCRIPT=$script TV_DUMP=$base/dump TV_SHOTS=$shots \
    timeout -k 10 180 cage -- sh -c '
      "$TV_PYTHON" "$TV_ROOT/tests/wl-output-size.py" $TV_SIZE || { echo size > "$TV_STATUS"; exit 1; }
      "$TV_PYTHON" "$TV_ROOT/launcher/couchliteos-tv.py" --script "$TV_SCRIPT" --dump-layout "$TV_DUMP" \
        ${TV_SHOTS:+--screenshots "$TV_SHOTS"}
      echo $? > "$TV_STATUS"' > "$base/log" 2>&1 || status=$?
  if [[ $status != 0 || $(cat "$base/status" 2>/dev/null) != 0 ]]; then
    echo "tv-headless: the $name run failed (cage $status, tv $(cat "$base/status" 2>/dev/null || echo none)):" >&2
    grep -v -i -e egl -e mesa -e '^$' "$base/log" >&2 || true
    exit 1
  fi
  if grep -q Traceback "$base/log"; then
    echo "tv-headless: the $name run logged a Python traceback:" >&2
    cat "$base/log" >&2
    exit 1
  fi
}

# Every screen from home; Down x3 reaches SYSTEM (SETTINGS, HOSTS, POWER) even with no APPS row.
# F1 first: nothing uses it (a key sent at the very start must not matter).
screens='F1,dump:home,Home,dump:quick,Escape,F9,dump:art,Escape,F10,dump:streamset,Escape,Down,Down,Down,Return,dump:settings,Escape'
screens+=',Right,Right,Return,dump:power,Escape,open:whatsnew,dump:whatsnew,Return,Up,Up,Up'
# The tour to its last card, HELP's list and a topic's text, and the busy screen's loading ring.
help=',open:tutorial,dump:tutorial,Right,Right,Right,Right,dump:tutorial-last,Return'
help+=',open:help,dump:help,Down,Return,dump:help-reading,Escape,Escape,open:loading,dump:loading,Escape'
screens+=$help
themes=''
for theme in "${THEMES[@]}"; do
  themes+=",theme:$theme,wait:1600,dump:theme-$theme"
done
rm -f -- "$SHOTS"/*.png "$SHOTS"/xmb/*.png
home_layout=rows
run_tv 1080 1920 1080 "$screens$themes,open:update,dump:update,quit" "$SHOTS"
run_tv 720 1280 720 "$screens,open:update,dump:update,quit"
# The XMB: every category (GAMES first; left to POWER, right to APPS), the quick menu, change
# artwork and stream settings over it, every theme behind the wave.
home_layout=cross
xmb='F1,dump:home,Home,dump:quick,Escape,F9,dump:art,Escape,F10,dump:streamset,Escape,Down,Down,dump:xmb-games'
xmb+=',Left,dump:xmb-video,Left,dump:xmb-settings,Down,dump:xmb-settings-down,Left,dump:xmb-power'
xmb+=',Right,Right,Right,Right,dump:xmb-apps,Right,Escape,Escape'$help
# What is behind the XMB, switched live as Settings > APPEARANCE saves it (the 1 s tick applies it).
looks=',theme:midnight,background:plain,wait:1600,dump:bg-plain,background:calm,wait:1600,dump:bg-calm'
looks+=',background:wave,accent:month,wait:1600,dump:accent-month,accent:,wait:1600,dump:bg-wave'
run_tv xmb1080 1920 1080 "$xmb$themes$looks,open:update,dump:update,quit" "$SHOTS/xmb"
run_tv xmb720 1280 720 "$xmb,open:update,dump:update,quit"
# Every classic Settings screen, drawn by the TV interface (couchliteos_uibridge): it opens,
# shows a title and text, and B / Esc closes it (the next one could not open otherwise).
CLASSIC=(display appearance audio bluetooth controllers network sleep applications remote-desktop streaming
         tv-control software-update controls)
classic='wait:2000'
for screen in "${CLASSIC[@]}"; do
  # Esc again: a screen may show an error page first here (no PipeWire, no network manager).
  classic+=",screen:$screen,wait:2500,dump:classic-$screen,Escape,wait:1200,Escape,wait:1200,Escape,wait:1200"
done
run_tv classic1080 1920 1080 "$classic,quit" "$SHOTS/classic"
run_tv classic720 1280 720 "$classic,quit"
if [[ -e $work/classic1080/run/foot.log ]]; then
  echo "tv-headless: a classic screen fell back to foot: $(cat "$work/classic1080/run/foot.log")" >&2
  exit 1
fi

# First boot: the wizard opens on top as `couchliteos-launcher --screen setup` in foot; when it
# closes, the tour; B on its first card skips it, then the home screen.
run_tv firstboot 1280 720 'wait:4000,dump:tutorial,Escape,wait:1000,dump:home,quit' '' firstboot
if ! grep -q -- '--screen setup' "$work/firstboot/run/foot.log" 2>/dev/null; then
  echo "tv-headless: first boot did not open the setup wizard (foot: $(cat "$work/firstboot/run/foot.log" 2>/dev/null || echo 'not started'))" >&2
  exit 1
fi
[[ -e $work/firstboot/run/launcher-ready ]] || { echo 'tv-headless: first boot wrote no launcher-ready' >&2; exit 1; }

CLASSIC_SCREENS="${CLASSIC[*]}" "$PYTHON" - "$work" "$ROOT" "${THEMES[@]}" <<'EOF'
import json, os, pathlib, sys

work, themes = pathlib.Path(sys.argv[1]), sys.argv[3:]
CLASSIC = [f"classic-{name}" for name in os.environ["CLASSIC_SCREENS"].split()]
sys.path.insert(0, sys.argv[2] + "/launcher")
import couchliteos_theme as theme
midnight = theme.load(pathlib.Path(sys.argv[2]) / "overlay/usr/share/couchliteos/themes/midnight.theme")
MIN_FONT = 28
SCREENS = {"home": "home", "quick": "home", "art": "art", "streamset": "streamset", "settings": "settings", "power": "power",
           "whatsnew": "whatsnew", "tutorial": "tutorial", "tutorial-last": "tutorial", "help": "help",
           "help-reading": "help", "loading": "busy", "update": "update"}
XMB = ["home", "quick", "art", "streamset", "xmb-games", "xmb-video", "xmb-settings", "xmb-settings-down", "xmb-power",
       "xmb-apps", "tutorial", "tutorial-last", "help", "help-reading", "loading", "update"]
LOOKS = ["bg-plain", "bg-calm", "accent-month", "bg-wave"]
problems = []
started = None  # how the XMB's wave ran at the start (GL where the GL wave runs here)
for run, size in (("1080", (1920, 1080)), ("720", (1280, 720)), ("xmb1080", (1920, 1080)), ("xmb720", (1280, 720)),
                  ("classic1080", (1920, 1080)), ("classic720", (1280, 720))):
    names = XMB if run.startswith("xmb") else CLASSIC if run.startswith("classic") else list(SCREENS)
    names = names + ([f"theme-{name}" for name in themes] if run in ("1080", "xmb1080") else [])
    names = names + (LOOKS if run == "xmb1080" else [])
    for name in names:
        path = work / run / "dump" / f"{name}.json"
        if not path.exists():
            problems.append(f"{run}/{name}: no layout dump (the screen did not open)")
            continue
        dump = json.loads(path.read_text())
        where = f"{run}/{name}"
        if (dump["width"], dump["height"]) != size:
            problems.append(f"{where}: window is {dump['width']}x{dump['height']}, not {size[0]}x{size[1]}")
        expected = "classic" if name.startswith("classic-") else SCREENS.get(name, "home")
        if dump["screen"] != expected or dump["quick"] != (name == "quick"):
            problems.append(f"{where}: showed {dump['screen']} (quick menu {dump['quick']})")
        if name.startswith("theme-") and dump["theme"] != name[6:]:
            problems.append(f"{where}: theme is still {dump['theme']}")
        if run.startswith("xmb") and "wave" not in dump:
            problems.append(f"{where}: no wave facts in the dump")
        if run == "xmb1080" and name == "home":
            started = dump.get("wave", {}).get("how")
        if name in LOOKS:
            wave = dump.get("wave", {})
            style = {"bg-plain": "plain", "bg-calm": "calm"}.get(name, "wave")
            ribbons, sparkles = name != "bg-plain", name in ("accent-month", "bg-wave")
            if (wave.get("background"), wave.get("alpha", 0) > 0, wave.get("sparkle", 0) > 0) != (style, ribbons, sparkles):
                problems.append(f"{where}: background {wave} is not {style}")
            if name == "bg-plain" and (wave.get("gl") or wave.get("how") == "gl"):
                problems.append(f"{where}: PLAIN still runs the GL wave ({wave})")
            if name != "bg-plain" and (wave.get("how"), wave.get("gl")) != (started, started == "gl"):
                problems.append(f"{where}: the wave is not back as it started ({started}): {wave}")
            accent = theme.with_accent(midnight, "month" if name == "accent-month" else "").colours["accent"]
            if dump.get("accent") != accent:
                problems.append(f"{where}: accent is {dump.get('accent')}, not {accent}")
        if not dump["labels"]:
            problems.append(f"{where}: no text on screen")
        for label in dump["labels"]:
            text = label["text"][:60]
            if run.endswith("1080") and label["font_px"] < MIN_FONT:
                problems.append(f"{where}: {label['font_px']} px text {text!r} {label['classes']}")
            if run.endswith("720") and not label["on_purpose"]:
                cut = label["ellipsized"] or (not label["wrap"] and label["natural"] > label["width"] + 1)
                if cut or label["x"] < 0 or label["right"] > size[0] + 1:
                    problems.append(f"{where}: clipped {text!r} {label['classes']} "
                                    f"({label['natural']} px in {label['width']} px at x={label['x']})")
tour = work / "firstboot" / "dump" / "tutorial.json"
if not tour.exists() or json.loads(tour.read_text())["screen"] != "tutorial":
    problems.append("firstboot: the tour did not follow the setup wizard")
first = work / "firstboot" / "dump" / "home.json"
if not first.exists() or json.loads(first.read_text())["screen"] != "home":
    problems.append("firstboot: the home screen did not follow the tour")
for problem in problems:
    print(f"tv-headless: {problem}", file=sys.stderr)
sys.exit(1 if problems else 0)
EOF
# The on-screen keyboard in GTK: it shows, types "2a" with controller keys (A on 2, a USB "a",
# Up to TYPE, A) and writes the payload; a screenshot is taken while it is up.
osk=$work/osk
mkdir -p "$osk/run" "$osk/xdg" "$osk/state"
chmod 700 "$osk/xdg"
status=0
COUCHLITEOS_RUN_DIR=$osk/run COUCHLITEOS_STATE_DIR=$osk/state HOME=$osk XDG_RUNTIME_DIR=$osk/xdg   WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 COUCHLITEOS_OSK_KEYS=Right,Return,a,Up,Return   OSK_PYTHON=$PYTHON OSK_ROOT=$ROOT OSK_SHOT=$SHOTS/osk.png OSK_STATUS=$osk/status   timeout -k 10 60 cage -- sh -c '
    "$OSK_PYTHON" "$OSK_ROOT/tests/wl-output-size.py" 1920 1080 || { echo size > "$OSK_STATUS"; exit 1; }
    (sleep 3; grim "$OSK_SHOT") &
    "$OSK_PYTHON" "$OSK_ROOT/launcher/couchliteos_osk.py" --gtk
    echo $? > "$OSK_STATUS"; wait' > "$osk/log" 2>&1 || status=$?
if [[ $status != 0 || $(cat "$osk/status" 2>/dev/null) != 0 ]] || ! grep -q '"2a"' "$osk/run/osk-payload.json" 2>/dev/null; then
  echo "tv-headless: the GTK keyboard did not type (cage $status, keyboard $(cat "$osk/status" 2>/dev/null || echo none)):" >&2
  cat "$osk/log" "$osk/run/osk-payload.json" >&2 2>/dev/null || true
  exit 1
fi
echo "tv-headless: all screens open at 1920x1080 and 1280x720 (XMB and rows), first boot opens setup then the tour, the GTK keyboard types ($PYTHON); screenshots in $SHOTS"
