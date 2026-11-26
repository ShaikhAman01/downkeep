import ntpath
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from unittest import mock

from downkeep import schedule
from downkeep.model import ActionError

COMMAND = ["/opt/down keep/app", "--auto-run"]
SPACE = {"t": schedule.TASK_SPACE}
ODD = ["plain", "sp ace", "do$llar", "d${HOME}x", "per%cent", "per%h", 'qu"ote', "apos'trophe", "back\\slash", "x\\x41", "é発",
       ";semi", "@at", "-dash", "a\\\\", 'a\\"', "#", "&<>"]


class Fake:
    """Stands in for the system: remembers commands, answers with a code and text."""

    def __init__(self, code=0, output=b"", fail=()):
        self.commands, self.code, self.output, self.fail = [], code, output, fail

    def __call__(self, command):
        self.commands.append(command)
        bad = any(word in command for word in self.fail)
        return subprocess.CompletedProcess(command, 1 if bad else self.code, self.output, b"")

    def words(self):
        return [" ".join(command[1:]) for command in self.commands]


def unreachable(command):
    raise ActionError("nothing there")


class Case(unittest.TestCase):
    platform = "linux"

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name).resolve()
        for patch in (
            mock.patch("downkeep.schedule.PLATFORM", self.platform),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "config"), "SystemRoot": r"C:\Windows"}),
            mock.patch("downkeep.schedule.shutil.which", return_value="/usr/bin/systemctl"),
            mock.patch("subprocess.run", side_effect=AssertionError("a real command was started")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        for key in ("APPIMAGE", "APPDIR", "DOWNKEEP_DATA", "XDG_STATE_HOME"):
            os.environ.pop(key, None)


class CommandTest(Case):
    def command(self, program, frozen=False, **environment):
        with (
            mock.patch("downkeep.schedule.sys.executable", program),
            mock.patch("downkeep.schedule.sys.frozen", frozen, create=True),
            mock.patch.dict(os.environ, environment),
        ):
            return schedule.command()

    def test_python(self):
        self.assertEqual(self.command("/usr/bin/python3"), ["/usr/bin/python3", "-m", "downkeep", "auto-run"])

    def test_packaged(self):
        self.assertEqual(self.command("/opt/downkeep", frozen=True), ["/opt/downkeep", "--auto-run"])

    def test_appimage(self):
        expected = ["/home/x/Downkeep.AppImage", "--auto-run"]
        inside = {"APPIMAGE": expected[0], "APPDIR": "/tmp/.mount_x"}
        self.assertEqual(self.command("/tmp/.mount_x/usr/bin/downkeep", frozen=True, **inside), expected)
        self.assertEqual(self.command("/tmp/.mount_x/usr/bin/python3", **inside), expected)
        self.assertEqual(self.command("/tmp/x/downkeep", frozen=True, APPIMAGE=expected[0]), expected)

    def test_values_inherited_from_another_appimage(self):
        inside = {"APPIMAGE": "/home/x/Terminal.AppImage", "APPDIR": "/tmp/.mount_t"}
        self.assertEqual(self.command("/opt/downkeep", frozen=True, **inside), ["/opt/downkeep", "--auto-run"])
        self.assertEqual(self.command("/tmp/.mount_t2/downkeep", frozen=True, **inside)[0], "/tmp/.mount_t2/downkeep")

    def test_no_program(self):
        with self.assertRaises(ActionError):
            self.command("")


class WindowsCommandTest(Case):
    platform = "win32"

    def test_no_console_window(self):
        with mock.patch("downkeep.schedule.sys.executable", r"C:\Py\python.exe"):
            with mock.patch("os.path.isfile", lambda path: path == r"C:\Py\pythonw.exe"):
                self.assertEqual(schedule.command(), [r"C:\Py\pythonw.exe", "-m", "downkeep", "auto-run"])
            with mock.patch("os.path.isfile", return_value=False):
                self.assertEqual(schedule.command()[0], r"C:\Py\python.exe")

    def test_packaged(self):
        with (
            mock.patch("downkeep.schedule.sys.executable", r"C:\D\Downkeep.exe"),
            mock.patch("downkeep.schedule.sys.frozen", True, create=True),
            mock.patch("os.path.isfile", return_value=True),
        ):
            self.assertEqual(schedule.command(), [r"C:\D\Downkeep.exe", "--auto-run"])


class RunCommandTest(unittest.TestCase):
    def test_a_missing_or_stuck_program_is_a_plain_problem(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("x", 30), PermissionError()):
            with mock.patch("subprocess.run", side_effect=error), self.assertRaises(ActionError):
                schedule.run_command(["x"])

    def test_never_waits_for_input_or_forever(self):
        with mock.patch("subprocess.run") as started:
            schedule.run_command(["x", "y"])
        self.assertEqual(started.call_args.args, (["x", "y"],))
        self.assertEqual(started.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(started.call_args.kwargs["timeout"], 30)


class MacTest(Case):
    platform = "darwin"

    def test_not_available(self):
        fake = Fake()
        self.assertEqual(schedule.available(fake), (False, schedule.NOT_HERE))
        with self.assertRaises(ActionError):
            schedule.install(COMMAND, run=fake)
        schedule.remove(run=fake)
        self.assertIsNone(schedule.installed_command(run=fake))
        self.assertEqual(fake.commands, [])


class LinuxTest(Case):
    def setUp(self):
        super().setUp()
        self.units = self.base / "config" / "systemd" / "user"
        self.service, self.timer = self.units / "downkeep.service", self.units / "downkeep.timer"
        self.link = self.units / "timers.target.wants" / "downkeep.timer"

    def enabled(self, command):
        """What a working systemctl does when a timer is enabled."""
        if "enable" in command:
            self.link.parent.mkdir(exist_ok=True)
            self.link.symlink_to(self.timer)
        if "disable" in command:
            self.link.unlink(missing_ok=True)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def test_available(self):
        fake = Fake()
        self.assertEqual(schedule.available(fake), (True, ""))
        self.assertEqual(fake.words(), ["--user show --property=Version"])
        for run in (Fake(code=1), unreachable):
            self.assertEqual(schedule.available(run), (False, schedule.NO_SESSION))
        with mock.patch("downkeep.schedule.shutil.which", return_value=None):
            self.assertEqual(schedule.available(fake), (False, schedule.NO_SESSION))

    @unittest.skipIf(sys.platform == "win32", "needs links")
    def test_install_read_and_remove(self):
        self.assertIsNone(schedule.installed_command(run=self.enabled))
        schedule.install(COMMAND, run=self.enabled)
        self.assertEqual(schedule.installed_command(run=unreachable), COMMAND)
        self.assertEqual(sorted(path.name for path in self.units.iterdir()),
                         ["downkeep.service", "downkeep.timer", "timers.target.wants"])
        self.assertEqual(stat.S_IMODE(self.service.stat().st_mode), 0o644)
        schedule.remove(run=self.enabled)
        self.assertEqual(sorted(path.name for path in self.units.iterdir()), ["timers.target.wants"])
        self.assertIsNone(schedule.installed_command())

    def test_install_commands_in_order(self):
        fake = Fake()
        schedule.install(COMMAND, run=fake)
        self.assertEqual(fake.words()[1:], ["--user daemon-reload", "--user enable --now downkeep.timer"])
        self.assertTrue(all(command[0] == "systemctl" for command in fake.commands))

    def test_timer(self):
        schedule.install(COMMAND, run=Fake())
        lines = self.timer.read_text(encoding="utf-8").splitlines()
        for line in ("OnCalendar=hourly", "Persistent=true", "RandomizedDelaySec=10min", "WantedBy=timers.target"):
            self.assertIn(line, lines)

    def test_service(self):
        schedule.install(COMMAND, run=Fake())
        lines = self.service.read_text(encoding="utf-8").splitlines()
        for line in ("Type=oneshot", 'ExecStart=":/opt/down keep/app" "--auto-run"', "WorkingDirectory=/",
                     "TimeoutStartSec=10min", "NoNewPrivileges=yes", "RestrictAddressFamilies=AF_UNIX"):
            self.assertIn(line, lines)
        text = "\n".join(lines)
        for breaks_on_some_systems in ("Protect", "Private", "ReadOnly", "MemoryDenyWriteExecute", "SystemCallFilter", "UMask"):
            self.assertNotIn(breaks_on_some_systems, text)

    def test_appimage_is_not_kept_from_mounting_itself(self):
        self.assertNotIn("NoNewPrivileges", schedule.make_unit(["/home/x/Downkeep-1.AppImage", "--auto-run"]))
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/x/renamed"}):
            self.assertNotIn("NoNewPrivileges", schedule.make_unit(["/home/x/renamed", "--auto-run"]))

    def test_odd_paths_survive(self):
        for name in ODD:
            command = [f"/opt/{name}/app", name, "a b", "$HOME", "%h", "\\n", ";"]
            with self.subTest(name=name):
                text = schedule.make_unit(command)
                self.assertEqual(schedule.read_unit(text), command)
                line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
                self.assertTrue(line.startswith('ExecStart=":/'))
                self.assertNotRegex(line.replace("%%", ""), "%")

    def test_exact_escaping(self):
        self.assertIn('ExecStart=":/a b/100%%/$x/é" "q\\"" "\\\\n" "%%h"\n',
                      schedule.make_unit(["/a b/100%/$x/é", 'q"', "\\n", "%h"]))

    def test_paths_systemd_refuses_go_through_the_shell(self):
        self.assertIn('ExecStart=":/bin/sh" "-c" "exec \\"$@\\"" "downkeep" "/home/Bob\'s/app" "--auto-run"\n',
                      schedule.make_unit(["/home/Bob's/app", "--auto-run"]))

    def test_names_that_cannot_be_written_are_refused(self):
        for path in ("/a\nb", "/a\udce9b", "/a\x1bb", "/a\u202eb"):
            fake = Fake()
            with self.assertRaises(ActionError):
                schedule.install([path, "--auto-run"], run=fake)
            self.assertFalse(self.service.exists())

    def test_records_folder_is_passed_on(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": '/x/100% "a"', "DOWNKEEP_DATA": "bad\nExecStart=/evil"}):
            schedule.install(COMMAND, run=Fake())
        text = self.service.read_text(encoding="utf-8")
        self.assertIn('Environment="XDG_STATE_HOME=/x/100%% \\"a\\""\n', text)
        self.assertNotIn("evil", text)
        self.assertEqual(schedule.read_unit(text), COMMAND)

    def test_a_failed_install_leaves_nothing(self):
        for word in ("daemon-reload", "enable"):
            with self.assertRaises(ActionError):
                schedule.install(COMMAND, run=Fake(fail=[word]))
            self.assertEqual(list(self.units.iterdir()), [])
        with self.assertRaises(ActionError):
            schedule.install(COMMAND, run=Fake(code=1))

    def test_install_without_permission_is_a_plain_problem(self):
        with mock.patch("downkeep.schedule._write", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(ActionError):
                schedule.install(COMMAND, run=Fake())

    def test_failed_write_keeps_the_old_file_and_no_leftovers(self):
        schedule.install(COMMAND, run=Fake())
        before = self.service.read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError), self.assertRaises(OSError):
            schedule._write(self.service, "new")
        self.assertEqual(self.service.read_text(encoding="utf-8"), before)
        self.assertEqual(len(list(self.units.iterdir())), 2)

    def test_remove_only_touches_its_own(self):
        schedule.install(COMMAND, run=Fake())
        other = self.units / "other.service"
        other.write_text("x")
        fake = Fake()
        schedule.remove(run=fake)
        self.assertEqual(fake.words(), ["--user disable --now downkeep.timer", "--user daemon-reload"])
        self.assertEqual([path.name for path in self.units.iterdir()], ["other.service"])
        schedule.remove(run=fake)
        self.assertEqual(len(fake.commands), 2)

    def test_remove_works_without_a_session(self):
        schedule.install(COMMAND, run=Fake())
        schedule.remove(run=unreachable)
        self.assertEqual(list(self.units.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "needs links")
    def test_remove_clears_the_enabling_link_by_itself(self):
        schedule.install(COMMAND, run=self.enabled)
        schedule.remove(run=unreachable)
        self.assertFalse(os.path.lexists(self.link))

    @unittest.skipIf(sys.platform == "win32", "needs links")
    def test_incomplete_schedules_are_not_installed(self):
        for part in ("service", "timer", "link"):
            schedule.install(COMMAND, run=self.enabled)
            getattr(self, part).unlink()
            self.assertIsNone(schedule.installed_command(), part)
            schedule.remove(run=self.enabled)
        schedule.install(COMMAND, run=self.enabled)
        self.service.write_text("[Service]\nExecStart=/usr/bin/true\n")
        self.assertIsNone(schedule.installed_command())
        self.service.write_bytes(b"\xff\xfe\xff")
        self.assertIsNone(schedule.installed_command())

    def test_other_names(self):
        schedule.install(COMMAND, name="DownkeepTest", run=Fake())
        self.assertTrue((self.units / "downkeeptest.timer").exists())
        for bad in ("../x", "", "a b", "a/b", "-x", "x.service"):
            with self.assertRaises(ValueError):
                schedule.remove(name=bad, run=Fake())

    def test_relative_config_folder_is_ignored(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}), mock.patch("pathlib.Path.home", return_value=self.base):
            self.assertEqual(schedule._units("Downkeep")[0], self.base / ".config/systemd/user/downkeep.service")


class WindowsTest(Case):
    platform = "win32"

    def setUp(self):
        super().setUp()
        patch = mock.patch("os.path.isfile", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, command, code=0):
        """Returns the task as it was handed over, and the commands."""
        seen = []

        def run(words):
            seen.append(words)
            if "/XML" in words and "/Create" in words:
                self.path = words[words.index("/XML") + 1]
                self.raw = Path(self.path).read_bytes()
            return subprocess.CompletedProcess(words, code, b"", b"")

        schedule.install(command, run=run)
        return ElementTree.fromstring(self.raw), seen

    def test_available(self):
        self.assertEqual(schedule.available(Fake()), (True, ""))
        with mock.patch("os.path.isfile", return_value=False):
            self.assertEqual(schedule.available(Fake()), (False, schedule.NO_TASKS))

    def test_full_path_to_the_system_program(self):
        _, seen = self.install(COMMAND)
        self.assertEqual(seen[0][:4], [r"C:\Windows\System32\schtasks.exe", "/Create", "/TN", "Downkeep"])
        self.assertEqual((seen[0][4], seen[0][6:]), ("/XML", ["/F"]))
        with mock.patch.dict(os.environ):
            del os.environ["SystemRoot"]
            self.assertTrue(schedule._schtasks().startswith("C:\\Windows\\"))

    def test_task(self):
        task, _ = self.install([r"C:\Down keep\Downkeep.exe", "--auto-run"])
        self.assertTrue(self.raw.startswith(b"\xff\xfe"))
        for path, expected in {
            "Triggers/t:TimeTrigger/t:Repetition/t:Interval": "PT1H",
            "Principals/t:Principal/t:LogonType": "InteractiveToken",
            "Principals/t:Principal/t:RunLevel": "LeastPrivilege",
            "Settings/t:MultipleInstancesPolicy": "IgnoreNew",
            "Settings/t:DisallowStartIfOnBatteries": "false",
            "Settings/t:StopIfGoingOnBatteries": "false",
            "Settings/t:StartWhenAvailable": "true",
            "Settings/t:ExecutionTimeLimit": "PT10M",
            "Settings/t:WakeToRun": "false",
            "Settings/t:Enabled": "true",
            "Actions/t:Exec/t:Command": r"C:\Down keep\Downkeep.exe",
            "Actions/t:Exec/t:Arguments": "--auto-run",
        }.items():
            self.assertEqual(task.findtext(f"t:{path}", namespaces=SPACE), expected, path)
        self.assertIsNone(task.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Duration", SPACE))

    def test_temporary_file_is_removed(self):
        self.install(COMMAND)
        self.assertFalse(os.path.exists(self.path))
        with self.assertRaises(ActionError):
            self.install(COMMAND, code=1)
        self.assertFalse(os.path.exists(self.path))

    def test_odd_paths_survive(self):
        for name in ODD:
            command = [ntpath.join("C:\\", name, "a.exe"), name, "", "a b", 'x"y', "\\"]
            with self.subTest(name=name):
                self.install(command)
                self.assertEqual(schedule.read_task(self.raw.decode("utf-16")), command)

    def test_split_is_the_reverse_of_list2cmdline(self):
        pieces = ["", "a", " ", "\t", '"', "\\", '\\"', '\\\\"', "a\\", "a b\\", "\\ ", '""']
        for one in pieces:
            for two in pieces:
                for three in pieces:
                    words = [one + two, three, two + one + three]
                    self.assertEqual(schedule._split(subprocess.list2cmdline(words)), words)

    def test_installed_command_in_any_encoding(self):
        command = [r"C:\Décor\a.exe", "--auto-run"]
        text = schedule.make_task(command)
        for raw in (text.encode("utf-16"), text.encode("utf-16-le"), text.encode("utf-8"), text.encode("latin-1")):
            self.assertEqual(schedule.installed_command(run=Fake(output=raw)), command)

    def test_nothing_installed_or_not_ours(self):
        self.assertIsNone(schedule.installed_command(run=Fake(code=1)))
        self.assertIsNone(schedule.installed_command(run=unreachable))
        for raw in (b"", b"junk", b"<Task/>", b"\xff\xfe"):
            self.assertIsNone(schedule.installed_command(run=Fake(output=raw)))

    def test_remove(self):
        fake = Fake(output=schedule.make_task(COMMAND).encode())
        schedule.remove(run=fake)
        self.assertEqual(fake.words(), ["/Query /TN Downkeep /XML", "/Delete /TN Downkeep /F"])
        fake = Fake(code=1)
        schedule.remove(run=fake)
        self.assertEqual(fake.words(), ["/Query /TN Downkeep /XML"])
        with self.assertRaises(ActionError):
            schedule.remove(run=Fake(output=b"<Task/>", fail=["/Delete"]))

    def test_names_are_checked(self):
        for bad in ("..\\x", "a/b", "a b", "/F", "Microsoft\\Windows\\x"):
            with self.assertRaises(ValueError):
                schedule.install(COMMAND, name=bad, run=Fake())


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("DOWNKEEP_TEST_SCHEDULE") == "1", "really uses Task Scheduler")
class RealWindowsTest(unittest.TestCase):
    def test_create_query_and_remove(self):
        name = f"DownkeepTest-{uuid.uuid4().hex}"
        command = [sys.executable, "-c", "pass", 'a b "c" \\']
        self.addCleanup(schedule.remove, name)
        self.assertEqual(schedule.available(), (True, ""))
        self.assertIsNone(schedule.installed_command(name))
        schedule.install(command, name)
        self.assertEqual(schedule.installed_command(name), command)
        found = ElementTree.fromstring(schedule._query(name, schedule.run_command).split("?>", 1)[-1])
        for setting, expected in {"StartWhenAvailable": "true", "DisallowStartIfOnBatteries": "false",
                                  "MultipleInstancesPolicy": "IgnoreNew", "ExecutionTimeLimit": "PT10M"}.items():
            value = found.findtext(f"t:Settings/t:{setting}", namespaces=SPACE)
            self.assertIn(value, (expected, None if setting == "MultipleInstancesPolicy" else expected))
        self.assertEqual(found.findtext("t:Principals/t:Principal/t:LogonType", namespaces=SPACE), "InteractiveToken")
        self.assertTrue(found.findtext("t:Principals/t:Principal/t:UserId", namespaces=SPACE))
        self.assertEqual(found.findtext(".//t:Repetition/t:Interval", namespaces=SPACE), "PT1H")
        schedule.install([sys.executable, "--changed"], name)
        self.assertEqual(schedule.installed_command(name), [sys.executable, "--changed"])
        schedule.remove(name)
        self.assertIsNone(schedule.installed_command(name))
        schedule.remove(name)
