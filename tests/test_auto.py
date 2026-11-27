import contextlib
import json
import os
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from downkeep import auto, engine
from downkeep.model import ActionError, Busy, Result, Stay
from downkeep.sorting import DEFAULT_RULES, RULES_KEY, check_rules, saved_rules
from downkeep.store import Store
from tests.helpers import FolderTest

HOUR = 3_600 * 10**9
DAY = 24 * HOUR
START = 1_750_000_000 * 10**9


class FakeStore:
    """Only what automatic tidying uses. Settings go through JSON, as in the real one."""

    def __init__(self):
        self.settings, self.batches, self.left, self.locked = {}, {}, set(), False

    def get(self, key, default=None):
        return json.loads(self.settings[key]) if key in self.settings else default

    def set(self, key, value):
        self.settings[key] = json.dumps(value)

    @contextlib.contextmanager
    def lock(self):
        if self.locked:
            raise Busy("busy")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def operations(self, batch):
        return self.batches.get(batch, [])

    def leave(self, file):
        self.left.add(file.inode)

    def is_left(self, file):
        return file.inode in self.left

    def close(self):
        pass


def fake_apply(store, folder, moves, kind="tidy"):
    result = Result(batch=len(store.batches) + 1)
    store.batches[result.batch] = []
    with store.lock():
        for move in moves:
            dest = Path(folder, move.folder, move.file.name)
            dest.parent.mkdir(exist_ok=True)
            if dest.exists():
                raise AssertionError(f"{dest} would be replaced")
            os.rename(move.file.path, dest)
            store.batches[result.batch].append(SimpleNamespace(
                action="move", state="DONE", source=move.file.path, dest=str(dest), identity=move.file))
            result.done.append(move.file.name)
    return result


def fake_undo(store, batch):
    for op in store.operations(batch):
        if not os.path.exists(op.source) and os.path.exists(op.dest):
            os.rename(op.dest, op.source)


class Case(FolderTest):
    def setUp(self):
        super().setUp()
        self.now, self.batch = START, None
        self.store = self.make_store()
        self.addCleanup(self.store.close)
        self.schedule = mock.Mock(**{
            "available.return_value": (True, ""), "command.return_value": ["downkeep"],
            "installed_command.return_value": ["downkeep"],
        })
        self.hosts = {}
        for patch in (
            mock.patch("downkeep.auto.schedule", self.schedule),
            mock.patch("downkeep.auto.time", SimpleNamespace(time_ns=lambda: self.now)),
            mock.patch("downkeep.auto.engine.SUPPORTED", True),
            mock.patch("downkeep.auto.find_hosts", lambda paths: {p: h for p in paths if (h := self.hosts.get(os.path.basename(p)))}),
            *self.patches(),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def make_store(self):
        return FakeStore()

    def patches(self):
        return [mock.patch("downkeep.auto.engine.apply_moves", fake_apply)]

    def put_back(self, batch):
        fake_undo(self.store, batch)

    # A small world

    def put(self, name, content=b"x", age=0):
        """A file whose last change was age ago on the made-up clock."""
        path = self.folder / name
        path.write_bytes(content)
        os.utime(path, ns=(self.now - age, self.now - age))
        return path

    def on(self, *own, days=1, **defaults):
        rules = [item | {"days": days} | defaults for item in DEFAULT_RULES]
        self.store.set(RULES_KEY, check_rules([*own, *rules]))
        auto.turn_on(self.store, self.folder)

    def run_at(self, *waits):
        """Runs after each wait and returns the names moved by any of them."""
        done = []
        for wait in waits:
            self.now += wait
            result = auto.run(self.store, self.now)
            self.batch = result.batch or self.batch
            done += result.done
        return sorted(done)

    def hours(self, count):
        return self.run_at(*[HOUR] * count)

    def moved(self):
        return sorted(str(path.relative_to(self.folder).as_posix()) for path in self.folder.glob("*/*"))

    def undo(self, batch=None):
        self.put_back(batch or self.batch)
        auto.note_undo(self.store, batch or self.batch)

    def status(self):
        return auto.status(self.store, str(self.folder))


class Behaviour:
    def test_off_until_turned_on(self):
        self.put("a.png", age=90 * DAY)
        self.assertEqual(self.run_at(HOUR, 8 * DAY), [])
        status = self.status()
        self.assertEqual((status["on"], status["folder"], status["waiting"], status["next"], status["note"]),
                         (False, None, 0, None, ""))
        self.assertIsNone(self.store.get(auto.LEDGER))

    def test_new_file_moves_after_the_wait_and_old_ones_never(self):
        self.put("old.png", age=90 * DAY)
        self.on()
        self.put("new.png")
        self.assertEqual(self.hours(24), [])
        self.assertEqual(self.hours(1), ["new.png"])
        self.assertEqual(self.moved(), ["Images/new.png"])
        self.assertEqual(self.hours(72), [])
        self.assertTrue((self.folder / "old.png").exists())

    def test_wait_counts_from_first_seen_even_for_an_old_date(self):
        self.on()
        self.put("new.png", age=300 * DAY)
        self.assertEqual(self.hours(24), [])
        self.assertEqual(self.hours(1), ["new.png"])

    def test_wait_counts_from_the_last_change_too(self):
        self.on()
        path = self.put("new.png")
        self.hours(20)
        os.utime(path, ns=(self.now - 120 * 10**9, self.now - 120 * 10**9))
        self.assertEqual(self.hours(23), [])
        self.assertEqual(self.hours(1), ["new.png"])

    def test_everything_there_at_the_start_counts_even_what_a_scan_leaves_out(self):
        self.put("growing.mkv.part")
        self.put("empty.png", b"")
        self.put("justnow.png")
        self.put(".hidden.png")
        self.on()
        (self.folder / "growing.mkv.part").rename(self.folder / "growing.mkv")
        (self.folder / ".hidden.png").rename(self.folder / "shown.png")
        self.put("empty.png", b"now full")
        self.assertEqual(self.hours(72), [])
        self.assertEqual(self.moved(), [])

    def test_old_files_stay_old_when_renamed_edited_or_replaced(self):
        for name in ("renamed.png", "edited.png", "saved.pdf", "both.png", "chain.png"):
            self.put(name, age=90 * DAY)
        self.on()
        (self.folder / "renamed.png").rename(self.folder / "renamed2.png")
        self.put("edited.png", b"longer", age=DAY)
        self.put("new.tmp", b"atomic save", age=DAY).replace(self.folder / "saved.pdf")
        (self.folder / "both.png").rename(self.folder / "both2.png")
        self.put("both2.png", b"edited after the rename", age=DAY)
        self.run_at(HOUR)
        self.put("new.tmp", b"atomic", age=DAY).replace(self.folder / "chain.png")
        self.run_at(HOUR)
        (self.folder / "chain.png").rename(self.folder / "chain2.png")
        self.assertEqual(self.hours(72), [])
        self.assertEqual(self.moved(), [])

    def test_old_file_changed_in_the_last_minute_is_not_forgotten(self):
        path = self.put("old.png", age=90 * DAY)
        self.on()
        self.now += HOUR
        os.utime(path, ns=(self.now, self.now))
        path.rename(self.folder / "older.png")
        auto.run(self.store, self.now)
        self.assertEqual(self.hours(72), [])

    def test_a_new_file_with_the_name_of_a_deleted_old_one(self):
        self.put("a.png", age=90 * DAY)
        keep = self.put("b.png", age=90 * DAY)
        self.on()
        (self.folder / "a.png").rename(self.folder.parent / "elsewhere.png")
        self.run_at(HOUR)
        self.put("a.png")
        self.assertEqual(self.hours(25), ["a.png"])
        self.assertTrue(keep.exists())

    def test_must_be_the_same_in_two_runs_a_minute_apart(self):
        self.on(days=0)
        self.put("a.png", b"1", age=DAY)
        self.assertEqual(self.run_at(HOUR), [])
        self.assertEqual(self.run_at(59 * 10**9), [])
        self.assertEqual(self.run_at(10**9), ["a.png"])

    def test_growing_file_with_a_kept_date_is_not_finished(self):
        self.on(days=0)
        for size in range(1, 6):
            self.put("a.zip", b"x" * size)
            os.utime(self.folder / "a.zip", ns=(START - DAY, START - DAY))
            self.assertEqual(self.run_at(HOUR), [])
        self.assertEqual(self.run_at(HOUR), ["a.zip"])

    def test_same_size_but_another_date_is_not_finished(self):
        self.on(days=0)
        for step in range(4):
            os.utime(self.put("a.zip"), ns=(START - DAY + step * 10**9,) * 2)
            self.assertEqual(self.run_at(HOUR), [])
        self.assertEqual(self.run_at(HOUR), ["a.zip"])

    def test_unfinished_empty_and_fresh_files_wait(self):
        self.on(days=0)
        self.put("a.png.crdownload", age=DAY)
        self.put("b.png", b"", age=DAY)
        self.put("c.png", age=DAY)
        self.put("c.png.part", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR, HOUR), [])
        (self.folder / "c.png.part").rename(self.folder.parent / "gone")
        self.assertEqual(self.run_at(HOUR), [])
        self.assertEqual(self.run_at(HOUR), ["c.png"])

    def test_replaced_by_another_file_starts_again(self):
        self.on()
        self.put("a.png")
        self.hours(20)
        self.put("b", age=300 * DAY).replace(self.folder / "a.png")
        self.assertEqual(self.hours(24), [])
        self.assertEqual(self.hours(1), ["a.png"])

    def test_only_rules_that_are_automatic(self):
        own = {"folder": "Cats", "words": ["cat"], "auto": False, "days": 0}
        self.on(own, days=0)
        self.put("cat.png", age=DAY)
        self.put("dog.png", age=DAY)
        self.put("x.zzz", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR), ["dog.png"])
        self.assertEqual(self.moved(), ["Images/dog.png"])

    def test_each_rule_has_its_own_wait(self):
        self.on({"folder": "Cats", "words": ["cat"], "auto": True, "days": 1}, days=2)
        self.put("cat.png")
        self.put("dog.png")
        self.assertEqual(self.hours(25), ["cat.png"])
        self.assertEqual(self.hours(23), [])
        self.assertEqual(self.hours(1), ["dog.png"])

    def test_sites(self):
        self.on({"folder": "College", "sites": ["example.edu"], "auto": True, "days": 0}, days=0)
        self.put("notes.pdf", age=DAY)
        self.hosts = {"notes.pdf": "www.example.edu"}
        self.run_at(HOUR, HOUR)
        self.assertEqual(self.moved(), ["College/notes.pdf"])

    def test_name_taken_stays_and_is_never_replaced(self):
        (self.folder / "Images").mkdir()
        (self.folder / "Images" / "A.png").write_bytes(b"first")
        self.on(days=0)
        self.put("a.png", b"second", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR, DAY), [])
        self.assertEqual((self.folder / "Images" / "A.png").read_bytes(), b"first")
        self.assertEqual((self.folder / "a.png").read_bytes(), b"second")

    def test_at_most_200_at_once(self):
        self.on(days=0)
        for number in range(230):
            self.put(f"{number:03}.png", age=DAY)
        self.assertEqual(self.run_at(HOUR), [])
        self.assertEqual(self.status()["waiting"], 230)
        self.assertEqual(len(self.run_at(HOUR)), 200)
        self.assertEqual(self.status()["waiting"], 30)
        self.assertEqual(len(self.run_at(HOUR)), 30)
        self.assertEqual(self.status()["waiting"], 0)

    def test_files_that_fail_do_not_block_the_rest(self):
        self.on(days=0)
        for number in range(210):
            self.put(f"{number:03}.png", age=DAY)
        self.run_at(HOUR)
        tried = []

        def failing(store, folder, moves, kind):
            tried.append([move.file.name for move in moves])
            return Result(skipped=[Stay(move.file.name, "in use") for move in moves])

        with mock.patch("downkeep.auto.engine.apply_moves", failing):
            self.run_at(HOUR, HOUR)
        self.assertEqual(tried[1][:10], [f"{number:03}.png" for number in range(200, 210)])

    def test_undone_file_never_moves_again(self):
        self.on()
        for name in ("a.png", "b.png", "c.png"):
            self.put(name)
        self.assertEqual(len(self.hours(25)), 3)
        self.undo()
        self.put("b.png", b"edited", age=DAY)
        self.put("x", b"atomic save", age=DAY).replace(self.folder / "c.png")
        (self.folder / "a.png").rename(self.folder / "renamed.png")
        self.assertEqual(self.hours(72), [])
        (self.folder / "b.png").rename(self.folder.parent / "away.png")
        self.run_at(HOUR)
        (self.folder.parent / "away.png").rename(self.folder / "b.png")
        self.assertEqual(self.hours(72), [])

    def test_undone_file_stays_after_off_and_on(self):
        self.on()
        self.put("a.png")
        self.assertEqual(self.hours(25), ["a.png"])
        auto.turn_off(self.store)
        self.undo()
        (self.folder / "a.png").rename(self.folder.parent / "away")
        self.on()
        (self.folder.parent / "away").rename(self.folder / "a.png")
        self.assertEqual(self.hours(72), [])

    def test_two_undone_passes_stop_a_rule_and_say_so(self):
        self.on(days=0)
        for name in ("a.png", "b.mp3"):
            self.put(name, age=DAY)
        self.run_at(HOUR, HOUR)
        self.undo()
        auto.note_undo(self.store, self.batch)
        self.assertTrue(all(rule["auto"] for rule in saved_rules(self.store)))
        self.assertEqual(self.status()["note"], "")
        self.put("c.png", age=DAY)
        self.run_at(HOUR, HOUR)
        self.undo()
        self.assertEqual([rule["id"] for rule in saved_rules(self.store) if not rule["auto"]], ["images"])
        self.assertEqual(self.status()["note"], "New files no longer go to Images by themselves, because you undid that twice.")
        self.put("d.png", age=DAY)
        self.put("d.mp3", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR), ["d.mp3"])

    def test_turning_the_rule_back_on_starts_counting_again(self):
        self.test_two_undone_passes_stop_a_rule_and_say_so()
        self.store.set(RULES_KEY, [rule | {"auto": True} for rule in saved_rules(self.store)])
        self.assertEqual(self.status()["note"], "")
        self.assertEqual(self.run_at(HOUR), ["d.png"])
        self.undo()
        self.assertTrue(all(rule["auto"] for rule in saved_rules(self.store)))

    def test_undo_that_brought_nothing_back_counts_for_nothing(self):
        self.on(days=0)
        for name in ("a.png", "b.png"):
            self.put(name, age=DAY)
            self.run_at(HOUR, HOUR)
        for batch in (1, 2):
            auto.note_undo(self.store, batch)
        auto.note_undo(self.store, 99)
        self.assertTrue(all(rule["auto"] for rule in saved_rules(self.store)))
        self.assertEqual(self.store.get(auto.UNDONE)["names"], [])

    def test_clock_set_back_never_moves_early(self):
        self.on()
        self.run_at(HOUR)
        ledger = self.store.get(auto.LEDGER)
        self.now -= 2000 * DAY
        self.put("a.png", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR), [])
        self.assertIn("clock", self.status()["note"])
        self.assertEqual(self.store.get(auto.LEDGER), ledger)
        self.now += 2000 * DAY
        self.assertEqual(self.hours(24), [])
        self.assertEqual(self.hours(1), ["a.png"])

    def test_file_first_seen_while_the_clock_was_behind(self):
        self.on()
        self.hours(30)
        self.now -= 20 * HOUR
        self.put("a.png", age=DAY)
        self.assertEqual(self.hours(2), [])
        self.now += 20 * HOUR
        self.assertEqual(self.hours(20), [])
        self.assertEqual(self.store.get(auto.LEDGER)["files"]["a.png"]["waited"], 20 * HOUR)
        self.assertEqual(self.hours(3), [])
        self.assertEqual(self.hours(1), ["a.png"])

    def test_clock_set_back_after_first_seen(self):
        self.on()
        self.put("a.png", age=DAY)
        self.hours(20)
        self.now -= 10 * HOUR
        self.assertEqual(self.hours(3), [])
        self.assertEqual(self.store.get(auto.LEDGER)["files"]["a.png"]["waited"], 21 * HOUR)
        self.assertEqual(self.hours(2), [])
        self.assertEqual(self.hours(1), ["a.png"])

    def test_time_already_counted_is_not_counted_again(self):
        self.on()
        self.put("a.png", age=DAY)
        self.hours(10)
        for _ in range(5):
            self.now -= 3 * HOUR
            self.hours(1)
            self.now += 3 * HOUR
            self.hours(1)
        self.assertEqual(self.store.get(auto.LEDGER)["files"]["a.png"]["waited"], 19 * HOUR)

    def test_time_counted_while_behind_is_not_counted_again_when_the_clock_is_fixed(self):
        self.on()
        self.put("a.png", age=DAY)
        self.hours(10)
        self.now -= 3 * HOUR
        self.hours(2)
        self.now += 3 * HOUR
        self.hours(1)
        self.assertEqual(self.store.get(auto.LEDGER)["files"]["a.png"]["waited"], 12 * HOUR)

    def test_one_run_far_in_the_future_does_not_block_later_files(self):
        self.on()
        auto.run(self.store, self.now + 4000 * DAY)
        self.put("a.png")
        auto.run(self.store, self.now + 4000 * DAY)
        self.assertEqual(self.hours(24), [])
        self.assertEqual(self.hours(1), ["a.png"])
        self.put("b.png")
        self.assertEqual(self.hours(24), [])
        self.assertEqual(self.hours(1), ["b.png"])

    def test_clock_jumping_ahead_only_counts_a_few_hours(self):
        self.on()
        self.put("report.pdf", age=120 * 10**9)
        self.assertEqual(self.run_at(61 * 10**9, 61 * 10**9, 8 * DAY), [])
        self.assertEqual(self.store.get(auto.LEDGER)["files"]["report.pdf"]["waited"], 61 * 10**9 + auto.LONGEST_GAP)
        self.assertEqual(self.run_at(*[30 * DAY] * 4), [])
        self.assertEqual(self.run_at(30 * DAY), ["report.pdf"])

    def test_a_computer_off_every_night_still_gets_there(self):
        self.on()
        self.put("a.png")
        for day in range(4):
            self.assertEqual(self.hours(3), [])
            self.now += 20 * HOUR
        self.assertEqual(self.hours(1), ["a.png"])

    def test_future_date_waits_for_it(self):
        self.on(days=0)
        self.put("a.png", age=-20 * DAY)
        self.assertEqual(self.run_at(HOUR, HOUR, 19 * DAY), [])
        self.assertEqual(self.run_at(DAY), ["a.png"])

    def test_missing_or_replaced_folder_is_left_alone(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        ledger = self.store.get(auto.LEDGER)
        away = self.folder.parent / "away"
        self.folder.rename(away)
        self.assertEqual(self.run_at(HOUR), [])
        self.assertIn("The last check stopped", self.status()["note"])
        self.folder.mkdir()
        self.put("b.png", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR, DAY), [])
        self.assertIn("the folder was replaced", self.status()["note"])
        self.assertEqual(self.store.get(auto.LEDGER), ledger)
        self.folder.rename(self.folder.parent / "other")
        away.rename(self.folder)
        self.assertEqual(self.run_at(HOUR), ["a.png"])
        self.assertEqual(self.status()["note"], "")

    def test_link_in_place_of_the_folder_is_left_alone(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        away = self.folder.parent / "away"
        self.folder.rename(away)
        try:
            self.folder.symlink_to(away, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not allowed here")
        self.assertEqual(self.run_at(HOUR), [])
        self.assertIn("the folder was replaced", self.status()["note"])
        self.assertTrue((away / "a.png").exists())

    def test_a_run_that_fails_leaves_the_next_one_working(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        with mock.patch("downkeep.auto.engine.apply_moves", side_effect=RuntimeError("secret detail")):
            with self.assertRaises(RuntimeError):
                self.run_at(HOUR)
        self.assertIn("RuntimeError", self.status()["note"])
        self.assertNotIn("secret", self.status()["note"])
        for error in (ActionError("no"), OSError(5, "Input/output error")):
            with mock.patch("downkeep.auto.engine.apply_moves", side_effect=error):
                self.assertEqual(self.run_at(HOUR), [])
            self.assertIn("The last check stopped", self.status()["note"])
        self.assertEqual(self.run_at(HOUR), ["a.png"])
        self.assertEqual(self.status()["note"], "")

    def test_busy_waits_for_the_next_run_quietly(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        with self.store.lock():
            self.assertEqual(self.run_at(HOUR), [])
        self.assertEqual((self.status()["note"], self.status()["waiting"]), ("", 1))
        self.assertEqual(self.run_at(HOUR), ["a.png"])

    def test_broken_rules_move_nothing(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        self.store.set(RULES_KEY, [{"folder": "../.."}])
        self.assertEqual(self.run_at(HOUR), [])
        self.assertIn("The last check stopped", self.store.get(auto.LOG)["problem"])

    def test_turned_off_while_a_run_is_choosing(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        choose = auto._choose

        def slow(*given):
            found = choose(*given)
            auto.turn_off(self.store)
            return found

        with mock.patch("downkeep.auto._choose", slow):
            self.assertEqual(self.run_at(HOUR), [])
        self.assertFalse(self.status()["on"])
        self.assertEqual(self.moved(), [])

    def test_lost_or_foreign_notes_make_everything_old(self):
        for notes in (None, {"folder": "/elsewhere", "peak": 0, "files": {}}):
            self.on(days=0)
            self.put("a.png", age=DAY)
            self.run_at(HOUR)
            self.store.set(auto.LEDGER, notes)
            self.assertEqual(self.run_at(HOUR, HOUR, DAY), [])

    def test_turn_off_keeps_history_and_stops(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR, HOUR)
        auto.turn_off(self.store)
        self.schedule.remove.assert_called_once_with()
        self.assertEqual(len(self.store.operations(1)) > 0, True)
        self.put("b.png", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR), [])
        self.assertIsNone(self.status()["folder"])

    def test_off_even_if_the_schedule_cannot_be_removed(self):
        self.on()
        self.schedule.remove.side_effect = ActionError("no")
        with self.assertRaises(ActionError):
            auto.turn_off(self.store)
        self.assertFalse(self.status()["on"])

    def test_files_that_arrived_while_off_are_old(self):
        self.on(days=0)
        auto.turn_off(self.store)
        self.put("a.png", age=DAY)
        self.on(days=0)
        self.assertEqual(self.run_at(HOUR, HOUR, 10 * DAY), [])

    def test_turning_on_again_changes_nothing_for_the_same_folder(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        auto.turn_on(self.store, self.folder)
        self.assertEqual(self.schedule.install.call_count, 2)
        self.assertEqual(self.run_at(HOUR), ["a.png"])

    def test_turning_on_for_another_folder_starts_over_there(self):
        self.on(days=0)
        other = self.folder.parent / "Other"
        other.mkdir()
        (other / "a.png").write_bytes(b"x")
        os.utime(other / "a.png", ns=(self.now - DAY, self.now - DAY))
        auto.turn_on(self.store, other)
        self.assertEqual(self.run_at(HOUR, HOUR, DAY), [])
        self.assertEqual(self.status()["folder"], str(other))

    def test_turn_on_refuses_what_cannot_work(self):
        for change in (
            lambda: self.schedule.configure_mock(**{"available.return_value": (False, "no session")}),
            lambda: self.schedule.configure_mock(**{"install.side_effect": ActionError("no")}),
        ):
            change()
            with self.assertRaises(ActionError):
                auto.turn_on(self.store, self.folder)
            self.assertFalse(self.status()["on"])
            self.schedule.configure_mock(**{"available.return_value": (True, ""), "install.side_effect": None})
        for folder in (self.folder / "missing", Path.home(), Path(self.folder.anchor)):
            with self.assertRaises(ActionError):
                auto.turn_on(self.store, folder)
        with mock.patch("downkeep.auto.engine.SUPPORTED", False):
            with self.assertRaises(ActionError):
                auto.turn_on(self.store, self.folder)
            self.assertEqual(self.status()["available"], False)
            self.assertIn("not available", self.status()["why"])

    def test_status(self):
        self.put("old.png")
        self.on()
        self.put("a.png")
        self.assertEqual(self.status(), {"available": True, "why": "", "on": True, "next": "within the next hour",
                                         "waiting": 0, "folder": str(self.folder), "note": "", "summary": (
            "On. Every hour, new files older than 1 day move to their folder, even when Downkeep is closed.")})
        self.run_at(HOUR)
        self.now += 20 * 60 * 10**9
        self.assertEqual((self.status()["next"], self.status()["waiting"]), ("in about 40 minutes", 1))
        self.now += 2 * HOUR
        self.assertEqual(self.status()["next"], "in a few minutes")
        self.schedule.installed_command.return_value = None
        self.assertEqual(self.status()["next"], None)
        self.assertIn("missing", self.status()["note"])

    def test_summary(self):
        self.assertEqual(self.status()["summary"], "Off. Nothing moves by itself.")
        for days, own, expected in [
            (0, [], "new files move to their folder,"), (3, [], "older than 3 days move"),
            (1, [{"folder": "Cats", "words": ["cat"], "days": 7}], "once they are older than its wait (1 to 7 days),"),
            (2, [{"folder": "Cats", "words": ["cat"], "days": 0}], "once they are older than its wait (0 to 2 days),"),
        ]:
            self.on(*own, days=days)
            self.assertIn(expected, self.status()["summary"])
        self.on(days=1, auto=False)
        self.assertEqual(self.status()["summary"],
                         "On, but no folder is set to take new files by itself, so nothing moves.")
        self.store.set(RULES_KEY, "broken")
        self.assertIn("no folder", self.status()["summary"])

    def test_own_folders_are_automatic_like_the_built_in_ones(self):
        self.on({"folder": "Bills", "words": ["bill"]})
        self.put("bill.pdf")
        self.assertEqual(self.hours(25), ["bill.pdf"])
        self.assertEqual(self.moved(), ["Bills/bill.pdf"])
        self.assertTrue(all(rule["days"] == 1 and rule["auto"] for rule in saved_rules(self.store)))

    def test_turning_off_stops_between_two_files(self):
        self.on(days=0)
        for number in range(5):
            self.put(f"{number}.png", age=DAY)
        self.hours(1)
        seen = []

        def stopping(store, folder, moves, kind, should_stop):
            for move in moves:
                if should_stop():
                    break
                seen.append(move.file.name)
                auto.turn_off(store)
            return Result()

        with mock.patch("downkeep.auto.engine.apply_moves", stopping):
            self.hours(1)
        self.assertEqual(seen, ["0.png"])

    @unittest.skipIf(sys.platform in ("win32", "darwin"), "names there are always valid text")
    def test_names_that_are_not_valid_text(self):
        self.put("old\udce9.png", age=90 * DAY)
        self.on(days=0)
        self.put("new\udce9.png", age=DAY)
        self.assertEqual(self.run_at(HOUR, HOUR), ["new\udce9.png"])
        self.undo()
        self.assertEqual(self.run_at(HOUR, HOUR, DAY), [])

    def test_never_uses_the_trash_and_says_auto(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        with mock.patch("downkeep.auto.engine.apply_moves", return_value=Result()) as applied:
            self.run_at(HOUR)
        self.assertEqual(applied.call_args.args[3], "auto")
        self.assertNotIn("trash", Path(auto.__file__).read_text(encoding="utf-8").lower())


class FakeAutoTest(Behaviour, Case):
    def test_a_simulated_month(self):
        for seed in (1, 2):
            with self.subTest(seed=seed):
                self.folder = self.folder.parent / f"Downloads {seed}"
                self.folder.mkdir()
                self.store = self.make_store()
                Month(self, seed).live(24 * 31)


@unittest.skipUnless(engine.SUPPORTED, "moving is not available here")
class RealAutoTest(Behaviour, Case):
    def make_store(self):
        return Store(self.folder.parent / "data")

    def patches(self):
        return [mock.patch("downkeep.engine.site_of", return_value=None)]

    def put_back(self, batch):
        engine.undo(self.store, batch)

    def test_a_simulated_week(self):
        Month(self, 99).live(24 * 12, days=3)

    def test_undo_alone_is_enough_to_stay(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR, HOUR)
        self.put_back(1)
        self.assertEqual(self.run_at(HOUR, HOUR, 10 * DAY), [])

    def test_turning_off_stops_the_real_engine_between_two_files(self):
        self.on(days=0)
        for number in range(5):
            self.put(f"{number}.png", age=DAY)
        self.hours(1)
        steps = []

        def step(name):
            if name == "move saved" and not steps:
                steps.append(name)
                auto.turn_off(self.store)

        with mock.patch("downkeep.engine._checkpoint", step):
            self.assertEqual(self.hours(1), ["0.png"])
        self.assertEqual(len(list(self.folder.glob("*.png"))), 4)

    def test_history_says_auto(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR, HOUR)
        (found,) = self.store.history()
        self.assertEqual((found["kind"], found["names"], found["can_undo"]), ("auto", ["a.png"], True))

    def test_a_file_in_use_is_skipped(self):
        self.on(days=0)
        self.put("a.png", age=DAY)
        self.run_at(HOUR)
        with mock.patch("downkeep.engine.in_use", return_value=True):
            self.assertEqual(self.run_at(HOUR), [])
        self.assertEqual(self.run_at(HOUR), ["a.png"])


class Month:
    """Invents what a person does each hour and checks every move against its own notes. Each file starts with a
    number that stays the same through edits, so it can be followed anywhere."""

    KINDS = ("png", "pdf", "mp3", "zip", "exe", "zzz")

    def __init__(self, case, seed):
        self.case, self.random = case, random.Random(seed)
        self.old, self.undone, self.growing = set(), set(), {}
        self.arrived, self.changed, self.renamed, self.count, self.seen = {}, {}, {}, 0, set()

    def live(self, hours, days=7):
        case = self.case
        self.days = days
        for _ in range(15):
            self.old.add(self.arrive(age=self.random.randint(1, 400) * DAY))
        for _ in range(3):
            self.old.add(self.arrive(download=True))
        case.on(days=days)
        for hour in range(hours):
            case.now += HOUR + self.random.randint(-120, 120) * 10**9
            for act in self.random.sample([self.nothing, self.arrive_any, self.edit, self.rename, self.grow, self.clash,
                                           self.finish], 3):
                act()
            clock = case.now - self.random.choice([0] * 90 + [3 * DAY, 400 * DAY])
            result = auto.run(case.store, clock)
            if result.done or hour % 24 == 0:
                self.check(result)
            if result.batch and self.random.random() < 0.15:
                self.undo(result.batch)
            case.store.set(RULES_KEY, [rule | {"auto": True} for rule in saved_rules(case.store)])
        case.assertGreater(len(self.seen), 20, "the month must move enough to mean something")
        case.assertGreater(len(self.undone), 0)

    def token(self, path):
        return int(path.read_bytes().split(b"|")[0])

    def visible(self):
        return [path for path in sorted(self.case.folder.iterdir()) if path.is_file() and path.suffix != ".part"]

    def write(self, name, token, filler, when):
        path = self.case.folder / name
        path.write_bytes(b"%d|%s" % (token, filler))
        os.utime(path, ns=(when, when))
        self.changed[token] = self.case.now

    def arrive(self, age=0, download=False, name=None):
        self.count += 1
        token = self.count
        name = name or f"file {token}.{self.random.choice(self.KINDS)}"
        self.arrived[token] = self.case.now
        self.write(name + (".part" if download else ""), token, b"start", self.case.now - age)
        if download:
            self.growing[token] = name
        return token

    def nothing(self):
        pass

    def arrive_any(self):
        self.arrive(age=self.random.choice([0, 0, DAY, 500 * DAY]), download=self.random.random() < 0.3)

    def clash(self):
        moved = sorted(self.case.folder.glob("*/*"))
        if moved and self.random.random() < 0.3:
            name = self.random.choice(moved).name
            name = self.random.choice([name, name.upper()])
            if not (self.case.folder / name).exists():
                self.arrive(name=name)

    def grow(self):
        for token, name in self.growing.items():
            kept = self.random.choice([self.case.now, self.case.now - 900 * DAY])
            self.write(name + ".part", token, b"x" * self.random.randint(1, 99), kept)

    def finish(self):
        for token in list(self.growing):
            if self.random.random() < 0.3:
                name = self.growing.pop(token)
                (self.case.folder / (name + ".part")).rename(self.case.folder / name)
                self.renamed[token] = self.case.now
                self.arrived[token] = self.case.now if token not in self.old else 0

    def pick(self):
        files = self.visible()
        return self.random.choice(files) if files else None

    def edit(self):
        path = self.pick()
        if not path:
            return
        token, when = self.token(path), self.random.choice([self.case.now - 600 * 10**9, self.case.now])
        if self.random.random() < 0.5 and self.renamed.get(token, 0) < self.case.now - 2 * HOUR:
            self.write("save.tmp", token, b"atomic %d" % self.random.randint(1, 999), when)
            (self.case.folder / "save.tmp").replace(path)
        else:
            self.write(path.name, token, b"edit %d" % self.random.randint(1, 999), when)

    def rename(self):
        path = self.pick()
        # A whole-file save and a rename within one hour can't be told from a new file
        if path and self.changed[self.token(path)] < self.case.now - 2 * HOUR:
            self.renamed[self.token(path)] = self.case.now
            path.rename(path.with_name(f"renamed {self.count} {self.random.randint(1, 10**9)}{path.suffix}"))

    def check(self, result):
        case, now = self.case, self.case.now
        inside = {self.token(path): path for path in case.folder.glob("*/*")}
        case.assertEqual(len(inside), len(list(case.folder.glob("*/*"))))
        for token in set(inside) - self.seen:
            case.assertNotIn(token, self.old, "was there before")
            case.assertNotIn(token, self.undone, "was undone")
            case.assertNotIn(token, self.growing, "was not finished")
            case.assertGreaterEqual(now - self.arrived[token], self.days * DAY, "moved early")
            case.assertGreaterEqual(now - self.changed[token], self.days * DAY - 600 * 10**9, "moved too soon after a change")
            case.assertNotEqual(inside[token].suffix, ".zzz")
        case.assertEqual(len(set(inside) - self.seen), len(result.done))
        self.seen |= set(inside)
        everything = {self.token(path) for path in case.folder.rglob("*") if path.is_file()}
        case.assertEqual(everything, set(self.arrived), "a file was lost")

    def undo(self, batch):
        before = {self.token(path) for path in self.case.folder.glob("*/*")}
        self.case.undo(batch)
        back = before - {self.token(path) for path in self.case.folder.glob("*/*")}
        self.undone |= back
        self.seen -= back
