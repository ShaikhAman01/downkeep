import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from downkeep import folders
from downkeep.cli import LINK_FILE, main
from downkeep.model import ActionError, Busy, Move, Result, Stay
from downkeep.scan import scan
from tests.helpers import FolderTest
from tests.test_actions import FakeActions


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(args))
        except SystemExit as error:
            code = error.code
    return code, out.getvalue(), err.getvalue()


class CliTest(FolderTest):
    def setUp(self):
        super().setUp()
        self.actions = FakeActions()
        for name, how in [
            ("downkeep.cli.open_window", {"side_effect": AssertionError("opened a real window")}),
            ("downkeep.server.Actions", {"return_value": self.actions}),
            ("downkeep.cli.Actions", {"return_value": self.actions}),
        ]:
            patch = mock.patch(name, **how)
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(LINK_FILE, None)

    def test_plain_command_opens_the_window_and_stops_when_it_closes(self):
        for args in [(), ("open",), ("open", str(self.folder))]:
            with self.subTest(args=args):
                with (
                    mock.patch("downkeep.cli.downloads_folder", return_value=self.folder),
                    mock.patch("downkeep.cli.open_window") as window,
                    mock.patch("downkeep.cli.App.finished", return_value=True),
                ):
                    code, out, err = run(*args)
                self.assertEqual((code, err), (0, ""))
                self.assertRegex(window.call_args.args[0], r"^http://127\.0\.0\.1:\d+/#.{40,}$")
                self.assertNotIn("http", out)

    def test_stops_when_the_window_process_exits(self):
        process = mock.Mock()
        process.poll.side_effect = [None, None, 0]
        gone = []
        with (
            mock.patch("downkeep.cli.downloads_folder", return_value=self.folder),
            mock.patch("downkeep.cli.open_window", return_value=process) as window,
            mock.patch("downkeep.cli.App.window_gone", lambda app: gone.append(True)),
            mock.patch("downkeep.cli.App.finished", lambda app: bool(gone)),
        ):
            code, _, err = run()
        self.assertEqual((code, err, gone), (0, "", [True]))
        self.assertEqual(process.poll.call_count, 3)
        self.assertEqual(window.call_args.args[1], str(self.folder))

    def test_open_without_a_browser_prints_the_link(self):
        with mock.patch("downkeep.cli.App.finished", return_value=True):
            code, out, _ = run("open", str(self.folder), "--no-browser")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^http://127\.0\.0\.1:\d+/#")

    def test_open_prints_the_link_when_no_browser_opens(self):
        with (
            mock.patch("downkeep.cli.open_window", return_value=False),
            mock.patch("downkeep.cli.App.finished", return_value=True),
        ):
            out = run("open", str(self.folder))[1]
        self.assertRegex(out, r"\nhttp://127\.0\.0\.1:\d+/#")

    def test_missing_downloads_folder_still_opens_the_window(self):
        with (
            mock.patch("downkeep.cli.downloads_folder", return_value=self.folder / "missing"),
            mock.patch("downkeep.cli.open_window", return_value=True) as window,
            mock.patch("downkeep.cli.App.finished", return_value=True),
        ):
            self.assertEqual(run()[0], 0)
        window.assert_called_once()

    def test_open_with_a_bad_folder_starts_nothing(self):
        with mock.patch("downkeep.cli.make_server", side_effect=AssertionError("started")):
            self.assertEqual(run("open", str(self.folder / "missing"))[0], 2)

    def open_with_link_file(self, place, *args):
        with mock.patch.dict(os.environ, {LINK_FILE: str(place)}), mock.patch("downkeep.cli.App.finished", return_value=True):
            return run("open", str(self.folder), *args)

    def test_link_file_gets_the_link_and_nothing_is_opened_or_printed(self):
        for args in [(), ("--no-browser",)]:
            place = self.folder / f"link{len(args)}"
            with mock.patch("downkeep.window.log", side_effect=AssertionError("logged")), mock.patch("webbrowser.open", side_effect=AssertionError("opened")):
                self.assertEqual(self.open_with_link_file(place, *args), (0, "", ""))
            self.assertRegex(place.read_text(encoding="utf-8"), r"^http://127\.0\.0\.1:\d+/#[A-Za-z0-9_-]{43}$")
            if os.name == "posix":
                self.assertEqual(place.stat().st_mode & 0o777, 0o600)

    def test_link_file_works_while_it_runs(self):
        place = self.folder / "link"
        seen = []

        def finished(app):
            seen.append(place.read_text(encoding="utf-8").endswith(app.code))
            return True

        with mock.patch.dict(os.environ, {LINK_FILE: str(place)}), mock.patch("downkeep.cli.App.finished", finished):
            self.assertEqual(run("open", str(self.folder))[0], 0)
        self.assertEqual(seen, [True])

    def test_link_file_never_replaces_or_follows(self):
        there = self.add("there", b"mine")
        self.symlink("dangling", self.folder / "elsewhere")
        self.symlink("link", there)
        for place in [there, self.folder / "dangling", self.folder / "link", self.folder, self.folder / "no" / "folder"]:
            with self.subTest(place=place), mock.patch("downkeep.window.log", side_effect=AssertionError("logged")):
                code, out, err = self.open_with_link_file(place)
                self.assertEqual((code, out), (2, ""))
                self.assertNotIn("http", err)
        self.assertEqual(there.read_bytes(), b"mine")
        self.assertFalse((self.folder / "elsewhere").exists())

    def test_empty_link_file_means_none(self):
        with (
            mock.patch.dict(os.environ, {LINK_FILE: ""}),
            mock.patch("downkeep.cli.open_window", return_value=True) as window,
            mock.patch("downkeep.cli.App.finished", return_value=True),
        ):
            run("open", str(self.folder))
        window.assert_called_once()

    def test_stopping_waits_for_the_move(self):
        with (
            mock.patch("downkeep.cli.App.finished", side_effect=KeyboardInterrupt),
            mock.patch("downkeep.cli.App.settle") as settle,
        ):
            self.assertEqual(run("open", str(self.folder), "--no-browser")[0], 0)
        settle.assert_called_once_with()

    def test_opening_recovers_once(self):
        with mock.patch("downkeep.cli.App.finished", return_value=True):
            run("open", str(self.folder), "--no-browser")
        self.assertEqual(self.actions.did("recover"), [("recover",)])

    def test_tidy_shows_and_changes_nothing(self):
        self.add("a.pdf")
        self.add("b\x1b[31m.pdf" if os.name == "posix" else "b.pdf")
        before = sorted(os.listdir(self.folder))
        code, out, err = run("tidy", str(self.folder))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 files would move into 1 folder. Nothing was changed.", out)
        self.assertIn("Documents (2 files)", out)
        self.assertIn("odd.xyz  (No rule matches)", out)
        self.assertIn("--apply", out)
        self.assertNotIn("\x1b", out)
        self.assertEqual(self.actions.did("tidy") + self.actions.did("recover"), [])
        self.assertEqual(sorted(os.listdir(self.folder)), before)

    def test_tidy_says_what_each_file_is(self):
        self.add("a.pdf")
        self.actions.moves = [Move(scan(self.folder).files[0], "Documents", "Documents go to Documents")]
        self.assertIn("a.pdf  (A PDF document)", run("tidy", str(self.folder))[1])

    def test_tidy_apply(self):
        self.add("a.pdf")
        self.actions.result = Result(12, ["a.pdf"], [Stay("b\n.pdf", "It is open.\x07")])
        code, out, _ = run("tidy", str(self.folder), "--apply")
        self.assertEqual(code, 0)
        self.assertIn("Moved 1 file.", out)
        self.assertIn("downkeep undo 12", out)
        self.assertNotIn("\x07", out)
        self.assertEqual([call[0] for call in self.actions.calls], ["recover", "plan", "tidy"])
        self.assertEqual(self.actions.did("tidy")[0][1:], (str(self.folder), [("a.pdf", "Documents")]))

    def test_tidy_apply_that_moves_nothing(self):
        self.add("a.pdf")
        self.actions.result = Result(12, [], [Stay("a.pdf", "It is open.")])
        code, out, _ = run("tidy", str(self.folder), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was moved.", out)
        self.assertNotIn("undo", out)

    def test_nothing_to_tidy(self):
        for args in [(), ("--apply",)]:
            code, out, _ = run("tidy", str(self.folder), *args)
            self.assertEqual(code, 0)
            self.assertIn("Nothing to tidy", out)
        self.assertEqual(self.actions.did("tidy"), [])

    def test_tidy_uses_the_usual_folder(self):
        with mock.patch("downkeep.cli.downloads_folder", return_value=self.folder):
            run("tidy")
        self.assertEqual(self.actions.did("plan"), [("plan", str(self.folder))])

    def test_long_lists_of_left_files_are_cut(self):
        self.actions.stays = [Stay(f"{number}.xyz", "No rule matches") for number in range(50)]
        out = run("tidy", str(self.folder))[1]
        self.assertIn("and 30 more", out)
        self.assertIn("Left alone (50 files)", out)

    def runs(self):
        base = {"when": 1_700_000_000, "folder": "/d", "names": [], "left": []}
        self.actions.runs = [
            {**base, "batch": 5, "kind": "undo", "count": 1, "undone": False, "can_undo": False},
            {**base, "batch": 4, "kind": "tidy", "count": 2, "undone": True, "can_undo": True},
            {**base, "batch": 3, "kind": "auto", "count": 2, "undone": False, "can_undo": True},
            {**base, "batch": 2, "kind": "trash", "count": 1, "undone": False, "can_undo": True},
        ]

    def test_history(self):
        self.assertEqual(run("history")[:2], (0, "Nothing has been moved yet.\n"))
        self.runs()
        lines = run("history")[1].splitlines()
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[1], r"4  \d{4}-\d\d-\d\d \d\d:\d\d  Tidied, 2 files \(undone\)")

    def test_undo(self):
        self.runs()
        code, out, _ = run("undo")
        self.assertEqual((code, out), (0, "Put back 1 file.\n"))
        run("undo", "2")
        self.assertEqual(self.actions.did("undo"), [("undo", 3), ("undo", 2)])
        self.assertEqual(self.actions.calls[0], ("recover",))

    def test_nothing_to_undo(self):
        self.assertEqual(run("undo")[:2], (0, "There is nothing to undo.\n"))
        self.assertEqual(self.actions.did("undo"), [])

    def test_auto(self):
        with mock.patch("downkeep.cli.downloads_folder", return_value=self.folder):
            self.assertIn("is off", run("auto")[1])
            self.assertEqual(self.actions.did("set_auto"), [])
            out = run("auto", "on")[1]
            self.assertIn("is on for", out)
            self.assertIn("Folders: Images", out)
            self.assertIn("is on", run("auto", "status")[1])
            self.assertIn("is off", run("auto", "off")[1])
        self.assertEqual(self.actions.did("set_auto"), [("set_auto", str(self.folder), True), ("set_auto", str(self.folder), False)])

    def test_auto_not_available(self):
        self.actions.scheduled = (False, "No timer here")
        with mock.patch("downkeep.cli.downloads_folder", return_value=self.folder):
            self.assertEqual(run("auto", "on"), (2, "", "downkeep: No timer here\n"))
            self.assertEqual(run("auto", "off")[0], 0)
        self.assertEqual(self.actions.did("set_auto"), [("set_auto", str(self.folder), False)])

    def test_auto_run_says_nothing_and_is_not_listed(self):
        self.assertEqual(run("auto-run"), (0, "", ""))
        self.assertEqual(self.actions.calls, [("auto_run",)])
        self.assertNotIn("auto-run", run("--help")[1])

    def test_problems_are_plain(self):
        self.runs()
        for args in [("tidy", str(self.folder)), ("history",), ("undo", "3")]:
            for error, said in [(ActionError("It\x1b failed"), "failed"), (Busy(""), "Try again")]:
                with self.subTest(args=args, error=error):
                    self.actions.error = error
                    code, out, err = run(*args)
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn(said, err)
                    self.assertNotIn("\x1b", err)

    def test_not_available_on_this_system(self):
        self.actions.features["tidy"] = False
        for args in [("tidy", str(self.folder)), ("tidy", "--apply"), ("history",), ("undo",), ("auto", "on")]:
            with self.subTest(args=args):
                code, out, err = run(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("isn't available on this system yet", err)
        self.assertEqual(self.actions.calls, [])

    def test_text_report(self):
        self.add("a.zip", b"x" * 2000)
        code, out, err = run("report", str(self.folder))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 file, 2.0 KB", out)

    def test_json_report(self):
        self.add("a.zip")
        code, out, _ = run("report", str(self.folder), "--json", "--top", "1")
        self.assertEqual((code, json.loads(out)["files"]), (0, 1))

    def test_proof_is_included(self):
        self.add("a.txt", b"same")
        self.add("b.txt", b"same")
        with mock.patch("downkeep.cli.find_hosts", side_effect=lambda paths: {path: "x.example" for path in paths}):
            result = json.loads(run("report", str(self.folder), "--json")[1])
        self.assertEqual(result["duplicates"][0]["names"], ["a.txt", "b.txt"])
        self.assertEqual(result["sites"]["known"][0]["files"], 2)
        self.assertEqual(result["extracted"], [])

    def test_quick_reads_no_contents(self):
        self.add("a.txt", b"same")
        self.add("b.txt", b"same")
        for name in ["find_hosts", "find_duplicates", "find_extracted"]:
            patch = mock.patch(f"downkeep.cli.{name}", side_effect=AssertionError("not quick"))
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(run("report", str(self.folder), "--quick")[0], 0)

    def test_default_folder(self):
        self.add("a.zip")
        with mock.patch("downkeep.cli.downloads_folder", return_value=self.folder):
            self.assertIn("1 file", run("report")[1])

    def test_errors_go_to_stderr(self):
        code, out, err = run("report", str(self.folder / "missing"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("downkeep: Can't open", err)

    def test_bad_arguments(self):
        for args in [("report", "--top", "-1"), ("report", "--top", "x"), ("delete",), ("undo", "x"), ("undo", "-1"), ("auto", "maybe"), ("tidy", "--delete")]:
            with self.subTest(args=args):
                self.assertEqual(run(*args)[0], 2)

    def test_version(self):
        code, out, _ = run("--version")
        self.assertEqual((code, out.startswith("downkeep ")), (0, True))

    def test_module_with_a_limited_terminal(self):
        self.add("\u0928\u092e\u0938\u094d\u0924\u0947.txt")
        env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONPATH": os.pathsep.join(sys.path)}
        done = subprocess.run([sys.executable, "-m", "downkeep", "report", str(self.folder)], env=env, capture_output=True)
        self.assertEqual((done.returncode, done.stderr), (0, b""))


class DownloadsFolderTest(unittest.TestCase):
    def xdg(self, text):
        with tempfile.TemporaryDirectory() as base:
            if text is not None:
                Path(base, "user-dirs.dirs").write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": base}):
                return folders._xdg()

    def test_xdg(self):
        absolute = str(Path.home().resolve().parent / "shared")
        cases = {
            None: None,
            "": None,
            'XDG_DOWNLOAD_DIR="$HOME/Saved files"': Path.home() / "Saved files",
            f'# XDG_DOWNLOAD_DIR="$HOME/no"\nXDG_DOWNLOAD_DIR="{absolute}"': Path(absolute),
            'XDG_DOWNLOAD_DIR="$HOME"': None,
            'XDG_DOWNLOAD_DIR="$HOME/"': None,
            'XDG_DOWNLOAD_DIR="relative"': None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.xdg(text), expected)

    def test_this_system_gives_an_absolute_folder(self):
        self.assertTrue(folders.downloads_folder().is_absolute())


if __name__ == "__main__":
    unittest.main()
