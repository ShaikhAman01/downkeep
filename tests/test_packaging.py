import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import downkeep
from downkeep import cli, window

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
WORKFLOW = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")


def load(name):
    spec = importlib.util.spec_from_file_location(f"packaging_{name}", PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle = load("bundle")
glibc = load("glibc")
smoke = load("smoke")


class VersionTest(unittest.TestCase):
    def test_one_version_everywhere(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["version"], downkeep.__version__)
        self.assertEqual(bundle.version(), downkeep.__version__)
        self.assertIn(f"# Downkeep {downkeep.__version__}\n", (PACKAGING / "RELEASE.md").read_text(encoding="utf-8"))

    def test_a_tag_must_name_the_version(self):
        bundle.check_tag("tag", f"v{downkeep.__version__}")
        bundle.check_tag("branch", "main")
        bundle.check_tag(None, None)
        for name in (downkeep.__version__, "v9.9.9", None):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                bundle.check_tag("tag", name)


class SpecTest(unittest.TestCase):
    def run_spec(self, platform):
        calls = {}

        def record(name):
            def call(*args, **options):
                calls[name] = (args, options)
                return mock.Mock()

            return call

        info = types.ModuleType("PyInstaller.utils.win32.versioninfo")
        kinds = "VSVersionInfo FixedFileInfo StringFileInfo StringTable StringStruct VarFileInfo VarStruct"
        for name in kinds.split():
            setattr(info, name, record(name))
        win32 = types.ModuleType("PyInstaller.utils.win32")
        win32.versioninfo = info
        modules = {name: types.ModuleType(name) for name in ("PyInstaller", "PyInstaller.utils")}
        modules |= {win32.__name__: win32, info.__name__: info}
        names = {name: record(name) for name in ("Analysis", "PYZ", "EXE", "COLLECT")}
        text = (PACKAGING / "downkeep.spec").read_text(encoding="utf-8")
        with mock.patch.dict(sys.modules, modules), mock.patch.object(sys, "platform", platform):
            exec(compile(text, "downkeep.spec", "exec"), {"SPECPATH": str(PACKAGING), **names})
        return calls

    def test_everything_it_names_exists(self):
        args, options = self.run_spec("linux")["Analysis"]
        self.assertTrue(Path(args[0][0]).is_file())
        self.assertTrue(Path(options["pathex"][0], "downkeep", "__init__.py").is_file())
        (source, target), = options["datas"]
        self.assertEqual(target, "downkeep/page")
        for name in ("index.html", "app.js", "style.css", "icon.svg"):
            self.assertTrue(Path(source, name).is_file())

    def test_every_module_is_packed_even_when_imported_late(self):
        _, options = self.run_spec("linux")["Analysis"]
        files = {path.stem for path in (ROOT / "src" / "downkeep").glob("*.py")} - {"__init__", "__main__"}
        self.assertEqual(set(options["hiddenimports"]), {f"downkeep.{name}" for name in files})

    def test_needed_parts_are_not_left_out(self):
        _, options = self.run_spec("linux")["Analysis"]
        needed = {"sqlite3", "_sqlite3", "ctypes", "_ctypes", "hashlib", "zlib", "zipfile", "json", "http.server", "uuid"}
        self.assertFalse(needed & set(options["excludes"]))
        self.assertLessEqual({"tkinter", "test", "lib2to3", "pydoc_data", "ssl", "_ssl"}, set(options["excludes"]))

    def test_the_source_imports_nothing_that_is_left_out(self):
        _, options = self.run_spec("linux")["Analysis"]
        left_out = set(options["excludes"])
        for path in (ROOT / "src" / "downkeep").glob("*.py"):
            names = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", path.read_text(encoding="utf-8"), re.MULTILINE)
            for name in names:
                self.assertFalse({name, name.split(".")[0]} & left_out, f"{path.name} imports {name}")

    def test_windows_gets_a_window_an_icon_and_a_version(self):
        calls = self.run_spec("win32")
        _, options = calls["EXE"]
        self.assertEqual((options["name"], options["console"], options["uac_admin"]), ("Downkeep", False, False))
        self.assertTrue(Path(options["icon"]).is_file())
        self.assertIsNotNone(options["version"])
        _, numbers = calls["FixedFileInfo"]
        wanted = (*(int(part) for part in downkeep.__version__.split(".")), 0)
        self.assertEqual((numbers["filevers"], numbers["prodvers"]), (wanted, wanted))

    def test_a_folder_not_a_single_file_and_never_compressed(self):
        for platform in ("linux", "win32"):
            calls = self.run_spec(platform)
            self.assertTrue(calls["EXE"][1]["exclude_binaries"])
            for part in ("EXE", "COLLECT"):
                self.assertIs(calls[part][1]["upx"], False)
                self.assertIs(calls[part][1]["strip"], False)
            self.assertEqual(calls["COLLECT"][1]["name"], calls["EXE"][1]["name"])

    def test_macos_is_not_packaged(self):
        with self.assertRaises(SystemExit):
            self.run_spec("darwin")


class StartTest(unittest.TestCase):
    def setUp(self):
        self.start = load("start")

    def test_started_programs_get_the_usual_libraries(self):
        for before, after in (
            ({"LD_LIBRARY_PATH": "/app/_internal"}, None),
            ({"LD_LIBRARY_PATH": "/app/_internal", "LD_LIBRARY_PATH_ORIG": "/mine"}, "/mine"),
            ({}, None),
        ):
            with (
                self.subTest(before=before),
                mock.patch.dict(os.environ, before, clear=True),
                mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(sys, "platform", "linux"),
            ):
                self.start.clean_environment()
                self.assertEqual(os.environ.get("LD_LIBRARY_PATH"), after)
                self.assertNotIn("LD_LIBRARY_PATH_ORIG", os.environ)

    def test_nothing_changes_when_not_packaged(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/mine"}, clear=True):
            self.start.clean_environment()
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/mine")


class RequirementsTest(unittest.TestCase):
    def test_every_tool_is_pinned_with_hashes(self):
        text = (PACKAGING / "requirements.txt").read_text(encoding="utf-8")
        lines = [line for line in text.replace("\\\n", " ").splitlines() if line.strip() and not line.startswith("#")]
        pattern = re.compile(
            r'([a-z0-9-]+)==(\d+(?:\.\d+)+)(?:\s*;\s*sys_platform == "win32")?((?:\s+--hash=sha256:[0-9a-f]{64})+)\s*'
        )
        found = {}
        for line in lines:
            match = pattern.fullmatch(line)
            self.assertIsNotNone(match, line)
            self.assertNotIn(match[1], found)
            found[match[1]] = re.findall(r"sha256:(\w+)", match[3])
        wanted = "pyinstaller pyinstaller-hooks-contrib altgraph packaging setuptools pefile pywin32-ctypes"
        self.assertEqual(set(found), set(wanted.split()))
        # One wheel for each system; the rest are the same everywhere
        self.assertEqual({name: len(hashes) for name, hashes in found.items() if len(hashes) != 1}, {"pyinstaller": 2})
        every = [value for hashes in found.values() for value in hashes]
        self.assertEqual(len(every), len(set(every)))


class WorkflowTest(unittest.TestCase):
    def test_only_by_hand_or_tag(self):
        block = re.search(r"^on:\n((?:  .*\n)+)", WORKFLOW, re.MULTILINE)[1]
        self.assertEqual(block, '  workflow_dispatch:\n  push:\n    tags: ["v*"]\n')

    def test_may_only_read(self):
        self.assertEqual(re.findall(r"permissions:.*\n(?:\s+\S.*\n)?", WORKFLOW), ["permissions:\n  contents: read\n"])
        self.assertNotIn("write", WORKFLOW)
        self.assertNotIn("secrets.", WORKFLOW)
        self.assertIn("persist-credentials: false", WORKFLOW)

    def test_actions_are_pinned_to_commits(self):
        uses = re.findall(r"uses: *(.*)", WORKFLOW)
        names = []
        for use in uses:
            match = re.fullmatch(r"(actions/[a-z-]+)@[0-9a-f]{40} # v\d+\.\d+\.\d+", use)
            self.assertIsNotNone(match, use)
            names.append(match[1])
        self.assertEqual(names, ["actions/checkout", "actions/setup-python", "actions/upload-artifact"])

    def test_publishes_and_fetches_nothing(self):
        for word in "release|gh |git push|git tag|twine|pypi|curl|wget|invoke-|docker|upx|apt|choco".split("|"):
            self.assertNotIn(word, WORKFLOW.lower())

    def test_nothing_from_outside_reaches_the_shell(self):
        inside = re.findall(r"\$\{\{\s*(.*?)\s*\}\}", WORKFLOW)
        self.assertEqual({name.split(".")[0] for name in inside}, {"matrix", "github"})
        runs = re.findall(r"^\s+(?:- )?run: (\|\n(?:\s{10,}.*\n)+|.*)", WORKFLOW, re.MULTILINE)
        self.assertGreaterEqual(len(runs), 6)
        for run in runs:
            self.assertNotIn("github.", run)

    def test_builds_the_same_way_every_time(self):
        self.assertIn('PYTHONHASHSEED: "1"', WORKFLOW)
        self.assertIn("SOURCE_DATE_EPOCH", WORKFLOW)
        self.assertIn("--require-hashes --only-binary :all: -r packaging/requirements.txt", WORKFLOW)
        self.assertRegex(WORKFLOW, r'python-version: "3\.\d+\.\d+"')
        self.assertIn("os: ubuntu-22.04", WORKFLOW)
        self.assertIn("os: windows-latest", WORKFLOW)

    def test_tests_and_tries_before_uploading(self):
        order = ["unittest discover", "requirements.txt", "pyinstaller --noconfirm"]
        order += ["glibc.py", "bundle.py", "smoke.py", "upload-artifact"]
        places = [WORKFLOW.index(part) for part in order]
        self.assertEqual(places, sorted(places))
        self.assertIn("DOWNKEEP_TEST_SCHEDULE: ${{ matrix.name == 'windows' && '1' || '' }}", WORKFLOW)
        self.assertNotIn("continue-on-error", WORKFLOW)


class BundleTest(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        patch = mock.patch.object(bundle, "python_license", return_value=b"made-up license\n")
        patch.start()
        self.addCleanup(patch.stop)
        environment = {"SOURCE_DATE_EPOCH": "1764000000"}
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("GITHUB_REF_TYPE", "GITHUB_REF_NAME"):
            os.environ.pop(name, None)

    def build(self, program):
        dist = self.root / "dist"
        (dist / "_internal").mkdir(parents=True)
        (dist / program).write_bytes(b"made-up program")
        (dist / program).chmod(0o755)
        (dist / "_internal" / "part.bin").write_bytes(b"made-up part")
        return dist

    def pack(self, dist, out="out"):
        with mock.patch("sys.stdout", io.StringIO()):
            bundle.main([str(dist), str(self.root / out)])
        return self.root / out

    def check_sums(self, out, system):
        digest, name = (out / f"SHA256SUMS-{system}.txt").read_text(encoding="utf-8").split()
        self.assertEqual(digest, hashlib.sha256((out / name).read_bytes()).hexdigest())
        self.assertEqual({path.name for path in out.iterdir()}, {name, f"SHA256SUMS-{system}.txt"})
        return out / name

    def test_linux(self):
        place = self.check_sums(self.pack(self.build("downkeep")), "linux")
        self.assertEqual(place.name, f"downkeep-{downkeep.__version__}-linux-x64.tar.gz")
        with tarfile.open(place) as archive:
            entries = {entry.name: entry for entry in archive}
        self.assertTrue(all(name.startswith("downkeep/") and entry.isfile() for name, entry in entries.items()))
        wanted = {
            "downkeep", "_internal/part.bin", "install.sh", "uninstall.sh", "downkeep.desktop", "downkeep.png",
            "Read me first.txt", "LICENSE.txt", "licenses/Python.txt",
        }
        self.assertEqual({name.removeprefix("downkeep/") for name in entries}, wanted)
        self.assertEqual({entry.mtime for entry in entries.values()}, {1764000000})
        self.assertEqual({(entry.uid, entry.gid, entry.uname) for entry in entries.values()}, {(0, 0, "")})
        self.assertEqual(entries["downkeep/install.sh"].mode, 0o755)
        self.assertEqual(entries["downkeep/downkeep.png"].mode, 0o644)

    @unittest.skipIf(os.name == "nt", "Windows has no executable bit")
    def test_linux_program_can_be_run(self):
        with tarfile.open(self.check_sums(self.pack(self.build("downkeep")), "linux")) as archive:
            self.assertEqual(archive.getmember("downkeep/downkeep").mode, 0o755)

    def test_windows(self):
        place = self.check_sums(self.pack(self.build("Downkeep.exe")), "windows")
        self.assertEqual(place.name, f"Downkeep-{downkeep.__version__}-windows-x64.zip")
        with zipfile.ZipFile(place) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            text = archive.read("Downkeep/Read me first.txt")
        wanted = {"Downkeep.exe", "_internal/part.bin", "Read me first.txt", "LICENSE.txt", "licenses/Python.txt"}
        self.assertEqual(set(names), {f"Downkeep/{name}" for name in wanted})
        self.assertEqual(text.count(b"\n"), text.count(b"\r\n"))
        self.assertIn(b"Run anyway", text)

    def test_same_input_gives_the_same_bytes(self):
        for program in ("downkeep", "Downkeep.exe"):
            with self.subTest(program=program):
                dist = self.build(program)
                first = [path.read_bytes() for path in sorted(self.pack(dist, "one").iterdir())]
                for path in dist.rglob("*"):
                    os.utime(path, (1, 1))
                second = [path.read_bytes() for path in sorted(self.pack(dist, "two").iterdir())]
                self.assertEqual(first, second)
                for name in ("dist", "one", "two"):
                    shutil.rmtree(self.root / name)

    def test_never_replaces_an_earlier_result(self):
        dist = self.build("downkeep")
        out = self.root / "out"
        out.mkdir()
        earlier = out / f"downkeep-{downkeep.__version__}-linux-x64.tar.gz"
        earlier.write_bytes(b"earlier")
        with self.assertRaises(FileExistsError):
            self.pack(dist)
        self.assertEqual(earlier.read_bytes(), b"earlier")
        self.assertEqual([path.name for path in out.iterdir()], [earlier.name])

    def test_the_date_inside_is_the_source_date(self):
        place = self.check_sums(self.pack(self.build("downkeep")), "linux")
        self.assertEqual(int.from_bytes(place.read_bytes()[4:8], "little"), 1764000000)

    def test_nothing_built(self):
        (self.root / "empty").mkdir()
        with self.assertRaises(SystemExit):
            self.pack(self.root / "empty")

    @unittest.skipIf(os.name == "nt", "links need rights on Windows")
    def test_links_that_lead_out_are_refused(self):
        dist = self.build("downkeep")
        (self.root / "private").write_bytes(b"private")
        (dist / "_internal" / "link").symlink_to(self.root / "private")
        with self.assertRaises(SystemExit):
            self.pack(dist)

    @unittest.skipIf(sys.platform == "darwin", "not packaged on macOS")
    def test_python_license_is_included(self):
        self.assertIn(b"PYTHON SOFTWARE FOUNDATION", load("bundle").python_license().upper())


class GlibcTest(unittest.TestCase):
    def check(self, text, *args):
        with tempfile.TemporaryDirectory() as name:
            Path(name, "program").write_bytes(b"\x7fELF made-up")
            Path(name, "notes.txt").write_bytes(b"GLIBC_9.9")
            result = mock.Mock(stdout=text)
            with (
                mock.patch.object(glibc.subprocess, "run", return_value=result),
                mock.patch("sys.stdout", io.StringIO()) as out,
                mock.patch("sys.stderr", io.StringIO()),
            ):
                return glibc.main([name, *args]), out.getvalue()

    def test_compares_numbers_not_text(self):
        code, text = self.check("(GLIBC_2.4) a\n(GLIBC_2.34) b\n(GLIBC_2.9) c\nGLIBCXX_3.4.99", "--at-most", "2.35")
        self.assertEqual(code, 0)
        self.assertIn("Needs glibc 2.34 or newer", text)

    def test_too_new_fails(self):
        self.assertEqual(self.check("(GLIBC_2.36) a", "--at-most", "2.35")[0], 1)

    def test_nothing_to_check_fails(self):
        with tempfile.TemporaryDirectory() as name, self.assertRaises(SystemExit):
            glibc.main([name])


class SmokeTest(unittest.TestCase):
    def test_knows_the_link_the_app_writes(self):
        app = mock.Mock(code="a-_9" * 10 + "xyz")
        self.assertEqual(len(app.code), 43)
        self.assertEqual(smoke.LINK.fullmatch(f"http://127.0.0.1:54321/#{app.code}").groups(), ("54321", app.code))
        self.assertEqual(cli.LINK_FILE, "DOWNKEEP_LINK_FILE")

    def test_every_browser_and_message_program_is_replaced(self):
        self.assertLessEqual(set(window.LINUX_BROWSERS), set(smoke.NEVER_STARTED))
        self.assertLessEqual({"xdg-open", "zenity", "kdialog", "notify-send", "dbus-send"}, set(smoke.NEVER_STARTED))

    def test_the_made_up_home_is_separate(self):
        with tempfile.TemporaryDirectory() as name:
            home = smoke.Home(Path(name) / "run")
            self.assertEqual(home.listing(), home.expected())
            values = home.environment()
            for key in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR", "DOWNKEEP_DATA"):
                self.assertTrue(Path(values[key]).is_relative_to(name), key)
            for key in [key for key in values if key.startswith("XDG_")]:
                self.assertTrue(Path(values[key]).is_relative_to(name), key)
            for key in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
                self.assertNotIn(key, values)
            if os.name != "nt":
                self.assertEqual(values["PATH"], str(home.stubs))


class WordsTest(unittest.TestCase):
    def texts(self):
        for path in [ROOT / "README.md", *PACKAGING.rglob("*")]:
            if path.suffix in {".md", ".txt", ".py", ".sh", ".spec", ".desktop"}:
                yield path, path.read_text(encoding="utf-8")

    def test_no_em_dashes(self):
        for path, text in self.texts():
            self.assertNotIn("\N{EM DASH}", text, path.name)

    def test_people_are_told_what_to_expect(self):
        for system in ("windows", "linux"):
            text = (PACKAGING / system / "Read me first.txt").read_text(encoding="utf-8")
            for part in ("TO OPEN IT", "WHAT IT DOES", "WHAT IT NEVER DOES", "TO REMOVE IT", "Automatic off"):
                self.assertIn(part, text)
            self.assertLessEqual(max(map(len, text.splitlines())), 80)
        windows = (PACKAGING / "windows" / "Read me first.txt").read_text(encoding="utf-8")
        for part in ("Extract All", "Windows protected your PC", "More info", "Smart App Control"):
            self.assertIn(part, windows)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "Linux only")
class InstallTest(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()
        self.home = self.root / "home of a friend"
        self.home.mkdir()
        self.unpacked = self.unpack("un packed", b"part one")
        self.app = self.home / ".local" / "lib" / "downkeep"
        self.entry = self.home / ".local" / "share" / "applications" / "downkeep.desktop"
        self.icon = self.home / ".local" / "share" / "icons" / "hicolor" / "512x512" / "apps" / "downkeep.png"
        self.timer = self.home / ".config" / "systemd" / "user" / "downkeep.timer"
        # The real systemctl is never reached: this one only writes down what it was asked
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.asked = self.root / "systemctl asked"
        self.systemctl(0)

    def systemctl(self, code):
        stub = self.stubs / "systemctl"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{self.asked}"\nexit {code}\n', encoding="utf-8")
        stub.chmod(0o755)

    def unpack(self, name, part):
        place = self.root / name
        files = {"downkeep": (Path(shutil.which("bash")).read_bytes(), True), "_internal/sub dir/part.bin": (part, False)}
        with mock.patch.object(bundle, "python_license", return_value=b"made-up license\n"):
            files |= bundle.extra_files("linux")
        with tarfile.open(fileobj=io.BytesIO(bundle.make_tar(files, "downkeep", 1764000000))) as archive:
            archive.extractall(place, filter="data")
        return place / "downkeep"

    def run_script(self, script, *args, **environment):
        values = {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin"} | environment
        return subprocess.run(
            [str(script), *args],
            env={key: value for key, value in values.items() if value is not None},
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def install(self, unpacked=None, **environment):
        return self.run_script((unpacked or self.unpacked) / "install.sh", **environment)

    def uninstall(self, **environment):
        return self.run_script(self.app / "uninstall.sh", **environment)

    def tree(self, folder=None):
        folder = folder or self.home
        found = {}
        for path in sorted(folder.rglob("*")):
            name = path.relative_to(folder).as_posix()
            if path.is_symlink():
                found[name] = f"-> {os.readlink(path)}"
            elif path.is_file():
                found[name] = (oct(path.stat().st_mode), hashlib.sha256(path.read_bytes()).hexdigest()[:16])
        return found

    def refused(self, done, word):
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn(word, done.stderr)

    def program_in(self, entry):
        """Reads the Exec line as strictly as the desktop entry rules allow."""
        line = next(line for line in entry.read_text(encoding="utf-8").splitlines() if line.startswith("Exec="))
        value = re.sub(r"\\(.)", lambda found: {"s": " ", "n": "\n", "t": "\t", "r": "\r"}.get(found[1], found[1]), line[5:])
        self.assertEqual(line[5:].count("\\") % 2, 0)
        self.assertRegex(value, r'^"(?:[^"`$\\%]|\\["`$\\]|%%)*"$')
        return re.sub(r"\\(.)", r"\1", value[1:-1]).replace("%%", "%")

    def test_installs_for_this_person_only(self):
        done = self.install()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        for name, value in self.tree(self.unpacked).items():
            if name != "downkeep.desktop":
                self.assertEqual(self.tree(self.app)[name], value)
        self.assertEqual(self.icon.read_bytes(), (self.unpacked / "downkeep.png").read_bytes())
        self.assertEqual(self.program_in(self.entry), str(self.app / "downkeep"))
        self.assertEqual({path.name for path in self.home.iterdir()}, {".local"})
        self.assertEqual([path.name for path in self.app.parent.iterdir()], ["downkeep"])

    def test_strange_home_names(self):
        strange = ("plain", "a b", 'q"uote', "do$HOME", "back\\slash", "tick`id`", "per%cent", "amp&er", "it's", "-dash")
        for name in (*strange, "*"):
            with self.subTest(name=name):
                self.home = self.root / "homes" / name
                self.home.mkdir(parents=True)
                done = self.install()
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                app = self.home / ".local" / "lib" / "downkeep"
                entry = self.home / ".local/share/applications/downkeep.desktop"
                self.assertEqual(self.program_in(entry), str(app / "downkeep"))
                self.assertEqual(self.run_script(app / "uninstall.sh").returncode, 0)
                self.assertEqual(self.tree(), {})

    def test_the_menu_entry_is_valid(self):
        if not shutil.which("desktop-file-validate"):
            self.skipTest("desktop-file-validate isn't here")
        self.install()
        done = subprocess.run(["desktop-file-validate", str(self.entry)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_homes_that_cannot_work(self):
        for home in (None, "", "relative", *(str(self.root / name) for name in ("missing", "new\nline", "a=b"))):
            with self.subTest(home=home):
                if home and home.startswith("/") and "missing" not in home:
                    Path(home).mkdir()
                before = self.tree(self.root)
                self.refused(self.install(HOME=home), "downkeep: ")
                self.assertEqual(self.tree(self.root), before)

    def test_never_as_root(self):
        (self.stubs / "id").write_text("#!/bin/sh\necho 0\n", encoding="utf-8")
        (self.stubs / "id").chmod(0o755)
        self.refused(self.install(), "without sudo")
        self.assertEqual(self.tree(), {})

    def test_an_incomplete_download_is_refused(self):
        shutil.rmtree(self.unpacked / "_internal")
        self.refused(self.install(), "_internal is missing")
        self.assertEqual(self.tree(), {})

    def test_other_files_are_never_replaced(self):
        for target in ("entry", "icon", "app"):
            for kind in ("file", "link", "dangling", "folder"):
                with self.subTest(target=target, kind=kind):
                    self.home = self.root / f"home {target} {kind}"
                    local = self.home / ".local"
                    place = {
                        "entry": local / "share/applications/downkeep.desktop",
                        "icon": local / "share/icons/hicolor/512x512/apps/downkeep.png",
                        "app": local / "lib/downkeep",
                    }[target]
                    place.parent.mkdir(parents=True)
                    other = self.root / f"other {target} {kind}"
                    if kind == "file":
                        place.write_bytes(b"mine")
                    elif kind == "link":
                        other.mkdir() if target == "app" else other.write_bytes(b"mine")
                        place.symlink_to(other)
                    elif kind == "dangling":
                        place.symlink_to(other)
                    else:
                        place.mkdir()
                        (place / "mine").write_bytes(b"mine")
                    before = self.tree(self.root)
                    self.refused(self.install(), "Downkeep didn't put it there. Nothing was changed")
                    self.assertEqual(self.tree(self.root), before)

    def test_a_link_to_the_same_contents_is_still_not_ours(self):
        self.install()
        for place in (self.entry, self.icon, self.app / "_internal/sub dir/part.bin"):
            with self.subTest(place=place.name):
                copy = self.root / f"copy of {place.name}"
                copy.write_bytes(place.read_bytes())
                place.unlink()
                place.symlink_to(copy)
                before = self.tree(self.root)
                self.refused(self.install(self.unpack(f"newer {place.name}", b"part two")), "Nothing was changed")
                self.assertTrue(place.is_symlink())
                shutil.rmtree(self.root / f"newer {place.name}")
                self.assertEqual(self.tree(self.root), before)
                place.unlink()
                copy.rename(place)

    def test_a_linked_program_folder_is_not_ours(self):
        self.install()
        moved = self.root / "moved away"
        self.app.rename(moved)
        self.app.symlink_to(moved)
        before = self.tree(self.root)
        self.refused(self.install(self.unpack("newer", b"part two")), "Downkeep didn't put it there")
        shutil.rmtree(self.root / "newer")
        self.assertEqual(self.tree(self.root), before)

    def test_update(self):
        self.install()
        newer = self.unpack("newer", b"part two")
        (newer / "_internal" / "new.bin").write_bytes(b"new")
        (newer / "downkeep.desktop").write_text(
            (newer / "downkeep.desktop").read_text(encoding="utf-8") + "X-New=1\n", encoding="utf-8"
        )
        (newer / "downkeep.png").write_bytes(b"new icon")
        done = self.install(newer)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual((self.app / "_internal/sub dir/part.bin").read_bytes(), b"part two")
        self.assertTrue((self.app / "_internal/new.bin").is_file())
        self.assertIn("X-New=1", self.entry.read_text(encoding="utf-8"))
        self.assertEqual(self.icon.read_bytes(), b"new icon")
        self.assertEqual([path.name for path in self.app.parent.iterdir()], ["downkeep"])
        self.assertEqual(self.install(newer).returncode, 0)
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertEqual(self.tree(), {})

    def test_update_drops_parts_that_are_gone(self):
        self.install()
        newer = self.unpack("newer", b"part two")
        shutil.rmtree(newer / "_internal" / "sub dir")
        (newer / "_internal" / "other.bin").write_bytes(b"other")
        self.assertEqual(self.install(newer).returncode, 0)
        self.assertFalse((self.app / "_internal/sub dir").exists())

    def test_update_is_refused_after_a_change(self):
        newer = self.unpack("newer", b"part two")
        for change in ("added", "link", "changed", "entry", "icon"):
            with self.subTest(change=change):
                self.home = self.root / f"home {change}"
                self.home.mkdir()
                self.install()
                app = self.home / ".local/lib/downkeep"
                if change == "added":
                    (app / "_internal" / "my notes.txt").write_bytes(b"mine")
                elif change == "link":
                    (app / "_internal" / "link").symlink_to(self.root)
                elif change == "changed":
                    (app / "_internal/sub dir/part.bin").write_bytes(b"mine")
                elif change == "entry":
                    (self.home / ".local/share/applications/downkeep.desktop").write_bytes(b"mine")
                else:
                    (self.home / ".local/share/icons/hicolor/512x512/apps/downkeep.png").write_bytes(b"mine")
                before = self.tree()
                self.refused(self.install(newer), "Nothing was changed")
                self.assertEqual(self.tree(), before)

    def test_the_installed_copy_cannot_install_itself(self):
        self.install()
        before = self.tree()
        self.refused(self.run_script(self.app / "install.sh"), "installed copy")
        self.assertEqual(self.tree(), before)

    def test_a_failing_step_leaves_what_was_there(self):
        newer = self.unpack("newer", b"part two")
        self.install()
        before = self.tree()
        stubs = self.stubs
        for name, fails_at in (("mv", 1), ("mv", 2), ("cp", 1), ("cp", 3)):
            with self.subTest(name=name, fails_at=fails_at):
                count = self.root / f"count {name} {fails_at}"
                stub = stubs / name
                fail = f'echo x >> "{count}"\n[ "$(wc -l < "{count}")" = {fails_at} ] && exit 1\n'
                stub.write_text(f'#!/bin/sh\n{fail}exec /usr/bin/{name} "$@"\n', encoding="utf-8")
                stub.chmod(0o755)
                done = self.install(newer, PATH=f"{stubs}:/usr/bin:/bin")
                stub.unlink()
                self.refused(done, "couldn't")
                self.assertEqual(self.tree(), before)
                self.assertEqual([path.name for path in self.app.parent.iterdir()], ["downkeep"])

    def test_a_folder_that_cannot_be_written(self):
        lib = self.app.parent
        lib.mkdir(parents=True)
        lib.chmod(0o500)
        self.addCleanup(lib.chmod, 0o700)
        self.refused(self.install(), "couldn't")
        self.assertEqual(self.tree(), {})

    def test_the_data_folder_of_this_person(self):
        data = self.root / "my data"
        self.assertEqual(self.install(XDG_DATA_HOME=str(data)).returncode, 0)
        self.assertTrue((data / "applications/downkeep.desktop").is_file())
        self.assertFalse(self.entry.exists())
        self.assertEqual(self.uninstall(XDG_DATA_HOME=str(data)).returncode, 0)
        self.assertEqual(self.tree(data), {})
        self.assertEqual(self.install(XDG_DATA_HOME="relative").returncode, 0)
        self.assertTrue(self.entry.is_file())

    def test_removes_only_its_own_files(self):
        self.install()
        mine = {
            self.home / "Downloads/mine.txt",
            self.home / ".local/share/applications/other.desktop",
            self.home / ".local/state/downkeep/downkeep.db",
        }
        for path in mine:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"mine")
        done = self.uninstall()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn(str(self.home / ".local/state/downkeep"), done.stdout)
        self.assertEqual(set(self.tree()), {path.relative_to(self.home).as_posix() for path in mine})
        self.assertFalse(self.app.exists())

    def test_removing_leaves_what_was_added_or_changed(self):
        self.install()
        (self.app / "my notes.txt").write_bytes(b"mine")
        (self.app / "_internal/sub dir/part.bin").write_bytes(b"changed")
        self.entry.write_bytes(b"mine")
        done = self.uninstall()
        self.assertIn("Left alone", done.stdout)
        self.assertEqual(
            set(self.tree()),
            {
                ".local/lib/downkeep/my notes.txt",
                ".local/lib/downkeep/_internal/sub dir/part.bin",
                ".local/share/applications/downkeep.desktop",
            },
        )

    def test_a_changed_list_cannot_reach_other_files(self):
        self.install()
        mine = self.home / ".local/lib/mine.txt"
        mine.write_bytes(b"mine")
        far = self.root / "far.txt"
        far.write_bytes(b"mine")
        digest = hashlib.sha256(b"mine").hexdigest()
        with open(self.app / "installed-files.txt", "a", encoding="utf-8") as listing:
            listing.write(f"{digest}  ../mine.txt\n{digest}  _internal/../../mine.txt\n{digest}  {far}\n{digest}  \n")
        self.uninstall()
        self.assertTrue(mine.exists() and far.exists())

    def test_nothing_changes_while_it_is_open(self):
        self.install()
        before = self.tree()
        open_copy = subprocess.Popen([str(self.app / "downkeep"), "-c", "read x"], stdin=subprocess.PIPE)
        try:
            self.refused(self.uninstall(), "Downkeep is open")
            self.refused(self.install(self.unpack("newer", b"part two")), "Downkeep is open")
            self.assertEqual(self.tree(), before)
        finally:
            open_copy.stdin.close()
            open_copy.wait(30)
        self.assertEqual(self.uninstall().returncode, 0)

    def make_schedule(self, program):
        wants = self.timer.parent / "timers.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        self.timer.write_bytes(b"made-up timer")
        self.timer.with_suffix(".service").write_text(f'ExecStart=":{program}" "--auto-run"\n', encoding="utf-8")
        (wants / "downkeep.timer").unlink(missing_ok=True)
        (wants / "downkeep.timer").symlink_to(self.timer)
        others = {wants / "other.timer": b"other", self.timer.parent / "other.service": b"other"}
        for path, data in others.items():
            path.write_bytes(data)
        return others

    def test_automatic_is_turned_off(self):
        for code in (0, 1):
            with self.subTest(code=code):
                self.systemctl(code)
                self.asked.unlink(missing_ok=True)
                self.install()
                others = self.make_schedule(self.app / "downkeep")
                done = self.uninstall()
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                self.assertIn("Automatic tidying is off", done.stdout)
                self.assertEqual(
                    self.asked.read_text(encoding="utf-8"), "--user disable --now downkeep.timer\n--user daemon-reload\n"
                )
                self.assertEqual({path for path in self.home.rglob("*") if not path.is_dir()}, set(others))

    def test_automatic_without_systemctl(self):
        self.install()
        (self.stubs / "systemctl").unlink()
        others = self.make_schedule(self.app / "downkeep")
        sealed = self.root / "sealed"
        sealed.mkdir()
        for name in ("bash", "sha256sum", "rm", "find", "grep", "id", "dirname", "cat", "mv", "cp", "mktemp", "chmod"):
            (sealed / name).symlink_to(shutil.which(name, path="/usr/bin:/bin"))
        done = self.uninstall(PATH=str(sealed))
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertFalse(self.asked.exists())
        self.assertEqual({path for path in self.home.rglob("*") if not path.is_dir()}, set(others))

    def test_another_schedule_is_left_alone(self):
        self.install()
        self.make_schedule("/opt/python")
        before = {path for path in self.home.rglob("*") if path.parent.name in ("user", "timers.target.wants")}
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertFalse(self.asked.exists())
        self.assertEqual({path for path in self.home.rglob("*") if path.parent.name in ("user", "timers.target.wants")}, before)

    def test_the_schedule_is_found_in_the_config_folder_of_this_person(self):
        config = self.root / "my config"
        self.timer = config / "systemd" / "user" / "downkeep.timer"
        self.install()
        self.make_schedule(self.app / "downkeep")
        self.assertEqual(self.uninstall(XDG_CONFIG_HOME=str(config)).returncode, 0)
        self.assertFalse(self.timer.exists())

    def test_automatic_is_found_however_the_path_is_quoted(self):
        try:
            from downkeep.schedule import make_unit
        except ImportError:
            self.skipTest("there is no schedule yet")
        for home in ("/home/plain", '/home/q"uote', "/home/per%cent", "/home/back\\slash", "/home/do$lar", "/home/it's"):
            self.assertIn("/.local/lib/downkeep/downkeep", make_unit([f"{home}/.local/lib/downkeep/downkeep", "--auto-run"]))

    def test_not_installed(self):
        self.refused(self.run_script(self.unpacked / "uninstall.sh"), "isn't installed")
        self.app.mkdir(parents=True)
        (self.app / "mine").write_bytes(b"mine")
        self.refused(self.run_script(self.unpacked / "uninstall.sh"), "isn't installed")
        self.assertTrue((self.app / "mine").exists())

    def test_takes_no_other_arguments(self):
        for script in ("install.sh", "uninstall.sh"):
            done = self.run_script(self.unpacked / script, "--force")
            self.assertEqual(done.returncode, 1)
        self.assertEqual(self.tree(), {})

    def test_the_records_stay_where_they_are_said_to_be(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/a"}, clear=True):
            self.assertEqual(str(window._log_file()), "/home/a/.local/state/downkeep/log.txt")
            try:
                from downkeep.store import data_folder
            except ImportError:
                self.skipTest("there is no store yet")
            self.assertEqual(str(data_folder()), "/home/a/.local/state/downkeep")

    def test_the_timer_is_the_one_downkeep_makes(self):
        schedule = ROOT / "src" / "downkeep" / "schedule.py"
        if not schedule.exists():
            self.skipTest("there is no schedule yet")
        text = schedule.read_text(encoding="utf-8")
        self.assertIn('NAME = "Downkeep"\n', text)
        self.assertIn('timer = f"{name.lower()}.timer"\n', text)

    def test_needs_no_network_password_or_question(self):
        for name in ("install.sh", "uninstall.sh"):
            lines = (PACKAGING / "linux" / name).read_text(encoding="utf-8").splitlines()
            code = "\n".join(line for line in lines if not line.startswith("#"))
            self.assertNotRegex(code, r"\b(curl|wget|pkexec|sudo -|read -p|eval)\b")
            self.assertNotIn("rm -r", code)


if __name__ == "__main__":
    unittest.main()
