import testenv  # noqa: F401  (first: scratch run and state directories)
"""couchliteos_safefile: root's writes never follow a name the couchliteos user could swap (real symlinks)."""

import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

import couchliteos_safefile as safefile

ROOT = os.geteuid() == 0


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.user = self.tmp / "user"   # stands for /run/couchliteos or /var/lib/couchliteos
        self.user.mkdir()
        self.secret = self.tmp / "shadow"  # stands for a file only root may change
        self.secret.write_text("root:secret\n")
        self.secret.chmod(0o600)

    def untouched(self):
        self.assertEqual(self.secret.read_text(), "root:secret\n")
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)


class WriteAtomicTest(Case):
    def test_writes_with_the_mode_and_leaves_no_temporary(self):
        target = self.user / "status.json"
        safefile.write_atomic(target, "{}\n", 0o644)
        self.assertEqual(target.read_text(), "{}\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(os.listdir(self.user), ["status.json"])

    def test_a_symlinked_target_is_replaced_never_followed(self):
        target = self.user / "config.ini"
        target.symlink_to(self.secret)
        safefile.write_atomic(target, "[update]\n", 0o640, (os.getuid(), os.getgid()))
        self.untouched()
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), "[update]\n")
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o640)

    def test_a_symlink_planted_at_the_temporary_name_is_never_written_through(self):
        target = self.user / "whatsnew-seen"
        names = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
        (self.user / ".whatsnew-seen.aaaaaaaaaaaa").symlink_to(self.secret)
        with mock.patch.object(safefile.secrets, "token_hex", lambda _n: next(names)):
            safefile.write_atomic(target, "0.3.0\n", 0o644)
        self.untouched()
        self.assertEqual(target.read_text(), "0.3.0\n")
        self.assertTrue((self.user / ".whatsnew-seen.aaaaaaaaaaaa").is_symlink(), "the planted link is left alone")
        self.assertFalse(os.path.lexists(self.user / ".whatsnew-seen.bbbbbbbbbbbb"))

    def test_a_symlinked_directory_is_refused(self):
        (self.tmp / "etc").mkdir()
        linked = self.tmp / "linked"
        linked.symlink_to(self.tmp / "etc")
        with self.assertRaises(OSError):
            safefile.write_atomic(linked / "passwd", "evil\n", 0o644)
        self.assertEqual(os.listdir(self.tmp / "etc"), [])

    def test_mode_and_owner_are_set_on_the_descriptor(self):
        calls = []
        safefile.write_atomic(self.user / "f", "x", 0o600, (1000, 1000), lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0][0], int, "chown gets the descriptor, not a path")
        self.assertEqual(calls[0][1:], (1000, 1000))

    def test_a_failure_removes_the_temporary_and_keeps_the_old_file(self):
        target = self.user / "f"
        target.write_text("old")

        def broken(*_args):
            raise PermissionError("no")

        with self.assertRaises(PermissionError):
            safefile.write_atomic(target, "new", 0o644, (1, 1), broken)
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(os.listdir(self.user), ["f"])


class RootDirTest(Case):
    def test_a_missing_directory_is_made_root_only(self):
        path = safefile.root_dir(self.tmp / "work", os.geteuid())
        found = os.lstat(path)
        self.assertTrue(stat.S_ISDIR(found.st_mode))
        self.assertEqual(stat.S_IMODE(found.st_mode), 0o700)
        self.assertEqual(found.st_uid, os.geteuid())

    def test_the_mode_of_our_own_directory_is_put_right(self):
        path = self.tmp / "work"
        path.mkdir(mode=0o777)
        path.chmod(0o777)
        safefile.root_dir(path, os.geteuid())
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)
        safefile.root_dir(path, os.geteuid(), 0o755)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o755)

    def test_a_symlink_is_refused_and_its_target_left_alone(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o755)
        elsewhere.chmod(0o755)
        (self.tmp / "work").symlink_to(elsewhere)
        with self.assertRaises(safefile.NotOurs):
            safefile.root_dir(self.tmp / "work", os.geteuid())
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o755)

    def test_a_file_is_refused(self):
        (self.tmp / "work").write_text("")
        with self.assertRaises(safefile.NotOurs):
            safefile.root_dir(self.tmp / "work", os.geteuid())

    def test_a_directory_of_someone_else_is_refused(self):
        path = self.tmp / "work"
        path.mkdir(mode=0o700)
        with self.assertRaises(safefile.NotOurs):
            safefile.root_dir(path, os.geteuid() + 1)

    @unittest.skipUnless(ROOT, "needs root to give a directory away")
    def test_a_directory_the_user_made_is_refused_as_root(self):
        path = self.tmp / "work"
        path.mkdir(mode=0o700)
        os.chown(path, 1000, 1000)
        with self.assertRaises(safefile.NotOurs):
            safefile.root_dir(path, 0)
        self.assertEqual(os.lstat(path).st_uid, 1000)


class AppendLineTest(Case):
    def test_lines_go_to_a_new_log_in_a_new_root_directory(self):
        log = self.tmp / "log" / "update.log"
        safefile.append_line(log, "one")
        safefile.append_line(log, "two\n")
        self.assertEqual(log.read_text(), "one\ntwo\n")
        self.assertEqual(stat.S_IMODE(os.lstat(log.parent).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.lstat(log).st_mode), 0o644)

    def test_a_symlinked_log_is_never_followed(self):
        (self.tmp / "log").mkdir()
        (self.tmp / "log" / "update.log").symlink_to(self.secret)
        with self.assertRaises(OSError):
            safefile.append_line(self.tmp / "log" / "update.log", "evil")
        self.untouched()

    def test_a_symlinked_log_directory_is_refused(self):
        (self.tmp / "log").symlink_to(self.user)
        with self.assertRaises(OSError):
            safefile.append_line(self.tmp / "log" / "update.log", "evil")
        self.assertEqual(os.listdir(self.user), [])

    def test_a_fifo_is_not_a_log(self):
        (self.tmp / "log").mkdir()
        os.mkfifo(self.tmp / "log" / "update.log")
        with self.assertRaises(OSError):
            safefile.append_line(self.tmp / "log" / "update.log", "x")


class LockTest(Case):
    def test_a_symlinked_lock_is_refused(self):
        (self.tmp / "update.lock").symlink_to(self.secret)
        with self.assertRaises(OSError):
            safefile.open_lock(self.tmp / "update.lock")
        self.untouched()

    def test_the_lock_is_made_root_only(self):
        with safefile.open_lock(self.tmp / "update.lock"):
            pass
        self.assertEqual(stat.S_IMODE(os.lstat(self.tmp / "update.lock").st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
