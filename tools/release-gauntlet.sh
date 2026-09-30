#!/bin/bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ISO=${1:-$ROOT/build/out/moonlightos-$(< "$ROOT/VERSION")-amd64.iso}
cd "$ROOT"

for report in \
  "${MOONLIGHTOS_QEMU_LOG:-}" \
  "${MOONLIGHTOS_QEMU_SCREENSHOT:-}" \
  "${MOONLIGHTOS_QEMU_INSTALL_LOG:-}" \
  "${MOONLIGHTOS_QEMU_INSTALLED_BOOT_LOG:-}" \
  "${MOONLIGHTOS_QEMU_INSTALL_MENU_SCREENSHOT:-}" \
  "${MOONLIGHTOS_QEMU_INSTALL_EDITOR_SCREENSHOT:-}" \
  "${MOONLIGHTOS_QEMU_INSTALLER_SCREENSHOT:-}" \
  "${MOONLIGHTOS_QEMU_INSTALLED_SCREENSHOT:-}" \
  "${MOONLIGHTOS_QEMU_INSTALL_CONFIG:-}" \
  "${MOONLIGHTOS_QEMU_PERSISTENCE_LOG:-}"; do
  [[ -z "$report" ]] || rm -f -- "$report"
done

tools/source-state.sh
git diff --check
make test
python3 tools/mutants.py
make qemu-smoke ISO="$ISO"
make qemu-install-smoke ISO="$ISO"
make qemu-persistence-smoke ISO="$ISO"

printf 'MoonlightOS release gauntlet passed.\n'
