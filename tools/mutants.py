#!/usr/bin/env python3
"""Small auditable mutation check for the keyboard driver and RDP security rules."""

from __future__ import annotations

import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
ISO_BOOT = ("tests/qemu_iso_boot.py", ["tests/test_qemu_iso_boot.py"], ROOT)
RDP = ("launcher/couchliteos_rdp.py", ["test_rdp.py", "test_app_runner.py"], ROOT / "launcher")
RUNNER = ("launcher/couchliteos_app_runner.py", ["test_app_runner.py"], ROOT / "launcher")
LAUNCHER = ("launcher/couchliteos-launcher.py", ["test_launcher.py"], ROOT / "launcher")
EXPORT = ("scripts/couchliteos-support-export", ["tests/test_support.py"], ROOT)
PERSIST = ("scripts/couchliteos-persist-setup", ["tests/test_persist_setup.py"], ROOT)
APPUPDATE = ("launcher/couchliteos_appupdate.py", ["test_appupdate.py"], ROOT / "launcher")
SESSION = ("scripts/couchliteos-session", ["tests/test_tv_fallback.py"], ROOT)
MUTANTS = (
    (
        ISO_BOOT,
        "wrong installer menu index",
        'commands = [("sendkey home", 0.25)] * 60\n    commands += [("sendkey down", 0.15)] * 3',
        'commands = [("sendkey home", 0.25)] * 60\n    commands += [("sendkey down", 0.15)] * 2',
    ),
    (
        ISO_BOOT,
        "skip required second editor line movement",
        '("sendkey down", 0.5),\n        ("sendkey end", 0.1),',
        '("sendkey end", 0.1),',
    ),
    (
        ISO_BOOT,
        "drop uppercase modifier",
        'return f"shift-{character.lower()}"',
        'return character.lower()',
    ),
    (
        ISO_BOOT,
        "type underscore as hyphen",
        '"_": "shift-minus",',
        '"_": "minus",',
    ),
    (
        RDP,
        "stop reading the password from stdin",
        '"/from-stdin:force",',
        '"/from-stdin",',
    ),
    (
        RDP,
        "accept any server certificate",
        'f"/cert:deny,fingerprint:sha256:{connection.certificate}",',
        '"/cert:ignore",',
    ),
    (
        RDP,
        "allow IPv6 literals",
        "if address.version != 4:",
        "if address.version == 0:",
    ),
    (
        RUNNER,
        "retry authentication failures",
        'RDP_FINAL_EXITS.update({code: "remote desktop licensing failed" for code in range(16, 27)})',
        'RDP_FINAL_EXITS.update({code: "remote desktop licensing failed" for code in range(16, 27)})\n'
        "RDP_FINAL_EXITS.pop(132)",
    ),
    (
        RUNNER,
        "keep the password after a final failure",
        "        if finished:\n            rdp.clear_session_state(run_dir)",
        "        if False:\n            rdp.clear_session_state(run_dir)",
    ),
    (
        LAUNCHER,
        "connect despite a changed certificate",
        "elif connection.certificate != fingerprint:",
        "elif False:",
    ),
    (
        EXPORT,
        "stop redacting FreeRDP passwords",
        '    text = FREERDP_SECRET_ARGUMENT.sub(r"\\1[REDACTED]", text)\n',
        "",
    ),
    (
        PERSIST,
        "format the new partition without checking where the kernel has it",
        "            if kernel_start(env, plan.disk, argv[-1]) != planned.start:",
        "            if False:",
    ),
    (
        PERSIST,
        "start the data partition inside the ISO",
        "    start = -(-max(end, align) // align) * align",
        "    start = -(-max(end - align, align) // align) * align",
    ),
    (
        PERSIST,
        "leave the backup GPT inside the new partition",
        '        relocate = int(table.get("lastlba") or 0) < usable_end - 1',
        "        relocate = False",
    ),
    (
        PERSIST,
        "persist the whole root",
        '    return "\\n".join(lines) + "\\n"\n\n\ndef ssh_host_keys',
        '    return "\\n".join(lines + ["/ union"]) + "\\n"\n\n\ndef ssh_host_keys',
    ),
    (
        APPUPDATE,
        "take an unreachable GitHub for a release without an app list",
        "missing += isinstance(error, urllib.error.HTTPError) and error.code == 404",
        "missing += 1",
    ),
    (
        APPUPDATE,
        "install a download whose checksum does not match",
        "    if digest.hexdigest() != entry.sha256:",
        "    if False:",
    ),
    (
        SESSION,
        "ignore the QEMU timeout scale",
        "READY_SECONDS=$(( ${COUCHLITEOS_READY_SECONDS:-12} * scale ))",
        "READY_SECONDS=${COUCHLITEOS_READY_SECONDS:-12}",
    ),
    (
        SESSION,
        "accept any number as the scale",
        "[[ ${scale-} =~ ^[1-9]$ ]] || scale=1",
        "[[ ${scale-} =~ ^[0-9]+$ ]] || scale=1",
    ),
)


def clear_bytecode(target: pathlib.Path) -> None:
    for cache in (target.parent / "__pycache__").glob(f"{target.stem}.*.pyc"):
        cache.unlink()


def main() -> int:
    killed = 0
    for (relative, tests, directory), name, before, after in MUTANTS:
        target = ROOT / relative
        original = target.read_text(encoding="utf-8")
        try:
            if original.count(before) != 1:
                raise RuntimeError(f"mutation anchor is not unique: {name}")
            target.write_text(original.replace(before, after), encoding="utf-8")
            clear_bytecode(target)
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "-q", *tests],
                cwd=directory,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                print(f"SURVIVED: {name}", file=sys.stderr)
            else:
                killed += 1
                print(f"killed: {name}")
        finally:
            target.write_text(original, encoding="utf-8")
            clear_bytecode(target)

    print(f"manual mutation: {killed}/{len(MUTANTS)} killed")
    return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
