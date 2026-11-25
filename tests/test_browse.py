import os
import stat
import sys
import unittest
from unittest import mock

from downkeep import browse
from downkeep.browse import choose, inside, subfolders
from downkeep.scan import ScanError
from tests.helpers import FolderTest


class BrowseTest(FolderTest):
    def setUp(self):
        super().setUp()
        self.home = self.folder
        (self.home / "Docs" / "Old").mkdir(parents=True)
        (self.home / "beach").mkdir()

    def test_only_real_visible_folders(self):
        (self.home / ".config").mkdir()
        self.add("file.txt")
        self.symlink("link", self.home / "Docs")
        self.symlink("up", self.home.parent)
        self.assertEqual(subfolders(self.home), ["beach", "Docs"])

    @unittest.skipUnless(sys.platform == "win32", "Windows junctions")
    def test_junctions_are_left_out(self):
        import _winapi

        _winapi.CreateJunction(str(self.home / "Docs"), str(self.home / "jump"))
        self.assertEqual(subfolders(self.home), ["beach", "Docs"])

    def test_mac_hidden_folders_are_left_out(self):
        real = os.stat

        def hide(path, **options):
            info = real(path, **options)
            if str(path).endswith("beach"):
                return mock.Mock(wraps=info, st_mode=info.st_mode, st_flags=stat.UF_HIDDEN, st_file_attributes=0)
            return info

        with mock.patch("downkeep.browse.os.stat", hide):
            self.assertEqual(subfolders(self.home), ["Docs"])

    def test_one_unreadable_entry_does_not_hide_the_rest(self):
        real = os.stat

        def fail(path, **options):
            if str(path).endswith("beach"):
                raise PermissionError
            return real(path, **options)

        with mock.patch("downkeep.browse.os.stat", fail):
            self.assertEqual(subfolders(self.home), ["Docs"])

    def test_unreadable_folder(self):
        with self.assertRaisesRegex(ScanError, "Can't read"):
            subfolders(self.home / "missing")

    def test_walking_down(self):
        self.assertEqual(inside(self.home, []), self.home)
        self.assertEqual(inside(self.home, ["Docs", "Old"]), self.home / "Docs" / "Old")

    def test_nothing_outside_home(self):
        outside = self.home.parent / "outside"
        outside.mkdir()
        self.symlink("way-out", outside)
        (self.home / ".secret").mkdir()
        for parts in [
            [".."], ["Docs", ".."], ["."], [""], ["Docs/Old"], ["Docs\\Old"], ["/"], [str(outside)], ["way-out"],
            [".secret"], ["missing"], ["Docs", "missing"], ["Docs\0"], [None], [5], [["Docs"]], "Docs", None, {}, 5,
            ["..", "outside"], ["~"], ["C:\\"],
        ]:
            for act in (inside, choose):
                with self.subTest(parts=parts, act=act.__name__), self.assertRaises(ScanError):
                    act(self.home, parts)

    def test_very_deep_paths_stop_early(self):
        with mock.patch("downkeep.browse.subfolders", side_effect=AssertionError("looked")):
            with self.assertRaises(ScanError):
                inside(self.home, ["Docs"] * (browse.MAX_DEPTH + 1))

    def test_choose(self):
        self.assertEqual(choose(self.home, ["Docs"]), self.home / "Docs")
        self.assertEqual(choose(self.home, ["Docs", "Old"]), self.home / "Docs" / "Old")

    def test_home_itself_is_refused(self):
        with self.assertRaises(ScanError):
            choose(self.home, [])

    def test_link_swapped_in_after_the_listing_is_refused(self):
        outside = self.home.parent / "outside"
        outside.mkdir()
        real = browse.check_folder

        def swap(path):
            (self.home / "beach").rmdir()
            self.symlink("beach", outside)
            return real(path)

        with mock.patch("downkeep.browse.check_folder", swap), self.assertRaises(ScanError):
            choose(self.home, ["beach"])


if __name__ == "__main__":
    unittest.main()
