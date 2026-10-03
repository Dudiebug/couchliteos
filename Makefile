SHELL := /bin/bash
.DEFAULT_GOAL := help

# Build profile: general (default) and nvidia are the release ISOs; intel and
# imac2013 are legacy single-machine profiles.
PROFILE ?= general
export PROFILE
VERSION := $(shell cat VERSION)
ISO_SUFFIX := $(shell . config/profiles/$(PROFILE)/profile.conf 2>/dev/null && printf '%s' "$$ISO_SUFFIX")
ISO ?= build/out/couchliteos-$(VERSION)-$(if $(ISO_SUFFIX),$(ISO_SUFFIX)-)amd64.iso
# RELEASE=1 builds a release ISO (xz squashfs).
# Without it `make build` makes a test ISO (zstd squashfs; docs/BUILDING.md).
# FRESH=1 installs every package even when COUCHLITEOS_LB_CACHE holds a matching
# chroot snapshot.
RELEASE ?= 0
FRESH ?= 0
# tests/tv-headless.sh skips when cage, grim or a GTK 4 Python is missing; CI passes
# TV_HEADLESS_ARGS= (empty) so a missing tool fails the run instead.
TV_HEADLESS_ARGS ?= --if-available

.PHONY: help fetch-apps configure build test qemu-smoke qemu-persistence-smoke qemu-install-smoke qemu-legacy-smoke release-gauntlet release-assets release-check clean

help:
	@printf '%s\n' \
	  'make fetch-apps  Download pinned application images' \
	  'make configure   Prepare the live-build work tree' \
	  'sudo make build  Build a test ISO (PROFILE=general|nvidia|intel|imac2013; RELEASE=1 for a release ISO)' \
	  'make test         Run source/static tests' \
	  'make qemu-smoke   Boot the ISO and wait for the appliance marker' \
	  'make qemu-persistence-smoke  Verify live persistence and recovery boot' \
	  'make qemu-install-smoke  Install to a VM disk and boot it independently' \
	  'make qemu-legacy-smoke OLD_ISO=old.iso  Install an older ISO, update it from this ISO, boot it' \
	  'make release-gauntlet  Run the final source and real-ISO release gate (OLD_ISO=old.iso adds the legacy update)' \
	  'make release-assets  Write SHA256SUMS for built ISOs and check asset sizes' 	  'make release-check  Check versions, public notes, the tag and SHA256SUMS before publishing' \
	  'sudo make clean   Remove generated build state' \
	  '' \
	  "Profile: $(PROFILE)  ISO: $(ISO)"

fetch-apps:
	./scripts/fetch-apps.sh

configure: fetch-apps
	./build/configure.sh

# Tests (skipped when `make test` already passed for this exact tree), configure, build;
# then the time each stage took.
build: export COUCHLITEOS_TIMINGS := $(CURDIR)/build/out/build-times.txt
build:
	@mkdir -p build/out && : > build/out/build-times.txt
	./build/timed.sh tests ./build/test-gate.sh run
	./build/timed.sh configure $(MAKE) configure
	./build/build.sh $(if $(filter 1,$(RELEASE)),--release) $(if $(filter 1,$(FRESH)),--fresh)
	@./build/timed.sh --summary

test:
	./tests/test-static.sh
	python3 -m unittest -v tests/test_host_address.py
	python3 -m unittest -v tests/test_migrate.py
	python3 -m unittest -v tests/test_boot_order.py
	python3 -m unittest -v tests/test_support.py
	python3 -m unittest -v tests/test_nvidia_firmware.py
	python3 -m unittest -v tests/test_tailscale_enrollment.py
	python3 -m unittest -v tests/test_terminal_apps.py
	python3 -m unittest -v tests/test_log_limits.py
	python3 -m unittest -v tests/test_bluetooth_service.py
	python3 -m unittest -v tests/test_qemu_iso_boot.py
	python3 -m unittest -v tests/test_rdp_secret.py
	python3 -m unittest -v tests/test_hwdetect.py
	python3 -m unittest -v tests/test_faster.py
	python3 -m unittest -v tests/test_boot_time.py
	python3 -m unittest -v tests/test_lb_cache.py
	python3 -m unittest -v tests/test_fast_build.py
	python3 -m unittest -v tests/test_moonlight_prefs.py
	python3 -m unittest -v tests/test_display_failed.py
	python3 -m unittest -v tests/test_run_app.py
	python3 -m unittest -v tests/test_run_app_errors.py
	python3 -m unittest -v tests/test_network_ready.py
	python3 -m unittest -v tests/test_host_address_modes.py
	python3 -m unittest -v tests/test_bluetoothd_helpers.py
	python3 -m unittest -v tests/test_cage_build.py
	python3 -m unittest -v tests/test_tv_fallback.py
	if [ -z '$(TV_HEADLESS_ARGS)' ] || command -v cage >/dev/null; then ./tests/tv-headless.sh $(TV_HEADLESS_ARGS); else echo 'tv-headless: skipped: cage is not installed'; fi
	$(MAKE) -C launcher test
	./build/test-gate.sh mark

qemu-smoke:
	./tests/qemu-smoke.sh "$(ISO)"

qemu-persistence-smoke:
	./tests/qemu-persistence-smoke.sh "$(ISO)"

qemu-install-smoke:
	./tests/qemu-install-smoke.sh "$(ISO)"

# OLD_ISO: an older published ISO (0.2.0, or MoonlightOS 0.1.13) to update from.
qemu-legacy-smoke:
	@[ -n "$(OLD_ISO)" ] || { echo 'usage: make qemu-legacy-smoke OLD_ISO=path/to/older.iso' >&2; exit 64; }
	./tests/qemu-legacy-smoke.sh "$(OLD_ISO)" "$(ISO)"

release-gauntlet:
	OLD_ISO="$(OLD_ISO)" ./tools/release-gauntlet.sh "$(ISO)"

release-assets:
	./tools/release-assets.sh

release-check:
	./tools/release-check.sh

clean:
	./build/clean.sh
