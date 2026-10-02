SHELL := /bin/bash
.DEFAULT_GOAL := help

# Build profile: general (default) and nvidia are the release ISOs; intel and
# imac2013 are legacy single-machine profiles.
PROFILE ?= general
export PROFILE
VERSION := $(shell cat VERSION)
ISO_SUFFIX := $(shell . config/profiles/$(PROFILE)/profile.conf 2>/dev/null && printf '%s' "$$ISO_SUFFIX")
ISO ?= build/out/couchliteos-$(VERSION)-$(if $(ISO_SUFFIX),$(ISO_SUFFIX)-)amd64.iso

.PHONY: help fetch-apps configure build test qemu-smoke qemu-persistence-smoke qemu-install-smoke release-gauntlet release-assets clean

help:
	@printf '%s\n' \
	  'make fetch-apps  Download pinned application images' \
	  'make configure   Prepare the live-build work tree' \
	  'sudo make build  Build the Debian 13 hybrid ISO (PROFILE=general|nvidia|intel|imac2013)' \
	  'make test         Run source/static tests' \
	  'make qemu-smoke   Boot the ISO and wait for the appliance marker' \
	  'make qemu-persistence-smoke  Verify live persistence and recovery boot' \
	  'make qemu-install-smoke  Install to a VM disk and boot it independently' \
	  'make release-gauntlet  Run the final source and real-ISO release gate' \
	  'make release-assets  Write SHA256SUMS for built ISOs and check asset sizes' \
	  'sudo make clean   Remove generated build state' \
	  '' \
	  "Profile: $(PROFILE)  ISO: $(ISO)"

fetch-apps:
	./scripts/fetch-apps.sh

configure: fetch-apps
	./build/configure.sh

build: configure
	./build/build.sh

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
	python3 -m unittest -v tests/test_lb_cache.py
	python3 -m unittest -v tests/test_moonlight_prefs.py
	python3 -m unittest -v tests/test_display_failed.py
	python3 -m unittest -v tests/test_run_app.py
	python3 -m unittest -v tests/test_run_app_errors.py
	python3 -m unittest -v tests/test_network_ready.py
	python3 -m unittest -v tests/test_host_address_modes.py
	python3 -m unittest -v tests/test_bluetoothd_helpers.py
	python3 -m unittest -v tests/test_cage_build.py
	$(MAKE) -C launcher test


qemu-smoke:
	./tests/qemu-smoke.sh "$(ISO)"

qemu-persistence-smoke:
	./tests/qemu-persistence-smoke.sh "$(ISO)"

qemu-install-smoke:
	./tests/qemu-install-smoke.sh "$(ISO)"

release-gauntlet:
	./tools/release-gauntlet.sh "$(ISO)"

release-assets:
	./tools/release-assets.sh

clean:
	./build/clean.sh
