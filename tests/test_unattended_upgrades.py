"""The generated unattended-upgrades configuration: the origin allowlist and the held pins."""

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "build" / "unattended-upgrades.sh"
PINS = ROOT / "build" / "apt-pins.sh"
LISTS = sorted((ROOT / "config" / "live-build" / "package-lists").glob("*.list.chroot"))
PROFILE_LISTS = sorted((ROOT / "config" / "profiles").glob("*/package-lists/*.list.chroot"))


def bash(*args):
    return subprocess.run(["bash", *map(str, args)], capture_output=True, text=True, check=True).stdout


def block(text, name):
    match = re.search(rf"^{re.escape(name)} {{\n(.*?)^}};", text, re.MULTILINE | re.DOTALL)
    assert match, name
    return re.findall(r'"([^"]*)";', match.group(1))


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class GeneratedConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = bash(GENERATOR, *LISTS, *PROFILE_LISTS)

    def test_only_debian_security_google_and_tailscale_are_allowed(self):
        self.assertEqual(block(self.text, "Unattended-Upgrade::Origins-Pattern"), [
            "origin=Debian,codename=trixie-security,label=Debian-Security",
            "origin=Google LLC",
            "origin=Tailscale",
        ])

    def test_the_package_defaults_are_cleared_first(self):
        # apt.conf lists add up across files: without #clear, 50unattended-upgrades' Debian
        # origins (main updates, too) would stay allowed.
        for name in ("Allowed-Origins", "Origins-Pattern", "Package-Blacklist"):
            clear = self.text.index(f"#clear Unattended-Upgrade::{name};")
            if name != "Allowed-Origins":
                self.assertLess(clear, self.text.index(f"Unattended-Upgrade::{name} {{"))
        self.assertNotIn("Allowed-Origins {", self.text)

    def test_every_build_pin_is_held(self):
        pinned = re.findall(r"^Package: (\S+)$", bash(PINS, *LISTS, *PROFILE_LISTS), re.MULTILINE)
        self.assertIn("tailscale", pinned)
        held = block(self.text, "Unattended-Upgrade::Package-Blacklist")
        for package in [*pinned, "cage", "gamescope"]:
            self.assertIn(f"^{re.escape(package)}$", held)

    def test_a_new_pin_is_held_without_editing_the_generator(self):
        with tempfile.TemporaryDirectory() as directory:
            extra = pathlib.Path(directory) / "extra.list.chroot"
            extra.write_text("# comment\nlibfoo1.2=1.2.3-1\nplain-package\n")
            held = block(bash(GENERATOR, extra), "Unattended-Upgrade::Package-Blacklist")
        self.assertIn(r"^libfoo1\.2$", held)
        self.assertNotIn("^plain-package$", held)

    def test_the_box_never_restarts_and_apts_own_timers_do_nothing(self):
        for line in ('Unattended-Upgrade::Automatic-Reboot "false";', 'APT::Periodic::Unattended-Upgrade "0";',
                     'APT::Periodic::Update-Package-Lists "0";'):
            self.assertIn(line, self.text)
        self.assertNotIn('Automatic-Reboot "true"', self.text)

    def test_the_image_gets_it_after_the_packages_own_file(self):
        configure = (ROOT / "build" / "configure.sh").read_text()
        self.assertIn('"$ROOT/build/unattended-upgrades.sh" "$WORK"/config/package-lists/*.list.chroot', configure)
        self.assertIn("/etc/apt/apt.conf.d/52couchliteos-unattended", configure)
        self.assertGreater("52couchliteos-unattended", "50unattended-upgrades")
        self.assertGreater("52couchliteos-unattended", "20auto-upgrades")


class PackagingTest(unittest.TestCase):
    def test_unattended_upgrades_is_installed(self):
        packages = (ROOT / "config/live-build/package-lists/couchliteos.list.chroot").read_text().split()
        self.assertIn("unattended-upgrades", packages)
        self.assertIn("squashfs-tools", packages)

    def test_the_units_are_enabled_in_the_image(self):
        hook = (ROOT / "config/live-build/hooks/live/0100-couchliteos.hook.chroot").read_text()
        self.assertIn("systemctl enable couchliteos-security-update.timer couchliteos-security-update.path", hook)
        self.assertIn("systemctl enable couchliteos-app-update.timer couchliteos-app-update.path", hook)
        self.assertIn("install -d -o root -g root -m 0755 /var/lib/couchliteos/apps", hook)
        self.assertLess(hook.index("chown -R couchliteos:couchliteos /var/lib/couchliteos"),
                        hook.index("install -d -o root -g root -m 0755 /var/lib/couchliteos/apps"))

    def test_both_services_run_under_an_inhibitor_and_never_restart_the_box(self):
        for name, why in (("security", "Installing security fixes"), ("app", "Updating apps")):
            service = (ROOT / f"services/couchliteos-{name}-update.service").read_text()
            self.assertIn(f'ExecStart=/usr/bin/systemd-inhibit --what=sleep:idle --why="{why}" /usr/bin/python3 '
                          f"/usr/libexec/couchliteos_{name}update.py run", service)
            self.assertNotIn("reboot", service.lower())
            timer = (ROOT / f"services/couchliteos-{name}-update.timer").read_text()
            self.assertIn("OnCalendar=daily", timer)
            self.assertIn("RandomizedDelaySec=", timer)
            self.assertIn("Persistent=true", timer)

    def test_the_app_service_only_writes_its_own_folders(self):
        service = (ROOT / "services/couchliteos-app-update.service").read_text()
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ReadWritePaths=/var/lib/couchliteos /var/cache/couchliteos /var/log/couchliteos "
                      "/run/couchliteos -/var/lib/flatpak", service)

    def test_the_request_files_match_the_modules(self):
        import sys

        sys.path.insert(0, str(ROOT / "launcher"))
        try:
            import couchliteos_appupdate as appupdate
            import couchliteos_securityupdate as security
        finally:
            sys.path.pop(0)
        app_path = (ROOT / "services/couchliteos-app-update.path").read_text()
        for name in (appupdate.INSTALL_REQUEST, appupdate.ROLLBACK_REQUEST, appupdate.HEALTH_REQUEST):
            self.assertIn(f"PathExists=/run/couchliteos/{name}\n", app_path)
        self.assertIn(f"PathExists=/run/couchliteos/{security.REQUEST_NAME}\n",
                      (ROOT / "services/couchliteos-security-update.path").read_text())
        run_app = (ROOT / "scripts/couchliteos-run-app").read_text()
        self.assertIn(f'"$RUN_DIR/{appupdate.HEALTH_REQUEST}"', run_app)

    def test_the_modules_are_installed(self):
        configure = (ROOT / "build" / "configure.sh").read_text()
        for module in ("couchliteos_busy.py", "couchliteos_appupdate.py", "couchliteos_securityupdate.py"):
            self.assertIn(f'"$ROOT/launcher/{module}" "$CHROOT/usr/libexec/{module}"', configure)

    def test_the_app_folders_exist_on_boxes_installed_before_app_updates(self):
        tmpfiles = (ROOT / "overlay/etc/tmpfiles.d/couchliteos.conf").read_text()
        self.assertIn("d /var/lib/couchliteos/apps 0755 root root -", tmpfiles)
        self.assertIn("d /var/cache/couchliteos 0755 root root -", tmpfiles)


if __name__ == "__main__":
    unittest.main()
