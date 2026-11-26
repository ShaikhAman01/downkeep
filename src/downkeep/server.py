import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
import inspect
import stat
import sys
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from downkeep import __version__
from downkeep.browse import choose, inside, subfolders
from downkeep.evidence import find_duplicates, find_extracted
from downkeep.model import ActionError, Busy, Move, Result, Stay
from downkeep.origin import find_hosts
from downkeep.report import build, clean
from downkeep.scan import FileRecord, ScanError, scan

HOST = "127.0.0.1"
MAX_BODY = 16_384
MAX_LIST = 1_000_000
LISTS = {"/api/trash", "/api/tidy/apply", "/api/rules/save", "/api/rules/name"}
SUGGESTED = 3
MAX_KEEPERS = 20_000
RUN_FILES = 5_000
OTHER_USER = "This link belongs to another account on this computer. Open Downkeep from your own account"
ENDINGS = {
    "pdf": "A PDF document", "doc": "A Word document", "docx": "A Word document", "odt": "A text document",
    "rtf": "A text document", "txt": "A text file", "md": "A text file", "epub": "A book",
    "xls": "A spreadsheet", "xlsx": "A spreadsheet", "ods": "A spreadsheet", "csv": "A spreadsheet",
    "ppt": "A presentation", "pptx": "A presentation", "odp": "A presentation",
    "exe": "A Windows installer", "msi": "A Windows installer", "deb": "A Linux installer", "rpm": "A Linux installer",
    "appimage": "A Linux app", "dmg": "A Mac installer", "pkg": "A Mac installer", "iso": "A disk image",
    "apk": "An Android app", "torrent": "A torrent file",
}
KIND_WORDS = {
    "documents": "A document", "spreadsheets": "A spreadsheet", "presentations": "A presentation", "images": "A picture",
    "videos": "A video", "music": "A sound file", "archives": "An archive", "books": "A book", "fonts": "A font",
    "code": "A code or data file",
}
HIDDEN = "suggest_hidden"
HIDDEN_FOLDERS = 50
PLAN_SECONDS = 30 * 60
CODE_SECONDS = 120
CLOSE_SECONDS = 4
BEAT_SECONDS = 1

PAGES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/core.js": ("core.js", "text/javascript; charset=utf-8"),
    "/ask.js": ("ask.js", "text/javascript; charset=utf-8"),
    "/clutter.js": ("clutter.js", "text/javascript; charset=utf-8"),
    "/tidy.js": ("tidy.js", "text/javascript; charset=utf-8"),
    "/rules.js": ("rules.js", "text/javascript; charset=utf-8"),
    "/runs.js": ("runs.js", "text/javascript; charset=utf-8"),
    "/auto.js": ("auto.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


FAILED = "Something went wrong. History shows what was done"
BUSY = "Downkeep is already moving files. Try again in a moment"
STALE = "The folder changed, look again"
UNKNOWN = "Some of these files aren't in the list any more. Look again"
NO_TIDY = "Tidying isn't available on this system yet"
NO_TRASH = "Moving files to the Trash isn't available on this system yet"
NO_AUTO = "Automatic tidying isn't available on this system yet"
NOT_CONFIRMED = "Turning on automatic tidying needs a yes on its confirm step first"


class Actions:
    """Everything that changes files or settings. Imported only when used, and replaced whole in tests."""

    def able(self) -> dict[str, bool]:
        try:
            from downkeep import engine, trash

            tidy = bool(engine.SUPPORTED)
            return {"tidy": tidy, "trash": tidy and bool(trash.SUPPORTED), "restore": tidy and bool(trash.CAN_RESTORE)}
        except ImportError:
            return {"tidy": False, "trash": False, "restore": False}

    def schedule(self) -> tuple[bool, str]:
        if not self.able()["tidy"]:
            return False, NO_AUTO
        from downkeep.schedule import available

        return available()

    def recover(self) -> Result:
        from downkeep.engine import recover
        from downkeep.store import Store

        with Store() as store:
            return recover(store)

    def trash(self, folder: str, names: Sequence[str]) -> Result:
        from downkeep.clear import apply_clear
        from downkeep.store import Store

        from downkeep.engine import recover

        keep_in = self.keep_in(folder)
        with Store() as store:
            # Moving to the Trash doesn't settle an earlier crash by itself
            recover(store)
            if keep_in and _takes(apply_clear, "keep_in"):
                return apply_clear(store, folder, names, keep_in=keep_in)
            return apply_clear(store, folder, names)

    def keep_in(self, folder: str) -> list[str]:
        """The real folders inside this one that the rules file into. Copies of what is there count as proof."""
        try:
            rules = self.rules()["rules"]
        except (ActionError, ImportError):
            return []
        names = dict.fromkeys(rule["folder"] for rule in rules if rule.get("on"))
        return [name for name in names if _is_folder(os.path.join(folder, name))]

    def keepers(self, folder: str) -> list[FileRecord]:
        from downkeep import evidence

        finder = getattr(evidence, "find_keepers", None)
        if finder is not None:
            try:
                return finder(folder, self.keep_in(folder))[:MAX_KEEPERS]
            except OSError:
                return []
        found = []
        for name in self.keep_in(folder):
            try:
                files = scan(os.path.join(folder, name)).files
            except ScanError:
                continue
            found += [replace(file, name=f"{name}/{file.name}") for file in files]
            if len(found) >= MAX_KEEPERS:
                return found[:MAX_KEEPERS]
        return found

    def plan(self, folder: str) -> tuple[list[Move], list[Stay], list[dict]]:
        from downkeep import sorting

        snapshot = scan(folder)
        rules = self.rules()["rules"]
        hosts = find_hosts(file.path for file in snapshot.files)
        moves, stays = sorting.plan(snapshot, rules, hosts)
        suggest = getattr(sorting, "suggest", None)
        return moves, stays, suggest(snapshot, rules, hosts) if suggest else []

    def name(self, rule: dict) -> str:
        from downkeep import sorting

        suggest_name = getattr(sorting, "suggest_name", None)
        if suggest_name is None:
            raise ActionError("Suggested names aren't available yet")
        # A folder being made has no name yet; the check needs one
        checked = sorting.check_rules([rule | {"id": "", "folder": rule.get("folder") or "New folder"}])[0]
        return suggest_name(checked)

    def setting(self, key: str, default: object = None) -> object:
        from downkeep.store import Store

        with Store() as store:
            return store.get(key, default)

    def set_setting(self, key: str, value: object) -> None:
        from downkeep.store import Store

        with Store() as store:
            store.set(key, value)

    def tidy(self, folder: str, moves: Sequence[Move]) -> Result:
        from downkeep.engine import apply_moves
        from downkeep.store import Store

        with Store() as store:
            return apply_moves(store, folder, moves)

    def history(self, limit: int = 30) -> list[dict]:
        from downkeep.store import Store

        with Store() as store:
            runs = store.history(limit)
            for run in runs:
                run["files"] = _files(store, run["batch"], run["kind"], run["folder"])
            return runs

    def run_files(self, batch: int) -> tuple[str, list[dict]]:
        from downkeep.store import Store

        with Store() as store:
            found = store.batch(batch)
            if found is None:
                raise ActionError("That run isn't in the history")
            return found.folder, _files(store, batch, found.kind, found.folder, places=True)

    def undo(self, batch: int, names: Sequence[str] | None = None) -> Result:
        from downkeep.engine import undo
        from downkeep.store import Store

        with Store() as store:
            before = store.batch(batch)
            if before is None:
                raise ActionError("That run isn't in the history")
            if names is None:
                result = undo(store, batch)
            elif _takes(undo, "names"):
                result = undo(store, batch, names=list(names))
            else:
                raise ActionError("Putting back one file at a time isn't available yet")
            if before.kind == "auto" and result.done:
                from downkeep.auto import note_undo

                with suppress(ActionError):  # The files are already back
                    note_undo(store, batch)
            return result

    def rules(self) -> dict:
        from downkeep.sorting import DEFAULT_RULES, KINDS, saved_rules
        from downkeep.store import Store

        with Store() as store:
            kinds = [{"id": kind, "name": name} for kind, name in KINDS.items()]
            return {"rules": saved_rules(store), "kinds": kinds, "defaults": DEFAULT_RULES}

    def save_rules(self, value: object) -> None:
        from downkeep.sorting import RULES_KEY, check_rules
        from downkeep.store import Store

        rules = check_rules(value)
        with Store() as store:
            store.set(RULES_KEY, rules)

    def auto(self, folder: str) -> dict:
        from downkeep.auto import status
        from downkeep.store import Store

        with Store() as store:
            return status(store, folder)

    def set_auto(self, folder: str, on: bool) -> None:
        from downkeep.auto import turn_off, turn_on
        from downkeep.store import Store

        with Store() as store:
            turn_on(store, folder) if on else turn_off(store)

    def auto_run(self) -> Result:
        from downkeep.auto import run
        from downkeep.store import Store

        with Store() as store:
            return run(store)

    def refresh_schedule(self, folder: str) -> None:
        """Points the schedule at this copy if the program it starts has gone."""
        from downkeep import auto, schedule
        from downkeep.store import Store

        with Store() as store:
            if not auto.status(store, folder)["on"]:
                return
        installed = schedule.installed_command()
        if installed and not os.path.exists(installed[0]):
            schedule.install(schedule.command())


@dataclass(frozen=True, slots=True)
class _Plan:
    id: str
    folder: str
    mark: tuple[int, int]
    made: float
    moves: dict[str, Move]
    suggested: list[str] = field(default_factory=list)


class App:
    """One folder, one window. The launch code works once and is swapped for a key the page keeps."""

    def __init__(
        self,
        folder: str,
        show: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        home: Path | None = None,
        actions: Actions | None = None,
        same_user: Callable[[tuple, int], bool] | None = None,
    ):
        self.folder = folder
        self.home = home or Path.home()
        self.show = show
        self.clock = clock
        self.actions = actions or Actions()
        self.same_user = same_user or same_user_here
        self.code = secrets.token_urlsafe(32)
        self.key: str | None = None
        self.paths: dict[str, str] = {}
        self.proven: set[str] = set()
        self.planned: _Plan | None = None
        self.recovered: Result | None = None
        self.pages = 0
        self.started = self.seen = clock()
        self.working = 0
        self.acting = 0
        self.lock = threading.Lock()
        self.turn = threading.Lock()

    def open_session(self, code: object) -> str | None:
        with self.lock:
            fresh = self.clock() - self.started <= CODE_SECONDS
            if self.key or not fresh or not isinstance(code, str) or not _equal(code, self.code):
                return None
            self.key = secrets.token_urlsafe(32)
            self.seen = self.clock()
            return self.key

    def allows(self, key: str | None) -> bool:
        with self.lock:
            return bool(self.key and key and _equal(key, self.key))

    def arrive(self) -> None:
        with self.lock:
            self.pages += 1

    def leave(self) -> None:
        with self.lock:
            self.pages -= 1
            self.seen = self.clock()

    def finished(self) -> bool:
        with self.lock:
            now = self.clock()
            if self.working:
                return False
            if self.key is None:
                return now - self.started > CODE_SECONDS
            return self.pages == 0 and now - self.seen > CLOSE_SECONDS

    def settle(self) -> None:
        """Waits until no file is being moved."""
        while self.acting:
            time.sleep(0.05)

    @contextmanager
    def _working(self, acting: bool = False) -> Iterator[str]:
        with self.lock:
            self.working += 1
            self.acting += acting
        try:
            # One at a time: a scan never sees a folder that is being changed
            with self.turn:
                with self.lock:
                    folder = self.folder
                yield folder
        finally:
            with self.lock:
                self.working -= 1
                self.acting -= acting
                self.seen = self.clock()

    def report(self) -> dict:
        with self._working() as folder:
            snapshot = scan(folder)
            keepers = self.actions.keepers(folder) if self.actions.able()["tidy"] else []
            if keepers and _takes(find_duplicates, "keepers"):
                duplicates = find_duplicates(snapshot.files, keepers=keepers)
            else:
                duplicates = find_duplicates(snapshot.files)
            result = build(
                snapshot,
                hosts=find_hosts(file.path for file in snapshot.files),
                duplicates=duplicates,
                extracted=find_extracted(snapshot.files, snapshot.folder),
            )
            here = {file.name for file in snapshot.files}
            with self.lock:
                if self.folder == folder:
                    self.paths = {file.name: file.path for file in snapshot.files}
                    # A copy filed in Documents is proof, but is never itself offered for the Trash
                    self.proven = {name for group in result["duplicates"] for name in group["names"][1:] if name in here}
                    self.proven |= {item["name"] for item in result["extracted"]}
        return result

    def choose(self, parts: object) -> None:
        folder = str(choose(self.home, parts))
        with self.lock:
            self.folder = folder
            self.paths = {}
            self.proven = set()
            self.planned = None

    def show_file(self, name: object, batch: object = None) -> bool:
        if batch is not None:
            return self._show_moved(batch, name)
        with self.lock:
            path = self.paths.get(name) if isinstance(name, str) else None
        if path is None or not os.path.lexists(path):
            return False
        self.show(path)
        return True

    def _show_moved(self, batch, name):
        """Only where the records of that run say the file is now, and only inside the chosen folder."""
        if type(batch) is not int or batch < 1 or not isinstance(name, str) or not self.actions.able()["tidy"]:
            return False
        try:
            _, files = self.actions.run_files(batch)
        except ActionError:
            return False
        place = next((file.get("place") for file in files if file["name"] == name), None)
        with self.lock:
            folder = self.folder
        if not place or not _within(place, folder) or not os.path.lexists(place):
            return False
        self.show(place)
        return True

    def recover(self) -> None:
        """Once, before anything else: finds out what an interrupted run left behind."""
        try:
            if self.actions.able()["tidy"]:
                self.recovered = self.actions.recover()
                self.actions.refresh_schedule(self.folder)
        except Exception:
            # The window still opens; every action checks for itself again
            return

    def system(self) -> dict:
        available, why = self.actions.schedule()
        return {
            **self.actions.able(),
            "auto": {"available": available, "why": why},
            "recovered": _answer(self.recovered) if self.recovered else None,
        }

    def trash(self, names: object) -> dict:
        with self._working(acting=True) as folder:
            self._need("trash", NO_TRASH)
            with self.lock:
                known = self.proven if self.folder == folder else set()
                wanted = _names(names, known)
            return self._change(lambda: self.actions.trash(folder, wanted))

    def plan(self) -> dict:
        with self._working() as folder:
            self._need("tidy", NO_TIDY)
            moves, stays, suggested = self.actions.plan(folder)
            suggested = list(suggested)[:SUGGESTED]
            signature = sorted(str(item.get("folder")) for item in suggested)
            hidden = self.actions.setting(HIDDEN, {})
            if isinstance(hidden, dict) and hidden.get(folder) == signature:
                suggested = []
            made = _Plan(
                secrets.token_urlsafe(16), folder, _mark(folder), self.clock(), {move.file.name: move for move in moves}, signature,
            )
            with self.lock:
                if self.folder == folder:
                    self.planned = made
            names = list(dict.fromkeys(move.folder for move in moves))
            with self.lock:
                proven = set(self.proven) if self.folder == folder else set()
            return {
                "plan": made.id,
                "moves": [_move(move, move.file.name in proven) for move in moves],
                "stays": [_stay(stay) for stay in stays],
                "folders": [{"name": name, "exists": _is_folder(os.path.join(folder, name))} for name in names],
                "suggested": suggested,
            }

    def later(self) -> dict:
        """Hides the suggestions for this folder until they change."""
        with self._working() as folder:
            self._need("tidy", NO_TIDY)
            with self.lock:
                made = self.planned
            if made is None or made.folder != folder:
                raise ActionError(STALE)
            hidden = self.actions.setting(HIDDEN, {})
            hidden = {key: value for key, value in hidden.items() if key != folder} if isinstance(hidden, dict) else {}
            kept = list(hidden.items())[-(HIDDEN_FOLDERS - 1):]
            self.actions.set_setting(HIDDEN, dict([*kept, (folder, made.suggested)]))
            return {}

    def name(self, rule: object) -> dict:
        with self._working():
            self._need("tidy", NO_TIDY)
            if not isinstance(rule, dict):
                raise ActionError("A rule is not in the right form")
            return {"folder": self.actions.name(rule)}

    def tidy(self, plan: object, names: object) -> dict:
        with self._working(acting=True) as folder:
            self._need("tidy", NO_TIDY)
            with self.lock:
                made = self.planned
                if (
                    made is None
                    or plan != made.id
                    or made.folder != folder
                    or self.folder != folder
                    or self.clock() - made.made > PLAN_SECONDS
                    or _mark(folder) != made.mark
                ):
                    raise ActionError(STALE)
                wanted = set(_names(names, made.moves))
            moves = [move for name, move in made.moves.items() if name in wanted]
            return self._change(lambda: self.actions.tidy(folder, moves))

    def history(self) -> dict:
        with self._working():
            return {"batches": self.actions.history() if self.actions.able()["tidy"] else []}

    def undo(self, batch: object, names: object = None) -> dict:
        with self._working(acting=True):
            self._need("tidy", NO_TIDY)
            if type(batch) is not int or batch < 1:
                raise ActionError("That run isn't in the history")
            if names is None:
                return self._change(lambda: self.actions.undo(batch))
            _, files = self.actions.run_files(batch)
            wanted = _names(names, {file["name"] for file in files if file["can_put_back"]})
            return self._change(lambda: self.actions.undo(batch, wanted))

    def rules(self) -> dict:
        with self._working():
            self._need("tidy", NO_TIDY)
            return self.actions.rules()

    def save_rules(self, rules: object) -> dict:
        with self._working():
            self._need("tidy", NO_TIDY)
            self.actions.save_rules(rules)
            with self.lock:
                self.planned = None
            return self.actions.rules()

    def auto(self) -> dict:
        with self._working() as folder:
            return self._auto(folder)

    def set_auto(self, on: object, confirmed: object = None) -> dict:
        with self._working(acting=True) as folder:
            if type(on) is not bool:
                raise ActionError("Choose on or off")
            self._need("tidy", NO_AUTO)
            # Turning it off must always be possible
            available, why = self.actions.schedule() if on else (True, "")
            if not available:
                raise ActionError(why or NO_AUTO)
            if on and confirmed is not True:
                raise ActionError(NOT_CONFIRMED)
            self.actions.set_auto(folder, on)
            return self._auto(folder)

    def _auto(self, folder):
        if not self.actions.able()["tidy"]:
            return {"available": False, "why": NO_AUTO, "on": False, "next": None, "waiting": 0, "folder": None}
        return self.actions.auto(folder)

    def _need(self, feature, why):
        if not self.actions.able()[feature]:
            raise ActionError(why)

    def _change(self, work):
        with self.lock:
            self.planned = None
        result = work()
        with self.lock:
            for name in result.done:
                self.paths.pop(name, None)
                self.proven.discard(name)
        return _answer(result)


def same_user_here(client: tuple, port: int) -> bool:
    """On Linux every user can read the link from the browser's command line, so only our own account may use it."""
    if not sys.platform.startswith("linux"):
        # Windows hides other users' command lines; macOS doesn't change files, so the window is read-only there
        return True
    return peer_uid(client[1], port) == os.getuid()


def peer_uid(client_port: int, server_port: int, tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")) -> int | None:
    """The account that owns the other end of a connection to this computer, or None if it can't be found."""
    for table in tables:
        try:
            with open(table, encoding="ascii") as rows:
                next(rows, None)
                for row in rows:
                    fields = row.split()
                    if len(fields) < 8:
                        continue
                    local, remote = fields[1].rpartition(":")[2], fields[2].rpartition(":")[2]
                    if int(local, 16) == client_port and int(remote, 16) == server_port:
                        return int(fields[7])
        except (OSError, ValueError):
            continue
    return None


def _takes(function, name):
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def _within(path, folder):
    """Directly or deeper inside the folder, through real folders only."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return False
        parent, root = os.path.realpath(os.path.dirname(path)), os.path.realpath(folder)
    except OSError:
        return False
    return parent == root or parent.startswith(root.rstrip(os.sep) + os.sep)


def _files(store, batch, kind, folder, places=False):
    """Each file of a run: where it went, where it is now by the records, and whether it can still go back."""
    waiting = {op.id for op in store.to_undo(batch)} if kind != "undo" else set()
    files = []
    for op in store.operations(batch):
        if op.state != "DONE" or op.action not in ("move", "trash"):
            continue
        if kind == "undo":
            name, to, place, back = os.path.basename(op.dest or op.source), "back", op.dest, False
        else:
            back = op.id not in waiting
            name = os.path.basename(op.source)
            if op.action == "trash":
                to = "trash"
            else:
                to = _relative(os.path.dirname(op.dest), folder) if op.dest else ""
            place = op.source if back else op.dest if op.action == "move" else None
        item = {"name": name, "to": to, "back": back, "can_put_back": not back and kind != "undo" and bool(op.dest)}
        if places:
            item["place"] = place
        files.append(item)
        if len(files) >= RUN_FILES:
            break
    return files


def _relative(path, folder):
    try:
        return os.path.relpath(path, folder)
    except ValueError:  # another drive
        return path


def _move(move, proven):
    warning, why = parts(move.file.name, move.why)
    found = {"name": move.file.name, "folder": move.folder, "why": why, "bytes": move.file.size}
    if warning:
        found["warning"] = warning
    if proven:
        found["proven"] = True
    return found


def parts(name: str, why: str) -> tuple[str, str]:
    warning, _, rest = why.rpartition(". ") if why.startswith("Named like") else ("", "", why)
    return (f"{warning}." if warning else ""), reason(name, rest)


def reason(name: str, why: str) -> str:
    """What the file is, instead of where it goes: the folder is already shown next to it."""
    warning, _, rest = why.rpartition(". ") if why.startswith("Named like") else ("", "", why)
    found = re.fullmatch(r"The name contains (.+)", rest)
    if found:
        rest = f"Has {found[1]} in the name"
    elif re.fullmatch(r".+ go to .+", rest, re.S):
        extension = name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""
        kind = _kind(name)
        rest = ENDINGS.get(extension) or KIND_WORDS.get(kind) or (f"A .{extension} file" if extension else rest)
    return f"{warning}. {rest}" if warning else rest


def _kind(name):
    try:
        from downkeep.sorting import kind_of
    except ImportError:
        return None
    return kind_of(name)


def _names(value, known):
    if not isinstance(value, list) or not value or not all(isinstance(name, str) for name in value):
        raise ActionError("Choose at least one file")
    if len(set(value)) != len(value) or any(name not in known for name in value):
        raise ActionError(UNKNOWN)
    return value


def _stay(stay):
    return {"name": stay.name, "why": stay.why}


def _answer(result):
    return {"batch": result.batch, "done": list(result.done), "skipped": [_stay(stay) for stay in result.skipped]}


def _mark(folder):
    try:
        info = os.stat(folder, follow_symlinks=False)
    except OSError as error:
        raise ActionError(STALE) from error
    return info.st_dev, info.st_ino


def _is_folder(path):
    return os.path.isdir(path) and not os.path.islink(path) and not os.path.isjunction(path)


def make_server(app: App) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((HOST, 0), handler)
    server.daemon_threads = True
    return server


def _equal(a, b):
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode())


class _Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "downkeep"
    sys_version = ""
    timeout = 10

    def do_GET(self):
        if not self._from_this_page():
            return
        page = PAGES.get(self.path)
        if page is None:
            return self._json(404, {"error": "not found"})
        name, kind = page
        self._send(200, files("downkeep").joinpath("page", name).read_bytes(), kind)

    def do_POST(self):
        if not self._from_this_page():
            return
        body = self._body()
        if body is None:
            return
        if self.path == "/api/session":
            if not self.app.same_user(self.client_address, self.server.server_address[1]):
                return self._json(403, {"error": OTHER_USER})
            key = self.app.open_session(body.get("code"))
            return self._json(200, {"key": key, "version": __version__}) if key else self._json(403, {"error": "link expired"})
        if not self.app.allows(self.headers.get("X-Downkeep-Key")):
            return self._json(403, {"error": "not allowed"})
        action = {
            "/api/report": self._report,
            "/api/show": self._show,
            "/api/folders": self._folders,
            "/api/choose": self._choose,
            "/api/stay": self._stay,
        }.get(self.path)
        if action:
            return action(body)
        app = self.app
        work = {
            "/api/system": lambda: app.system(),
            "/api/trash": lambda: app.trash(body.get("names")),
            "/api/tidy/plan": lambda: app.plan(),
            "/api/tidy/apply": lambda: app.tidy(body.get("plan"), body.get("names")),
            "/api/history": lambda: app.history(),
            "/api/undo": lambda: app.undo(body.get("batch"), body.get("names")),
            "/api/rules": lambda: app.rules(),
            "/api/rules/save": lambda: app.save_rules(body.get("rules")),
            "/api/rules/name": lambda: app.name(body.get("rule")),
            "/api/tidy/later": lambda: app.later(),
            "/api/auto": lambda: app.auto(),
            "/api/auto/set": lambda: app.set_auto(body.get("on"), body.get("confirmed")),
        }.get(self.path)
        if work is None:
            return self._json(404, {"error": "not found"})
        try:
            value = work()
        except Busy as error:
            return self._json(409, {"error": clean(str(error)) or BUSY, "busy": True})
        except (ActionError, ScanError) as error:
            return self._json(409, {"error": clean(str(error)) or FAILED})
        except Exception:
            return self._json(500, {"error": FAILED})
        self._json(200, value)

    def _report(self, body):
        try:
            self._json(200, self.app.report())
        except ScanError as error:
            self._json(409, {"error": clean(str(error))})
        except Exception:
            self._json(500, {"error": "Couldn't finish checking"})

    def _show(self, body):
        try:
            found = self.app.show_file(body.get("name"), body.get("batch"))
        except OSError:
            return self._json(500, {"error": "Your file manager didn't open"})
        self._json(200, {}) if found else self._json(404, {"error": "That file isn't there any more"})

    def _folders(self, body):
        try:
            self._json(200, {"folders": subfolders(inside(self.app.home, body.get("path")))})
        except ScanError as error:
            self._json(409, {"error": clean(str(error))})

    def _choose(self, body):
        try:
            self.app.choose(body.get("path"))
        except ScanError as error:
            return self._json(409, {"error": clean(str(error))})
        self._json(200, {})

    def _stay(self, body):
        # The page keeps this open. When it closes, however it closes, the next write fails.
        self.app.arrive()
        try:
            self._head(200, "text/plain")
            while True:
                self.wfile.write(b".")
                time.sleep(BEAT_SECONDS)
        except OSError:
            pass
        finally:
            self.app.leave()

    def _from_this_page(self):
        here = f"{HOST}:{self.server.server_address[1]}"
        hosts = self.headers.get_all("Host") or []
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        # A changed Host means DNS rebinding; another Origin or site means another page is asking
        if hosts != [here] or origin not in (None, f"http://{here}") or site not in (None, "same-origin", "none"):
            self._json(403, {"error": "not allowed"})
            return False
        return True

    def _body(self):
        length = self.headers.get("Content-Length", "")
        if self.headers.get_content_type() != "application/json" or not (length.isascii() and length.isdigit()):
            self._json(400, {"error": "bad request"})
            return None
        if int(length) > (MAX_LIST if self.path in LISTS else MAX_BODY):
            self._json(413, {"error": "too large"})
            return None
        try:
            body = json.loads(self.rfile.read(int(length)))
        except (ValueError, RecursionError, OSError):
            body = None
        if not isinstance(body, dict):
            self._json(400, {"error": "bad request"})
            return None
        return body

    def _json(self, status, value):
        self._send(status, json.dumps(value).encode(), "application/json")

    def _send(self, status, content, kind):
        try:
            self._head(status, kind, len(content))
            self.wfile.write(content)
        except OSError:
            pass

    def _head(self, status, kind, length=None):
        self.close_connection = True
        self.send_response(status)
        for name, value in HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", kind)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.end_headers()

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "bad request"})

    def log_message(self, format, *args):
        pass
