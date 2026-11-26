import contextlib
import errno
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from downkeep import engine
from downkeep.engine import ABSENT, MATCH, OTHER, apply_moves, in_use, move_no_replace, recover, undo
from downkeep.model import ActionError, Busy, Identity, Move
from downkeep.scan import FileRecord, scan
from downkeep.store import Store
from tests.helpers import FolderTest

LINUX = sys.platform == "linux"
needs_moves = unittest.skipUnless(engine.SUPPORTED, "moving files is not available on this system")
needs_linux = unittest.skipUnless(LINUX and engine.SUPPORTED, "Linux only")
not_root = unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")


class MoveTest(FolderTest):
    def setUp(self):
        super().setUp()
        self.data = self.folder.parent / "data"
        self.store = Store(self.data)
        self.addCleanup(self.store.close)
        self.sites = {}
        for name, fake in (("site_of", self.sites.get), ("save_site", self.sites.__setitem__)):
            patch = mock.patch.object(engine, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def moves(self, **folders):
        """Every scanned file into Images, or into the folder named for it."""
        return [Move(file, folders.get(file.name, "Images"), "why") for file in scan(self.folder).files]

    def tidy(self, moves=None, kind="tidy"):
        return apply_moves(self.store, str(self.folder), self.moves() if moves is None else moves, kind)

    def tree(self):
        return {
            os.path.relpath(path, self.folder): path.read_bytes() if path.is_file() else None
            for path in self.folder.rglob("*")
        }

    def states(self, batch):
        return [(op.action, os.path.basename(op.source), op.state) for op in self.store.operations(batch)]

    def refused(self, result, *reasons):
        self.assertEqual(result.done, [])
        self.assertEqual([stay.why for stay in result.skipped], list(reasons))

    def at(self, step, action):
        """Runs something in the middle of a move, the first time that step is reached."""
        left = [action]

        def hook(reached):
            if reached == step and left:
                left.pop()()

        patch = mock.patch.object(engine, "_checkpoint", hook)
        patch.start()
        self.addCleanup(patch.stop)


@needs_moves
class Primitive(MoveTest):
    def test_moves(self):
        source = self.add("a", b"one")
        move_no_replace(str(source), str(self.folder / "b"))
        self.assertEqual(self.tree(), {"b": b"one"})

    def test_never_replaces(self):
        source, dest = self.add("a", b"one"), self.add("b", b"two")
        with self.assertRaises(FileExistsError):
            move_no_replace(str(source), str(dest))
        self.assertEqual(self.tree(), {"a": b"one", "b": b"two"})

    def test_never_replaces_an_empty_folder_or_a_broken_link(self):
        source = self.add("a", b"one")
        (self.folder / "dir").mkdir()
        self.symlink("link", "nowhere")
        for name in ("dir", "link"):
            with self.assertRaises(FileExistsError):
                move_no_replace(str(source), str(self.folder / name))
        self.assertTrue(source.exists())

    def test_missing(self):
        with self.assertRaises(FileNotFoundError):
            move_no_replace(str(self.folder / "a"), str(self.folder / "b"))
        source = self.add("a")
        with self.assertRaises(FileNotFoundError):
            move_no_replace(str(source), str(self.folder / "none" / "b"))

    def test_null_characters_dont_cut_the_name(self):
        source = self.add("a")
        for names in ((str(source) + "\0x", str(self.folder / "b")), (str(source), str(self.folder / "b\0x"))):
            with self.assertRaises(ActionError):
                move_no_replace(*names)
        self.assertEqual(list(self.tree()), ["a"])

    @needs_linux
    def test_a_link_is_never_a_destination_folder(self):
        outside = self.folder.parent / "outside"
        outside.mkdir()
        source = self.add("a")
        self.symlink("link", outside)
        with self.assertRaises(OSError):
            move_no_replace(str(source), str(self.folder / "link" / "a"))
        self.assertEqual((list(outside.iterdir()), source.exists()), ([], True))

    @needs_linux
    def test_odd_bytes(self):
        for raw in (b"\xff\xfe.txt", "ü 日本.txt".encode(), b"line\nbreak", b" space "):
            name = os.fsdecode(raw)
            move_no_replace(str(self.add(name)), str(self.folder / ("to" + name)))
            self.assertIn(b"to" + raw, os.listdir(os.fsencode(self.folder)))

    @needs_linux
    def test_flag_and_bytes_given_to_the_system(self):
        source = self.add("a")
        with mock.patch.object(engine, "_renameat2", return_value=0) as call:
            move_no_replace(str(source), str(self.folder / "b"))
        (_, old, _, new, flags), _ = call.call_args
        self.assertEqual((old, new, flags), (b"a", b"b", 1))

    @needs_linux
    def test_no_fallback_when_the_system_cant_refuse_to_replace(self):
        source = self.add("a")
        for code in (errno.EINVAL, errno.ENOSYS):
            with mock.patch.object(engine, "_renameat2", return_value=-1), mock.patch.object(
                engine.ctypes, "get_errno", return_value=code
            ):
                with self.assertRaisesRegex(ActionError, "no safe way"):
                    move_no_replace(str(source), str(self.folder / "b"))
        self.assertEqual(list(self.tree()), ["a"])

    @needs_linux
    def test_handles_are_closed(self):
        source = self.add("a")
        before = len(os.listdir("/proc/self/fd"))
        move_no_replace(str(source), str(self.folder / "b"))
        with self.assertRaises(FileNotFoundError):
            move_no_replace(str(source), str(self.folder / "b"))
        in_use(str(self.folder / "b"))
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


class Errors(unittest.TestCase):
    def test_linux(self):
        for code, kind in {
            errno.EEXIST: FileExistsError, errno.ENOENT: FileNotFoundError, errno.EACCES: PermissionError,
            errno.ENOSPC: OSError, errno.EXDEV: ActionError, errno.EINVAL: ActionError, errno.ENOSYS: ActionError,
        }.items():
            self.assertIs(type(engine._linux_error(code, "a", "b")), kind)

    def test_windows(self):
        for code, kind in {
            80: FileExistsError, 183: FileExistsError, 2: FileNotFoundError, 3: FileNotFoundError, 5: PermissionError,
            17: ActionError, 32: ActionError, 33: ActionError, 1234: OSError,
        }.items():
            self.assertIs(type(engine._windows_error(code, "a", "b")), kind, code)
        self.assertEqual(str(engine._windows_error(32, "a", "b")), engine.BUSY)
        self.assertEqual(str(engine._windows_error(17, "a", "b")), engine.OTHER_DISK)

    def test_plain_reasons(self):
        self.assertEqual(engine._reason(ActionError("Said.")), "Said.")
        self.assertEqual(engine._reason(FileNotFoundError()), engine.GONE)
        self.assertIn("isn't allowed", engine._reason(PermissionError()))
        self.assertIn("No space", engine._reason(OSError(errno.ENOSPC, "No space left")))


class Windows(unittest.TestCase):
    """Only the decisions: the calls themselves need Windows."""

    def setUp(self):
        self.kernel = mock.Mock()
        for name, value in (("_kernel32", lambda: self.kernel), ("_last_error", lambda: self.code)):
            patch = mock.patch.object(engine, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.code = 0

    def test_only_write_through(self):
        self.kernel.MoveFileExW.return_value = 1
        engine._windows_move("C:\\a", "C:\\b")
        self.kernel.MoveFileExW.assert_called_once_with("C:\\a", "C:\\b", 8)

    def test_failure(self):
        self.kernel.MoveFileExW.return_value = 0
        self.code = 183
        with self.assertRaises(FileExistsError):
            engine._windows_move("C:\\a", "C:\\b")

    def test_long_paths(self):
        tail = "x" * 300
        self.assertEqual(engine._long("C:\\short"), "C:\\short")
        self.assertEqual(engine._long("C:\\" + tail), "\\\\?\\C:\\" + tail)
        self.assertEqual(engine._long("\\\\server\\share\\" + tail), "\\\\?\\UNC\\server\\share\\" + tail)
        self.assertEqual(engine._long("\\\\?\\C:\\" + tail), "\\\\?\\C:\\" + tail)

    def test_in_use_opens_without_sharing(self):
        self.kernel.CreateFileW.return_value = 5
        self.assertFalse(engine._windows_in_use("C:\\a"))
        path, access, sharing, _, how, flags, _ = self.kernel.CreateFileW.call_args.args
        self.assertEqual((path, access, sharing, how, flags), ("C:\\a", 0x80000000, 0, 3, 0x00200000))
        self.kernel.CloseHandle.assert_called_once_with(5)

    def test_in_use(self):
        for handle in (engine.INVALID_HANDLE, None):
            self.kernel.CreateFileW.return_value = handle
            self.assertTrue(engine._windows_in_use("C:\\a"))
        self.kernel.CloseHandle.assert_not_called()

    def test_the_right_code_is_chosen(self):
        with mock.patch.object(engine.sys, "platform", "win32"), mock.patch.object(engine, "SUPPORTED", True):
            self.kernel.MoveFileExW.return_value = 1
            move_no_replace("C:\\a", "C:\\b")
            self.kernel.CreateFileW.return_value = 5
            self.assertFalse(in_use("C:\\a"))
            self.assertIsNone(engine._sync("C:\\a"))
            self.assertIsNone(engine.site_of("C:\\a"))
        self.kernel.MoveFileExW.assert_called_once()


class NotHere(MoveTest):
    def test_nothing_moves_without_support(self):
        self.add("a")
        with mock.patch.object(engine, "SUPPORTED", False):
            for call in (
                lambda: self.tidy(), lambda: undo(self.store, 1),
                lambda: move_no_replace(str(self.folder / "a"), str(self.folder / "b")),
            ):
                with self.assertRaisesRegex(ActionError, "not available"):
                    call()
        self.assertEqual(list(self.tree()), ["a"])

    def test_support(self):
        self.assertEqual(engine.SUPPORTED, sys.platform in ("linux", "win32"))


class InUse(MoveTest):
    @needs_linux
    def test_free_file(self):
        self.assertFalse(in_use(str(self.add("a"))))

    @needs_linux
    def test_open_for_writing(self):
        path = self.add("a")
        for mode in ("ab", "r+b"):
            with path.open(mode):
                self.assertTrue(in_use(str(path)))
        self.assertFalse(in_use(str(path)))

    @needs_linux
    def test_open_for_writing_by_another_program(self):
        path = self.add("a")
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; f = open(sys.argv[1], 'ab'); print(1, flush=True); sys.stdin.read()",
             str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        try:
            child.stdout.readline()
            self.assertTrue(in_use(str(path)))
        finally:
            child.communicate()
        self.assertFalse(in_use(str(path)))

    @needs_linux
    def test_still_mapped_by_a_program_that_closed_it(self):
        import mmap

        path = self.add("a", b"x" * 100)
        with path.open("r+b") as stream:
            held = mmap.mmap(stream.fileno(), 0)
        self.assertTrue(in_use(str(path)))
        held.close()
        self.assertFalse(in_use(str(path)))

    @needs_linux
    def test_reading_is_fine(self):
        path = self.add("a")
        with path.open("rb"):
            self.assertFalse(in_use(str(path)))

    @needs_linux
    def test_read_only_file_is_not_in_use(self):
        path = self.add("a")
        path.chmod(0o444)
        self.assertFalse(in_use(str(path)))

    @needs_linux
    @not_root
    def test_unreadable_file_is_unknown(self):
        path = self.add("a")
        path.chmod(0)
        self.assertTrue(in_use(str(path)))

    @needs_linux
    def test_links_and_missing_files_are_unknown(self):
        self.add("a")
        self.symlink("link", self.folder / "a")
        for name in ("link", "none", "."):
            self.assertTrue(in_use(str(self.folder / name)))

    @needs_linux
    def test_no_lease_is_unknown(self):
        path = self.add("a")
        for code in (errno.EACCES, errno.EAGAIN, errno.EINVAL):
            with mock.patch.object(engine.fcntl, "fcntl", side_effect=OSError(code, "no")):
                self.assertTrue(in_use(str(path)))

    @needs_linux
    @not_root
    def test_someone_elses_file_is_unknown(self):
        self.assertTrue(in_use("/etc/hostname" if os.path.exists("/etc/hostname") else "/etc/passwd"))

    @needs_linux
    def test_a_broken_lease_cant_end_the_program(self):
        with mock.patch.object(engine.fcntl, "fcntl") as call:
            in_use(str(self.add("a")))
        steps = [args.args[1:] for args in call.call_args_list]
        self.assertEqual(steps, [
            (engine.fcntl.F_SETSIG, signal.SIGURG),
            (engine.fcntl.F_SETLEASE, engine.fcntl.F_RDLCK),
            (engine.fcntl.F_SETLEASE, engine.fcntl.F_UNLCK),
        ])

    @needs_linux
    def test_checking_is_not_reading(self):
        path = self.add("a")
        before = os.stat(path)
        in_use(str(path))
        after = os.stat(path)
        self.assertEqual((before.st_atime_ns, before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_atime_ns, after.st_mtime_ns, after.st_ctime_ns))


@needs_moves
class Apply(MoveTest):
    def test_moves_into_new_and_existing_folders(self):
        (self.folder / "Papers").mkdir()
        (self.folder / "Papers" / "old.pdf").write_bytes(b"old")
        self.add("a.png", b"one")
        self.add("b.pdf", b"two")
        result = self.tidy(self.moves(**{"b.pdf": "Papers"}))
        self.assertEqual((result.done, result.skipped), (["a.png", "b.pdf"], []))
        self.assertEqual(self.tree(), {
            "Images": None, os.path.join("Images", "a.png"): b"one",
            "Papers": None, os.path.join("Papers", "b.pdf"): b"two", os.path.join("Papers", "old.pdf"): b"old",
        })
        self.assertEqual(self.states(result.batch), [
            ("mkdir", "Images", "DONE"), ("move", "a.png", "DONE"), ("move", "b.pdf", "DONE"),
        ])
        self.assertEqual(self.store.history()[0]["kind"], "tidy")

    def test_nothing_to_do(self):
        result = self.tidy([])
        self.assertEqual((result.batch, result.done, result.skipped), (None, [], []))
        self.assertEqual(self.store.history(), [])

    def test_everything_about_the_file_stays(self):
        path = self.add("a", b"one")
        path.chmod(0o640)
        noted = False
        if LINUX:
            with contextlib.suppress(OSError):
                os.setxattr(path, "user.note", b"kept")
                noted = True
        before = path.stat()
        self.tidy()
        after = (self.folder / "Images" / "a").stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_atime_ns, before.st_mode),
                         (after.st_ino, after.st_mtime_ns, after.st_atime_ns, after.st_mode))
        if noted:
            self.assertEqual(os.getxattr(self.folder / "Images" / "a", "user.note"), b"kept")

    def test_one_that_fails_never_stops_the_rest(self):
        (self.folder / "Images").mkdir()
        (self.folder / "Images" / "b").write_bytes(b"mine")
        for name in "abc":
            self.add(name)
        result = self.tidy()
        self.assertEqual(result.done, ["a", "c"])
        self.assertEqual([(stay.name, stay.why) for stay in result.skipped], [("b", "Images already has that name.")])
        self.assertEqual((self.folder / "Images" / "b").read_bytes(), b"mine")
        self.assertEqual(self.store.history()[0]["left"], [{"name": "b", "why": "Images already has that name."}])

    def test_names_that_differ_in_case_or_accents_clash(self):
        (self.folder / "Images").mkdir()
        for mine, theirs in (("Photo.JPG", "photo.jpg"), ("caf\u00e9", "cafe\u0301"), ("stra\u00dfe", "STRASSE")):
            (self.folder / "Images" / theirs).write_bytes(b"mine")
            path = self.add(mine)
            self.refused(self.tidy(), "Images already has that name.")
            path.unlink()

    def test_name_taken_at_the_last_moment(self):
        self.add("a", b"one")
        self.at("move planned", lambda: (self.folder / "Images" / "a").write_bytes(b"mine"))
        result = self.tidy()
        self.refused(result, "Images already has that name.")
        self.assertEqual(self.tree()[os.path.join("Images", "a")], b"mine")
        self.assertEqual(self.tree()["a"], b"one")
        self.assertEqual(self.states(result.batch)[-1], ("move", "a", "ABORTED"))

    def test_changed_since_the_scan(self):
        path = self.add("a", b"one")
        moves = self.moves()
        self.add("a", b"four")
        self.refused(self.tidy(moves), engine.CHANGED)
        self.assertNotIn("Images", self.tree())
        self.assertEqual(self.store._run("SELECT * FROM operations WHERE action = 'mkdir'"), [])
        other = self.add("other", b"one")
        path.unlink()
        other.rename(path)
        self.refused(self.tidy(moves), engine.CHANGED)
        path.unlink()
        self.refused(self.tidy(moves), engine.GONE)
        self.assertEqual(self.tree(), {})

    def test_only_the_date_changed(self):
        path = self.add("a", b"one")
        moves = self.moves()
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000))
        self.refused(self.tidy(moves), engine.CHANGED)

    def test_replaced_at_the_last_moment(self):
        path = self.add("a", b"one")

        def replace():
            os.replace(self.add("new", b"two"), path)

        self.at("move planned", replace)
        result = self.tidy()
        self.refused(result, engine.CHANGED)
        self.assertEqual(path.read_bytes(), b"two")
        self.assertEqual(self.states(result.batch)[1], ("move", "a", "ABORTED"))

    def test_edited_at_the_last_moment(self):
        path = self.add("a", b"one")
        self.at("move planned", lambda: self.add("a", b"two edited"))
        self.refused(self.tidy(), engine.CHANGED)
        self.assertEqual(path.read_bytes(), b"two edited")

    def test_gone_at_the_last_moment(self):
        path = self.add("a")
        self.at("move planned", path.unlink)
        result = self.tidy()
        self.refused(result, engine.GONE)
        self.assertEqual(self.states(result.batch)[1], ("move", "a", "ABORTED"))

    def test_hard_link_made_at_the_last_moment(self):
        path = self.add("a")
        self.at("move planned", lambda: os.link(path, self.folder / "Images" / "b"))
        self.refused(self.tidy(), engine.TWO_NAMES)

    def test_in_use(self):
        self.add("a")
        with mock.patch.object(engine, "in_use", return_value=True):
            self.refused(self.tidy(), engine.BUSY)
        self.assertEqual(list(self.tree()), ["a"])

    @needs_linux
    def test_really_in_use(self):
        path = self.add("a")
        moves = self.moves()
        with path.open("r+b"):
            self.refused(self.tidy(moves), engine.BUSY)
        self.assertEqual(self.tidy(moves).done, ["a"])

    def test_the_rules_of_the_scan_again(self):
        self.add("a", b"one")
        (good,) = self.moves()
        names = {
            ".hidden": "hidden or system files", "~$a.docx": "hidden or system files",
            "desktop.ini": "hidden or system files",
            "a.part": "unfinished downloads", "a.CRDOWNLOAD": "unfinished downloads",
            "Empty": "empty files",
        }
        for name, reason in names.items():
            with self.subTest(name=name):
                path = self.add(name, b"" if reason == "empty files" else b"x")
                info = path.stat()
                file = FileRecord(name, str(path), info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, 1)
                self.refused(self.tidy([Move(file, "Images", "")]), engine.WHY[reason])
                path.unlink()
        self.add("a.PART")
        self.refused(self.tidy([good]), engine.WHY["unfinished downloads"])
        (self.folder / "a.PART").unlink()
        fresh = self.add("fresh", days=0)
        info = fresh.stat()
        file = FileRecord("fresh", str(fresh), 1, info.st_mtime_ns, info.st_dev, info.st_ino, 1)
        self.refused(self.tidy([Move(file, "Images", "")]), engine.WHY["changed in the last minute"])
        self.assertNotIn("Images", self.tree())

    def test_links_folders_and_special_files(self):
        target = self.add("target")
        info = target.stat()
        self.symlink("link", target)
        (self.folder / "dir").mkdir()
        wanted = {"link": "links", "dir": "folders"}
        if LINUX:
            os.mkfifo(self.folder / "pipe")
            wanted["pipe"] = "special files"
        for name, reason in wanted.items():
            file = FileRecord(name, str(self.folder / name), 1, info.st_mtime_ns, info.st_dev, info.st_ino, 1)
            self.refused(self.tidy([Move(file, "Images", "")]), engine.WHY[reason])

    def test_file_swapped_for_a_link_to_it(self):
        path = self.add("a")
        moves = self.moves()
        path.rename(self.folder / "real")
        self.symlink("a", self.folder / "real")
        self.refused(self.tidy(moves), engine.WHY["links"])

    def test_hard_links(self):
        path = self.add("a")
        os.link(path, self.folder.parent / "twin")
        self.refused(self.tidy(), engine.TWO_NAMES)
        self.assertEqual(list(self.tree()), ["a"])
        self.assertEqual(self.store._run("SELECT * FROM operations WHERE action = 'mkdir'"), [])

    def test_files_from_elsewhere(self):
        (self.folder / "sub").mkdir()
        for path in (self.folder / "sub" / "a", self.folder.parent / "b"):
            path.write_bytes(b"x")
            os.utime(path, (1, 1))
            info = path.stat()
            for name in (path.name, os.path.relpath(path, self.folder)):
                file = FileRecord(name, str(path), 1, info.st_mtime_ns, info.st_dev, info.st_ino, 1)
                self.refused(self.tidy([Move(file, "Images", "")]), "It is not directly inside the folder.")
            self.assertTrue(path.exists())

    def test_folder_names(self):
        self.add("a")
        bad = ["", ".", "..", ".hidden", "a/b", "/tmp", "..\\x", "C:", "a\\b", "a\0b", "line\nbreak", " a", "a ", "a.",
               "CON", "nul.txt", "Com1", "what?", "a*", "x|y", "\u202eab", '"', "<", ">"]
        for name in bad:
            with self.subTest(name=name):
                (move,) = self.moves()
                result = self.tidy([Move(move.file, name, "")])
                self.assertEqual((result.done, len(result.skipped)), ([], 1))
                self.assertIn("folder name", result.skipped[0].why)
        self.assertEqual(list(self.tree()), ["a"])
        for number, name in enumerate(["Bilder \u00fc", "\u65e5\u672c", "Com10", "Ebooks & PDFs", "a.b"]):
            self.add(f"f{number}")
            self.assertEqual(self.tidy(self.moves(**{f"f{number}": name})[-1:]).done, [f"f{number}"], name)

    def test_folder_that_is_a_file(self):
        self.add("a")
        (self.folder / "Images").write_bytes(b"x")
        moves = [move for move in self.moves() if move.file.name == "a"]
        self.refused(self.tidy(moves), "Images is not a normal folder.")

    def test_folder_that_is_a_link(self):
        outside = self.folder.parent / "outside"
        outside.mkdir()
        self.add("a")
        self.symlink("Images", outside)
        self.refused(self.tidy(), "Images is not a normal folder.")
        self.assertEqual(list(outside.iterdir()), [])

    def test_folder_that_is_a_junction(self):
        (self.folder / "Images").mkdir()
        self.add("a")
        with mock.patch.object(engine.os.path, "isjunction", return_value=True):
            self.refused(self.tidy(), "Images is not a normal folder.")

    @needs_linux
    def test_folder_swapped_for_a_link_at_the_last_moment(self):
        outside = self.folder.parent / "outside"
        outside.mkdir()
        path = self.add("a")

        def swap():
            (self.folder / "Images").rename(self.folder / "Kept")
            (self.folder / "Images").symlink_to(outside)

        self.at("move planned", swap)
        result = self.tidy()
        self.assertEqual((result.done, len(result.skipped)), ([], 1))
        self.assertEqual((list(outside.iterdir()), path.exists()), ([], True))

    @needs_linux
    def test_folder_swapped_for_a_link_right_after(self):
        outside = self.folder.parent / "outside"
        outside.mkdir()
        self.add("a", b"one")

        def swap():
            (self.folder / "Images").rename(outside / "Images")
            (self.folder / "Images").symlink_to(outside / "Images")

        self.at("moved", swap)
        result = self.tidy()
        self.assertEqual(result.done, [])
        self.assertEqual(self.states(result.batch)[1], ("move", "a", "CONFLICT"))
        self.assertIn(str(self.folder), result.skipped[0].why)

    def test_other_disk(self):
        path = self.add("a")
        (self.folder / "Images").mkdir()
        real = engine._real_folder

        def elsewhere(folder):
            return SimpleNamespace(st_dev=real(folder).st_dev + 1) if folder.endswith("Images") else real(folder)

        with mock.patch.object(engine, "_real_folder", elsewhere):
            self.refused(self.tidy(), engine.OTHER_DISK)
        self.assertTrue(path.exists())

    @needs_linux
    @not_root
    def test_read_only_folder(self):
        folder = self.folder / "Images"
        folder.mkdir()
        self.add("a")
        self.add("b")
        folder.chmod(0o555)
        self.addCleanup(folder.chmod, 0o755)
        result = self.tidy(self.moves(b="Other"))
        self.assertEqual(result.done, ["b"])
        self.assertEqual(result.skipped[0].why, "Downkeep isn't allowed to move it.")
        self.assertEqual(self.states(result.batch)[0], ("move", "a", "ABORTED"))

    @needs_linux
    @not_root
    def test_read_only_downloads_folder(self):
        self.add("a")
        moves = self.moves()
        self.folder.chmod(0o555)
        self.addCleanup(self.folder.chmod, 0o755)
        result = self.tidy(moves)
        self.refused(result, "Downkeep isn't allowed to move it.")
        self.assertEqual(self.states(result.batch)[0], ("mkdir", "Images", "ABORTED"))

    @needs_linux
    def test_read_only_file(self):
        self.add("a", b"one").chmod(0o444)
        self.assertEqual(self.tidy().done, ["a"])

    @needs_linux
    def test_odd_names(self):
        raw = ["ü 日本.txt", "line\nbreak.txt", " spaces ", os.fsdecode(b"\xff\xfe.bin"), "'; DROP TABLE x; --", "-rf"]
        for name in raw:
            self.add(name, name.encode("utf-8", "surrogateescape"))
        result = self.tidy()
        self.assertEqual((sorted(result.done), result.skipped), (sorted(raw), []))
        undo(self.store, result.batch)
        self.assertEqual({name: name.encode("utf-8", "surrogateescape") for name in raw}, self.tree())

    def test_same_file_twice_in_one_plan(self):
        self.add("a")
        result = self.tidy(self.moves() * 2)
        self.assertEqual((result.done, [stay.why for stay in result.skipped]), (["a"], [engine.GONE]))

    def test_folder_made_for_nothing_is_removed(self):
        self.add("a")
        self.at("folder saved", lambda: (self.folder / "a").unlink())
        result = self.tidy()
        self.refused(result, engine.GONE)
        self.assertEqual(self.tree(), {})
        self.assertEqual(self.states(result.batch)[-1], ("rmdir", "Images", "DONE"))

    def test_the_folder_must_be_a_safe_one(self):
        self.add("a")
        moves = self.moves()
        with self.assertRaises(ActionError):
            apply_moves(self.store, str(self.folder / "none"), moves)
        with mock.patch("pathlib.Path.home", return_value=self.folder), self.assertRaises(ActionError):
            apply_moves(self.store, str(self.folder), moves)
        self.assertEqual(list(self.tree()), ["a"])

    def test_intent_is_saved_before_the_move(self):
        self.add("a")
        seen = []
        real = engine.move_no_replace

        def look(source, dest):
            with Store(self.data) as other:
                seen.extend((op.action, op.source, op.dest) for op in other.open_intents())
            real(source, dest)

        with mock.patch.object(engine, "move_no_replace", look):
            self.tidy()
        self.assertEqual(seen, [("move", str(self.folder / "a"), str(self.folder / "Images" / "a"))])

    def test_intent_is_saved_before_a_folder_is_made_or_removed(self):
        self.add("a")
        seen = []
        other = Store(self.data)
        self.addCleanup(other.close)

        def spying(real):
            def call(path, *rest):
                seen.extend((op.action, op.source) for op in other.open_intents())
                return real(path, *rest)

            return call

        with mock.patch.object(engine.os, "mkdir", spying(os.mkdir)):
            result = self.tidy()
        self.assertEqual(seen, [("mkdir", str(self.folder / "Images"))])
        with mock.patch.object(engine.os, "rmdir", spying(os.rmdir)):
            undo(self.store, result.batch)
        self.assertEqual(seen[1:], [("rmdir", str(self.folder / "Images"))])

    def test_a_record_that_cant_be_started_moves_nothing(self):
        self.add("a")
        moves = self.moves()
        with mock.patch.object(self.store, "intent", side_effect=ActionError("disk full")):
            self.refused(self.tidy(moves), "disk full")
        self.assertEqual(list(self.tree()), ["a"])

    def test_a_record_that_cant_be_finished_is_settled_later(self):
        (self.folder / "Images").mkdir()
        self.add("a")
        with mock.patch.object(self.store, "finish", side_effect=ActionError("disk full")):
            result = self.tidy()
        self.assertEqual(result.done, [])
        self.assertIn("check it again", result.skipped[0].why)
        self.assertEqual(self.states(result.batch), [("move", "a", "INTENT")])
        self.assertEqual(recover(self.store).done, ["a"])
        self.assertEqual(self.states(result.batch), [("move", "a", "DONE")])

    @needs_linux
    def test_both_folders_are_flushed_before_done(self):
        self.add("a")
        steps = []
        real = self.store.finish

        def finish(op, state, *rest, **named):
            steps.append(state)
            real(op, state, *rest, **named)

        with mock.patch.object(engine, "_sync", steps.append), mock.patch.object(self.store, "finish", finish):
            self.tidy()
        self.assertEqual(steps, [
            str(self.folder), "DONE", str(self.folder / "Images"), str(self.folder), "DONE",
        ])

    @needs_linux
    def test_a_flush_that_fails_is_not_done(self):
        (self.folder / "Images").mkdir()
        self.add("a")
        with mock.patch.object(engine, "_sync", side_effect=OSError(errno.EIO, "bad disk")):
            result = self.tidy()
            self.assertIn("bad disk", result.skipped[0].why)
            self.assertEqual(recover(self.store).done, [])
        self.assertEqual(recover(self.store).done, ["a"])

    @needs_linux
    def test_flush_is_real(self):
        with mock.patch.object(engine.os, "fsync") as call:
            engine._sync(str(self.folder))
            call.assert_called_once()
            call.side_effect = OSError(errno.EINVAL, "not here")
            engine._sync(str(self.folder))
            call.side_effect = OSError(errno.EIO, "bad")
            with self.assertRaises(OSError):
                engine._sync(str(self.folder))

    def test_a_move_that_says_it_failed_but_worked(self):
        self.add("a")
        real = engine.move_no_replace

        def odd(source, dest):
            real(source, dest)
            raise OSError(errno.EIO, "network trouble")

        with mock.patch.object(engine, "move_no_replace", odd):
            self.assertEqual(self.tidy().done, ["a"])

    def test_a_move_that_says_it_worked_but_didnt(self):
        self.add("a")
        with mock.patch.object(engine, "move_no_replace"):
            result = self.tidy()
        self.assertEqual((result.done, self.states(result.batch)[1]), ([], ("move", "a", "ABORTED")))

    def test_automatic_kind_and_wrong_kind(self):
        self.add("a")
        self.assertEqual(self.store.batch(self.tidy(kind="auto").batch).kind, "auto")
        self.add("b")
        with self.assertRaises(ValueError):
            self.tidy(kind="other")

    def test_busy(self):
        self.add("a")
        with Store(self.data) as other, other.lock():
            for call in (self.tidy, lambda: undo(self.store, 1), lambda: recover(self.store)):
                with self.assertRaises(Busy):
                    call()
        self.assertEqual(list(self.tree()), ["a"])

    def test_the_lock_is_held_while_moving(self):
        self.add("a")
        busy = []

        def try_it():
            with Store(self.data) as other:
                try:
                    recover(other)
                except Busy:
                    busy.append(True)

        self.at("moved", try_it)
        self.tidy()
        self.assertEqual(busy, [True])

    def test_a_crash_is_settled_before_anything_new(self):
        self.add("a")
        result = self.tidy()
        (move,) = [op for op in self.store.operations(result.batch) if op.action == "move"]
        self.store._run("UPDATE operations SET state = 'INTENT' WHERE id = ?", move.id)
        self.add("b")
        self.tidy()
        self.assertEqual(self.states(result.batch)[1], ("move", "a", "DONE"))
        self.store._run("UPDATE operations SET state = 'INTENT' WHERE id = ?", move.id)
        self.assertEqual(undo(self.store, result.batch).done, ["a"])


class Sites(MoveTest):
    @needs_moves
    def test_the_site_moves_along_and_back(self):
        path = str(self.add("a"))
        self.add("b")
        self.sites[path] = "https://example.com/a?token=1"
        result = self.tidy()
        moved = str(self.folder / "Images" / "a")
        self.assertEqual(self.sites, {path: "https://example.com/a?token=1", moved: "https://example.com/a?token=1"})
        del self.sites[path]
        undo(self.store, result.batch)
        self.assertIn(path, self.sites)

    @needs_moves
    def test_not_saved_when_the_move_failed(self):
        path = str(self.add("a"))
        self.sites[path] = "https://example.com"
        (self.folder / "Images").mkdir()
        self.at("move planned", lambda: (self.folder / "Images" / "a").write_bytes(b"x"))
        self.tidy()
        self.assertEqual(list(self.sites), [path])


class Gio(unittest.TestCase):
    URL = b"https://example.com/f?x=1"

    def run_with(self, output=b"", program="/usr/bin/gio", error=None, path="/d/a", platform="linux"):
        with mock.patch.object(engine.shutil, "which", return_value=program), mock.patch.object(
            engine.subprocess, "run", return_value=SimpleNamespace(stdout=output), side_effect=error
        ) as run, mock.patch.object(engine.sys, "platform", platform):
            return engine.site_of(path), run

    def answer(self, url):
        return b"uri: file:///d/a\nattributes:\n  metadata::download-uri: " + url + b"\n"

    def test_reads(self):
        found, run = self.run_with(self.answer(self.URL))
        self.assertEqual(found, self.URL.decode())
        self.assertEqual(run.call_args.args[0][1:], ["info", "-a", b"metadata::download-uri", "--", b"/d/a"])

    def test_nothing_found(self):
        odd = (b"file:///etc/passwd", b"https://e.com/" + b"x" * 70_000)
        for output in (b"", b"attributes:\n", *map(self.answer, odd)):
            self.assertIsNone(self.run_with(output)[0])

    def test_not_asked(self):
        for kind in ({"program": None}, {"platform": "win32"}, {"platform": "darwin"}, {"path": "/d/line\nbreak"}):
            found, run = self.run_with(self.answer(self.URL), **kind)
            self.assertIsNone(found)
            run.assert_not_called()

    def test_failures_are_ignored(self):
        for error in (OSError("no"), subprocess.TimeoutExpired("gio", 3), ValueError("null byte")):
            self.assertIsNone(self.run_with(error=error)[0])

    def test_saves(self):
        with mock.patch.object(engine.shutil, "which", return_value="/usr/bin/gio"), mock.patch.object(
            engine.subprocess, "run"
        ) as run, mock.patch.object(engine.sys, "platform", "linux"):
            engine.save_site("/d/a", "https://example.com")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/gio", "set", "--", b"/d/a", b"metadata::download-uri", "https://example.com"],
        )
        with mock.patch.object(engine.shutil, "which", return_value=None), mock.patch.object(
            engine.subprocess, "run"
        ) as run:
            engine.save_site("/d/a", "https://example.com")
        run.assert_not_called()


@needs_moves
class Undo(MoveTest):
    def test_everything_is_as_before(self):
        (self.folder / "Papers").mkdir()
        paths = [self.add(name, name.encode()) for name in ("a", "b", "c")]
        tree, before = self.tree(), [path.stat() for path in paths]
        batch = self.tidy(self.moves(c="Papers")).batch
        result = undo(self.store, batch)
        self.assertEqual((result.done, result.skipped), (["c", "b", "a"], []))
        self.assertEqual(self.tree(), tree)
        for path, old in zip(paths, before):
            new = path.stat()
            self.assertEqual((old.st_ino, old.st_mtime_ns, old.st_mode), (new.st_ino, new.st_mtime_ns, new.st_mode))
        self.assertEqual(self.store.batch(result.batch).undoes, batch)
        run = next(run for run in self.store.history() if run["batch"] == batch)
        self.assertEqual((run["undone"], run["can_undo"]), (True, False))

    def test_twice(self):
        self.add("a")
        batch = self.tidy().batch
        undo(self.store, batch)
        tree, history = self.tree(), self.store.history()
        with self.assertRaisesRegex(ActionError, "already back"):
            undo(self.store, batch)
        self.assertEqual((self.tree(), self.store.history()), (tree, history))

    def test_what_cant_be_undone(self):
        self.add("a")
        back = undo(self.store, self.tidy().batch).batch
        for batch in (back, 999):
            with self.assertRaisesRegex(ActionError, "no such change"):
                undo(self.store, batch)

    def test_edits_come_along(self):
        self.add("a", b"one")
        batch = self.tidy().batch
        with (self.folder / "Images" / "a").open("ab") as stream:
            stream.write(b" and an edit")
        self.assertEqual(undo(self.store, batch).done, ["a"])
        self.assertEqual(self.tree(), {"a": b"one and an edit"})

    def test_different_file_there_now(self):
        self.add("a", b"one")
        batch = self.tidy().batch
        (self.folder / "Images" / "a").rename(self.folder / "Images" / "kept")
        (self.folder / "Images" / "a").write_bytes(b"two")
        self.refused(undo(self.store, batch), "A different file is there now.")
        self.assertEqual((self.folder / "Images" / "a").read_bytes(), b"two")

    def test_no_longer_there(self):
        self.add("a")
        batch = self.tidy().batch
        (self.folder / "Images" / "a").rename(self.folder / "elsewhere")
        self.refused(undo(self.store, batch), "It is no longer in Images.")
        self.assertTrue(self.store.history()[0]["can_undo"])

    def test_name_taken_then_tried_again(self):
        for name in "ab":
            self.add(name, name.encode())
        batch = self.tidy().batch
        mine = self.add("A", b"mine")
        result = undo(self.store, batch)
        self.assertEqual(result.done, ["b"])
        self.assertEqual([stay.why for stay in result.skipped], ["Downloads already has that name."])
        self.assertEqual(mine.read_bytes(), b"mine")
        run = next(run for run in self.store.history() if run["batch"] == batch)
        self.assertEqual((run["undone"], run["can_undo"]), (False, True))
        mine.unlink()
        self.assertEqual(undo(self.store, batch).done, ["a"])
        self.assertEqual(self.tree(), {"a": b"a", "b": b"b"})

    def test_name_taken_at_the_last_moment(self):
        self.add("a", b"one")
        batch = self.tidy().batch
        self.at("move planned", lambda: self.add("a", b"mine"))
        self.refused(undo(self.store, batch), "Downloads already has that name.")
        self.assertEqual(self.tree(), {"a": b"mine", "Images": None, os.path.join("Images", "a"): b"one"})

    def test_in_use(self):
        self.add("a")
        batch = self.tidy().batch
        with mock.patch.object(engine, "in_use", return_value=True):
            self.refused(undo(self.store, batch), engine.BUSY)

    def test_hard_link_or_link(self):
        self.add("a")
        batch = self.tidy().batch
        moved = self.folder / "Images" / "a"
        os.link(moved, self.folder / "twin")
        self.refused(undo(self.store, batch), engine.TWO_NAMES)
        moved.unlink()
        self.symlink(os.path.join("Images", "a"), self.folder / "twin")
        self.refused(undo(self.store, batch), engine.CHANGED)

    def test_folder_reached_through_a_link(self):
        self.add("a")
        batch = self.tidy().batch
        (self.folder / "Images").rename(self.folder.parent / "moved")
        self.symlink("Images", self.folder.parent / "moved")
        self.refused(undo(self.store, batch), "It is no longer in Images.")

    def test_downloads_folder_swapped_for_a_link(self):
        self.add("a")
        batch = self.tidy().batch
        real = self.folder.parent / "real"
        self.folder.rename(real)
        self.symlink(str(self.folder), real)
        result = undo(self.store, batch)
        self.refused(result, "The folder it came from is no longer there.")
        self.assertTrue((real / "Images" / "a").exists())

    def test_a_folder_above_swapped_for_a_link(self):
        self.add("a")
        batch = self.tidy().batch
        above, real = self.folder.parent, self.folder.parent.parent / (self.folder.parent.name + " real")
        with mock.patch.object(engine.os.path, "realpath", return_value=str(real / "Downloads")):
            self.refused(undo(self.store, batch), "The folder it came from is no longer there.")
        self.assertTrue((above / "Downloads" / "Images" / "a").exists())

    def test_other_disk(self):
        self.add("a")
        batch = self.tidy().batch
        real = engine._real_folder

        def elsewhere(folder):
            return SimpleNamespace(st_dev=real(folder).st_dev + 1) if folder.endswith("Downloads") else real(folder)

        with mock.patch.object(engine, "_real_folder", elsewhere):
            self.refused(undo(self.store, batch), engine.OTHER_DISK)

    def test_folder_with_something_in_it_stays(self):
        self.add("a")
        batch = self.tidy().batch
        (self.folder / "Images" / "mine").write_bytes(b"mine")
        self.assertEqual(undo(self.store, batch).done, ["a"])
        self.assertEqual(self.tree(), {"a": b"x", "Images": None, os.path.join("Images", "mine"): b"mine"})

    def test_folder_that_was_there_before_stays(self):
        (self.folder / "Images").mkdir()
        self.add("a")
        undo(self.store, self.tidy().batch)
        self.assertEqual(self.tree(), {"a": b"x", "Images": None})

    def test_folder_made_by_another_batch_stays(self):
        self.add("a")
        self.tidy()
        self.add("b")
        second = self.tidy().batch
        (self.folder / "Images" / "a").unlink()
        undo(self.store, second)
        self.assertEqual(self.tree(), {"b": b"x", "Images": None})

    def test_folder_made_again_by_the_person_stays(self):
        self.add("a")
        batch = self.tidy().batch
        (self.folder / "Images").rename(self.folder / "Mine")
        (self.folder / "Images").mkdir()
        undo(self.store, batch)
        self.assertIn("Images", self.tree())

    def test_folder_filled_at_the_last_moment_stays(self):
        self.add("a")
        batch = self.tidy().batch
        self.at("folder removal planned", lambda: (self.folder / "Images" / "mine").write_bytes(b"mine"))
        result = undo(self.store, batch)
        self.assertEqual((result.done, result.skipped), (["a"], []))
        self.assertEqual(self.states(result.batch)[-1], ("rmdir", "Images", "ABORTED"))
        self.assertIn(os.path.join("Images", "mine"), self.tree())

    def test_undone_files_are_left_there(self):
        self.add("a")
        batch = self.tidy(kind="auto").batch
        undo(self.store, batch)
        (file,) = scan(self.folder).files
        self.assertTrue(self.store.is_left(file))

    def test_new_device_number_after_a_restart(self):
        self.add("a")
        self.add("b")
        batch = self.tidy().batch
        self.store._run("UPDATE operations SET device = '1'")
        (self.folder / "Images" / "b").write_bytes(b"edited")
        result = undo(self.store, batch)
        self.assertEqual(result.done, ["a"])
        self.assertEqual([stay.why for stay in result.skipped], ["A different file is there now."])

    def test_tidy_undo_tidy_undo(self):
        self.add("a", b"one")
        for _ in range(3):
            undo(self.store, self.tidy().batch)
            self.assertEqual(self.tree(), {"a": b"one"})


@needs_moves
class UndoTrash(MoveTest):
    def setUp(self):
        super().setUp()
        self.trash = SimpleNamespace(CAN_RESTORE=True, restore=self.restore, find=lambda path, identity: None)
        package = sys.modules["downkeep"]
        for patch in (
            mock.patch.dict(sys.modules, {"downkeep.trash": self.trash}),
            mock.patch.object(package, "trash", self.trash, create=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.bin = self.folder.parent / "bin"
        self.bin.mkdir()
        self.calls = []

    def restore(self, original, place, identity):
        self.calls.append((original, place, identity))
        move_no_replace(place, original)

    def send(self, name):
        path = self.add(name)
        batch = self.store.begin("trash", str(self.folder))
        info = path.stat()
        identity = Identity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        op = self.store.intent(batch, "trash", str(path), None, identity)
        path.rename(self.bin / name)
        self.store.finish(op, "DONE", dest=str(self.bin / name))
        return batch, identity

    def test_restored(self):
        batch, identity = self.send("a")
        result = undo(self.store, batch)
        self.assertEqual((result.done, result.skipped), (["a"], []))
        self.assertEqual(self.calls, [(str(self.folder / "a"), str(self.bin / "a"), identity)])
        self.assertEqual(self.tree(), {"a": b"x"})
        back, run = self.store.history()
        self.assertEqual((run["undone"], run["can_undo"], back["names"]), (True, False, ["a"]))

    def test_not_possible_here(self):
        batch, _ = self.send("a")
        self.trash.CAN_RESTORE = False
        self.assertIn("Recycle Bin", undo(self.store, batch).skipped[0].why)

    def test_place_unknown(self):
        batch, _ = self.send("a")
        self.store._run("UPDATE operations SET dest = NULL")
        self.assertIn("Recycle Bin", undo(self.store, batch).skipped[0].why)
        self.assertEqual(self.calls, [])

    def test_no_longer_in_the_trash(self):
        batch, _ = self.send("a")
        (self.bin / "a").unlink()
        self.refused(undo(self.store, batch), "It is no longer in bin.")
        self.assertEqual(self.calls, [])

    def test_name_taken(self):
        batch, _ = self.send("a")
        self.add("a", b"mine")
        self.refused(undo(self.store, batch), "Downloads already has that name.")
        self.assertEqual((self.bin / "a").read_bytes(), b"x")

    def test_a_reason_from_the_trash(self):
        batch, _ = self.send("a")
        self.trash.restore = mock.Mock(side_effect=ActionError("Its note is missing."))
        self.refused(undo(self.store, batch), "Its note is missing.")
        self.assertTrue(self.store.history()[0]["can_undo"])


@needs_moves
class Recover(MoveTest):
    def planned(self, name="a", action="move"):
        """An INTENT for a file that is still in place, as a crash would leave it."""
        (self.folder / "Images").mkdir(exist_ok=True)
        source, dest = self.add(name, b"one"), self.folder / "Images" / name
        info = source.stat()
        identity = Identity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        batch = self.store.begin("tidy", str(self.folder))
        self.store.intent(batch, action, str(source), str(dest) if action == "move" else None, identity)
        return batch, source, dest

    def settled(self, batch, state, done=(), names=("a",)):
        tree = self.tree()
        result = recover(self.store)
        self.assertEqual(result.done, list(done))
        self.assertEqual([stay.name for stay in result.skipped], [] if done else list(names))
        self.assertEqual([op.state for op in self.store.operations(batch)], [state])
        again = recover(self.store)
        self.assertEqual((again.done, again.skipped, self.tree()), ([], [], tree))
        return result

    def test_the_whole_table(self):
        wanted = {
            (MATCH, ABSENT): "ABORTED", (MATCH, OTHER): "ABORTED", (MATCH, MATCH): "CONFLICT",
            (ABSENT, MATCH): "DONE", (OTHER, MATCH): "DONE",
            (ABSENT, ABSENT): "CONFLICT", (ABSENT, OTHER): "CONFLICT",
            (OTHER, ABSENT): "CONFLICT", (OTHER, OTHER): "CONFLICT",
        }
        for places, state in wanted.items():
            self.assertEqual(engine._judge(*places)[0], state, places)

    def test_before_the_move(self):
        self.settled(self.planned()[0], "ABORTED")

    def test_before_the_move_with_another_file_at_the_destination(self):
        batch, _, dest = self.planned()
        dest.write_bytes(b"mine")
        self.settled(batch, "ABORTED")

    def test_after_the_move(self):
        batch, source, dest = self.planned()
        source.rename(dest)
        with mock.patch.object(engine, "_sync") as sync:
            self.settled(batch, "DONE", done=["a"])
        self.assertEqual([call.args[0] for call in sync.call_args_list], [str(self.folder / "Images"), str(self.folder)])
        self.assertEqual(undo(self.store, batch).done, ["a"])

    def test_after_the_move_with_a_new_file_in_the_old_place(self):
        batch, source, dest = self.planned()
        source.rename(dest)
        source.write_bytes(b"new")
        self.settled(batch, "DONE", done=["a"])

    def test_in_both_places(self):
        batch, source, dest = self.planned()
        os.link(source, dest)
        result = self.settled(batch, "CONFLICT")
        for path in (source, dest):
            self.assertIn(str(path), result.skipped[0].why)

    def test_in_neither_place(self):
        batch, source, dest = self.planned()
        source.unlink()
        result = self.settled(batch, "CONFLICT")
        self.assertIn(str(source), result.skipped[0].why)

    def test_edited_after_the_move(self):
        batch, source, dest = self.planned()
        source.rename(dest)
        dest.write_bytes(b"edited")
        self.settled(batch, "CONFLICT")

    def test_a_link_to_the_file_is_not_the_file(self):
        batch, source, dest = self.planned()
        source.rename(self.folder / "real")
        self.symlink("a", self.folder / "real")
        self.settled(batch, "CONFLICT")

    def test_destination_behind_a_link(self):
        batch, source, dest = self.planned()
        source.rename(dest)
        dest.parent.rename(self.folder.parent / "away")
        self.symlink("Images", self.folder.parent / "away")
        self.settled(batch, "CONFLICT")

    def test_new_device_number_after_a_restart(self):
        batch, source, dest = self.planned()
        source.rename(dest)
        self.store._run("UPDATE operations SET device = '1'")
        self.settled(batch, "DONE", done=["a"])

    def test_unreadable_now_is_tried_again(self):
        batch, source, dest = self.planned()
        with mock.patch.object(engine, "_look", side_effect=PermissionError):
            self.assertEqual(len(recover(self.store).skipped), 1)
        self.assertEqual(len(self.store.open_intents()), 1)
        self.settled(batch, "ABORTED")

    def test_one_that_is_unreadable_doesnt_stop_the_rest(self):
        self.planned("a")
        self.planned("b")
        real = engine._look

        def look(path, identity):
            if path.endswith("a"):
                raise OSError("no")
            return real(path, identity)

        with mock.patch.object(engine, "_look", look):
            recover(self.store)
        self.assertEqual([os.path.basename(op.source) for op in self.store.open_intents()], ["a"])

    def test_folder_not_made(self):
        batch = self.store.begin("tidy", str(self.folder))
        self.store.intent(batch, "mkdir", str(self.folder / "Images"), None, None)
        self.settled(batch, "ABORTED", names=())

    def test_folder_maybe_made_is_never_removed(self):
        batch = self.store.begin("tidy", str(self.folder))
        self.store.intent(batch, "mkdir", str(self.folder / "Images"), None, None)
        (self.folder / "Images").mkdir()
        self.settled(batch, "CONFLICT", names=("Images",))

    def test_folder_removal(self):
        for removed, state in ((True, "DONE"), (False, "ABORTED")):
            folder = self.folder / "Images"
            folder.mkdir(exist_ok=True)
            info = folder.stat()
            batch = self.store.begin("undo", str(self.folder))
            self.store.intent(batch, "rmdir", str(folder), None, Identity(info.st_dev, info.st_ino, 0, 0))
            if removed:
                folder.rmdir()
            self.settled(batch, state, done=["Images"] if removed else (), names=())

    def test_nothing_is_ever_changed_on_disk(self):
        self.planned()
        refuse = mock.Mock(side_effect=AssertionError("recover changed something"))
        with contextlib.ExitStack() as stack:
            for name in ("rename", "replace", "unlink", "remove", "mkdir", "rmdir", "link", "symlink"):
                stack.enter_context(mock.patch.object(engine.os, name, refuse))
            stack.enter_context(mock.patch.object(engine, "move_no_replace", refuse))
            recover(self.store)

    def trash(self, place):
        found = SimpleNamespace(find=lambda path, identity: place and str(place))
        return mock.patch.dict(sys.modules, {"downkeep.trash": found}), mock.patch.object(
            sys.modules["downkeep"], "trash", found, create=True
        )

    def test_trash_not_sent(self):
        batch, source, _ = self.planned(action="trash")
        first, second = self.trash(source)
        with first, second:
            self.settled(batch, "ABORTED")

    def test_trash_sent_and_found(self):
        batch, source, dest = self.planned(action="trash")
        source.rename(dest)
        first, second = self.trash(dest)
        with first, second:
            self.settled(batch, "DONE", done=["a"])
        self.assertEqual(self.store.operations(batch)[0].dest, str(dest))

    def test_trash_sent_and_a_new_file_in_the_old_place(self):
        for found, state in ((True, "DONE"), (False, "CONFLICT")):
            batch, source, dest = self.planned(action="trash")
            os.replace(source, dest)
            source.write_bytes(b"new")
            first, second = self.trash(dest if found else None)
            with first, second:
                self.settled(batch, state, done=["a"] if found else ())
            dest.unlink()

    def test_trash_sent_and_not_found(self):
        for place in (None, "other"):
            batch, source, dest = self.planned(action="trash")
            source.rename(dest)
            first, second = self.trash(place and self.add(place))
            with first, second:
                result = self.settled(batch, "CONFLICT")
            self.assertIn(str(source), result.skipped[0].why)
            dest.unlink()

    def test_trash_without_a_search(self):
        batch, source, dest = self.planned(action="trash")
        source.rename(dest)
        nothing = SimpleNamespace()
        with mock.patch.dict(sys.modules, {"downkeep.trash": nothing}), mock.patch.object(
            sys.modules["downkeep"], "trash", nothing, create=True
        ):
            self.settled(batch, "CONFLICT")


class NoDangerousCalls(unittest.TestCase):
    def test_the_engine_never_uses_a_call_that_replaces_or_deletes(self):
        source = Path(engine.__file__).read_text(encoding="utf-8")
        for call in ("os.rename", "os.replace", "shutil.move", "shutil.rmtree", "os.unlink", "os.remove(", ".unlink(",
                     "os.removedirs", "os.link", "copyfile"):
            self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
