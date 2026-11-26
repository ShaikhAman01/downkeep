import contextlib
import io
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest import mock

from downkeep import clear, evidence, trash
from downkeep.clear import apply_clear
from downkeep.model import ActionError, Busy, Identity
from downkeep.scan import ScanError, scan
from tests.helpers import FolderTest


class FakeStore:
    def __init__(self):
        self.events, self.held, self.busy = [], False, False

    @contextlib.contextmanager
    def lock(self):
        if self.busy:
            raise Busy("Another Downkeep is moving files.")
        self.held = True
        yield
        self.held = False

    def begin(self, kind, folder, undoes=None):
        assert self.held
        self.events.append(("begin", kind, folder))
        return 7

    def intent(self, batch, action, source, dest, identity):
        assert self.held
        self.events.append(("intent", batch, action, source, dest, identity))
        return len(self.events)

    def finish(self, op, state, reason="", dest=None):
        self.events.append(("finish", op, state, reason, dest))

    def end(self, batch):
        self.events.append(("end", batch))

    def open_intents(self):
        return []

    def names(self):
        return [event[0] for event in self.events]


def zipped(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as made:
        for name, content in members.items():
            made.writestr(zipfile.ZipInfo(name), content)
    return stream.getvalue()


class ClearTest(FolderTest):
    """The decisions, on every system: the Trash is a plain folder here."""

    def setUp(self):
        super().setUp()
        self.store = FakeStore()
        self.bin = self.folder.parent / "bin"
        self.bin.mkdir()
        self.sent = []
        for target, value in {
            "downkeep.clear.trash.SUPPORTED": True,
            "downkeep.clear.trash.send": self.send,
            "downkeep.clear.trash.find": self.find,
            "downkeep.clear._in_use": lambda path: False,
        }.items():
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)

    new_store = FakeStore

    def send(self, path, identity):
        self.assertEqual(self.store.names()[-1], "intent")
        self.sent.append(os.path.basename(path))
        place = self.bin / f"{len(self.sent)} {os.path.basename(path)}"
        os.rename(path, place)
        return str(place)

    def find(self, original, identity):
        return None

    def clear(self, *names):
        result = apply_clear(self.store, str(self.folder), list(names))
        self.assertEqual(sorted(result.done + [stay.name for stay in result.skipped]), sorted(set(names)))
        self.why = {stay.name: stay.why for stay in result.skipped}
        return result

    def outside(self):
        return sorted(path.relative_to(self.folder).as_posix() for path in self.folder.rglob("*") if path.is_file())

    def test_a_copy_goes_and_the_oldest_stays(self):
        self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        result = self.clear("new.txt")
        self.assertEqual((result.batch, result.done, self.outside()), (7, ["new.txt"], ["old.txt"]))

    def test_every_step_is_written_down_first(self):
        self.add("old.txt", b"same", days=9)
        path = self.add("new.txt", b"same", days=2)
        identity = Identity.of(next(file for file in scan(self.folder).files if file.name == "new.txt"))
        self.clear("new.txt")
        self.assertEqual(
            self.store.events,
            [
                ("begin", "trash", str(self.folder)),
                ("intent", 7, "trash", str(path), None, identity),
                ("finish", 2, "DONE", "", str(self.bin / "1 new.txt")),
                ("end", 7),
            ],
        )

    def test_asking_for_the_whole_group_leaves_one(self):
        for index in range(4):
            self.add(f"{index}.txt", b"same", days=9 - index)
        self.clear("0.txt", "1.txt", "2.txt", "3.txt")
        self.assertEqual(self.outside(), ["0.txt"])
        self.assertIn("no proof", self.why["0.txt"])

    def test_asking_twice_is_asking_once(self):
        self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        result = self.clear("new.txt", "new.txt", "old.txt", "old.txt")
        self.assertEqual((result.done, len(result.skipped)), (["new.txt"], 1))

    def test_unknown_names_do_nothing(self):
        self.add("a.txt", b"one")
        result = self.clear("a.txt", "missing", "../Downloads/a.txt", "")
        self.assertEqual((result.batch, result.done, self.store.events), (None, [], []))
        self.assertEqual(self.outside(), ["a.txt"])

    def test_only_what_was_asked_for(self):
        for index in range(3):
            self.add(f"{index}.txt", b"same", days=9 - index)
        self.clear("1.txt")
        self.assertEqual(self.outside(), ["0.txt", "2.txt"])

    def test_the_keeper_changed_after_the_check(self):
        keeper = self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        real = clear._cases

        def edit(snapshot):
            cases = real(snapshot)
            keeper.write_bytes(b"diff")
            return cases

        with mock.patch("downkeep.clear._cases", edit):
            self.clear("new.txt")
        self.assertEqual(self.outside(), ["new.txt", "old.txt"])
        self.assertIn("proof", self.why["new.txt"])
        self.assertNotIn("intent", self.store.names())

    def test_the_keeper_vanished_but_another_copy_is_there(self):
        keeper = self.add("0.txt", b"same", days=9)
        self.add("1.txt", b"same", days=5)
        self.add("2.txt", b"same", days=2)
        real = clear._cases

        def drop(snapshot):
            cases = real(snapshot)
            os.rename(keeper, self.bin / "gone")
            return cases

        with mock.patch("downkeep.clear._cases", drop):
            self.clear("1.txt", "2.txt")
        self.assertEqual(self.outside(), ["1.txt"])

    def test_the_file_itself_changed_while_it_was_proven(self):
        self.add("old.txt", b"same", days=9)
        path = self.add("new.txt", b"same", days=2)
        real = clear.verify_copy

        def edit(file, other):
            paths = real(file, other)
            path.write_bytes(b"diff")
            return paths

        with mock.patch("downkeep.clear.verify_copy", edit):
            self.clear("new.txt")
        self.assertEqual((self.sent, path.read_bytes()), ([], b"diff"))
        self.assertIn("changed", self.why["new.txt"])

    def test_a_file_that_looks_unfinished_by_now(self):
        self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        real = clear.verify_copy

        def start(file, other):
            self.add("new.txt.part", b"x")
            return real(file, other)

        with mock.patch("downkeep.clear.verify_copy", start):
            self.clear("new.txt")
        self.assertEqual(self.sent, [])

    def test_hidden_unfinished_fresh_and_empty_files_stay(self):
        self.add("old.txt", b"same", days=9)
        names = [".hidden", "a.part", "~$a.doc", "fresh.txt"]
        for name in names[:3]:
            self.add(name, b"same", days=2)
        self.add("fresh.txt", b"same", days=0)
        self.add("e1", b"")
        self.add("e2", b"")
        self.clear(*names, "e1", "e2")
        self.assertEqual(self.sent, [])

    def test_a_file_in_use_stays(self):
        self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        with mock.patch("downkeep.clear._in_use", lambda path: path.endswith("new.txt")):
            self.clear("new.txt")
        self.assertEqual(self.sent, [])
        self.assertIn("open", self.why["new.txt"])

    def test_a_file_with_two_names_stays(self):
        self.add("old.txt", b"same", days=9)
        path = self.add("new.txt", b"same", days=2)
        try:
            os.link(path, self.folder.parent / "second name")
        except OSError:
            self.skipTest("hard links are not allowed here")
        self.clear("new.txt")
        self.assertEqual(self.sent, [])
        self.assertIn("more than one name", self.why["new.txt"])

    def test_one_failure_does_not_stop_the_rest(self):
        for index in range(4):
            self.add(f"{index}.txt", b"same", days=9 - index)
        real = self.send

        def fail(path, identity):
            if path.endswith("2.txt"):
                raise PermissionError(13, "Permission denied")
            return real(path, identity)

        with mock.patch("downkeep.clear.trash.send", fail):
            result = self.clear("1.txt", "2.txt", "3.txt")
        self.assertEqual(result.done, ["1.txt", "3.txt"])
        self.assertIn("Permission denied", self.why["2.txt"])
        states = [event[2] for event in self.store.events if event[0] == "finish"]
        self.assertEqual(states, ["DONE", "ABORTED", "DONE"])

    def test_an_error_after_the_move_is_settled_by_looking(self):
        for found, state in (("place", "DONE"), (None, "CONFLICT")):
            with self.subTest(state):
                self.store = FakeStore()
                self.add("old.txt", b"same", days=9)
                self.add("new.txt", b"same", days=2)

                def fail(path, identity):
                    self.send(path, identity)
                    raise OSError(5, "Input/output error")

                with mock.patch("downkeep.clear.trash.send", fail), \
                        mock.patch("downkeep.clear.trash.find", lambda path, identity: found):
                    result = self.clear("new.txt")
                self.assertEqual(self.store.events[-2][2:], (state, "" if found else self.why["new.txt"], found))
                self.assertEqual(result.done, ["new.txt"] if found else [])

    def test_the_batch_is_ended_even_after_a_crash(self):
        self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        with mock.patch("downkeep.clear.trash.send", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.clear("new.txt")
        self.assertEqual(self.store.names(), ["begin", "intent", "end"])

    def test_busy_unsupported_and_bad_folders(self):
        self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        with mock.patch("downkeep.clear.trash.SUPPORTED", False), self.assertRaises(ActionError):
            self.clear("new.txt")
        with self.assertRaises(ActionError):
            apply_clear(self.store, str(self.folder / "missing"), ["a"])
        self.store.busy = True
        with self.assertRaises(Busy):
            self.clear("new.txt")
        self.assertEqual(self.store.events, [])

    def test_the_folder_goes_away_halfway(self):
        self.add("old.txt", b"same", days=9)
        self.add("new.txt", b"same", days=2)
        real = clear.verify_copy

        def lose(file, other):
            paths = real(file, other)
            patch = mock.patch("downkeep.clear.scan", side_effect=ScanError("Can't open it"))
            patch.start()
            self.addCleanup(patch.stop)
            return paths

        with mock.patch("downkeep.clear.verify_copy", lose):
            self.clear("new.txt")
        self.assertEqual((self.sent, self.why), ([], {"new.txt": "Can't open it"}))

    # zips

    def extract(self, name, members, where="", days=5.0):
        self.add(name, zipped(members), days)
        for member, content in members.items():
            path = self.folder / where / member
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(content)
                os.utime(path, (1_600_000_000, 1_600_000_000))

    def test_an_extracted_zip_goes(self):
        self.extract("a.zip", {"a/1.txt": b"one", "a/2.txt": b"two"})
        self.assertEqual(self.clear("a.zip").done, ["a.zip"])
        self.assertEqual(self.outside(), ["a/1.txt", "a/2.txt"])

    def test_one_extracted_file_changed_after_the_check(self):
        for content in (b"tw", b"TWO", None):
            with self.subTest(content):
                self.extract("a.zip", {"a/1.txt": b"one", "a/2.txt": b"two"})
                (self.folder / "a" / "2.txt").write_bytes(b"two")
                real = clear._cases

                def edit(snapshot):
                    cases = real(snapshot)
                    if content is None:
                        os.rename(self.folder / "a" / "2.txt", self.bin / "two")
                    else:
                        (self.folder / "a" / "2.txt").write_bytes(content)
                    return cases

                with mock.patch("downkeep.clear._cases", edit):
                    self.clear("a.zip")
                self.assertEqual(self.sent, [])

    def test_a_loose_file_that_proves_a_zip_stays(self):
        self.extract("a.zip", {"1.txt": b"one"})
        self.add("older 1.txt", b"one", days=9999)
        self.clear("1.txt", "a.zip")
        self.assertEqual(self.outside(), ["1.txt", "older 1.txt"])
        self.assertIn("a.zip", self.why["1.txt"])

    def test_a_zip_whose_proof_is_already_gone_stays(self):
        self.extract("a.zip", {"1.txt": b"one"})
        self.add("older 1.txt", b"one", days=9999)
        self.clear("1.txt")
        self.clear("a.zip")
        self.assertEqual(self.outside(), ["a.zip", "older 1.txt"])

    def test_two_identical_zips_need_the_extracted_files(self):
        self.extract("a.zip", {"a/1.txt": b"one"}, days=9)
        self.add("b.zip", zipped({"a/1.txt": b"one"}), days=2)
        self.clear("a.zip", "b.zip")
        self.assertEqual(self.outside(), ["a/1.txt"])

    def test_two_identical_zips_not_extracted_leave_one(self):
        self.add("a.zip", zipped({"a/1.txt": b"one"}), days=9)
        self.add("b.zip", zipped({"a/1.txt": b"one"}), days=2)
        self.clear("a.zip", "b.zip")
        self.assertEqual(self.outside(), ["a.zip"])

    def test_a_copy_of_a_zip_that_goes_too(self):
        self.extract("a.zip", {"a/1.txt": b"one"}, days=9)
        self.add("b.bin", zipped({"a/1.txt": b"one"}), days=2)
        self.clear("a.zip", "b.bin")
        self.assertEqual(self.outside(), ["a/1.txt", "b.bin"])

    def test_a_zip_that_lost_its_files_but_has_a_copy(self):
        self.extract("a.zip", {"a/1.txt": b"one"}, days=2)
        self.add("b.zip", zipped({"a/1.txt": b"one"}), days=9)
        real = clear._cases

        def lose(snapshot):
            cases = real(snapshot)
            os.rename(self.folder / "a" / "1.txt", self.bin / "one")
            return cases

        with mock.patch("downkeep.clear._cases", lose):
            self.clear("a.zip", "b.zip")
        self.assertEqual(len(self.sent), 1)

    def test_a_zip_inside_an_extracted_zip(self):
        inner = zipped({"x.txt": b"one"})
        self.extract("outer.zip", {"inner.zip": inner, "big.bin": b"b" * 5000})
        self.extract("inner.zip", {"x.txt": b"one"})
        self.clear("outer.zip", "inner.zip")
        self.assertEqual(self.outside(), ["big.bin", "inner.zip", "x.txt"])
        self.assertIn("outer.zip", self.why["inner.zip"])

    def test_a_zip_is_never_its_own_proof(self):
        path = self.add("a.zip", zipped({"a.txt": b"one"}))
        file = scan(self.folder).files[0]
        item = evidence.Extracted(file, str(self.folder), 1)
        with mock.patch("downkeep.clear.find_extracted", return_value=[item]), \
                mock.patch("downkeep.clear.verify_extracted", return_value=(str(path),)):
            self.clear("a.zip")
        self.assertEqual(self.sent, [])

    def test_files_in_a_trash_are_not_proof(self):
        for where in (".Trash-1000/files", ".Trash/1000/files", "$RECYCLE.BIN/S-1"):
            with self.subTest(where):
                self.extract("a.zip", {"a/1.txt": b"one"}, where)
                self.assertEqual(evidence.find_extracted(scan(self.folder).files, str(self.folder)), [])
                with mock.patch("downkeep.evidence.is_trash", return_value=False):
                    self.clear("a.zip")
                self.assertEqual(self.sent, [])

    def test_files_in_the_home_trash_are_not_proof(self):
        self.extract("a.zip", {"a/1.txt": b"one"}, "share/Trash/files")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.folder / "share")}):
            self.clear("a.zip")
        self.assertEqual(self.sent, [])

    def test_files_on_another_disk_are_not_proof(self):
        path = self.add("a.txt", b"one")
        self.add("b.txt", b"one")
        file = next(file for file in scan(self.folder).files if file.name == "b.txt")
        info = path.stat()
        self.assertEqual(clear._outside((str(path),), file), {(info.st_dev, info.st_ino)})
        self.assertEqual(clear._outside((str(path),), replace(file, device=file.device + 1)), set())

    def test_the_same_file_under_two_names_is_not_a_copy(self):
        self.add("a.txt", b"one")
        file = scan(self.folder).files[0]
        self.assertEqual(evidence.verify_copy(file, file), ())
        self.assertEqual(evidence.verify_copy(file, replace(file, name="b.txt")), ())

    def test_verify_reads_both_sides_to_the_end(self):
        body = b"x" * (evidence.HEAD * 2)
        self.add("a.bin", body, days=9)
        self.add("b.bin", body, days=2)
        files = scan(self.folder).files
        self.assertEqual(evidence.verify_copy(files[1], files[0]), (files[0].path,))
        for name in ("a.bin", "b.bin"):
            path = self.folder / name
            when = path.stat()
            path.write_bytes(body[:-1] + b"y")
            os.utime(path, ns=(when.st_atime_ns, when.st_mtime_ns))
            self.assertEqual(evidence.verify_copy(files[1], files[0]), ())
            path.write_bytes(body)
            os.utime(path, ns=(when.st_atime_ns, when.st_mtime_ns))

    def test_verify_extracted_does_not_follow_links(self):
        self.extract("a.zip", {"a/1.txt": b"one"})
        item = evidence.find_extracted(scan(self.folder).files, str(self.folder))[0]
        self.assertEqual(evidence.verify_extracted(item), (os.path.join(str(self.folder), "a", "1.txt"),))
        os.rename(self.folder / "a", self.bin / "a")
        self.symlink("a", self.bin / "a")
        self.assertEqual(evidence.verify_extracted(item), ())

    def test_verify_extracted_reads_the_zip_again(self):
        self.extract("a.zip", {"a/1.txt": b"one"})
        item = evidence.find_extracted(scan(self.folder).files, str(self.folder))[0]
        self.add("a.zip", zipped({"a/1.txt": b"one", "a/2.txt": b"two"}))
        self.assertEqual(evidence.verify_extracted(item), ())

    def test_a_zip_with_something_in_front_is_not_proven(self):
        self.extract("a.zip", {"a/1.txt": b"one"})
        self.add("b.zip", zipped({"secret.txt": b"two"}) + zipped({"a/1.txt": b"one"}))
        self.add("c.zip", b"some program" + zipped({"a/1.txt": b"one"}))
        found = evidence.find_extracted(scan(self.folder).files, str(self.folder))
        self.assertEqual([item.archive.name for item in found], ["a.zip"])

    def test_many_random_folders_never_lose_anything(self):
        random_folders(self, 150)


def random_folder(chance, folder):
    contents = [bytes([65 + index]) * chance.randint(1, 40) for index in range(4)]
    names = [f"n{index}.txt" for index in range(8)]
    places = ["", "", "s1", "s2/deep"]
    made = []
    for step in range(chance.randint(4, 16)):
        kind = chance.random()
        if kind < 0.55 or not made:
            where = folder / chance.choice(places)
            name, content = chance.choice(names), chance.choice(contents)
        elif kind < 0.65:
            where, content = folder, chance.choice(made).read_bytes()
            name = f"copy{step}" + chance.choice([".txt", ".zip"])
        else:
            base = chance.choice(["", "s1", "s2"])
            loose = [path for path in sorted((folder / base).rglob("*")) if path.is_file() and path in made]
            members = chance.sample(loose, min(len(loose), chance.randint(1, 3)))
            if not members:
                continue
            content = zipped({path.relative_to(folder / base).as_posix(): path.read_bytes() for path in members})
            where, name = folder / chance.choice(["", "", "s1"]), f"z{chance.randint(0, 5)}.zip"
        where.mkdir(parents=True, exist_ok=True)
        path = where / name
        if path.exists() or path.is_dir():
            continue
        path.write_bytes(content)
        when = 1_600_000_000 + chance.randint(0, 20)
        os.utime(path, (when, when))
        made.append(path)
        if chance.random() < 0.05:
            with contextlib.suppress(OSError):
                os.link(path, folder / f"link{step}.txt")
    return made


def random_folders(test, count):
    total = Counter()
    base = test.folder

    def kept_somewhere(content, kept):
        if content in kept:
            return True
        with zipfile.ZipFile(io.BytesIO(content)) as opened:
            return all(kept_somewhere(opened.read(entry), kept) for entry in opened.infolist())

    for seed in range(count):
        chance = random.Random(seed)
        test.folder = base / str(seed)
        test.folder.mkdir()
        test.store = test.new_store()
        random_folder(chance, test.folder)
        files = [path for path in test.folder.rglob("*") if path.is_file()]
        before = {path: path.read_bytes() for path in files}
        linked = {path for path in files if path.stat().st_nlink > 1}
        top = sorted(path.name for path in files if path.parent == test.folder)
        for _ in range(chance.choice([1, 1, 2])):
            asked = chance.sample(top, chance.randint(len(top) // 2, len(top))) if top else []
            present = {path for path in before if path.exists()}
            outside_before = {before[path] for path in present}
            result = test.clear(*asked)
            after = {path for path in before if path.exists()}
            gone = present - after
            kept = {before[path] for path in after}
            with test.subTest(seed=seed):
                test.assertEqual(sorted(path.name for path in gone), sorted(result.done))
                test.assertLessEqual(set(result.done), set(asked))
                test.assertFalse(gone & linked)
                test.assertTrue(all(path.parent == test.folder for path in gone))
                test.assertTrue(all(path.read_bytes() == before[path] for path in after))
                for content in outside_before - kept:
                    with zipfile.ZipFile(io.BytesIO(content)) as opened:
                        members = {opened.read(entry) for entry in opened.infolist()}
                    test.assertLessEqual(members, kept)
                    total["zips"] += 1
                test.assertTrue(all(kept_somewhere(content, kept) for content in before.values()))
            total["done"] += len(result.done)
            total["stayed"] += sum("so it stays" in why for why in test.why.values())
            total["needed"] += sum("nothing is lost" in why for why in test.why.values())
    test.assertGreater(min(total[key] for key in ("done", "zips", "stayed", "needed")), 1, total)


def engine_ready():
    try:
        from downkeep import engine, store  # noqa: F401
    except ImportError:
        return False
    return trash.LINUX and engine.SUPPORTED


@unittest.skipUnless(engine_ready(), "needs Linux and the engine")
class LinuxClearTest(FolderTest):
    """The real thing: real Trash folder (in a temp place), real records, real moves."""

    def setUp(self):
        super().setUp()
        from downkeep.store import Store

        self.data = self.folder.parent / "data"
        patch = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data)})
        patch.start()
        self.addCleanup(patch.stop)
        self.store = Store(self.folder.parent / "records")
        self.addCleanup(self.store.close)

    def clear(self, *names):
        result = apply_clear(self.store, str(self.folder), list(names))
        self.why = {stay.name: stay.why for stay in result.skipped}
        return result

    def new_store(self):
        return self.store

    def test_there_and_back_again(self):
        from downkeep.engine import undo

        self.add("old.txt", b"same", days=9)
        path = self.add("new.txt", b"same", days=2)
        before = path.stat()
        result = self.clear("new.txt")
        self.assertEqual((result.done, path.exists()), (["new.txt"], False))
        self.assertEqual((self.data / "Trash" / "files" / "new.txt").read_bytes(), b"same")
        self.assertEqual([op.state for op in self.store.operations(result.batch)], ["DONE"])
        self.assertTrue(self.store.history()[0]["can_undo"])
        back = undo(self.store, result.batch)
        self.assertEqual((back.done, back.skipped, path.read_bytes()), (["new.txt"], [], b"same"))
        after = path.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(os.listdir(self.data / "Trash" / "info"), [])

    def test_a_file_being_written_stays(self):
        self.add("old.txt", b"same", days=9)
        path = self.add("new.txt", b"same", days=2)
        with open(path, "ab"):
            self.clear("new.txt")
        self.assertTrue(path.exists())
        self.assertIn("open", self.why["new.txt"])

    def test_two_at_once(self):
        with self.store.lock(), self.assertRaises(Busy):
            self.clear("a")

    def test_random_folders(self):
        random_folders(self, 25)

    def test_killed_at_every_step(self):
        from downkeep.engine import recover

        steps = [
            ("downkeep.trash._write_new", "before"), ("downkeep.trash._write_new", "after"),
            ("downkeep.engine.move_no_replace", "before"), ("downkeep.engine.move_no_replace", "after"),
            ("downkeep.trash.send", "before"), ("downkeep.trash.send", "after"),
        ]
        for (target, when), again in [(step, False) for step in steps] + [(step, True) for step in steps]:
            with self.subTest(target, when=when, again=again), tempfile.TemporaryDirectory(dir=self.folder.parent) as home:
                code = textwrap.dedent("""
                    import os, sys
                    from importlib import import_module
                    from pathlib import Path
                    target, when, folder, records = sys.argv[1:]
                    module, _, name = target.rpartition(".")
                    module = import_module(module)
                    real = getattr(module, name)
                    def stop(*values):
                        if when == "after":
                            real(*values)
                        os._exit(9)
                    setattr(module, name, stop)
                    from downkeep.clear import apply_clear
                    from downkeep.store import Store
                    apply_clear(Store(Path(records)), folder, ["new.txt"])
                """)
                folder = Path(home) / "Downloads"
                folder.mkdir()
                for name, age in (("old.txt", 2000), ("new.txt", 1000)):
                    (folder / name).write_bytes(b"same")
                    os.utime(folder / name, (1_700_000_000 - age, 1_700_000_000 - age))
                records, data = Path(home) / "records", Path(home) / "data"
                env = {**os.environ, "XDG_DATA_HOME": str(data), "PYTHONPATH": os.pathsep.join(sys.path)}
                done = subprocess.run([sys.executable, "-c", code, target, when, str(folder), str(records)], env=env)
                self.assertEqual(done.returncode, 9)
                from downkeep.store import Store

                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}), Store(records) as store:
                    if again:
                        apply_clear(store, str(folder), ["new.txt"])
                    else:
                        recover(store)
                    self.assertEqual(store.open_intents(), [])
                    self.assertEqual(recover(store).done, [])
                    (op,) = [op for op in store.operations(1) if op.action == "trash"]
                    done = [op for batch in (1, 2) for op in store.operations(batch) if op.state == "DONE"]
                trashed = list((data / "Trash" / "files").glob("*")) if data.exists() else []
                moved = (target, when) in steps[3:4] + steps[5:]
                self.assertEqual(op.state, "DONE" if moved else "ABORTED")
                self.assertEqual(len(done), 1 if moved or again else 0)
                self.assertEqual((folder / "new.txt").exists(), not (moved or again))
                self.assertEqual(len(trashed), 1 if moved or again else 0)
                self.assertEqual([path.read_bytes() for path in trashed], [b"same"] if moved or again else [])
                self.assertEqual(op.dest, str(trashed[0]) if moved else None)
                self.assertEqual((folder / "old.txt").read_bytes(), b"same")


if __name__ == "__main__":
    unittest.main()
