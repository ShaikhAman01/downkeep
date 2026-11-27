"""Runs a built Downkeep on made-up files in a temporary home and talks to it the way its window does.

    python smoke.py path/to/downkeep [--version 1.2.3]
    python smoke.py out/downkeep-1.2.3-linux-x64.tar.gz

A packed download is unpacked first, and on Linux installed into a made-up home and removed again.
No window is opened, and the real home, Trash and Downloads are never used.
"""

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

WINDOWS = sys.platform == "win32"
START_SECONDS = 60
CLOSE_SECONDS = 15
AUTO_SECONDS = 60
OLD = 40 * 86_400
LINK = re.compile(r"http://127\.0\.0\.1:(\d+)/#([A-Za-z0-9_-]{43})")
MARK = "DOWNKEEP_SMOKE"
NEVER_STARTED = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge-stable", "microsoft-edge",
    "brave", "brave-browser", "firefox", "x-www-browser", "www-browser", "gnome-open", "kde-open", "xdg-open",
    "gvfs-open", "open", "zenity", "kdialog", "notify-send", "dbus-send",
)
# These may be asked things, but only this
ONLY = {"gio": {"info", "set"}, "systemctl": {"--user show"}, "crontab": set(), "schtasks": set()}
PAGE = ("index.html", "app.js", "style.css", "icon.svg")


class Failed(Exception):
    pass


def check(condition: object, problem: str) -> None:
    if not condition:
        raise Failed(problem)


class Home:
    """A made-up person: home, Downloads, Trash, settings and the programs on their PATH."""

    def __init__(self, root: Path):
        self.root = root
        self.user = root / "home"
        self.downloads = self.user / "Downloads"
        self.data = root / "data"
        self.trash = root / "share" / "Trash"
        self.stubs = root / "bin"
        self.started = root / "started"
        self.mark = secrets.token_hex(8)
        self.links = 0
        for folder in (self.downloads, self.stubs, self.started, root / "tmp", root / "runtime"):
            folder.mkdir(parents=True)
        (root / "runtime").chmod(0o700)
        if not WINDOWS:
            self._write_stubs()
        self.files = self._write_files()

    def _write_stubs(self) -> None:
        for name in (*NEVER_STARTED, *ONLY):
            stub = self.stubs / name
            record = f'printf "%s %s\\n" "$1" "$2" >> "{self.started}/{name}"'
            stub.write_text(f"#!/bin/sh\n{record}\nexit 1\n", encoding="utf-8")
            stub.chmod(0o755)

    def _write_files(self) -> dict[str, bytes]:
        content = {
            "holiday.jpg": b"\xff\xd8\xff made-up picture " * 900,
            "song.mp3": b"ID3 made-up song " * 700,
            "notes.pdf": b"%PDF-1.4 made-up notes " * 500,
            "notes (1).pdf": b"%PDF-1.4 made-up notes " * 500,
            "readme.txt": b"made-up text, only once\n",
            "photos/one.png": b"made-up photo one " * 400,
            "photos/two.png": b"made-up photo two " * 300,
        }
        for name, data in content.items():
            place = self.downloads.joinpath(*name.split("/"))
            place.parent.mkdir(exist_ok=True)
            place.write_bytes(data)
        with zipfile.ZipFile(self.downloads / "photos.zip", "w") as archive:
            for name in ("photos/one.png", "photos/two.png"):
                archive.write(self.downloads.joinpath(*name.split("/")), name)
        old = time.time() - OLD
        for place in self.downloads.rglob("*"):
            if place.is_file():
                os.utime(place, (old, old))
        # The copy is the newer of the two, so it is the one that counts as extra
        os.utime(self.downloads / "notes (1).pdf", (old + 3600, old + 3600))
        content["photos.zip"] = (self.downloads / "photos.zip").read_bytes()
        return content

    def environment(self, **extra: str) -> dict[str, str]:
        keep = ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERNAME", "USERDOMAIN", "LANG", "LC_ALL")
        values = {name: os.environ[name] for name in keep if name in os.environ}
        values |= {
            "HOME": str(self.user),
            "USERPROFILE": str(self.user),
            "LOCALAPPDATA": str(self.root / "local"),
            "APPDATA": str(self.root / "roaming"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "share"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.root / 'runtime' / 'none'}",
            "TMPDIR": str(self.root / "tmp"),
            "TEMP": str(self.root / "tmp"),
            "TMP": str(self.root / "tmp"),
            "DOWNKEEP_DATA": str(self.data),
            MARK: self.mark,
        }
        if WINDOWS:
            values["PATH"] = str(Path(os.environ["SystemRoot"], "System32"))
        else:
            # /usr/bin is left out on purpose: only the stubs can be found
            values["PATH"] = str(self.stubs)
        return values | extra

    def new_link(self) -> Path:
        self.links += 1
        return self.root / f"link-{self.links}"

    def listing(self) -> dict[str, str]:
        return {
            path.relative_to(self.downloads).as_posix(): digest(path.read_bytes())
            for path in sorted(self.downloads.rglob("*"))
            if path.is_file()
        }

    def expected(self) -> dict[str, str]:
        return {name: digest(data) for name, data in self.files.items()}


def digest(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def processes_of(home: Home, program: Path) -> set[int]:
    """Processes started for this run, however far down."""
    if WINDOWS:
        command = [
            str(Path(os.environ["SystemRoot"], "System32", "tasklist.exe")),
            "/FI", f"IMAGENAME eq {program.name}", "/FO", "CSV", "/NH",
        ]
        text = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60).stdout
        return {int(number) for number in re.findall(r'^"[^"]*","(\d+)"', text, re.MULTILINE)}
    found, wanted = set(), f"{MARK}={home.mark}".encode()
    for place in Path("/proc").iterdir():
        if place.name.isdigit():
            with contextlib.suppress(OSError):
                if wanted in (place / "environ").read_bytes().split(b"\0"):
                    found.add(int(place.name))
    return found


class Copy:
    """One running Downkeep."""

    def __init__(self, program: Path, home: Home):
        self.home = home
        self.link = home.new_link()
        self.output = home.root / f"output-{home.links}"
        self.key = ""
        before = time.monotonic()
        with open(self.output, "wb") as output:
            self.process = subprocess.Popen(
                [str(program)],
                env=home.environment(DOWNKEEP_LINK_FILE=str(self.link)),
                cwd=home.root,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            self.port, self.code = self._read_link()
            self.seconds = time.monotonic() - before
        except BaseException:
            self.stop()
            raise

    def _read_link(self) -> tuple[int, str]:
        until = time.monotonic() + START_SECONDS
        while time.monotonic() < until:
            with contextlib.suppress(FileNotFoundError, PermissionError):
                found = LINK.fullmatch(self.link.read_text(encoding="utf-8").strip())
                if found:
                    return int(found[1]), found[2]
            if self.process.poll() is not None:
                said = self.output.read_text(encoding="utf-8", errors="replace").strip()
                raise Failed(f"it stopped with code {self.process.returncode} before writing its link: {said}")
            time.sleep(0.01)
        raise Failed(f"no link in DOWNKEEP_LINK_FILE within {START_SECONDS} s")

    def ask(self, method: str, path: str, body: object = None, **headers: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            data = None if body is None else json.dumps(body).encode()
            sent = {"Content-Type": "application/json", "X-Downkeep-Key": self.key, **headers}
            connection.request(method, path, data, sent)
            answer = connection.getresponse()
            return answer.status, answer.read()
        finally:
            connection.close()

    def post(self, path: str, body: object | None = None) -> dict:
        status, data = self.ask("POST", path, {} if body is None else body)
        check(status == 200, f"{path} answered {status}: {data[:200].decode(errors='replace')}")
        return json.loads(data)

    def begin(self) -> dict:
        session = self.post("/api/session", {"code": self.code})
        self.key = session["key"]
        self.post("/api/choose", {"path": ["Downloads"]})
        return session

    @contextlib.contextmanager
    def held_open(self) -> Iterator[None]:
        """What the page does while its window is open."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("POST", "/api/stay", b"{}", {"Content-Type": "application/json", "X-Downkeep-Key": self.key})
        answer = connection.getresponse()
        check(answer.status == 200 and answer.read(1) == b".", "the page's open connection wasn't accepted")
        try:
            yield
        finally:
            answer.close()
            connection.close()

    def wait_closed(self) -> float:
        before = time.monotonic()
        try:
            code = self.process.wait(CLOSE_SECONDS)
        except subprocess.TimeoutExpired:
            raise Failed(f"still running {CLOSE_SECONDS} s after its window closed") from None
        check(code == 0, f"it ended with code {code}")
        return time.monotonic() - before

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(30)


class Smoke:
    def __init__(self, program: Path, home: Home, version: str | None):
        self.program = program
        self.home = home
        self.version = version
        self.copies: list[Copy] = []
        self.before = processes_of(home, program)
        self.notes: list[str] = []

    def start(self) -> Copy:
        copy = Copy(self.program, self.home)
        self.copies.append(copy)
        return copy

    def files_are_back(self, when: str) -> None:
        now, wanted = self.home.listing(), self.home.expected()
        check(now == wanted, f"after {when}, Downloads isn't as it was: {sorted(set(now.items()) ^ set(wanted.items()))}")

    def opens(self) -> None:
        self.main = self.start()
        self.notes.append(f"link ready in {self.main.seconds:.2f} s")
        if not WINDOWS:
            mode = stat.S_IMODE(self.main.link.stat().st_mode)
            check(mode == 0o600, f"the link file can be read by others ({mode:o})")
        session = self.main.begin()
        check(not self.version or session["version"] == self.version, f"it says version {session['version']}")

    def page_is_inside(self) -> None:
        status, page = self.main.ask("GET", "/")
        check(status == 200 and b"<script" in page, f"the page answered {status}")
        wanted = set(re.findall(rb'(?:src|href)="/?([\w.-]+)"', page)) | {name.encode() for name in PAGE[1:]}
        seen = set()
        while wanted - seen:
            name = sorted(wanted - seen)[0]
            seen.add(name)
            status, data = self.main.ask("GET", f"/{name.decode()}")
            check(status == 200 and data, f"/{name.decode()} answered {status}")
            if name.endswith(b".js"):
                wanted |= set(re.findall(rb'from\s*"\./([\w.-]+)"', data))

    def keeps_others_out(self) -> None:
        for what, headers in {
            "a wrong key": {"X-Downkeep-Key": "x" * 43},
            "another host name": {"Host": "example.com"},
            "another page": {"Origin": "http://example.com"},
        }.items():
            status, _ = self.main.ask("POST", "/api/report", {}, **headers)
            check(status == 403, f"{what} was answered with {status}")
        status, _ = self.main.ask("POST", "/api/session", {"code": self.main.code})
        check(status == 403, f"the one-time link worked twice ({status})")

    def reports(self) -> None:
        report = self.main.post("/api/report")
        check(Path(report["folder"]) == self.home.downloads.resolve(), f"it looked in {report['folder']}")
        files = self.home.files
        loose = [name for name in files if "/" not in name]
        check(report["files"] == len(loose), f"it counted {report['files']} files, not {len(loose)}")
        check(report["bytes"] == sum(len(files[name]) for name in loose), "the total size is wrong")
        wanted = {"files": 2, "bytes": len(files["notes (1).pdf"]) + len(files["photos.zip"])}
        check(report.get("proven") == wanted, f"proven is {report.get('proven')}, not {wanted}")
        names = [group["names"] for group in report["duplicates"]]
        check(names == [["notes.pdf", "notes (1).pdf"]], f"copies: {names}")
        check([item["name"] for item in report["extracted"]] == ["photos.zip"], f"zips: {report['extracted']}")
        self.files_are_back("the report")

    def can_act(self) -> None:
        system = self.main.post("/api/system")
        check(system["tidy"] is True, "tidying isn't available: a part of the program may be missing")
        check(system["trash"] is True, "the Trash isn't available: a part of the program may be missing")
        check(system["restore"] is (not WINDOWS), f"restore is {system['restore']}")
        self.main.post("/api/rules")
        status = self.main.post("/api/auto")
        check(status["on"] is False, "automatic is on before anyone turned it on")

    def tidies_and_undoes(self) -> None:
        plan = self.main.post("/api/tidy/plan")
        moves = {move["name"]: move["folder"] for move in plan["moves"]}
        check("holiday.jpg" in moves, f"the plan leaves out holiday.jpg: {moves}")
        self.files_are_back("the plan")
        names = sorted(moves)
        result = self.main.post("/api/tidy/apply", {"plan": plan["plan"], "names": names})
        check(sorted(result["done"]) == names and not result["skipped"], f"tidied {result}")
        now, wanted = self.home.listing(), self.home.expected()
        for name, folder in moves.items():
            wanted[f"{folder}/{name}"] = wanted.pop(name)
        off = sorted(set(now.items()) ^ set(wanted.items()))
        check(now == wanted, f"tidied files aren't where the plan said: {off}")
        status, _ = self.main.ask("POST", "/api/tidy/apply", {"plan": plan["plan"], "names": names})
        check(status != 200, "a used plan was accepted again")
        undone = self.main.post("/api/undo", {"batch": result["batch"]})
        check(len(undone["done"]) == len(names) and not undone["skipped"], f"undo skipped {undone['skipped']}")
        self.files_are_back("undo")
        left = [path.name for path in self.home.downloads.iterdir() if path.is_dir()]
        check(left == ["photos"], f"undo left folders behind: {left}")

    def trashes(self) -> None:
        if WINDOWS and os.environ.get("GITHUB_ACTIONS") != "true":
            self.notes.append("Recycle Bin not tested: only done on a GitHub runner")
            return
        self.main.post("/api/report")
        status, _ = self.main.ask("POST", "/api/trash", {"names": ["readme.txt"]})
        check(status != 200, "a file without proof was accepted for the Trash")
        names = ["notes (1).pdf", "photos.zip"]
        result = self.main.post("/api/trash", {"names": names})
        check(sorted(result["done"]) == names and not result["skipped"], f"trashed {result}")
        wanted = self.home.expected()
        for name in names:
            del wanted[name]
        check(self.home.listing() == wanted, "something else than the two files left Downloads")
        if WINDOWS:
            return
        trashed = {path.name: path.read_bytes() for path in (self.home.trash / "files").iterdir()}
        check(
            sorted(trashed.values()) == sorted(self.home.files[name] for name in names),
            f"the Trash holds {sorted(trashed)}",
        )
        notes = list((self.home.trash / "info").glob("*.trashinfo"))
        check(len(notes) == 2, "the Trash has no note of where they came from")
        undone = self.main.post("/api/undo", {"batch": result["batch"]})
        check(len(undone["done"]) == 2 and not undone["skipped"], f"undo skipped {undone['skipped']}")
        self.files_are_back("taking them back out of the Trash")

    def two_at_once(self) -> None:
        second = self.start()
        second.begin()
        with self.main.held_open(), second.held_open():
            first_report, second_report = self.main.post("/api/report"), second.post("/api/report")
            check(first_report["files"] == second_report["files"], "the two copies disagree")
            check(self.main.port != second.port, "both copies use one port")
        # Only the second was used and closed here; the first is held again below
        self.second = second

    def closes(self) -> None:
        with self.main.held_open():
            seconds = self.second.wait_closed()
            # Longer than the grace time: an open window must keep it running
            time.sleep(6)
            check(self.main.process.poll() is None, "it stopped while its window was open")
        self.notes.append(f"second copy ended after {seconds:.1f} s, first {self.main.wait_closed():.1f} s after closing")

    def stays_quiet(self) -> None:
        for copy in self.copies:
            said = copy.output.read_bytes()
            check(not said, f"it printed {said[:200]!r}")

    def auto_run(self) -> None:
        before = self.home.listing()
        done = subprocess.run(
            [str(self.program), "--auto-run"],
            env=self.home.environment(),
            cwd=self.home.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=AUTO_SECONDS,
        )
        check(done.returncode == 0, f"--auto-run ended with code {done.returncode}: {done.stderr[:300]!r}")
        check(not done.stdout and not done.stderr, f"--auto-run printed {(done.stdout + done.stderr)[:300]!r}")
        check(self.home.listing() == before, "--auto-run moved files although automatic is off")

    def clean_start_of_others(self) -> None:
        """Without a link file it starts a browser; that browser must not get the bundled libraries."""
        seen = self.home.root / "browser"
        stub = self.home.stubs / "chromium"
        usual = stub.read_text(encoding="utf-8")
        stub.write_text(
            '#!/bin/sh\n'
            f'{{ printf "LD=%s\\n" "${{LD_LIBRARY_PATH-unset}}"; printf "URL=%s\\n" "$1"; }} > "{seen}.part"\n'
            f'exec /bin/mv "{seen}.part" "{seen}"\n',
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [str(self.program)],
            env=self.home.environment(LD_LIBRARY_PATH="/made-up/libraries"),
            cwd=self.home.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            until = time.monotonic() + START_SECONDS
            while not seen.exists() and time.monotonic() < until and process.poll() is None:
                time.sleep(0.02)
            check(seen.exists(), "it never started the browser")
            text = seen.read_text(encoding="utf-8")
            check("LD=/made-up/libraries\n" in text, f"the browser got changed library settings: {text.splitlines()[0]}")
            check(re.search(r"URL=--app=http://127\.0\.0\.1:\d+/\?window=app#[\w-]{43}\n", text), "the browser got another link")
        finally:
            process.kill()
            process.wait(30)
            stub.write_text(usual, encoding="utf-8")

    def nothing_left(self) -> None:
        until = time.monotonic() + 5
        while (left := processes_of(self.home, self.program) - self.before) and time.monotonic() < until:
            time.sleep(0.2)
        check(not left, f"still running: {sorted(left)}")
        for place in sorted(self.home.started.iterdir()):
            for line in place.read_text(encoding="utf-8", errors="replace").splitlines():
                allowed = any(f"{line} ".startswith(f"{words} ") for words in ONLY.get(place.name, ()))
                check(allowed, f"it started {place.name} {line}")
        strays = [str(path) for path in (self.home.root / "tmp").iterdir()]
        check(not strays, f"it left temporary files: {strays}")
        usual = {"Downloads", "AppData"} if WINDOWS else {"Downloads"}
        strays = sorted(path.name for path in self.home.user.iterdir() if path.name not in usual)
        check(not strays, f"it wrote into the home folder: {strays}")
        check(not (self.home.root / "config" / "systemd").exists(), "it installed a schedule")

    def steps(self) -> list[tuple[str, Callable[[], None]]]:
        return [
            ("opens without a window", self.opens),
            ("the page is inside", self.page_is_inside),
            ("keeps others out", self.keeps_others_out),
            ("reports, with the proven total", self.reports),
            ("every action is available", self.can_act),
            ("tidies by plan and undoes", self.tidies_and_undoes),
            ("trash, only with proof", self.trashes),
            ("two copies at once", self.two_at_once),
            ("stays while open, ends when closed", self.closes),
            ("prints nothing, not even the link", self.stays_quiet),
            ("--auto-run is quiet and ends", self.auto_run),
            *([] if WINDOWS else [("other programs start clean", self.clean_start_of_others)]),
            ("leaves nothing behind", self.nothing_left),
        ]

    def run(self) -> bool:
        for name, step in self.steps():
            try:
                step()
            except Exception as error:
                text = str(error) if isinstance(error, Failed) else f"{type(error).__name__}: {error}"
                print(f"FAIL  {name}: {text}")
                return False
            print(f"pass  {name}")
        for note in self.notes:
            print(f"note  {note}")
        return True

    def stop(self) -> None:
        for copy in self.copies:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                copy.stop()


def unpack(archive: Path, root: Path) -> Path:
    place = root / "un packed"
    place.mkdir()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as opened:
            opened.extractall(place)
        return place / "Downkeep" / "Downkeep.exe"
    with tarfile.open(archive) as opened:
        opened.extractall(place, filter="data")
    return place / "downkeep" / "downkeep"


def install(program: Path, root: Path) -> tuple[Path, Callable[[], None]]:
    """Runs the download's own install.sh, and returns the installed program and how to remove it."""
    home = root / "made up $home"
    home.mkdir()
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    app = home / ".local" / "lib" / "downkeep"

    def script(place: Path) -> None:
        done = subprocess.run([str(place)], env=environment, cwd=root, capture_output=True, text=True, timeout=60)
        check(done.returncode == 0, f"{place.name}: {done.stderr.strip()}")

    def remove() -> None:
        script(app / "uninstall.sh")
        left = [str(path) for path in home.rglob("*") if not path.is_dir()]
        check(not left, f"uninstall.sh left {left}")

    script(program.with_name("install.sh"))
    check((home / ".local" / "share" / "applications" / "downkeep.desktop").is_file(), "there is no menu entry")
    return app / "downkeep", remove


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("program", type=Path)
    parser.add_argument("--version", help="the version it must say it is")
    args = parser.parse_args(argv)
    if sys.platform == "darwin":
        raise SystemExit("This check is for Windows and Linux")
    program = args.program.resolve()
    if not program.is_file():
        raise SystemExit(f"No program at {program}")

    root = Path(tempfile.mkdtemp(prefix="downkeep-smoke-")).resolve()
    remove = None
    try:
        if program.name.endswith((".zip", ".tar.gz")):
            program = unpack(program, root)
            if not WINDOWS:
                program, remove = install(program, root)
                print("pass  installs")
        smoke = Smoke(program, Home(root / "run"), args.version)
        try:
            passed = smoke.run()
        finally:
            smoke.stop()
        if passed and remove:
            remove()
            print("pass  uninstalls")
    except Failed as error:
        print(f"FAIL  {error}")
        passed = False
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
