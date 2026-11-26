import contextlib
import ctypes
import os
import stat
import sys
import time
import types
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

from downkeep import trash
from downkeep.model import ActionError, Identity
from tests.helpers import FolderTest

if trash.LINUX:
    try:
        from downkeep import engine
    except ImportError:
        engine = None
else:
    engine = None


def identity_of(path):
    info = os.stat(path, follow_symlinks=False)
    return Identity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class PlatformTest(FolderTest):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.folder.parent / "data")})
        patch.start()
        self.addCleanup(patch.stop)

    def test_only_where_it_is_safe(self):
        self.assertEqual(trash.SUPPORTED, sys.platform in ("linux", "win32"))
        self.assertEqual(trash.CAN_RESTORE, sys.platform == "linux")

    def test_unsupported_systems_touch_nothing(self):
        path = self.add("a.txt", b"one")
        with mock.patch("downkeep.trash.SUPPORTED", False), mock.patch("downkeep.trash.CAN_RESTORE", False):
            with self.assertRaises(ActionError):
                trash.send(str(path), identity_of(path))
            with self.assertRaisesRegex(ActionError, "Recycle Bin"):
                trash.restore(str(path), str(path), identity_of(path))
        with mock.patch("downkeep.trash.LINUX", False):
            self.assertIsNone(trash.find(str(path), identity_of(path)))
        self.assertEqual(path.read_bytes(), b"one")

    def test_files_that_are_refused_before_anything_happens(self):
        path = self.add("a.txt", b"one")
        identity = identity_of(path)
        self.add("b.txt", b"two")
        try:
            os.link(self.folder / "b.txt", self.folder / "c.txt")
        except OSError:
            pass
        (self.folder / "d").mkdir()
        cases = {
            "missing": ("missing", identity, "isn't there"),
            "a folder": ("d", identity, "changed"),
            "another file": ("b.txt", identity, "changed"),
            "size": ("a.txt", replace(identity, size=4), "changed"),
            "time": ("a.txt", replace(identity, mtime_ns=1), "changed"),
            "inode": ("a.txt", replace(identity, inode=1), "changed"),
            "device": ("a.txt", replace(identity, device=1), "changed"),
        }
        if (self.folder / "c.txt").exists():
            cases["two names"] = ("b.txt", identity_of(self.folder / "b.txt"), "more than one name")
        with mock.patch("downkeep.trash.SUPPORTED", True), mock.patch("downkeep.trash.WINDOWS", True), \
                mock.patch("downkeep.trash._recycle", side_effect=AssertionError("went on")):
            for label, (name, wanted, why) in cases.items():
                with self.subTest(label), self.assertRaisesRegex(ActionError, why):
                    trash.send(str(self.folder / name), wanted)
            with contextlib.chdir(self.folder), self.assertRaisesRegex(ActionError, "link"):
                trash.send("a.txt", identity)

    def test_a_folder_reached_through_a_link(self):
        path = self.add("a.txt", b"one")
        os.symlink(self.folder, self.folder.parent / "link") if os.name == "posix" else self.skipTest("links")
        with mock.patch("downkeep.trash.SUPPORTED", True), self.assertRaisesRegex(ActionError, "link"):
            trash.send(str(self.folder.parent / "link" / "a.txt"), identity_of(path))
        self.assertTrue(path.exists())

    def test_what_is_in_a_trash(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.folder / "share")}):
            for name, inside in {
                "share/Trash": True, "share/Trash/files/a": True, "share/Trashy/a": False, "share/a": False,
                ".Trash/1000/files/a": True, ".Trash-1000/files/a": True, "$Recycle.Bin/x": True, "Trash/a": False,
                ".Trashed": False,
            }.items():
                with self.subTest(name):
                    self.assertEqual(trash.holds(str(self.folder / name)), inside)


@unittest.skipUnless(engine and engine.SUPPORTED, "needs Linux and the engine")
class LinuxTest(FolderTest):
    def setUp(self):
        super().setUp()
        self.data = self.folder.parent / "data"
        self.files, self.info = self.data / "Trash" / "files", self.data / "Trash" / "info"
        patch = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data)})
        patch.start()
        self.addCleanup(patch.stop)

    def send(self, path):
        return trash.send(str(path), identity_of(path))

    def notes(self):
        return sorted(os.listdir(self.info)) if self.info.exists() else []

    def stayed(self, path, content=b"one"):
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.notes(), [])

    def test_there_and_back_byte_for_byte(self):
        content = os.urandom(300_000)
        path = self.add("a b.bin", content)
        os.chmod(path, 0o640)
        before, identity = path.stat(), identity_of(path)
        place = self.send(path)
        self.assertEqual(place, str(self.files / "a b.bin"))
        self.assertFalse(path.exists())
        trash.restore(str(path), place, identity)
        after = path.stat()
        self.assertEqual(path.read_bytes(), content)
        for name in ("st_ino", "st_mode", "st_mtime_ns", "st_size", "st_nlink"):
            self.assertEqual(getattr(after, name), getattr(before, name))
        self.assertEqual((os.listdir(self.files), self.notes()), ([], []))

    def test_the_note_follows_the_specification(self):
        path = self.add("100% new\nline é.txt", b"one")
        before = int(time.time())
        place = self.send(path)
        lines = (self.info / (os.path.basename(place) + ".trashinfo")).read_text("ascii").splitlines()
        folder = str(self.folder)
        self.assertEqual(lines[:2], ["[Trash Info]", f"Path={folder}/100%25%20new%0Aline%20%C3%A9.txt"])
        self.assertTrue(lines[2].startswith("DeletionDate="))
        when = time.mktime(time.strptime(lines[2].removeprefix("DeletionDate="), "%Y-%m-%dT%H:%M:%S"))
        self.assertLessEqual(abs(when - before), 2)
        self.assertEqual(len(lines), 3)
        for folder in (self.files.parent, self.files, self.info):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_names_that_are_not_text(self):
        path = os.fsdecode(os.fsencode(str(self.folder)) + b"/bad\xff.txt")
        try:
            Path(path).write_bytes(b"one")
        except OSError:
            self.skipTest("this disk wants text names")
        identity = identity_of(path)
        place = trash.send(path, identity)
        self.assertIn(b"bad%FF.txt", next(self.info.iterdir()).read_bytes())
        trash.restore(path, place, identity)
        self.assertEqual((Path(path).read_bytes(), self.notes()), (b"one", []))

    def test_same_name_twice(self):
        places = []
        for content in (b"one", b"two", b"three"):
            places.append(self.send(self.add("a.tar.gz", content)))
        self.assertEqual([os.path.basename(place) for place in places], ["a.tar.gz", "a.tar 2.gz", "a.tar 3.gz"])
        self.assertEqual([Path(place).read_bytes() for place in places], [b"one", b"two", b"three"])

    def test_a_file_without_a_note_is_not_replaced(self):
        self.send(self.add("other.txt", b"x"))
        (self.files / "a.txt").write_bytes(b"orphan")
        place = self.send(self.add("a.txt", b"one"))
        self.assertEqual(os.path.basename(place), "a 2.txt")
        self.assertEqual((self.files / "a.txt").read_bytes(), b"orphan")
        self.assertEqual(self.notes(), ["a 2.txt.trashinfo", "other.txt.trashinfo"])

    def test_a_note_without_a_file_is_not_replaced(self):
        self.send(self.add("other.txt", b"x"))
        (self.info / "a.txt.trashinfo").write_bytes(b"theirs")
        place = self.send(self.add("a.txt", b"one"))
        self.assertEqual(os.path.basename(place), "a 2.txt")
        self.assertEqual((self.info / "a.txt.trashinfo").read_bytes(), b"theirs")

    def test_the_name_is_taken_at_the_last_moment(self):
        path = self.add("a.txt", b"one")
        real = engine.move_no_replace

        def race(source, dest):
            if not (self.files / "a.txt").exists():
                (self.files / "a.txt").write_bytes(b"theirs")
            real(source, dest)

        with mock.patch("downkeep.engine.move_no_replace", race):
            place = self.send(path)
        self.assertEqual(Path(place).read_bytes(), b"one")
        self.assertEqual((self.files / "a.txt").read_bytes(), b"theirs")
        self.assertEqual(self.notes(), ["a 2.txt.trashinfo"])

    def test_long_names(self):
        for name in ("x" * 251 + ".pdf", "é" * 125 + ".txt", "x." + "y" * 250, "y" * 255):
            for content in (b"one", b"two"):
                with self.subTest(name[:3], content=content):
                    path = self.add(name, content)
                    identity = identity_of(path)
                    place = self.send(path)
                    self.assertEqual(Path(place).read_bytes(), content)
                    self.assertTrue((self.info / (os.path.basename(place) + ".trashinfo")).exists())
                    if content == b"two":
                        trash.restore(str(path), place, identity)
                        self.assertEqual(path.read_bytes(), b"two")
                        os.rename(path, self.folder.parent / "away")
        self.assertTrue(next(trash._names("x" * 251 + ".pdf")).endswith("x.pdf"))

    def test_the_note_is_on_disk_before_the_move(self):
        path = self.add("a.txt", b"one")
        real, seen = engine.move_no_replace, []

        def look(source, dest):
            seen.append((self.notes(), os.path.lexists(source), synced[-1] == "info"))
            real(source, dest)

        synced, flush = [], trash._flush

        def note_flush(folder):
            synced.append(os.path.basename(folder))
            flush(folder)

        with mock.patch("downkeep.engine.move_no_replace", look), mock.patch("downkeep.trash._flush", note_flush), \
                mock.patch("os.fsync", side_effect=os.fsync) as fsync:
            self.send(path)
        self.assertEqual(seen, [(["a.txt.trashinfo"], True, True)])
        self.assertEqual(synced, ["info", "files", "Downloads"])
        self.assertEqual(fsync.call_count, 4)

    def test_a_failed_move_leaves_only_the_file(self):
        path = self.add("a.txt", b"one")
        for error in (OSError(5, "Input/output error"), ActionError("other disk"), KeyboardInterrupt()):
            with self.subTest(error), mock.patch("downkeep.engine.move_no_replace", side_effect=error):
                with self.assertRaises(type(error)):
                    self.send(path)
                self.stayed(path)

    def test_stopped_right_after_the_move_keeps_the_note(self):
        path = self.add("a.txt", b"one")
        identity, real = identity_of(path), engine.move_no_replace

        def stop(source, dest):
            real(source, dest)
            raise KeyboardInterrupt

        with mock.patch("downkeep.engine.move_no_replace", stop), self.assertRaises(KeyboardInterrupt):
            self.send(path)
        self.assertEqual(self.notes(), ["a.txt.trashinfo"])
        place = trash.find(str(path), identity)
        trash.restore(str(path), place, identity)
        self.stayed(path)

    def test_no_safe_move_means_no_move(self):
        path = self.add("a.txt", b"one")
        with mock.patch.dict(sys.modules, {"downkeep.engine": types.ModuleType("downkeep.engine")}):
            with self.assertRaisesRegex(ActionError, "not available"):
                self.send(path)
        self.stayed(path)

    def test_the_note_cannot_be_written(self):
        path = self.add("a.txt", b"one")
        with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")), self.assertRaises(OSError):
            self.send(path)
        self.stayed(path)

    def test_another_file_takes_the_place_before_the_move(self):
        path = self.add("a.txt", b"one")
        identity, real = identity_of(path), trash._write_new

        def swap(note, text):
            os.rename(path, self.folder / "away")
            path.write_bytes(b"new")
            return real(note, text)

        with mock.patch("downkeep.trash._write_new", swap), \
                mock.patch("downkeep.engine.move_no_replace", side_effect=AssertionError("moved")), \
                self.assertRaisesRegex(ActionError, "since it was checked"):
            trash.send(str(path), identity)
        self.stayed(path, b"new")

    def test_a_refused_file_leaves_no_note_beside_someone_elses(self):
        self.send(self.add("other.txt", b"x"))
        (self.files / "a.txt").write_bytes(b"orphan")
        path, real = self.add("a.txt", b"one"), trash._write_new
        identity = identity_of(path)

        def edit(note, text):
            path.write_bytes(b"later")
            return real(note, text)

        with mock.patch("downkeep.trash._write_new", edit), self.assertRaisesRegex(ActionError, "since"):
            trash.send(str(path), identity)
        self.assertEqual((path.read_bytes(), self.notes()), (b"later", ["other.txt.trashinfo"]))

    def test_another_file_takes_the_place_during_the_move(self):
        path = self.add("a.txt", b"one")
        identity, real = identity_of(path), engine.move_no_replace

        def swap(source, dest):
            if not (self.folder / "away").exists():
                os.rename(path, self.folder / "away")
                path.write_bytes(b"new")
            real(source, dest)

        with mock.patch("downkeep.engine.move_no_replace", swap), self.assertRaisesRegex(ActionError, "put back"):
            trash.send(str(path), identity)
        self.stayed(path, b"new")

    def test_a_wrong_file_that_cannot_go_back_keeps_its_note(self):
        path = self.add("a.txt", b"one")
        identity, real = identity_of(path), engine.move_no_replace

        def swap(source, dest):
            if source == str(path):
                os.rename(path, self.folder / "away")
                path.write_bytes(b"new")
                real(source, dest)
                path.write_bytes(b"newer")
            else:
                real(source, dest)

        with mock.patch("downkeep.engine.move_no_replace", swap), self.assertRaisesRegex(ActionError, "now at"):
            trash.send(str(path), identity)
        self.assertEqual((path.read_bytes(), (self.files / "a.txt").read_bytes()), (b"newer", b"new"))
        self.assertEqual(self.notes(), ["a.txt.trashinfo"])

    def test_odd_trash_folders_are_refused(self):
        path = self.add("a.txt", b"one")
        elsewhere = self.folder.parent / "elsewhere"
        elsewhere.mkdir()
        self.data.mkdir()
        for name in ("Trash", "Trash/files", "Trash/info"):
            with self.subTest(name):
                (self.data / name).symlink_to(elsewhere)
                with self.assertRaisesRegex(ActionError, "usual way"):
                    self.send(path)
                (self.data / name).unlink()
                (self.data / name).mkdir()
        with mock.patch("os.getuid", return_value=os.getuid() + 1), self.assertRaisesRegex(ActionError, "usual way"):
            self.send(path)
        self.assertEqual((path.read_bytes(), os.listdir(elsewhere), os.listdir(self.files)), (b"one", [], []))

    def test_relative_data_homes_are_ignored(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "data", "HOME": str(self.folder)}):
            self.assertEqual(trash._home_trash(), str(self.folder / ".local/share/Trash"))

    def test_restore_never_replaces(self):
        path = self.add("a.txt", b"one")
        identity = identity_of(path)
        place = self.send(path)
        for taken in (lambda: path.write_bytes(b"new"), lambda: path.symlink_to("nowhere"), path.mkdir):
            taken()
            with self.assertRaisesRegex(ActionError, "another file|Another file"):
                trash.restore(str(path), place, identity)
            self.assertEqual((Path(place).read_bytes(), self.notes()), (b"one", ["a.txt.trashinfo"]))
            path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()

    def test_restore_refuses_another_file(self):
        path = self.add("a.txt", b"one")
        identity = identity_of(path)
        place = self.send(path)
        for wrong in (replace(identity, inode=1), replace(identity, device=1)):
            with self.assertRaisesRegex(ActionError, "any more"):
                trash.restore(str(path), place, wrong)
        os.rename(place, place + "x")
        Path(place).write_bytes(b"one")
        with self.assertRaisesRegex(ActionError, "any more"):
            trash.restore(str(path), place, identity)
        self.assertFalse(path.exists())

    def test_restore_takes_only_from_a_trash(self):
        path = self.add("a.txt", b"one")
        with self.assertRaisesRegex(ActionError, "any more"):
            trash.restore(str(self.folder / "b.txt"), str(path), identity_of(path))
        self.assertTrue(path.exists())

    def test_restore_needs_the_real_folder(self):
        path = self.add("a.txt", b"one")
        identity = identity_of(path)
        place = self.send(path)
        os.rename(self.folder, self.folder.parent / "moved")
        with self.assertRaisesRegex(ActionError, "folder"):
            trash.restore(str(path), place, identity)
        self.folder.symlink_to(self.folder.parent / "moved")
        with self.assertRaisesRegex(ActionError, "folder"):
            trash.restore(str(path), place, identity)
        self.assertTrue(os.path.exists(place))

    def test_restore_takes_back_a_wrong_file(self):
        path = self.add("a.txt", b"one")
        identity, real = identity_of(path), engine.move_no_replace
        place = self.send(path)

        def swap(source, dest):
            if source == place:
                os.rename(place, place + "x")
                Path(place).write_bytes(b"new")
            real(source, dest)

        with mock.patch("downkeep.engine.move_no_replace", swap), self.assertRaisesRegex(ActionError, "put back"):
            trash.restore(str(path), place, identity)
        self.assertEqual((path.exists(), Path(place).read_bytes()), (False, b"new"))
        self.assertEqual(self.notes(), ["a.txt.trashinfo"])

    def test_restore_keeps_later_edits(self):
        path = self.add("a.txt", b"one")
        identity = identity_of(path)
        place = self.send(path)
        with open(place, "ab") as stream:
            stream.write(b" more")
        trash.restore(str(path), place, identity)
        self.assertEqual((path.read_bytes(), self.notes()), (b"one more", []))

    def test_restore_leaves_a_note_about_something_else(self):
        path = self.add("a.txt", b"one")
        identity = identity_of(path)
        place = self.send(path)
        note = self.info / "a.txt.trashinfo"
        note.write_bytes(b"[Trash Info]\nPath=/somewhere/else\n")
        trash.restore(str(path), place, identity)
        self.assertEqual((path.read_bytes(), self.notes()), (b"one", ["a.txt.trashinfo"]))

    def test_find(self):
        path = self.add("a.txt", b"one")
        identity = identity_of(path)
        self.assertIsNone(trash.find(str(path), identity))
        self.assertFalse(self.data.exists())
        place = self.send(path)
        self.assertEqual(trash.find(str(path), identity), place)
        self.assertEqual(trash.find(str(path), replace(identity, device=1)), place)
        for change in ({"inode": 1}, {"size": 9}, {"mtime_ns": 1}):
            self.assertIsNone(trash.find(str(path), replace(identity, **change)))
        os.rename(self.folder, self.folder.parent / "moved")
        self.assertEqual(trash.find(str(path), identity), place)

    def test_nothing_is_ever_deleted_but_our_notes(self):
        real, removed = os.unlink, []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("os.unlink", lambda path: (removed.append(str(path)), real(path))))
            for name in ("remove", "rename", "replace", "rmdir"):
                stack.enter_context(mock.patch(f"os.{name}", side_effect=AssertionError(name)))
            path = self.add("a.txt", b"one")
            identity = identity_of(path)
            trash.restore(str(path), self.send(path), identity)
        self.assertEqual(removed, [str(self.info / "a.txt.trashinfo")])


@unittest.skipUnless(engine and engine.SUPPORTED, "needs Linux and the engine")
class OtherDiskTest(FolderTest):
    """A disk with its own Trash: the home Trash is made to look far away."""

    def setUp(self):
        super().setUp()
        self.top = self.folder.parent
        for patch in (mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/proc"}),
                      mock.patch("downkeep.trash._top", lambda path: str(self.top))):
            patch.start()
            self.addCleanup(patch.stop)
        self.own = self.top / f".Trash-{os.getuid()}"
        self.shared = self.top / ".Trash"

    def send(self):
        path = self.add("a b.txt", b"one")
        return path, identity_of(path), Path(trash.send(str(path), identity_of(path)))

    def test_our_own_folder(self):
        path, identity, place = self.send()
        self.assertEqual(place, self.own / "files" / "a b.txt")
        self.assertIn("\nPath=Downloads/a%20b.txt\n", (self.own / "info" / "a b.txt.trashinfo").read_text())
        self.assertEqual(stat.S_IMODE(self.own.stat().st_mode), 0o700)
        self.assertEqual(trash.find(str(path), identity), str(place))
        trash.restore(str(path), str(place), identity)
        self.assertEqual((path.read_bytes(), os.listdir(self.own / "info")), (b"one", []))

    def test_the_shared_folder(self):
        self.shared.mkdir()
        self.shared.chmod(0o1777)
        path, identity, place = self.send()
        self.assertEqual(place, self.shared / str(os.getuid()) / "files" / "a b.txt")
        self.assertEqual(trash.find(str(path), identity), str(place))
        trash.restore(str(path), str(place), identity)
        self.assertEqual(path.read_bytes(), b"one")

    def test_a_shared_folder_anyone_could_change(self):
        self.shared.mkdir()
        self.shared.chmod(0o777)
        self.assertEqual(self.send()[2].parent.parent, self.own)

    def test_a_shared_folder_that_is_a_link(self):
        real = self.top / "real"
        real.mkdir()
        real.chmod(0o1777)
        self.shared.symlink_to(real)
        self.assertEqual(self.send()[2].parent.parent, self.own)
        self.assertEqual(os.listdir(real), [])

    def test_a_shared_folder_we_cannot_use(self):
        self.shared.mkdir()
        self.shared.chmod(0o1777)
        (self.shared / str(os.getuid())).write_bytes(b"not a folder")
        self.assertEqual(self.send()[2].parent.parent, self.own)

    def test_no_trash_at_all(self):
        self.own.write_bytes(b"not a folder")
        with self.assertRaisesRegex(ActionError, "no Trash"):
            self.send()
        self.assertEqual((self.folder / "a b.txt").read_bytes(), b"one")

    def test_a_trash_on_yet_another_disk(self):
        with self.assertRaises(ActionError):
            trash._ready(str(self.own), os.stat(self.top).st_dev + 1)


@unittest.skipUnless(trash.LINUX, "Linux only")
class TopTest(FolderTest):
    def test_the_top_of_a_disk(self):
        top = trash._top(str(self.folder / "missing" / "a.txt"))
        self.assertEqual(os.stat(top).st_dev, os.stat(self.folder).st_dev)
        self.assertTrue(top == "/" or os.stat(os.path.dirname(top)).st_dev != os.stat(top).st_dev)
        self.assertTrue(str(self.folder).startswith(top))


# Windows decisions, on every system

GIGABYTE = 1024**3
GOOD = trash.Bin(fixed=True, limited=False, off=0, capacity=GIGABYTE, used=0)
PATH = "C:\\Users\\me\\Downloads\\a b.zip"


class WhyNotTest(unittest.TestCase):
    def test_a_normal_file(self):
        self.assertIsNone(trash.why_not(PATH, 100, GOOD))
        self.assertIsNone(trash.why_not(PATH, GIGABYTE, GOOD))
        self.assertIsNone(trash.why_not("d:\\a.zip", 1, GOOD))

    def test_anything_that_could_be_deleted_for_good(self):
        for change, why in (
            ({"fixed": False}, "built-in"),
            ({"limited": None}, "couldn't be read"),
            ({"limited": True}, "turned off"),
            ({"off": None}, "couldn't be read"),
            ({"off": 1}, "turned off"),
            ({"capacity": None}, "couldn't be read"),
            ({"capacity": 99}, "larger"),
            ({"capacity": 0}, "larger"),
            ({"used": None}, "couldn't be read"),
            ({"used": GIGABYTE - 99}, "too full"),
        ):
            with self.subTest(change):
                self.assertIn(why, trash.why_not(PATH, 100, replace(GOOD, **change)))

    def test_names_windows_could_read_as_something_else(self):
        for path in (
            "a.txt", "\\a.txt", "C:a.txt", "C:/Users/a.txt", "C:\\Users/a.txt", "\\\\server\\share\\a.txt",
            "\\\\?\\C:\\a.txt", "\\\\.\\C:\\a.txt", "C:\\a*.txt", "C:\\a?.txt", "C:\\a.txt.", "C:\\a.txt ",
            "C:\\a\\..\\b.txt", "C:\\a\\.\\b.txt", "C:\\a.txt:stream", "C:\\a\\\\b.txt", "C:\\a\n.txt", "C:\\",
            "C:\\a\\", "C:\\a\0b", "C:\\" + "x" * 257, "",
        ):
            with self.subTest(path):
                self.assertIsNotNone(trash.why_not(path, 1, GOOD))
        self.assertIsNone(trash.why_not("C:\\" + "x" * 256, 1, GOOD))


class FakeRegistry(types.ModuleType):
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"
    KEY_READ, KEY_WOW64_64KEY, REG_DWORD, REG_SZ = 0x20019, 0x100, 4, 1

    def __init__(self, values):
        super().__init__("winreg")
        self.values = values

    @contextlib.contextmanager
    def OpenKey(self, hive, key, reserved, access):
        assert access == 0x20119
        yield hive, key

    def QueryValueEx(self, opened, name):
        value = self.values.get((*opened, name))
        if value is None:
            raise FileNotFoundError(2, "not found")
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, tuple) else (value, self.REG_DWORD)


class ReadBinTest(unittest.TestCase):
    EXPLORER = "Software\\Microsoft\\Windows\\CurrentVersion\\"
    KEY = EXPLORER + "Explorer\\BitBucket\\Volume\\{11111111-2222-3333-4444-555555555555}"
    POLICY = EXPLORER + "Policies\\Explorer"
    USER, MACHINE = "HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"

    def read(self, mount="C:\\", kind=3, volume=True, query=0, used=700, path=PATH, **changes):
        values = {(self.USER, self.KEY, "NukeOnDelete"): 0, (self.USER, self.KEY, "MaxCapacity"): 2}
        values |= changes.get("values", {})

        def mount_point(given, buffer, size):
            self.assertEqual((given.value, size), (path, 300))
            buffer.value = mount
            return 1 if mount else 0

        def volume_name(root, buffer, size):
            self.assertEqual((root.value, size), ("C:\\", 64))
            buffer.value = "\\\\?\\Volume{11111111-2222-3333-4444-555555555555}\\"
            return int(volume)

        def ask(root, info):
            self.assertEqual((root.value, info._obj.length), ("C:\\", 24))
            info._obj.size = used
            return query

        kernel32 = types.SimpleNamespace(
            GetVolumePathNameW=mount_point, GetVolumeNameForVolumeMountPointW=volume_name,
            GetDriveTypeW=lambda root: kind if root.value == "C:\\" else 0,
        )
        shell32 = types.SimpleNamespace(SHQueryRecycleBinW=ask)
        with mock.patch("downkeep.trash._dll", {"kernel32": kernel32, "shell32": shell32}.get), \
                mock.patch.dict(sys.modules, {"winreg": FakeRegistry(values)}):
            return trash._read_bin(path)

    def test_a_normal_disk(self):
        self.assertEqual(self.read(), trash.Bin(True, False, 0, 2 * 1024 * 1024, 700))

    def test_other_disks(self):
        for kind in (0, 1, 2, 4, 5, 6):
            self.assertFalse(self.read(kind=kind).fixed)
        self.assertFalse(self.read(mount="C:\\mounted\\").fixed)
        self.assertFalse(self.read(mount="").fixed)

    def test_settings_that_cannot_be_read(self):
        self.assertEqual(self.read(volume=False), trash.Bin(True, False, None, None, 700))
        self.assertIsNone(self.read(query=-1).used)
        self.assertIsNone(self.read(query=1).used)
        self.assertIsNone(self.read(values={(self.USER, self.KEY, "NukeOnDelete"): None}).off)
        self.assertIsNone(self.read(values={(self.USER, self.KEY, "MaxCapacity"): None}).capacity)
        self.assertEqual(self.read(values={(self.USER, self.KEY, "MaxCapacity"): 0}).capacity, 0)
        self.assertEqual(self.read(values={(self.USER, self.KEY, "NukeOnDelete"): 1}).off, 1)
        with self.assertRaises(OSError):
            self.read(values={(self.USER, self.KEY, "NukeOnDelete"): ("0", 1)})

    def test_rules_set_by_an_administrator(self):
        for hive in (self.USER, self.MACHINE):
            for name, value, limited in (
                ("NoRecycleFiles", 1, True), ("NoRecycleFiles", 0, False), ("RecycleBinSize", 10, True),
                ("RecycleBinSize", 0, True), ("NoRecycleFiles", PermissionError(13, "no"), None),
                ("NoRecycleFiles", ("1", 1), None),
            ):
                with self.subTest(hive=hive, name=name, value=value):
                    found = self.read(values={(hive, self.POLICY, name): value})
                    self.assertEqual(found.limited, limited)


class FakeWindows:
    """The COM calls, through real function tables: what Windows would see is what is checked."""

    def __init__(self, test, path, content_flags=0x80, hr_delete=0x270008, new=True, perform=None, aborted=0,
                 item_path=None, fail=()):
        self.test, self.path, self.flags, self.hr_delete = test, str(path), content_flags, hr_delete
        self.new, self.perform, self.aborted, self.fail = new, perform, aborted, fail
        self.item_path = ctypes.create_unicode_buffer(item_path or self.path)
        self.calls, self.released, self.freed, self.keep = [], [], [], []
        pointer, number = ctypes.c_void_p, ctypes.c_uint32
        self.operation = self.build("operation", 23, {
            2: ([], lambda this: self.released.append("operation") or 0),
            3: ([pointer, pointer], self.advise), 4: ([number], self.note("unadvise")),
            5: ([number], self.note("flags")), 18: ([pointer, pointer], self.delete),
            21: ([], self.run), 22: ([pointer], self.any_aborted),
        })
        self.item = self.build("item", 8, {
            2: ([], lambda this: self.released.append("item") or 0), 5: ([number, pointer], self.display_name),
        })
        self.ole32 = types.SimpleNamespace(
            CoInitializeEx=lambda reserved, kind: self.result("init", kind),
            CoUninitialize=lambda: self.calls.append("uninit"),
            CoCreateInstance=self.create, CoTaskMemFree=lambda text: self.freed.append(text.value),
        )
        self.shell32 = types.SimpleNamespace(SHCreateItemFromParsingName=self.parse)

    def build(self, label, size, methods):
        def unexpected(this, index=0):
            self.calls.append(f"unexpected call on {label}")
            return -1

        table = (ctypes.c_void_p * size)()
        for index in range(size):
            kinds, method = methods.get(index, ([], unexpected))
            call = trash._prototype(*kinds)(method)
            table[index] = ctypes.cast(call, ctypes.c_void_p).value
            self.keep.append(call)
        made = ctypes.c_void_p(ctypes.addressof(table))
        self.keep += [table, made]
        return ctypes.addressof(made)

    def result(self, name, *values):
        self.calls.append((name, *values))
        return -0x7FFF0001 if name in self.fail else 0

    def note(self, name):
        return lambda this, *values: self.result(name, *values)

    def create(self, kind, outer, context, wanted, answer):
        self.test.assertEqual(uuid.UUID(bytes_le=kind.raw), uuid.UUID("3ad05575-8857-4850-9277-11b85bdb8e09"))
        self.test.assertEqual(uuid.UUID(bytes_le=wanted.raw), uuid.UUID("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"))
        result = self.result("create", context)
        answer._obj.value = self.operation if not result else None
        return result

    def parse(self, path, context, wanted, answer):
        self.test.assertEqual(uuid.UUID(bytes_le=wanted.raw), uuid.UUID("43826d1e-e718-42ee-bc55-a1e261c37bfe"))
        result = self.result("parse", path.value)
        answer._obj.value = self.item if not result else None
        return result

    def display_name(self, this, kind, answer):
        result = self.result("name", kind)
        if not result:
            ctypes.c_void_p.from_address(answer).value = ctypes.addressof(self.item_path)
        return result

    def advise(self, this, sink, cookie):
        self.sink = sink
        ctypes.c_uint32.from_address(cookie).value = 77
        return self.result("advise")

    def delete(self, this, item, sink):
        self.test.assertEqual((item, sink), (self.item, None))
        return self.result("delete")

    def call(self, index, kinds, *values):
        return trash._com(ctypes.c_void_p(self.sink), index, kinds, *values)

    def run(self, this):
        pointer, number, signed = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        self.start = self.call(3, ())
        self.allowed = self.call(11, (number, pointer), self.flags, self.item)
        if self.allowed == 0:
            os.rename(self.path, self.path + ".bin")
            new = self.item if self.new else None
            self.call(12, (number, pointer, signed, pointer), self.flags, self.item, self.hr_delete, new)
        if self.perform:
            self.perform()
        return self.result("perform")

    def any_aborted(self, this, answer):
        ctypes.c_int32.from_address(answer).value = self.aborted
        return self.result("aborted")


class RecycleTest(FolderTest):
    def setUp(self):
        super().setUp()
        self.path = self.add("a.txt", b"one")
        self.identity = identity_of(self.path)

    def recycle(self, info=GOOD, **options):
        self.windows = FakeWindows(self, self.path, **options)
        libraries = {"ole32": self.windows.ole32, "shell32": self.windows.shell32}
        reader = {"side_effect": info} if isinstance(info, Exception) else {"return_value": info}
        with mock.patch("downkeep.trash._dll", libraries.get), mock.patch("downkeep.trash._read_bin", **reader), \
                mock.patch("downkeep.trash._plain_windows_path", lambda path: True), \
                mock.patch("downkeep.trash.SUPPORTED", True), mock.patch("downkeep.trash.WINDOWS", True):
            return trash.send(str(self.path), self.identity)

    def test_the_whole_conversation(self):
        self.assertEqual(self.recycle(), "")
        self.assertEqual(self.windows.calls, [
            ("init", 6), ("create", 0x17), ("parse", str(self.path)), ("name", 0x80058000), ("flags", 0x84454),
            ("advise",), ("delete",), ("perform",), ("aborted",), ("unadvise", 77), "uninit",
        ])
        self.assertEqual(self.windows.released, ["item", "operation"])
        self.assertEqual(self.windows.freed, [ctypes.addressof(self.windows.item_path)])
        self.assertEqual((self.windows.start, self.windows.allowed), (0, 0))

    def test_the_settings_say_no(self):
        for info in (replace(GOOD, off=1), replace(GOOD, capacity=1), OSError("not a number")):
            with self.subTest(info), self.assertRaises(ActionError):
                self.recycle(info)
            self.assertEqual((self.windows.calls, self.path.read_bytes()), ([], b"one"))

    def test_windows_would_delete_it_for_good(self):
        for flags in (0, 0x40, 0x7F, 0xFFFFFF7F):
            with self.subTest(flags), self.assertRaises(ActionError):
                self.recycle(content_flags=flags)
            self.assertEqual(self.windows.allowed, -2147467260)
            self.assertEqual(self.path.read_bytes(), b"one")

    def test_not_in_the_bin_afterwards(self):
        for options in ({"new": False}, {"hr_delete": -1}, {"aborted": 1}):
            with self.subTest(options), self.assertRaisesRegex(ActionError, "didn't say"):
                self.recycle(**options)
            os.rename(str(self.path) + ".bin", self.path)

    def test_still_there_afterwards(self):
        def back():
            os.rename(str(self.path) + ".bin", self.path)

        with self.assertRaisesRegex(ActionError, "left it"):
            self.recycle(perform=back)

    def test_every_step_can_fail(self):
        for step in ("init", "create", "parse", "name", "flags", "advise", "delete"):
            with self.subTest(step), self.assertRaisesRegex(ActionError, "code 8000ffff"):
                self.recycle(fail=(step,))
            self.assertEqual(self.path.read_bytes(), b"one")
            self.assertNotIn(("perform",), self.windows.calls)
            self.assertEqual("uninit" in self.windows.calls, step != "init")
            expected = {"init": [], "create": [], "parse": ["operation"]}.get(step, ["item", "operation"])
            self.assertEqual(self.windows.released, expected)
        for step in ("perform", "aborted"):
            with self.subTest(step), self.assertRaisesRegex(ActionError, "code 8000ffff"):
                self.recycle(fail=(step,))
            self.assertIn(("unadvise", 77), self.windows.calls)
            os.rename(str(self.path) + ".bin", self.path)

    def test_windows_understood_another_file(self):
        other = self.add("b.txt", b"two")
        with self.assertRaisesRegex(ActionError, "changed"):
            self.recycle(item_path=str(other))
        self.assertNotIn(("delete",), self.windows.calls)

    def test_the_file_changes_at_the_last_moment(self):
        real = FakeWindows.delete

        def edit(windows, this, item, sink):
            self.path.write_bytes(b"later")
            return real(windows, this, item, sink)

        with mock.patch.object(FakeWindows, "delete", edit), self.assertRaisesRegex(ActionError, "changed"):
            self.recycle()
        self.assertEqual(self.path.read_bytes(), b"later")
        self.assertNotIn(("perform",), self.windows.calls)

    def test_the_sink_answers_like_a_com_object(self):
        sink = trash._Sink()
        this = ctypes.c_void_p(sink.pointer)
        self.assertEqual(len(sink.table), 19)
        pointer = ctypes.c_void_p
        for name, known in (("00000000-0000-0000-C000-000000000046", True),
                            ("04b0f1a7-9490-44bc-96e1-4296a31252e2", True), (str(uuid.uuid4()), False)):
            answer = pointer(5)
            result = trash._com(this, 0, (pointer, pointer), trash._guid(name), ctypes.byref(answer))
            self.assertEqual((result, answer.value), (0, sink.pointer) if known else (-2147467262, None))
        self.assertEqual((trash._com(this, 1), trash._com(this, 2)), (1, 1))
        for index, method in enumerate(trash._Sink.METHODS):
            if index not in (0, 1, 2, 11):
                kinds = [trash._Sink.TYPES[kind] for kind in method.partition(":")[2]]
                self.assertEqual(trash._com(this, index, kinds, *(0 for _ in kinds)), 0, method)
        self.assertIn(sink, trash._Sink.kept)

    def test_the_layout_windows_expects(self):
        self.assertEqual(ctypes.sizeof(trash._BinQuery), 24)
        self.assertEqual((trash._BinQuery.size.offset, trash._BinQuery.count.offset), (8, 16))


@unittest.skipUnless(trash.WINDOWS, "Windows only")
class RealRecycleBinTest(FolderTest):
    """Uses made-up files in a temp folder. They stay in the Recycle Bin of the machine that runs this."""

    def setUp(self):
        super().setUp()
        self.content = b"made up by the Downkeep tests " + uuid.uuid4().bytes * 8
        self.path = self.add(f"downkeep {uuid.uuid4()}.txt", self.content)
        self.identity = identity_of(self.path)
        self.root = os.path.splitdrive(self.path)[0] + "\\"

    def items(self):
        query = trash._BinQuery(ctypes.sizeof(trash._BinQuery), 0, 0)
        asked = ctypes.WinDLL("shell32").SHQueryRecycleBinW(ctypes.c_wchar_p(self.root), ctypes.byref(query))
        self.assertEqual(asked, 0)
        return query.count

    def in_bin(self):
        found = []
        for place in Path(self.root, "$Recycle.Bin").glob("*/$R*"):
            with contextlib.suppress(OSError):
                if place.is_file() and place.stat().st_size == len(self.content):
                    found.append(place.read_bytes())
        return self.content in found

    def test_a_file_goes_to_the_bin(self):
        before = self.items()
        with mock.patch("downkeep.trash._read_bin", return_value=GOOD):
            self.assertEqual(trash.send(str(self.path), self.identity), "")
        self.assertFalse(self.path.exists())
        self.assertGreater(self.items(), before)
        self.assertTrue(self.in_bin())

    def test_the_refusal_really_stops_windows(self):
        with mock.patch("downkeep.trash._read_bin", return_value=GOOD), mock.patch("downkeep.trash.WILL_RECYCLE", 0):
            with self.assertRaises(ActionError):
                trash.send(str(self.path), self.identity)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_the_settings_of_this_machine(self):
        info = trash._read_bin(str(self.path))
        print(f"\nRecycle Bin here: {info}", file=sys.stderr)
        self.assertTrue(info.fixed)
        self.assertIs(info.limited, False)
        self.assertIsNotNone(info.used)
        if trash.why_not(str(self.path), self.identity.size, info):
            with self.assertRaises(ActionError):
                trash.send(str(self.path), self.identity)
            self.assertEqual(self.path.read_bytes(), self.content)
            self.skipTest("this account has no readable Recycle Bin settings, so nothing is sent")
        trash.send(str(self.path), self.identity)
        self.assertTrue(self.in_bin())

    def test_a_file_that_is_open_stays(self):
        with open(self.path, "rb"), mock.patch("downkeep.trash._read_bin", return_value=GOOD):
            with self.assertRaises(ActionError):
                trash.send(str(self.path), self.identity)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_a_name_windows_would_tidy(self):
        with self.assertRaises(ActionError):
            trash.send(str(self.path) + ".", self.identity)
        self.assertEqual(self.path.read_bytes(), self.content)


if __name__ == "__main__":
    unittest.main()
