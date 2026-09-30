#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ISO=${1:-$ROOT/build/out/couchliteos-$(< "$ROOT/VERSION")-amd64.iso}
cd "$ROOT"

for report in \
  "${COUCHLITEOS_QEMU_LOG:-}" \
  "${COUCHLITEOS_QEMU_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_LOG:-}" \
  "${COUCHLITEOS_QEMU_INSTALLED_BOOT_LOG:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_MENU_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_EDITOR_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALLER_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALLED_SCREENSHOT:-}" \
  "${COUCHLITEOS_QEMU_INSTALL_CONFIG:-}" \
  "${COUCHLITEOS_QEMU_PERSISTENCE_LOG:-}"; do
  [[ -z "$report" ]] || rm -f -- "$report"
done

tools/source-state.sh
git diff --check
make test
python3 tools/mutants.py
make qemu-smoke ISO="$ISO"
make qemu-install-smoke ISO="$ISO"
make qemu-persistence-smoke ISO="$ISO"

printf 'CouchLiteOS release gauntlet passed.\n'
