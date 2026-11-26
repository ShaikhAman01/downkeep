import hashlib
import io
import os
import time
import stat
import struct
import unittest
import zipfile
import zlib
from dataclasses import replace
from unittest import mock

from downkeep import evidence
from downkeep.evidence import find_duplicates, find_extracted, find_keepers, is_copy_name, verify_extracted
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
        with mock.patch("zipfile.ZipFile.extract", side_effect=AssertionError("extracted")), \
                mock.patch("zipfile.ZipFile.extractall", side_effect=AssertionError("extracted")):
            self.assertEqual(len(self.found()), 1)
        self.assertEqual(sorted(str(path) for path in self.folder.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()


def forge(data, target, at=0):
    """Flips bits so the checksum becomes target: CRC32 is linear, so this is easy on purpose."""
    base, basis = zlib.crc32(data), {}
    for bit in range(64):
        flipped = bytearray(data)
        flipped[at + bit // 8] ^= 1 << (bit % 8)
        vector, mask = zlib.crc32(flipped) ^ base, 1 << bit
        for high in reversed(range(32)):
            if vector >> high & 1:
                if high not in basis:
                    basis[high] = vector, mask
                    break
                vector, mask = vector ^ basis[high][0], mask ^ basis[high][1]
    want, mask = base ^ target, 0
    for high in reversed(range(32)):
        if want >> high & 1:
            want, mask = want ^ basis[high][0], mask ^ basis[high][1]
    out = bytearray(data)
    for bit in range(64):
        if mask >> bit & 1:
            out[at + bit // 8] ^= 1 << (bit % 8)
    return bytes(out)


def lie(data, size, crc):
    """Rewrites the size and checksum a zip declares for its only file."""
    data = bytearray(data)
    struct.pack_into("<L", data, 14, crc)
    struct.pack_into("<L", data, 22, size)
    central = data.rfind(b"PK\x01\x02")
    struct.pack_into("<L", data, central + 16, crc)
    struct.pack_into("<L", data, central + 24, size)
    return bytes(data)


class ContentsTest(FolderTest):
    extract = ExtractedTest.extract
    found = ExtractedTest.found

    def place(self, data, member="d/a.txt", on_disk=b"one"):
        (self.folder / "pack.zip").write_bytes(data)
        when = time.time() - 10 * 86_400
        os.utime(self.folder / "pack.zip", (when, when))
        self.extract(files={member: on_disk})

    def packed(self, content, kind=zipfile.ZIP_DEFLATED):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", kind) as archive:
            archive.writestr("d/a.txt", content)
        return stream.getvalue()

    def test_same_size_and_checksum_is_not_enough(self):
        real = b"Only copy of my thesis, chapter 1. " * 30
        fake = forge(b"X" * len(real), zlib.crc32(real), 100)
        self.assertEqual((len(fake), zlib.crc32(fake)), (len(real), zlib.crc32(real)))
        for kind in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(kind):
                self.place(self.packed(real, kind), on_disk=fake)
                self.assertEqual(self.found(), [])
                self.place(self.packed(real, kind), on_disk=real)
                (item,) = self.found()
                self.assertEqual(verify_extracted(item), (str(self.folder / "d" / "a.txt"),))
                (self.folder / "d" / "a.txt").write_bytes(fake)
                self.assertEqual(verify_extracted(item), ())

    def test_hidden_data_after_the_declared_size(self):
        for kind in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(kind):
                self.place(lie(self.packed(b"one and a secret", kind), 3, zlib.crc32(b"one")))
                self.assertEqual(self.found(), [])

    def test_less_data_than_declared(self):
        self.place(lie(self.packed(b"on"), 3, zlib.crc32(b"one")))
        self.assertEqual(self.found(), [])

    def test_a_zip_bomb_stops_at_the_declared_size(self):
        self.place(lie(self.packed(bytes(200 * 1024 * 1024)), 10, zlib.crc32(bytes(10))), on_disk=bytes(10))
        started = time.monotonic()
        with mock.patch("downkeep.evidence.BLOCK", 4096):
            self.assertEqual(self.found(), [])
        self.assertLess(time.monotonic() - started, 2)

    def test_other_ways_of_packing_are_not_proof(self):
        self.place(self.packed(b"one", zipfile.ZIP_BZIP2))
        self.assertEqual(self.found(), [])
        self.place(self.packed(b"one"))
        self.assertEqual(len(self.found()), 1)

    def test_big_files_in_small_blocks(self):
        content = os.urandom(300_000) * 3
        self.place(self.packed(content), on_disk=content)
        with mock.patch("downkeep.evidence.BLOCK", 1000):
            self.assertEqual(len(self.found()), 1)
            (self.folder / "d" / "a.txt").write_bytes(content[:-1] + b"x")
            self.assertEqual(self.found(), [])

    def test_an_extracted_file_that_changes_while_it_is_read(self):
        path = self.add("a.txt", b"one")
        self.assertEqual(evidence._digest(str(path), 3), hashlib.blake2b(b"one", digest_size=32).digest())
        real = evidence._blocks

        def edit(descriptor, limit=None):
            yield from real(descriptor, limit)
            os.utime(path, (1, 1))

        for size, blocks in ((3, edit), (4, real)):
            with mock.patch("downkeep.evidence._blocks", blocks), self.assertRaises(evidence.Changed):
                evidence._digest(str(path), size)

    def stored(self, content):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("d/a.txt", content)
        return bytearray(stream.getvalue())

    def test_an_unknown_way_of_packing_is_not_proof(self):
        data = self.stored(b"one")
        for offset in (8, data.rfind(b"PK\x01\x02") + 10):
            struct.pack_into("<H", data, offset, 99)
        self.place(bytes(data))
        self.assertEqual(self.found(), [])

    def test_a_broken_local_header(self):
        data = self.stored(b"one")
        data[0:2] = b"XX"
        self.place(bytes(data))
        self.assertEqual(self.found(), [])

    def resize(self, data, packed):
        """Gives the only file a new packed size: data hidden after its end, or a cut-off stream."""
        central = data.rfind(b"PK\x01\x02")
        start = 30 + sum(struct.unpack_from("<HH", data, 26))
        old = struct.unpack_from("<L", data, 18)[0]
        body = bytes(data[:start]) + packed + bytes(data[start + old:])
        body = bytearray(body)
        struct.pack_into("<L", body, 18, len(packed))
        central += len(packed) - old
        struct.pack_into("<L", body, central + 20, len(packed))
        end = body.rfind(b"PK\x05\x06")
        struct.pack_into("<L", body, end + 16, central)
        return bytes(body)

    def test_data_hidden_after_the_packed_stream(self):
        data = bytearray(self.packed(b"one"))
        start = 30 + sum(struct.unpack_from("<HH", data, 26))
        packed = bytes(data[start:start + struct.unpack_from("<L", data, 18)[0]])
        self.place(self.resize(data, packed + b"secret"))
        self.assertEqual(self.found(), [])
        self.place(self.resize(data, packed[:-2]))
        self.assertEqual(self.found(), [])
        self.place(self.resize(data, packed))
        self.assertEqual(len(self.found()), 1)

    def test_unpacking_uses_little_memory(self):
        import tracemalloc

        self.place(lie(self.packed(bytes(100 * 1024 * 1024)), 10, zlib.crc32(bytes(10))), on_disk=bytes(10))
        real, out = zlib.decompressobj, []

        class Counted:
            def __init__(self, *args):
                self.inner = real(*args)

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def decompress(self, *args):
                block = self.inner.decompress(*args)
                out.append(len(block))
                return block

        tracemalloc.start()
        try:
            with mock.patch.object(evidence.zlib, "decompressobj", Counted):
                self.assertEqual(self.found(), [])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 40 * 1024 * 1024)
        self.assertLessEqual(sum(out), evidence.BLOCK)


class KeepersTest(FolderTest):
    def keep_in(self, *names):
        return find_keepers(str(self.folder), names)

    def groups(self, *names):
        files = scan(self.folder).files
        return [[file.name for file in group.files] for group in find_duplicates(files, self.keep_in(*names))]

    def test_copy_names(self):
        for name in ("a (1).pdf", "a (12).tar.gz", "Copy of a.pdf", "a - Copy.pdf", "a - Copy (2).pdf", "a copy.pdf",
                     "a copy 2.pdf", "a (copy).pdf", "a (another copy).pdf", "a (3rd copy).pdf", "Documents/a (1).pdf"):
            self.assertTrue(is_copy_name(name), name)
        for name in ("a.pdf", "copy.pdf", "a1.pdf", "report 2025.pdf", "a(1)b.pdf", "copyright.pdf", "Copy of"):
            self.assertFalse(is_copy_name(name), name)

    def test_the_original_name_stays_even_if_newer(self):
        self.add("cv (1).pdf", b"same", days=9)
        self.add("Copy of cv.pdf", b"same", days=8)
        self.add("cv.pdf", b"same", days=1)
        self.assertEqual(self.groups(), [["cv.pdf", "cv (1).pdf", "Copy of cv.pdf"]])

    def test_a_tidied_original_stays(self):
        self.add("cv.pdf", b"same", days=1)
        (self.folder / "Documents").mkdir()
        self.add("Documents/cv (1).pdf", b"same", days=9)
        self.add("Documents/cv.pdf", b"same", days=5)
        self.assertEqual(self.groups(), [])
        self.assertEqual(self.groups("Documents"), [["Documents/cv.pdf", "cv.pdf"]])

    def test_copies_inside_subfolders_are_never_the_extra_one(self):
        for name in ("Documents", "Other"):
            (self.folder / name).mkdir()
            self.add(f"{name}/a.txt", b"same")
        self.add("Documents/b.txt", b"same")
        self.assertEqual(self.groups("Documents", "Other"), [])
        self.add("a.txt", b"same")
        self.assertEqual(len(self.groups("Documents", "Other")[0]), 2)

    def test_only_real_subfolders_on_the_same_disk(self):
        (self.folder / "Documents").mkdir()
        self.add("Documents/a.txt", b"same")
        (self.folder / "Documents" / "deeper").mkdir()
        self.add("Documents/deeper/b.txt", b"other")
        self.add("Documents/.hidden", b"same")
        self.symlink("Linked", self.folder / "Documents")
        (self.folder / ".Trash-1000").mkdir()
        self.add(".Trash-1000/a.txt", b"same")
        for name in ("Linked", "Documents/deeper", "..", ".", "", "missing", "/", ".Trash-1000", 7, None):
            with self.subTest(name):
                self.assertEqual(self.keep_in(name), [])
        self.assertEqual([file.name for file in self.keep_in("Documents", "Documents")], ["Documents/a.txt"])
        real = os.lstat

        def elsewhere(path, *rest, **options):
            info = real(path, *rest, **options)
            if str(path).endswith("Documents"):
                return os.stat_result((info.st_mode, info.st_ino, info.st_dev + 1, *info[3:]))
            return info

        with mock.patch("downkeep.evidence.os.lstat", elsewhere):
            self.assertEqual(self.keep_in("Documents"), [])
        with mock.patch("downkeep.evidence.os.path.realpath", lambda path: path + "x"):
            self.assertEqual(self.keep_in("Documents"), [])

    def test_a_hard_link_in_a_subfolder_is_not_a_copy(self):
        (self.folder / "Documents").mkdir()
        path = self.add("a.txt", b"same", days=9)
        self.add("b.txt", b"same", days=1)
        try:
            os.link(path, self.folder / "Documents" / "a.txt")
        except OSError:
            self.skipTest("hard links are not allowed here")
        self.assertEqual(self.groups("Documents"), [["a.txt", "b.txt"]])

    def test_keepers_of_another_size_are_not_read(self):
        (self.folder / "Documents").mkdir()
        self.add("Documents/big.bin", b"x" * 100)
        self.add("a.txt", b"same")
        keepers = self.keep_in("Documents")
        self.assertEqual(len(keepers), 1)
        with mock.patch("os.open", side_effect=AssertionError("opened")):
            self.assertEqual(find_duplicates(scan(self.folder).files, keepers), [])

    def test_the_report_names_the_subfolder(self):
        from downkeep.report import build

        (self.folder / "Documents").mkdir()
        self.add("Documents/Resume.pdf", b"same")
        self.add("Resume (1).pdf", b"same")
        snapshot = scan(self.folder)
        report = build(snapshot, duplicates=find_duplicates(snapshot.files, self.keep_in("Documents")), extracted=[])
        self.assertEqual(report["duplicates"][0]["names"], ["Documents/Resume.pdf", "Resume (1).pdf"])
        self.assertEqual(report["proven"], {"files": 1, "bytes": 4})
