import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from downkeep import store as module
from downkeep.model import ActionError, Busy, Identity
from downkeep.scan import FileRecord
from downkeep.store import MIGRATIONS, Store, data_folder

FILE = Identity(2**64 - 1, 2**127, 10, 123_456_789)


def record(name="a.txt", device=1, inode=7, mtime_ns=100):
    return FileRecord(name, f"/x/{name}", 10, mtime_ns, device, inode, 1)


class StoreTest(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name) / "data"
        self.store = self.open()

    def open(self):
        store = Store(self.folder)
        self.addCleanup(store.close)
        return store

    def move(self, batch, name, state="DONE", reason="", action="move", dest="to"):
        place = f"/d/{dest}/{name}" if dest else None
        op = self.store.intent(batch, action, f"/d/{name}", place, FILE)
        if state != "INTENT":
            self.store.finish(op, state, reason)
        return op


class Opening(StoreTest):
    def test_safe_settings(self):
        def pragma(name):
            return self.store._db.execute(f"PRAGMA {name}").fetchone()[0]

        self.assertEqual((pragma("journal_mode"), pragma("synchronous"), pragma("foreign_keys")), ("wal", 2, 1))

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_only_the_person_can_read_it(self):
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        for file in self.folder.iterdir():
            self.assertEqual(file.stat().st_mode & 0o077, 0, file)

    def test_opens_again_with_everything(self):
        batch = self.store.begin("tidy", "/d")
        self.move(batch, "a")
        self.store.set("rules", [{"on": True}])
        self.store.close()
        again = self.open()
        self.assertEqual([op.source for op in again.operations(batch)], ["/d/a"])
        self.assertEqual(again.get("rules"), [{"on": True}])

    def test_newer_records_are_refused_and_kept(self):
        self.store.close()
        with sqlite3.connect(self.folder / "downkeep.db") as db:
            db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
        db.close()
        with self.assertRaisesRegex(ActionError, "newer Downkeep"):
            Store(self.folder)
        db = sqlite3.connect(self.folder / "downkeep.db")
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS) + 1)
        db.close()

    def test_older_records_are_brought_forward(self):
        self.store.set("kept", 1)
        self.store.close()
        more = (*MIGRATIONS, ("CREATE TABLE extra (x)",))
        with mock.patch.object(module, "MIGRATIONS", more):
            newer = self.open()
            self.assertEqual(newer.get("kept"), 1)
            self.assertEqual(newer._run("SELECT x FROM extra"), [])
            self.assertEqual(newer._run("PRAGMA user_version"), [(len(more),)])

    def test_failed_migration_changes_nothing(self):
        self.store.close()
        bad = (*MIGRATIONS, ("CREATE TABLE extra (x)", "NOT SQL"))
        with mock.patch.object(module, "MIGRATIONS", bad), self.assertRaises(ActionError):
            Store(self.folder)
        tables = self.open()._run("SELECT name FROM sqlite_master WHERE name = 'extra'")
        self.assertEqual(tables, [])

    def test_damaged_records_are_refused_and_kept(self):
        self.store.close()
        for damage in (b"not a database, just text " * 100, b"SQLite format 3\0" + b"\xff" * 5000):
            path = self.folder / "downkeep.db"
            path.write_bytes(damage)
            with self.assertRaisesRegex(ActionError, "its own records|own records are damaged"):
                Store(self.folder)
            self.assertEqual(path.read_bytes(), damage)

    def test_damage_in_the_middle_is_noticed(self):
        batch = self.store.begin("tidy", "/d")
        for number in range(300):
            self.move(batch, f"file {number}")
        self.store.close()
        path = self.folder / "downkeep.db"
        content = bytearray(path.read_bytes())
        content[4096 * 4 : 4096 * 8] = b"\x5a" * 4096 * 4
        path.write_bytes(content)
        with self.assertRaises(ActionError):
            Store(self.folder)
        self.assertEqual(path.read_bytes(), bytes(content))

    def test_waits_while_another_program_sets_it_up(self):
        self.store.close()
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with mock.patch.object(module.time, "sleep") as sleep:
            with mock.patch.object(Store, "_prepare", autospec=True, side_effect=[busy, busy, None]):
                self.open()
            self.assertEqual(sleep.call_count, 2)
            with mock.patch.object(Store, "_prepare", side_effect=busy), self.assertRaisesRegex(ActionError, "locked"):
                Store(self.folder)

    def test_folder_that_cant_be_made(self):
        (self.folder.parent / "file").write_text("x")
        with self.assertRaises(ActionError):
            Store(self.folder.parent / "file" / "inside")

    def test_use_after_close_is_a_plain_error(self):
        self.store.close()
        with self.assertRaises(ActionError):
            self.store.get("x")
        with self.assertRaises(ActionError):
            self.store.remember([record()], 1)

    def test_context_manager_closes(self):
        with Store(self.folder) as store:
            store.set("a", 1)
        with self.assertRaises(ActionError):
            store.get("a")


class Place(unittest.TestCase):
    def place(self, platform="linux", **values):
        with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(module.sys, "platform", platform):
            with mock.patch.object(Path, "home", return_value=Path("/home/me")):
                return data_folder()

    def test_chosen_folder_wins(self):
        self.assertEqual(self.place(DOWNKEEP_DATA="/else", XDG_STATE_HOME="/state"), Path("/else"))

    def test_linux(self):
        self.assertEqual(self.place(XDG_STATE_HOME=os.path.abspath("/state")), Path(os.path.abspath("/state/downkeep")))
        self.assertEqual(self.place(), Path("/home/me/.local/state/downkeep"))
        self.assertEqual(self.place(XDG_STATE_HOME="relative"), Path("/home/me/.local/state/downkeep"))

    def test_windows(self):
        self.assertEqual(self.place("win32", LOCALAPPDATA="/local"), Path("/local/Downkeep"))
        self.assertEqual(self.place("win32"), Path("/home/me/AppData/Local/Downkeep"))


class Settings(StoreTest):
    def test_json_values(self):
        for value in ({"a": [1, None, "ü"]}, [], False, 0, "x"):
            self.store.set("key", value)
            self.assertEqual(self.store.get("key", "other"), value)

    def test_default(self):
        self.assertIsNone(self.store.get("missing"))
        self.assertEqual(self.store.get("missing", 3), 3)


class Operations(StoreTest):
    def test_life_of_a_move(self):
        batch = self.store.begin("tidy", "/d")
        op = self.store.intent(batch, "move", "/d/a", "/d/to/a", FILE)
        (waiting,) = self.store.open_intents()
        self.assertEqual(
            (waiting.id, waiting.batch, waiting.action, waiting.source, waiting.dest, waiting.identity, waiting.state),
            (op, batch, "move", "/d/a", "/d/to/a", FILE, "INTENT"),
        )
        self.store.finish(op, "DONE")
        self.assertEqual(self.store.open_intents(), [])
        self.assertEqual(self.store.operations(batch)[0].state, "DONE")

    def test_an_intent_is_on_disk_before_it_returns(self):
        batch = self.store.begin("tidy", "/d")
        self.move(batch, "a", "INTENT")
        self.assertEqual(len(self.open().open_intents()), 1)

    def test_finished_stays_finished(self):
        op = self.move(self.store.begin("tidy", "/d"), "a", "ABORTED")
        with self.assertRaises(ValueError):
            self.store.finish(op, "DONE")
        with self.assertRaises(ValueError):
            self.store.finish(999, "DONE")

    def test_only_known_words(self):
        batch = self.store.begin("tidy", "/d")
        op = self.move(batch, "a", "INTENT")
        for call in (
            lambda: self.store.begin("other", "/d"),
            lambda: self.store.intent(batch, "delete", "/d/a", None, None),
            lambda: self.store.finish(op, "INTENT"),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_batch_must_exist(self):
        with self.assertRaises(ActionError):
            self.store.intent(99, "move", "/a", "/b", FILE)

    def test_trash_place_is_added_at_the_end(self):
        batch = self.store.begin("trash", "/d")
        op = self.store.intent(batch, "trash", "/d/a", None, FILE)
        self.store.finish(op, "DONE", dest="/trash/a")
        self.assertEqual(self.store.operations(batch)[0].dest, "/trash/a")

    def test_folder_identity_is_added_at_the_end(self):
        batch = self.store.begin("tidy", "/d")
        op = self.store.intent(batch, "mkdir", "/d/to", None, None)
        self.assertIsNone(self.store.open_intents()[0].identity)
        self.store.finish(op, "DONE", identity=FILE)
        self.assertEqual(self.store.operations(batch)[0].identity, FILE)

    def test_odd_names_come_back_the_same(self):
        odd = ["/d/ü 日本 ‮.txt", "/d/line\nbreak", "/d/'; DROP TABLE seen; --"]
        if sys.platform == "linux":
            odd.append(os.fsdecode(b"/d/\xff\xfe"))
        batch = self.store.begin("tidy", odd[0])
        for path in odd:
            self.store.intent(batch, "move", path, path + "2", FILE)
        self.assertEqual([op.source for op in self.store.operations(batch)], odd)
        self.assertEqual(self.store.operations(batch)[-1].dest, odd[-1] + "2")
        self.assertEqual(self.store.batch(batch).folder, odd[0])
        self.assertEqual(self.store._run("SELECT DISTINCT typeof(source) FROM operations"), [("blob",)])

    def test_unknown_batch(self):
        self.assertIsNone(self.store.batch(99))
        self.assertEqual(self.store.operations(99), [])


class History(StoreTest):
    def undo(self, batch, *names):
        back = self.store.begin("undo", "/d", undoes=batch)
        for name in names:
            op = self.store.intent(back, "move", f"/d/to/{name}", f"/d/{name}", FILE)
            self.store.finish(op, "DONE")
        return back

    def test_newest_first_with_counts(self):
        first = self.store.begin("tidy", "/d")
        self.move(first, "a")
        self.move(first, "b")
        self.move(first, "c", "ABORTED", "It is empty.")
        second = self.store.begin("auto", "/e")
        self.move(second, "z")
        new, old = self.store.history()
        self.assertEqual((new["batch"], new["kind"], new["folder"], new["count"]), (second, "auto", "/e", 1))
        self.assertEqual((old["batch"], old["count"], old["names"]), (first, 2, ["a", "b"]))
        self.assertEqual(old["left"], [{"name": "c", "why": "It is empty."}])
        self.assertAlmostEqual(old["when"], time.time(), delta=60)
        self.assertEqual((old["undone"], old["can_undo"]), (False, True))

    def test_limit_counts_only_what_is_shown(self):
        for number in range(5):
            self.move(self.store.begin("tidy", "/d"), f"f{number}")
            self.move(self.store.begin("auto", "/d"), "skipped", "ABORTED", "why")
        self.assertEqual([run["names"] for run in self.store.history(limit=2)], [["f4"], ["f3"]])
        self.assertEqual(len(self.store.history()), 5)

    def test_batches_that_did_nothing_are_left_out(self):
        self.store.begin("tidy", "/d")
        self.move(self.store.begin("tidy", "/d"), "a", "INTENT")
        made = self.store.begin("tidy", "/d")
        self.move(made, "to", action="mkdir", dest=None)
        self.assertEqual(self.store.history(), [])

    def test_conflicts_are_shown(self):
        self.move(self.store.begin("tidy", "/d"), "a", "CONFLICT", "look here")
        (run,) = self.store.history()
        self.assertEqual((run["count"], run["left"], run["can_undo"], run["undone"]),
                         (0, [{"name": "a", "why": "look here"}], False, False))

    def test_partly_and_fully_undone(self):
        batch = self.store.begin("tidy", "/d")
        self.move(batch, "a")
        self.move(batch, "b")
        self.undo(batch, "a")
        shown = self.store.history()
        self.assertEqual([op.source for op in self.store.to_undo(batch)], ["/d/b"])
        self.assertEqual((shown[1]["undone"], shown[1]["can_undo"]), (False, True))
        self.undo(batch, "b")
        shown = self.store.history()
        self.assertEqual((shown[2]["undone"], shown[2]["can_undo"]), (True, False))
        self.assertEqual((shown[0]["kind"], shown[0]["names"], shown[0]["can_undo"]), ("undo", ["b"], False))

    def test_a_failed_undo_doesnt_count(self):
        batch = self.store.begin("tidy", "/d")
        self.move(batch, "a")
        back = self.store.begin("undo", "/d", undoes=batch)
        self.store.finish(self.store.intent(back, "move", "/d/to/a", "/d/a", FILE), "ABORTED", "no")
        self.assertTrue(self.store.history()[0]["can_undo"])
        self.assertEqual(len(self.store.to_undo(batch)), 1)

    def test_an_undo_of_another_batch_doesnt_count(self):
        first, second = self.store.begin("tidy", "/d"), self.store.begin("tidy", "/d")
        self.move(first, "a")
        self.move(second, "a")
        self.undo(second, "a")
        self.assertEqual(len(self.store.to_undo(first)), 1)

    def test_newest_move_comes_back_first(self):
        batch = self.store.begin("tidy", "/d")
        for name in "abc":
            self.move(batch, name)
        self.assertEqual([op.source for op in self.store.to_undo(batch)], ["/d/c", "/d/b", "/d/a"])

    def test_trash_without_a_known_place_cant_be_undone(self):
        batch = self.store.begin("trash", "/d")
        self.move(batch, "a", action="trash", dest=None)
        self.assertEqual((self.store.history()[0]["can_undo"], self.store.history()[0]["undone"]), (False, False))
        known = self.store.begin("trash", "/d")
        self.move(known, "b", action="trash")
        self.assertTrue(self.store.history()[0]["can_undo"])

    def test_at_most_200_names(self):
        batch = self.store.begin("tidy", "/d")
        for number in range(230):
            self.move(batch, f"f{number}")
        (run,) = self.store.history()
        self.assertEqual((run["count"], len(run["names"])), (230, 200))


class Seen(StoreTest):
    def test_first_time_is_kept(self):
        self.store.remember([record()], 5)
        self.store.remember([record()], 9)
        self.assertEqual(self.store.first_seen(record()), 5)
        self.assertIsNone(self.store.first_seen(record(inode=8)))

    def test_size_doesnt_matter(self):
        self.store.remember([Identity(1, 7, 10, 100)], 5)
        self.assertEqual(self.store.first_seen(Identity(1, 7, 99, 100)), 5)

    def test_edited_or_renamed_is_the_same_file(self):
        self.store.remember([record()], 5)
        self.assertEqual(self.store.first_seen(record(mtime_ns=200)), 5)
        self.assertEqual(self.store.first_seen(record(name="b.txt")), 5)

    def test_renamed_then_edited_is_followed(self):
        self.store.remember([record()], 5)
        self.store.remember([record(name="b.txt")], 6)
        self.store.remember([Identity(1, 7, 10, 150)], 7)
        self.assertIsNone(self.store.first_seen(record(name="c.txt", mtime_ns=200)))
        self.store.remember([record(name="b.txt", mtime_ns=200)], 7)
        self.assertEqual(self.store.first_seen(record(name="b.txt", mtime_ns=200)), 5)

    def test_reused_inode_is_a_new_file(self):
        self.store.leave(record())
        new = record(name="new.txt", mtime_ns=900)
        self.assertIsNone(self.store.first_seen(new))
        self.assertFalse(self.store.is_left(new))
        self.store.remember([new], 50)
        self.assertEqual(self.store.first_seen(new), 50)
        self.assertFalse(self.store.is_left(new))
        self.assertIsNone(self.store.first_seen(record()))

    def test_without_a_name_the_inode_decides(self):
        self.store.remember([record()], 5)
        self.assertEqual(self.store.first_seen(Identity(1, 7, 10, 999)), 5)
        self.store.leave(Identity(1, 7, 10, 999))
        self.assertTrue(self.store.is_left(record(mtime_ns=999)))
        self.assertEqual(self.store.first_seen(record(mtime_ns=999)), 5)

    def test_new_device_number_after_a_restart(self):
        self.store.remember([record()], 5)
        self.assertEqual(self.store.first_seen(record(device=2)), 5)
        self.assertEqual(self.store.first_seen(record(device=2, mtime_ns=200)), 5)
        self.assertEqual(self.store.first_seen(Identity(2, 7, 10, 100)), 5)
        self.assertIsNone(self.store.first_seen(Identity(2, 7, 10, 200)))
        self.assertIsNone(self.store.first_seen(record(name="b", device=2)))
        self.store.remember([record(device=2)], 9)
        self.assertEqual(self.store._run("SELECT device, first_ns FROM seen"), [("2", 5)])

    def test_same_inode_on_two_disks(self):
        self.store.remember([record(), record(name="b", device=2, mtime_ns=300)], 5)
        self.store.remember([record(name="c", device=3, mtime_ns=1)], 8)
        self.assertEqual(self.store.first_seen(record(name="c", device=3, mtime_ns=1)), 8)
        self.assertEqual(self.store.first_seen(record()), 5)

    def test_left_files(self):
        self.store.remember([record()], 5)
        self.assertFalse(self.store.is_left(record()))
        self.store.leave(record())
        self.assertTrue(self.store.is_left(record()))
        self.store.remember([record()], 9)
        self.assertTrue(self.store.is_left(record()))
        self.assertEqual(self.store.first_seen(record()), 5)

    def test_leave_a_file_never_seen(self):
        self.store.leave(FILE)
        self.assertTrue(self.store.is_left(FILE))

    def test_prune(self):
        self.store.remember([record(), record(name="b", inode=8)], 5)
        self.store.prune([record(inode=8, name="b")])
        self.assertIsNone(self.store.first_seen(record()))
        self.assertEqual(self.store.first_seen(record(inode=8, name="b")), 5)
        self.store.prune([])
        self.assertEqual(self.store._run("SELECT * FROM seen"), [])

    def test_remember_is_all_or_nothing(self):
        with self.assertRaises(AttributeError):
            self.store.remember([record(), object()], 5)
        self.assertIsNone(self.store.first_seen(record()))


CHILD = """
import sys
from pathlib import Path
from downkeep.store import Store
with Store(Path(sys.argv[1])) as store, store.lock():
    print("ready", flush=True)
    sys.stdin.read()
"""


class Locking(StoreTest):
    def test_one_at_a_time(self):
        other = self.open()
        with self.store.lock():
            for store in (other, self.store):
                with self.assertRaises(Busy), store.lock():
                    pass
        with other.lock():
            pass

    def test_released_after_an_error(self):
        with self.assertRaises(KeyError), self.store.lock():
            raise KeyError
        with self.store.lock():
            pass

    def test_a_killed_process_lets_go(self):
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(self.folder)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path(module.__file__).parents[1])},
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), b"ready")
            with self.assertRaises(Busy), self.store.lock():
                pass
            child.kill()
            child.wait(10)
            with self.store.lock():
                pass
        finally:
            child.kill()
            child.communicate()

    def test_lock_that_isnt_possible(self):
        with mock.patch.object(module.os, "open", side_effect=PermissionError("no")):
            with self.assertRaisesRegex(ActionError, "can't lock"), self.store.lock():
                pass

    def test_other_lock_errors_are_not_busy(self):
        name = "msvcrt.locking" if sys.platform == "win32" else "fcntl.flock"
        with mock.patch(f"downkeep.store.{name}", side_effect=OSError(37, "no locks")):
            with self.assertRaises(ActionError) as caught, self.store.lock():
                pass
        self.assertNotIsInstance(caught.exception, Busy)


class Threads(StoreTest):
    def test_many_threads_share_one_store(self):
        batch = self.store.begin("tidy", "/d")
        errors = []

        def work(number):
            try:
                for step in range(10):
                    self.move(batch, f"{number}-{step}")
                    self.store.remember([record(inode=number * 100 + step)], step)
                    self.store.history()
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.operations(batch)), 80)
        self.assertEqual(self.store._run("SELECT count(*) FROM seen"), [(80,)])

    def test_many_stores_at_once(self):
        errors = []

        def work():
            try:
                with Store(self.folder) as store:
                    store.remember([record()], 1)
                    store.set("a", 1)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
