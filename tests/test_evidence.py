import os
import stat
import struct
import unittest
import zipfile
from dataclasses import replace
from unittest import mock

from downkeep import evidence
from downkeep.evidence import find_duplicates, find_extracted
from downkeep.scan import scan
from tests.helpers import FolderTest


class DuplicatesTest(FolderTest):
    def found(self):
        return [[file.name for file in group.files] for group in find_duplicates(scan(self.folder).files)]

    def test_identical_files(self):
        self.add("new.txt", b"same", days=1)
        self.add("old.txt", b"same", days=9)
        self.add("other.txt", b"diff")
        self.assertEqual(self.found(), [["old.txt", "new.txt"]])

    def test_numbered_name_is_not_proof(self):
        self.add("cv.pdf", b"version one")
        self.add("cv (1).pdf", b"version two")
        self.assertEqual(self.found(), [])

    def test_difference_after_the_first_part(self):
        head = b"x" * evidence.HEAD
        self.add("a.bin", head + b"a")
        self.add("b.bin", head + b"b")
        self.add("c.bin", head + b"b")
        self.assertEqual(self.found(), [["b.bin", "c.bin"]])

    def test_biggest_waste_first(self):
        for index in range(3):
            self.add(f"small{index}", b"1234")
        for index in range(2):
            self.add(f"big{index}", b"1" * 100)
        groups = find_duplicates(scan(self.folder).files)
        self.assertEqual([group.wasted for group in groups], [100, 8])

    def test_hard_links_are_not_copies(self):
        path = self.add("one.txt", b"same")
        try:
            os.link(path, self.folder / "two.txt")
        except OSError:
            self.skipTest("hard links are not allowed here")
        self.assertEqual(self.found(), [])

    def test_changed_files_are_not_proof(self):
        self.add("a.txt", b"same")
        self.add("b.txt", b"same")
        files = scan(self.folder).files
        for change in [{"size": 4, "mtime_ns": 1}, {"inode": 1}]:
            with self.subTest(change=change):
                self.assertEqual(find_duplicates([files[0], replace(files[1], **change)]), [])

    def test_file_changed_during_the_last_read(self):
        self.add("a.txt", b"same")
        self.add("b.txt", b"same")
        files = scan(self.folder).files
        real = evidence._blocks

        def edit(descriptor, limit=None):
            yield from real(descriptor, limit)
            if limit is None and os.fstat(descriptor).st_ino == files[1].inode:
                with open(self.folder / "b.txt", "ab") as stream:
                    stream.write(b"more")

        with mock.patch("downkeep.evidence._blocks", edit):
            self.assertEqual(find_duplicates(files), [])

    def test_link_swapped_in_after_the_scan(self):
        self.add("a.txt", b"same")
        self.add("b.txt", b"same")
        files = scan(self.folder).files
        (self.folder / "b.txt").unlink()
        self.symlink("b.txt", self.folder / "a.txt")
        self.assertEqual(find_duplicates(files), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_pipe_swapped_in_does_not_hang(self):
        self.add("a.txt", b"same")
        self.add("b.txt", b"same")
        files = scan(self.folder).files
        (self.folder / "b.txt").unlink()
        os.mkfifo(self.folder / "b.txt")
        self.assertEqual(find_duplicates(files), [])

    def test_different_starts_are_not_read_to_the_end(self):
        self.add("a.bin", b"a" * 200_000)
        self.add("b.bin", b"b" * 200_000)
        limits = []
        real = evidence._blocks

        def note(descriptor, limit=None):
            limits.append(limit)
            return real(descriptor, limit)

        with mock.patch("downkeep.evidence._blocks", note):
            self.assertEqual(self.found(), [])
        self.assertEqual(limits, [evidence.HEAD, evidence.HEAD])

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "needs O_NOFOLLOW")
    def test_links_are_never_opened(self):
        self.symlink("link.txt", self.add("real.txt"))
        with self.assertRaises(OSError):
            evidence._open_quietly(str(self.folder / "link.txt"))

    def test_only_equal_sizes_are_opened(self):
        self.add("a.txt", b"1")
        self.add("b.txt", b"22")
        files = scan(self.folder).files
        with mock.patch("os.open", side_effect=AssertionError("opened a file")):
            self.assertEqual(find_duplicates(files), [])

    @unittest.skipUnless(hasattr(os, "O_NOATIME"), "needs O_NOATIME")
    def test_reading_does_not_look_like_use(self):
        paths = [self.add("a.txt", b"same"), self.add("b.txt", b"same")]
        before = [path.stat().st_atime_ns for path in paths]
        self.assertEqual(len(self.found()), 1)
        self.assertEqual([path.stat().st_atime_ns for path in paths], before)


class ExtractedTest(FolderTest):
    files = {"top/readme.txt": b"hello", "top/data/big.bin": b"z" * 5000, "top/empty.txt": b""}

    def zip(self, name="pack.zip", files=None, edit=None):
        path = self.folder / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, content in (self.files if files is None else files).items():
                info = zipfile.ZipInfo(member)
                if edit:
                    edit(info)
                archive.writestr(info, content)
        when = path.stat().st_mtime - 10 * 86_400
        os.utime(path, (when, when))
        return path

    def extract(self, where="", files=None):
        for member, content in (files or self.files).items():
            path = self.folder / where / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def found(self):
        return find_extracted(scan(self.folder).files, str(self.folder.resolve()))

    def test_examples_are_a_few_names_without_folders(self):
        files = {f"d/{letter}.txt": letter.encode() for letter in "edcba"}
        self.zip(files=files)
        for name, content in files.items():
            path = self.folder / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content)
        found = find_extracted(scan(self.folder).files, str(self.folder))
        self.assertEqual(found[0].names, ("a.txt", "b.txt", "c.txt"))

    def test_fully_extracted(self):
        self.zip()
        self.extract()
        (item,) = self.found()
        self.assertEqual((item.archive.name, item.folder, item.members), ("pack.zip", str(self.folder.resolve()), 3))

    def test_extracted_into_its_own_folder(self):
        self.zip()
        self.extract("pack")
        self.assertEqual([item.folder for item in self.found()], [str(self.folder.resolve() / "pack")])

    def test_single_file_beside_the_zip(self):
        files = {"photo.jpg": b"j" * 50}
        self.zip(files=files)
        self.extract(files=files)
        self.assertEqual(len(self.found()), 1)

    def test_mac_extras_are_ignored(self):
        self.zip(files={**self.files, "__MACOSX/top/._readme.txt": b"junk"})
        self.extract()
        self.assertEqual([item.members for item in self.found()], [3])

    def test_not_extracted(self):
        self.zip()
        self.assertEqual(self.found(), [])

    def test_one_file_missing(self):
        for missing in self.files:
            with self.subTest(missing=missing):
                self.zip()
                self.extract()
                (self.folder / missing).unlink()
                self.assertEqual(self.found(), [])

    def test_one_file_edited_with_the_same_size(self):
        self.zip()
        self.extract()
        (self.folder / "top/readme.txt").write_bytes(b"jello")
        self.assertEqual(self.found(), [])

    def test_files_spread_over_two_folders_are_not_proof(self):
        self.zip()
        self.extract("one")
        self.extract("two")
        (self.folder / "one/top/readme.txt").unlink()
        (self.folder / "two/top/data/big.bin").unlink()
        self.assertEqual(self.found(), [])

    def test_second_place_is_tried(self):
        self.zip()
        self.extract("a")
        self.extract("b")
        (self.folder / "a/top/readme.txt").write_bytes(b"jello")
        self.assertEqual([item.folder for item in self.found()], [str(self.folder.resolve() / "b")])

    def test_links_are_not_proof(self):
        self.zip()
        outside = self.folder.parent / "outside"
        for member, content in self.files.items():
            path = outside / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.symlink("top", outside / "top")
        self.assertEqual(self.found(), [])

    def test_a_linked_file_or_subfolder_is_not_proof(self):
        for linked in ["top/readme.txt", "top/docs"]:
            with self.subTest(linked=linked):
                files = {**self.files, "top/docs/guide.txt": b"guide"}
                self.zip(files=files)
                self.extract(files=files)
                place = self.folder / linked
                outside = self.folder.parent / place.name
                place.rename(outside)
                self.symlink(linked, outside)
                self.assertEqual(self.found(), [])
                place.unlink()
                outside.rename(place)

    def test_incomplete_place_does_not_stop_the_search(self):
        self.zip()
        self.extract("a")
        self.extract("b")
        (self.folder / "a/top/readme.txt").unlink()
        self.assertEqual([item.folder for item in self.found()], [str(self.folder.resolve() / "b")])

    def test_biggest_zip_first(self):
        for name, size in [("small", 10), ("big", 900)]:
            files = {f"{name}.bin": os.urandom(size)}
            self.zip(f"{name}.zip", files)
            self.extract(files=files)
        self.assertEqual([item.archive.name for item in self.found()], ["big.zip", "small.zip"])

    def test_index_has_a_limit(self):
        self.extract()
        with mock.patch("downkeep.evidence.MAX_INDEX", 2):
            self.assertEqual(len(evidence._index(str(self.folder))), 2)

    def test_zip64_is_refused(self):
        path = self.zip()
        raw = path.read_bytes()
        end = raw.rindex(b"PK\x05\x06")
        path.write_bytes(raw[:end] + b"PK\x06\x07" + b"\0" * 16 + raw[end:])
        os.utime(path, (0, 0))
        with mock.patch("zipfile.ZipFile", side_effect=AssertionError("read the listing")):
            self.assertIsNone(evidence._members(self.record(path)))

    def test_unsafe_names_are_refused(self):
        for name in ["../top/readme.txt", "/top/readme.txt", "top\\readme.txt", "..\\b.txt", "C:readme.txt", "a/../b.txt", "b.txt\0.exe"]:
            with self.subTest(name=name):
                path = self.zip(files={name: b"hello"}, edit=lambda info, name=name: setattr(info, "filename", name))
                self.extract(files={"top/readme.txt": b"hello", "b.txt": b"hello"})
                self.assertIsNone(evidence._members(self.record(path)))

    def record(self, path):
        return next(file for file in scan(self.folder).files if file.name == path.name)

    def test_tidied_names_are_judged_by_the_original(self):
        path = self.zip(files={"top/readme.txt": b"hello"})
        with zipfile.ZipFile(path) as archive:
            (entry,) = archive.infolist()
        for original in ["top\\readme.txt", "top/readme.txt\0.exe"]:
            with self.subTest(original=original):
                entry.orig_filename = original
                with mock.patch("zipfile.ZipFile.infolist", return_value=[entry]):
                    self.assertIsNone(evidence._members(self.record(path)))

    def test_links_inside_a_zip_are_refused(self):
        path = self.zip(files={"a": b"b"}, edit=lambda info: setattr(info, "external_attr", (stat.S_IFLNK | 0o777) << 16))
        self.assertIsNone(evidence._members(self.record(path)))

    def test_encrypted_zip_is_refused(self):
        path = self.zip(files={"a.txt": b"hello"})
        raw = bytearray(path.read_bytes())
        raw[raw.rindex(b"PK\x01\x02") + 8] |= 1
        path.write_bytes(raw)
        os.utime(path, (0, 0))
        self.assertIsNone(evidence._members(self.record(path)))

    def test_repeated_names_are_refused(self):
        path = self.folder / "twice.zip"
        with self.assertWarns(UserWarning), zipfile.ZipFile(path, "w") as archive:
            archive.writestr("a.txt", b"1")
            archive.writestr("a.txt", b"2")
        os.utime(path, (0, 0))
        self.assertIsNone(evidence._members(self.record(path)))

    def test_damaged_zips(self):
        good = self.zip().read_bytes()
        self.extract()
        for raw in [b"junk", b"PK\x05\x06", good[:-30], good[: len(good) // 2], b"PK\x05\x06" + b"\xff" * 18, good[:60] + b"\0" * (len(good) - 60)]:
            with self.subTest(size=len(raw)):
                (self.folder / "pack.zip").write_bytes(raw)
                os.utime(self.folder / "pack.zip", (0, 0))
                self.assertEqual(self.found(), [])

    def test_huge_listings_are_refused_before_reading_them(self):
        path = self.zip()
        raw = bytearray(path.read_bytes())
        end = raw.rindex(b"PK\x05\x06")
        for offset, value in [(10, struct.pack("<H", 10_001)), (12, struct.pack("<L", 17 * 1024 * 1024))]:
            with self.subTest(offset=offset):
                changed = bytearray(raw)
                changed[end + offset : end + offset + len(value)] = value
                path.write_bytes(changed)
                os.utime(path, (0, 0))
                with mock.patch("zipfile.ZipFile", side_effect=AssertionError("read the listing")):
                    self.assertIsNone(evidence._members(self.record(path)))

    def test_empty_zip_proves_nothing(self):
        self.zip(files={})
        self.assertEqual(self.found(), [])

    def test_project_folders_are_not_searched(self):
        self.zip()
        self.extract("node_modules")
        self.assertEqual(self.found(), [])

    def test_nothing_is_written_or_extracted(self):
        self.zip()
        self.extract("pack")
        before = sorted(str(path) for path in self.folder.rglob("*"))
        with mock.patch("zipfile.ZipFile.open", side_effect=AssertionError("extracted")):
            self.assertEqual(len(self.found()), 1)
        self.assertEqual(sorted(str(path) for path in self.folder.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
