import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from downkeep import gui, window
from downkeep.model import ActionError, Busy, Result
from tests.test_actions import FakeActions

PACKAGING = Path(__file__).parent.parent / "packaging"


class GuiTest(unittest.TestCase):
    def setUp(self):
        self.actions = FakeActions()
        for name in ["downkeep.server.Actions", "downkeep.cli.Actions"]:
            patch = mock.patch(name, return_value=self.actions)
            patch.start()
            self.addCleanup(patch.stop)

    def auto_run(self, *args):
        with (
            mock.patch("sys.stdout", None),
            mock.patch("sys.stderr", None),
            mock.patch("downkeep.window._message", side_effect=AssertionError("showed a message")),
            mock.patch("downkeep.cli.open_window", side_effect=AssertionError("opened a window")),
            mock.patch("downkeep.cli.make_server", side_effect=AssertionError("started a server")),
            mock.patch("downkeep.cli.log") as log,
        ):
            return gui.main(*args), [call.args[0] for call in log.call_args_list]

    def test_auto_run_is_silent(self):
        self.assertEqual(self.auto_run(["--auto-run"]), (0, []))
        self.assertEqual(self.actions.calls, [("auto_run",)])

    def test_auto_run_from_the_command_line_of_the_app(self):
        with mock.patch("sys.argv", ["Downkeep", "--auto-run"]):
            self.assertEqual(self.auto_run(), (0, []))
        self.assertEqual(self.actions.calls, [("auto_run",)])

    def test_auto_run_problems_go_to_the_log(self):
        for error, code, said in [
            (Busy("x"), 0, []),
            (ActionError("It is full\n"), 1, ["Automatic tidying: It is full\N{REPLACEMENT CHARACTER}"]),
            (OSError("/private"), 1, ["Automatic tidying failed (OSError)."]),
        ]:
            with self.subTest(error=error):
                self.actions.error = error
                self.assertEqual(self.auto_run(["--auto-run"]), (code, said))

    def test_auto_run_where_nothing_can_move(self):
        self.actions.features["tidy"] = False
        self.assertEqual(self.auto_run(["--auto-run"]), (0, []))
        self.assertEqual(self.actions.calls, [])

    def test_other_arguments_open_the_window(self):
        with mock.patch("downkeep.gui.open_app", return_value=0) as app, mock.patch("sys.argv", ["x", "--auto-run"]):
            gui.main([])
        app.assert_called_once()

    def test_opening_recovers_first(self):
        self.actions.left = Result(None, ["a"])
        with (
            mock.patch("downkeep.cli.open_window", side_effect=lambda url: self.actions.calls.append("window") or True),
            mock.patch("downkeep.cli.App.finished", return_value=True),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(gui.main([]), 0)
        self.assertEqual([call if call == "window" else call[0] for call in self.actions.calls], ["recover", "refresh", "window"])

    def test_opens_the_usual_folder(self):
        with mock.patch("downkeep.gui.open_app", return_value=0) as app:
            self.assertEqual(gui.main(), 0)
        app.assert_called_once_with(None, tell=window.tell)

    def test_unexpected_failure_is_shown_without_details(self):
        with (
            mock.patch("downkeep.gui.open_app", side_effect=OSError("/private/path")),
            mock.patch("downkeep.gui.tell") as tell,
        ):
            self.assertEqual(gui.main(), 1)
        self.assertIn("OSError", tell.call_args.args[0])
        self.assertNotIn("private", tell.call_args.args[0])

    def test_works_without_any_terminal(self):
        with (
            mock.patch("sys.stdout", None),
            mock.patch("sys.stderr", None),
            mock.patch("downkeep.cli.open_window", return_value=True),
            mock.patch("downkeep.cli.App.finished", return_value=True),
        ):
            self.assertEqual(gui.main(), 0)

    def test_no_browser_is_shown_and_the_link_is_not(self):
        said = []
        with (
            mock.patch("downkeep.cli.open_window", return_value=False) as opened,
            mock.patch("downkeep.gui.tell", said.append),
        ):
            self.assertEqual(gui.main(), 1)
        opened.assert_called_once()
        self.assertEqual(len(said), 1)
        self.assertNotIn("http", said[0])


class TellTest(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.log = Path(holder.name) / "very" / "deep" / "log.txt"

    def test_logs_and_shows(self):
        with mock.patch("downkeep.window._log_file", return_value=self.log), mock.patch("downkeep.window._message") as shown:
            window.tell("one")
            window.tell("two")
        self.assertEqual([call.args[0] for call in shown.call_args_list], ["one", "two"])
        self.assertEqual([line[17:] for line in self.log.read_text(encoding="utf-8").splitlines()], ["one", "two"])

    def test_a_failing_log_still_shows(self):
        with mock.patch("downkeep.window._log_file", side_effect=OSError), mock.patch("downkeep.window._message") as shown:
            window.tell("one")
        shown.assert_called_once_with("one")

    def test_a_failing_message_still_logs(self):
        with mock.patch("downkeep.window._log_file", return_value=self.log), mock.patch("downkeep.window._message", side_effect=OSError):
            window.tell("one")
        self.assertIn("one", self.log.read_text(encoding="utf-8"))

    def test_text_is_never_part_of_a_script(self):
        text = '" & do shell script "x'
        with mock.patch("downkeep.window.sys.platform", "darwin"), mock.patch("downkeep.window._start") as start:
            window._message(text)
        command = start.call_args.args[0]
        self.assertEqual(command[-1], text)
        self.assertNotIn(text, command[2])

    def test_linux_uses_what_is_there(self):
        for program in ["zenity", "kdialog", "notify-send"]:
            with (
                self.subTest(program=program),
                mock.patch("downkeep.window.sys.platform", "linux"),
                mock.patch("downkeep.window.shutil.which", lambda name: name if name == program else None),
                mock.patch("downkeep.window._start") as start,
            ):
                window._message("hello")
            self.assertEqual(start.call_args.args[0][0], program)
            self.assertTrue(any("hello" in part for part in start.call_args.args[0]))
        with (
            mock.patch("downkeep.window.sys.platform", "linux"),
            mock.patch("downkeep.window.shutil.which", return_value=None),
            mock.patch("downkeep.window._start") as start,
        ):
            window._message("hello")
        start.assert_not_called()


class IconTest(unittest.TestCase):
    def test_windows_icon(self):
        data = (PACKAGING / "downkeep.ico").read_bytes()
        zero, kind, count = struct.unpack_from("<HHH", data)
        self.assertEqual((zero, kind), (0, 1))
        sizes = set()
        for index in range(count):
            width, _, _, _, _, _, length, start = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
            self.assertEqual(data[start : start + 8], b"\x89PNG\r\n\x1a\n")
            self.assertLessEqual(start + length, len(data))
            sizes.add(width or 256)
        self.assertLessEqual({16, 32, 48, 256}, sizes)

    def test_mac_icon(self):
        data = (PACKAGING / "downkeep.icns").read_bytes()
        self.assertEqual((data[:4], struct.unpack(">I", data[4:8])[0]), (b"icns", len(data)))
        kinds, place = set(), 8
        while place < len(data):
            kind, length = struct.unpack_from(">4sI", data, place)
            self.assertEqual(data[place + 8 : place + 12], b"\x89PNG")
            kinds.add(kind)
            place += length
        self.assertLessEqual({b"ic07", b"ic08", b"ic09", b"ic10"}, kinds)

    def test_linux_icon(self):
        data = (PACKAGING / "downkeep.png").read_bytes()
        self.assertEqual(struct.unpack(">II", data[16:24]), (512, 512))


if __name__ == "__main__":
    unittest.main()
