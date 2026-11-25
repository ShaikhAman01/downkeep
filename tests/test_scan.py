import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from downkeep.scan import ScanError, scan
from tests.helpers import DAY, FolderTest


class ScanTest(FolderTest):
    def names(self, snapshot):
        return [file.name for file in snapshot.files]

    def test_empty_folder(self):
        snapshot = scan(self.folder)
        self.assertEqual((snapshot.files, snapshot.skipped), ((), {}))

    def test_records_each_file(self):
        path = self.add("Notes.PDF", b"hello")
        (file,) = scan(self.folder).files
        info = path.stat()
        self.assertEqual((file.name, file.extension, file.size), ("Notes.PDF", ".pdf", 5))
        self.assertEqual((file.device, file.inode, file.links), (info.st_dev, info.st_ino, 1))
        self.assertEqual(file.mtime_ns, info.st_mtime_ns)

    def test_extensions(self):
        for name, expected in {"a.TAR.GZ": ".tar.gz", "b.pkg.tar.zst": ".tar.zst", "c.tar": ".tar", "d": "", "e.gz": ".gz", "tar.gz": ".gz"}.items():
            with self.subTest(name=name):
                (self.folder / name).unlink(missing_ok=True)
                path = self.add(name)
                self.assertEqual(scan(self.folder).files[0].extension, expected)
                path.unlink()

    def test_files_are_in_name_order(self):
        for name in ["c.txt", "a.txt", "b.txt"]:
            self.add(name)
        self.assertEqual(self.names(scan(self.folder)), ["a.txt", "b.txt", "c.txt"])

    def test_skips_are_counted(self):
        self.add("keep.txt")
        self.add(".secret")
        self.add("~$report.docx")
        self.add("desktop.ini")
        self.add("nothing.txt", b"")
        self.add("fresh.txt", days=0)
        (self.folder / "project").mkdir()
        snapshot = scan(self.folder)
        self.assertEqual(self.names(snapshot), ["keep.txt"])
        self.assertEqual(
            snapshot.skipped,
            {
                "hidden or system files": 3,
                "empty files": 1,
                "changed in the last minute": 1,
                "folders": 1,
            },
        )

    def test_folders_are_never_entered(self):
        inside = self.folder / "project"
        inside.mkdir()
        (inside / "main.py").write_text("x")
        self.assertEqual(scan(self.folder).files, ())

    def test_unfinished_downloads(self):
        for name in [
            "a.zip.crdownload", "Unconfirmed 12.crdownload", "b.AbC123.zip.part", "c.iso.aria2",
            "c.iso", "d.mkv.!qB", "e.mp4.part", "e.mp4", "f.TMP",
        ]:
            self.add(name)
        self.add("done.zip")
        snapshot = scan(self.folder)
        self.assertEqual(self.names(snapshot), ["done.zip"])
        self.assertEqual(snapshot.skipped, {"unfinished downloads": 9})

    def test_firefox_placeholder_is_left_out(self):
        self.add("movie.mkv", b"")
        self.add("movie.Xy12Ab.mkv.part")
        self.assertEqual(scan(self.folder).skipped, {"empty files": 1, "unfinished downloads": 1})

    def test_dates_in_the_future_are_kept(self):
        self.add("future.txt", days=-400)
        self.assertEqual(self.names(scan(self.folder)), ["future.txt"])

    def test_file_dated_just_ahead_of_the_clock_is_still_fresh(self):
        path = self.add("fresh.txt", days=1)
        snapshot = scan(self.folder, now_ns=path.stat().st_mtime_ns - 10_000_000)
        self.assertEqual(self.names(snapshot), [])
        self.assertEqual(snapshot.skipped, {"changed in the last minute": 1})

    def test_links_are_not_followed(self):
        target = self.add("real.txt")
        self.symlink("link.txt", target)
        self.symlink("broken.txt", self.folder / "missing")
        self.symlink("folder-link", self.folder)
        snapshot = scan(self.folder)
        self.assertEqual(self.names(snapshot), ["real.txt"])
        self.assertEqual(snapshot.skipped, {"links": 3})

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_junctions_are_not_followed(self):
        target = self.folder.parent / "elsewhere"
        target.mkdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.folder / "jump"), str(target)], check=True, capture_output=True)
        self.assertEqual(scan(self.folder).skipped, {"links": 1})

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_hidden_attribute(self):
        path = self.add("hidden.txt")
        subprocess.run(["attrib", "+h", str(path)], check=True)
        self.assertEqual(scan(self.folder).skipped, {"hidden or system files": 1})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_special_files(self):
        os.mkfifo(self.folder / "pipe")
        self.assertEqual(scan(self.folder).skipped, {"special files": 1})

    @unittest.skipUnless(sys.platform == "linux", "Linux allows any bytes in a name")
    def test_names_that_are_not_unicode(self):
        raw = os.fsencode(self.folder) + b"/bad\xff.txt"
        with open(raw, "wb") as stream:
            stream.write(b"x")
        os.utime(raw, (0, 0))
        (file,) = scan(self.folder).files
        self.assertEqual(os.fsencode(file.path), raw)

    def test_hard_links_are_counted(self):
        path = self.add("one.txt")
        try:
            os.link(path, self.folder / "two.txt")
        except OSError:
            self.skipTest("hard links are not allowed here")
        self.assertEqual([file.links for file in scan(self.folder).files], [2, 2])

    def test_file_that_vanishes(self):
        self.add("gone.txt")
        with mock.patch("downkeep.scan.os.stat", side_effect=FileNotFoundError):
            with mock.patch("downkeep.scan.check_folder", return_value=self.folder):
                self.assertEqual(scan(self.folder).skipped, {"disappeared during the scan": 1})

    def test_file_that_cannot_be_read(self):
        self.add("locked.txt")
        with mock.patch("downkeep.scan.os.stat", side_effect=PermissionError):
            with mock.patch("downkeep.scan.check_folder", return_value=self.folder):
                self.assertEqual(scan(self.folder).skipped, {"couldn't be read": 1})

    def test_bad_folders_are_errors_not_empty_results(self):
        file = self.add("file.txt")
        for target in [self.folder / "missing", file, Path.home(), Path(self.folder.anchor)]:
            with self.subTest(target=str(target)), self.assertRaises(ScanError):
                scan(target)

    def test_unreadable_folder_is_an_error(self):
        with mock.patch("downkeep.scan.os.scandir", side_effect=PermissionError("no")):
            with self.assertRaises(ScanError):
                scan(self.folder)

    def test_scanning_changes_nothing(self):
        for index in range(20):
            self.add(f"file{index}.bin", os.urandom(index + 1), days=index + 1)
        (self.folder / "sub").mkdir()

        def state():
            result = {}
            for path in [self.folder, *sorted(self.folder.rglob("*"))]:
                info = path.stat()
                content = path.read_bytes() if path.is_file() else None
                result[path] = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino, content)
            return result

        before = state()
        with mock.patch("builtins.open", side_effect=AssertionError("scan opened a file")):
            with mock.patch("os.open", side_effect=AssertionError("scan opened a file")):
                self.assertEqual(len(scan(self.folder).files), 20)
        self.assertEqual(state(), before)

    def test_clock_can_be_fixed(self):
        self.add("a.txt", days=1)
        now = time.time_ns() + 5 * DAY * 1_000_000_000
        self.assertEqual(scan(self.folder, now_ns=now).taken_ns, now)


if __name__ == "__main__":
    unittest.main()
