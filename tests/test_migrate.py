# rename:keep-file  (this test names the pre-0.2.0 MoonlightOS paths on purpose)
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("couchliteos_migrate", str(ROOT / "scripts" / "couchliteos-migrate"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
migrate = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(migrate)

OLD_GID = 998
NEW_GID = 1001

MOUNTINFO = (
    "26 25 0:23 / /run rw,nosuid - tmpfs tmpfs rw\n"
    "40 26 8:2 /moonlightos-state /var/lib/moonlightos rw,relatime - ext4 /dev/sda2 rw\n"
    "41 26 8:2 /space/dir /mnt/with\\040space rw - ext4 /dev/sda2 rw\n"
)


def fake_lstat(gids, default=0):
    """lstat stand-in: only st_gid matters; keyed by path, so tests need no root."""
    return lambda path: types.SimpleNamespace(st_gid=gids.get(str(path), default))


class DecisionTest(unittest.TestCase):
    def test_mountpoints_parse_mountinfo_and_unescape_spaces(self):
        mounts = migrate.mountpoints(MOUNTINFO)
        self.assertIn("/var/lib/moonlightos", mounts)
        self.assertIn("/mnt/with space", mounts)
        self.assertNotIn("/var/lib/couchliteos", mounts)

    def test_old_persistence_mounted_and_new_not_means_bind(self):
        plan = migrate.plan(migrate.PAIRS, lambda p: p == "/var/lib/moonlightos")
        self.assertEqual(plan, [("/var/lib/moonlightos", "/var/lib/couchliteos")])

    def test_both_pairs_are_decided_independently(self):
        plan = migrate.plan(migrate.PAIRS, lambda p: p.endswith("/moonlightos"))
        self.assertEqual(plan, [
            ("/var/lib/moonlightos", "/var/lib/couchliteos"),
            ("/var/log/moonlightos", "/var/log/couchliteos"),
        ])

    def test_installed_system_or_new_stick_does_nothing(self):
        self.assertEqual(migrate.plan(migrate.PAIRS, lambda p: False), [])

    def test_new_persistence_already_mounted_wins(self):
        mounted = {"/var/lib/moonlightos", "/var/lib/couchliteos"}
        self.assertEqual(migrate.plan(migrate.PAIRS, mounted.__contains__), [])

    def test_regroup_only_when_gids_really_differ(self):
        self.assertTrue(migrate.wants_regroup(OLD_GID, NEW_GID))
        self.assertFalse(migrate.wants_regroup(NEW_GID, NEW_GID))
        self.assertFalse(migrate.wants_regroup(OLD_GID, None))
        # a tree rooted at gid 0 is root's own: never remap everything root owns
        self.assertFalse(migrate.wants_regroup(0, NEW_GID))


class RegroupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "state"
        (self.root / "home" / ".config").mkdir(parents=True)
        (self.root / "rdp-secrets").mkdir()
        self.paired = self.root / "home" / ".config" / "Moonlight.conf"
        self.paired.write_text("paired")
        self.config = self.root / "config.ini"
        self.config.write_text("[launcher]\n")
        self.secret = self.root / "rdp-secrets" / "host.secret"
        self.secret.write_text("x")
        self.calls = []

    def lchown(self, path, uid, gid):
        self.calls.append((str(path), uid, gid))

    def test_only_entries_owned_by_the_old_gid_change_group_and_never_the_owner(self):
        gids = {str(p): OLD_GID for p in (self.root, self.root / "home", self.root / "home" / ".config",
                                          self.paired, self.config)}
        # rdp-secrets and its file are root:root: they keep gid 0
        count = migrate.regroup(self.root, OLD_GID, NEW_GID, fake_lstat(gids), self.lchown)
        changed = {c[0] for c in self.calls}
        self.assertEqual(changed, {str(p) for p in (self.root, self.root / "home",
                                                    self.root / "home" / ".config", self.paired, self.config)})
        self.assertTrue(all(uid == -1 and gid == NEW_GID for _, uid, gid in self.calls))
        self.assertEqual(count, 5)
        self.assertNotIn(str(self.secret), changed)
        self.assertNotIn(str(self.root / "rdp-secrets"), changed)

    def test_the_root_directory_changes_last_so_an_interrupted_run_resumes(self):
        gids = {str(p): OLD_GID for p in (self.root, self.root / "home", self.paired, self.config)}
        migrate.regroup(self.root, OLD_GID, NEW_GID, fake_lstat(gids), self.lchown)
        self.assertEqual(self.calls[-1][0], str(self.root))

    def test_symlinks_are_not_followed(self):
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.write_text("v")
        link = self.root / "home" / "link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        gids = {str(link): OLD_GID, str(victim): OLD_GID, str(link / "victim"): OLD_GID,
                str(self.root): OLD_GID}
        migrate.regroup(self.root, OLD_GID, NEW_GID, fake_lstat(gids), self.lchown)
        changed = [c[0] for c in self.calls]
        self.assertIn(str(link), changed)  # the link itself, as chown -h does
        self.assertFalse([p for p in changed if p == str(victim) or p.startswith(str(link) + os.sep)])

    def test_real_lchown_is_used_by_default_and_never_chown(self):
        real_gid = os.lstat(self.config).st_gid  # every file here belongs to the current gid
        with mock.patch.object(migrate.os, "lchown", create=True) as lchown,              mock.patch.object(migrate.os, "chown", create=True) as chown:
            migrate.regroup(self.root, real_gid, NEW_GID)
        lchown.assert_any_call(str(self.config), -1, NEW_GID)
        chown.assert_not_called()


class RunTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.old = base / "lib-old"
        self.new = base / "lib-new"
        self.old.mkdir()
        (self.old / "config.ini").write_text("x")
        self.pairs = [(str(self.old), str(self.new))]
        self.mounts = []
        self.chowns = []
        self.events = []

    def mount(self, old, new):
        self.assertTrue(pathlib.Path(new).is_dir(), "the target directory must exist before mounting")
        self.mounts.append((old, new))
        self.events.append(("mount", old, new))

    def seed(self, new, old):
        self.events.append(("seed", new, old))

    def lchown(self, path, uid, gid):
        self.chowns.append((path, uid, gid))

    def gids(self, gid=OLD_GID):
        return fake_lstat({str(self.old): gid, str(self.old / "config.ini"): gid})

    def run_migration(self, is_mount, new_gid=NEW_GID, mount=None, lstat=None):
        return migrate.run(self.pairs, new_gid, is_mount, mount or self.mount,
                           lstat or self.gids(), self.lchown, self.seed)

    def test_old_persistence_is_bound_and_its_group_remapped(self):
        rc = self.run_migration(lambda p: p == str(self.old))
        self.assertEqual(rc, 0)
        self.assertEqual(self.mounts, [(str(self.old), str(self.new))])
        self.assertEqual(self.chowns, [(str(self.old / "config.ini"), -1, NEW_GID),
                                       (str(self.old), -1, NEW_GID)])

    def test_populated_old_persistence_is_never_seeded(self):
        self.run_migration(lambda p: p == str(self.old))
        self.assertEqual([e[0] for e in self.events], ["mount"])

    def test_empty_old_persistence_gets_the_image_defaults_before_binding(self):
        (self.old / "config.ini").unlink()  # an old-style stick never booted with persistence
        self.run_migration(lambda p: p == str(self.old))
        self.assertEqual(self.events, [("seed", str(self.new), str(self.old)),
                                       ("mount", str(self.old), str(self.new))])

    def test_same_gid_binds_without_touching_ownership(self):
        rc = self.run_migration(lambda p: p == str(self.old), lstat=self.gids(NEW_GID))
        self.assertEqual((rc, len(self.mounts), self.chowns), (0, 1, []))

    def test_unknown_new_group_binds_without_touching_ownership(self):
        rc = self.run_migration(lambda p: p == str(self.old), new_gid=None)
        self.assertEqual((rc, len(self.mounts), self.chowns), (0, 1, []))

    def test_no_old_persistence_does_nothing_at_all(self):
        rc = self.run_migration(lambda p: False)
        self.assertEqual((rc, self.mounts, self.chowns), (0, [], []))
        self.assertFalse(self.new.exists())

    def test_new_persistence_mounted_does_nothing(self):
        rc = self.run_migration(lambda p: True)
        self.assertEqual((rc, self.mounts, self.chowns), (0, [], []))

    def test_failed_mount_reports_failure_and_leaves_ownership_alone(self):
        def broken(old, new):
            raise subprocess.CalledProcessError(32, ["mount"])
        rc = self.run_migration(lambda p: p == str(self.old), mount=broken)
        self.assertEqual((rc, self.chowns), (1, []))

    def test_second_run_after_migration_changes_nothing(self):
        mounted = {str(self.old)}
        self.run_migration(mounted.__contains__)
        self.mounts.clear()
        self.chowns.clear()
        # next boot: the root (and so everything) already carries the new gid
        rc = self.run_migration(mounted.__contains__, lstat=self.gids(NEW_GID))
        self.assertEqual((rc, self.chowns), (0, []))


class SeedTest(unittest.TestCase):
    def test_seed_copies_the_image_directory_contents_keeping_ownership(self):
        with mock.patch.object(migrate.subprocess, "run") as run:
            migrate.seed_from_image("/var/lib/couchliteos", "/var/lib/moonlightos")
        run.assert_called_once_with(
            ["/usr/bin/cp", "-a", "--", "/var/lib/couchliteos/.", "/var/lib/moonlightos"], check=True)


class BindMountTest(unittest.TestCase):
    def test_bind_mount_runs_mount_without_a_shell(self):
        with mock.patch.object(migrate.subprocess, "run") as run:
            migrate.bind_mount("/var/lib/moonlightos", "/var/lib/couchliteos")
        run.assert_called_once_with(["/usr/bin/mount", "--bind", "/var/lib/moonlightos", "/var/lib/couchliteos"],
                                    check=True)


if __name__ == "__main__":
    unittest.main()
