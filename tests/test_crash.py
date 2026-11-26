import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from downkeep import engine
from downkeep.model import ActionError, Identity, Move
from downkeep.scan import scan
from downkeep.store import Store
from tests.helpers import DAY

# Runs one action and kills itself at step number `stop`. Every call that writes is a step.
CHILD = r"""
import json, os, signal, sys
from pathlib import Path
from downkeep import engine
from downkeep.model import Move
from downkeep.scan import scan
from downkeep.store import Store

action, folder, data, stop = sys.argv[1], sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
plan = json.loads(sys.argv[5])
steps = 0

def step(*_):
    global steps
    steps += 1
    if steps == stop:
        # TerminateProcess on Windows: as hard as SIGKILL, nothing is cleaned up
        os.kill(os.getpid(), signal.SIGKILL if os.name == "posix" else signal.SIGTERM)

def around(owner, name):
    real = getattr(owner, name)
    def call(*values, **named):
        result = real(*values, **named)
        step()
        return result
    setattr(owner, name, call)

engine._checkpoint = step
engine.site_of = lambda path: None
for name in ("move_no_replace", "_sync"):
    around(engine, name)
for name in ("mkdir", "rmdir"):
    around(engine.os, name)
with Store(data) as store:
    for name in ("begin", "intent", "finish", "end", "skip", "leave"):
        around(Store, name)
    if action == "apply":
        moves = [Move(file, plan[file.name], "") for file in scan(folder).files if file.name in plan]
        engine.apply_moves(store, folder, moves)
    elif action == "undo":
        engine.undo(store, plan)
    else:
        engine.recover(store)
print(steps)
"""


class CrashTest(unittest.TestCase):
    def setUp(self):
        if not engine.SUPPORTED:
            self.skipTest("moving files is not available on this system")
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name).resolve()
        patch = mock.patch.object(engine, "site_of", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def child(self, case, action, stop=0, plan=None):
        done = subprocess.run(
            [sys.executable, "-c", CHILD, action, str(case.folder), str(case.data), str(stop), json.dumps(plan)],
            capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(Path(engine.__file__).parents[1]), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        killed = done.returncode == (-signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
        if done.returncode and not killed:
            raise AssertionError(done.stderr.decode(errors="replace"))
        return None if killed else int(done.stdout)

    def every_step(self, prepare, action, check, plan=None):
        """Runs the action once to count its steps, then once per step on a fresh copy, killed there."""
        steps = self.child(prepare("whole"), action, plan=plan)

        def one(stop):
            case = prepare(f"{action}-{stop}")
            self.assertIsNone(self.child(case, action, stop, plan), f"step {stop} was not reached")
            check(case, stop)

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(one, range(1, steps + 1)))
        return steps


class Case:
    """A folder with files to move, where every file has its own contents."""

    def __init__(self, base, name):
        self.folder, self.data = base / name / "Downloads", base / name / "data"
        self.folder.mkdir(parents=True)
        self.fixed = {}

    def add(self, name, content=None):
        path = self.folder / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content or f"content of {name}".encode("utf-8", "surrogateescape"))
        when = time.time() - 10 * DAY
        os.utime(path, (when, when))

    def keep(self, name, content=None):
        self.add(name, content or b"mine")
        self.fixed[name] = (self.folder / name).read_bytes()

    def tree(self):
        return {
            str(path.relative_to(self.folder)): path.read_bytes() for path in self.folder.rglob("*") if path.is_file()
        }

    def start(self):
        self.before = self.tree()
        return self


def rows(store):
    return store._run("SELECT * FROM operations"), store._run("SELECT * FROM seen")


def check_safe(test, case, store):
    """After a crash: every file is there once, nothing was replaced, and recovering twice equals once."""
    tree = case.tree()
    test.assertEqual(sorted(tree.values()), sorted(case.before.values()))
    test.assertEqual({name: tree.get(name) for name in case.fixed}, case.fixed)
    engine.recover(store)
    test.assertEqual(store.open_intents(), [])
    test.assertEqual(case.tree(), tree)
    once = rows(store)
    again = engine.recover(store)
    test.assertEqual((again.done, again.skipped, rows(store), case.tree()), ([], [], once, tree))
    for (batch,) in store._run("SELECT id FROM batches"):
        for op in store.operations(batch):
            if op.state == "CONFLICT":
                test.assertEqual(op.action, "mkdir", op)
    moved = {op.dest: op.source for (batch,) in store._run("SELECT id FROM batches ORDER BY id") for op in
             store.operations(batch) if op.state == "DONE" and op.action == "move"}
    for name in tree:
        if name not in case.before:
            test.assertIn(str(case.folder / name), moved, "a file is where the records don't say")


def undo_all(test, case, store):
    for run in store.history(1000):
        if run["can_undo"]:
            result = engine.undo(store, run["batch"])
            test.assertEqual(result.skipped, [])
    test.assertEqual(case.tree(), case.before)
    for run in store.history(1000):
        test.assertFalse(run["can_undo"])


class EveryStep(CrashTest):
    PLAN = {"a.png": "Images", "ü b.pdf": "Papers", "clash.pdf": "Papers"}

    def prepare(self, name):
        case = Case(self.base, name)
        for file in self.PLAN:
            case.add(file)
        case.add("stays.txt")
        case.keep(os.path.join("Papers", "clash.pdf"))
        return case.start()

    def moves(self, case):
        return [Move(file, self.PLAN[file.name], "") for file in scan(case.folder).files if file.name in self.PLAN]

    def tidied(self, name):
        case = self.prepare(name)
        with Store(case.data) as store:
            result = engine.apply_moves(store, str(case.folder), self.moves(case))
        self.assertEqual(len(result.done), 2)
        return case

    def test_tidy(self):
        def check(case, stop):
            with Store(case.data) as store:
                check_safe(self, case, store)
                result = engine.apply_moves(store, str(case.folder), self.moves(case))
                self.assertEqual([stay.name for stay in result.skipped], ["clash.pdf"])
                self.assertEqual(len(case.tree()), len(case.before))
                self.assertTrue((case.folder / "Images" / "a.png").exists())
                undo_all(self, case, store)

        self.assertGreater(self.every_step(self.prepare, "apply", check, self.PLAN), 20)

    def test_undo(self):
        def check(case, stop):
            with Store(case.data) as store:
                check_safe(self, case, store)
                undo_all(self, case, store)
                with self.assertRaises(ActionError):
                    engine.undo(store, 1)

        self.assertGreater(self.every_step(self.tidied, "undo", check, plan=1), 20)

    def test_recover(self):
        """Killed while recovering from every kind of open plan."""

        def open_plan(name):
            case = self.prepare(name)
            (case.folder / "Images").mkdir()
            with Store(case.data) as store:
                batch = store.begin("tidy", str(case.folder))
                store.intent(batch, "mkdir", str(case.folder / "New"), None, None)
                for file in scan(case.folder).files:
                    dest = case.folder / "Images" / file.name
                    store.intent(batch, "move", file.path, str(dest), Identity.of(file))
                    if file.name != "stays.txt":
                        os.rename(file.path, dest)
            return case

        def check(case, stop):
            with Store(case.data) as store:
                check_safe(self, case, store)
                undo_all(self, case, store)

        self.assertGreater(self.every_step(open_plan, "recover", check), 10)


class Random(CrashTest):
    ODD = ["{} photo.png", "ü日本 {}.pdf", "{}", "{} \U0001f600.zip", "-rf {}", "{}.tar.gz", "It's {}; --"]
    LINUX_ODD = ["line\nbreak {}", " {} ", "{}\\:*?.txt", "\udcff\udcfe {}"]

    def test_200_files_and_many_crashes(self):
        chance = random.Random(20261125)
        patterns = self.ODD + (self.LINUX_ODD if sys.platform == "linux" else [])
        folders = ["Images", "Papers", "Old things", "日本"]
        case = Case(self.base, "random")
        for number in range(200):
            case.add(chance.choice(patterns).format(number))
        names = sorted(case.tree())
        for name in chance.sample(names, 20):
            case.keep(os.path.join(chance.choice(folders), name), b"mine " + os.fsencode(name))
        for name in chance.sample(names, 20):
            (case.folder / name).chmod(0o444)
        case.start()

        crashes = 0
        for _ in range(24):
            with Store(case.data) as store:
                waiting = [run["batch"] for run in store.history(1000) if run["can_undo"]]
            loose = sorted(path.name for path in case.folder.iterdir() if path.is_file())
            if waiting and (not loose or chance.random() < 0.4):
                action, plan = "undo", chance.choice(waiting)
            else:
                chosen = chance.sample(loose, min(len(loose), chance.randint(1, 40)))
                action, plan = "apply", {name: chance.choice(folders) for name in chosen}
            stop = chance.randint(1, 8 * len(plan) if action == "apply" else 100)
            crashes += self.child(case, action, stop, plan) is None
            with Store(case.data) as store:
                check_safe(self, case, store)
        self.assertGreater(crashes, 15)
        with Store(case.data) as store:
            undo_all(self, case, store)


class TwoAtOnce(CrashTest):
    def test_two_programs_never_lose_or_replace_a_file(self):
        case = Case(self.base, "two")
        plan = {f"f{number}": "Images" for number in range(40)}
        for name in plan:
            case.add(name)
        case.start()

        def run(_):
            try:
                return self.child(case, "apply", plan=plan) is not None
            except AssertionError as error:
                self.assertIn("Busy", str(error))
                return False

        with ThreadPoolExecutor(4) as pool:
            finished = list(pool.map(run, range(4)))
        self.assertIn(True, finished)
        with Store(case.data) as store:
            check_safe(self, case, store)
            self.assertEqual(sorted(os.listdir(case.folder / "Images")), sorted(plan))
            undo_all(self, case, store)


if __name__ == "__main__":
    unittest.main()
