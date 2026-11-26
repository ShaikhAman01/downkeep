import errno
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from downkeep.model import ActionError, Busy, Identity
from downkeep.scan import FileRecord

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

KINDS = ("tidy", "auto", "trash", "undo")
ACTIONS = ("move", "mkdir", "rmdir", "trash")
FINISHED = ("DONE", "ABORTED", "CONFLICT")
NAMES_SHOWN = 200
MOST_SEEN = 100_000
WAIT_TRIES = 200

# Device and inode are text because Windows numbers can be too big for SQLite.
MIGRATIONS = (
    (
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, folder BLOB NOT NULL,
            undoes INTEGER REFERENCES batches(id), started_ns INTEGER NOT NULL, ended_ns INTEGER)""",
        """CREATE TABLE operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch INTEGER NOT NULL REFERENCES batches(id),
            action TEXT NOT NULL, source BLOB NOT NULL, dest BLOB,
            device TEXT, inode TEXT, size INTEGER, mtime_ns INTEGER,
            state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'ABORTED', 'CONFLICT')),
            reason TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX operations_by_batch ON operations(batch)",
        "CREATE INDEX operations_open ON operations(id) WHERE state = 'INTENT'",
        "CREATE INDEX batches_by_undoes ON batches(undoes)",
        """CREATE TABLE seen (
            id INTEGER PRIMARY KEY, device TEXT NOT NULL, inode TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
            name BLOB, first_ns INTEGER NOT NULL, stays INTEGER NOT NULL DEFAULT 0, UNIQUE (inode, device))""",
    ),
    ("ALTER TABLE operations ADD COLUMN other_file INTEGER NOT NULL DEFAULT 0",),
)


@dataclass(frozen=True, slots=True)
class Op:
    id: int
    batch: int
    action: str
    source: str
    dest: str | None
    identity: Identity | None
    state: str
    reason: str
    other_file: bool = False


@dataclass(frozen=True, slots=True)
class Batch:
    id: int
    kind: str
    folder: str
    undoes: int | None
    started_ns: int = 0


def data_folder() -> Path:
    chosen = os.environ.get("DOWNKEEP_DATA")
    if chosen:
        return Path(chosen)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Downkeep"
    base = Path(os.environ.get("XDG_STATE_HOME") or "")
    return (base if base.is_absolute() else Path.home() / ".local" / "state") / "downkeep"


class Store:
    """One connection shared by every thread of this process: each use holds the guard."""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder else data_folder()
        self.path = self.folder / "downkeep.db"
        self._guard = threading.RLock()
        try:
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            _private(self.folder)
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
            self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        except (OSError, sqlite3.Error) as error:
            raise ActionError(f"Downkeep can't keep its records in {self.folder}: {error}") from error
        try:
            self._open()
        except BaseException:
            self._db.close()
            raise

    def _open(self):
        for left in reversed(range(WAIT_TRIES)):
            try:
                return self._prepare()
            except sqlite3.Error as error:
                # SQLite doesn't wait by itself when two programs first open the file together
                code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                if left and code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    time.sleep(0.05)
                    continue
                raise ActionError(
                    f"Downkeep can't read its own records ({error}). It changed nothing and left them at {self.path}"
                ) from error

    def _prepare(self):
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        (found,) = self._db.execute("PRAGMA quick_check(1)").fetchone()
        if found != "ok":
            raise ActionError(
                f"Downkeep's own records are damaged ({found}). It changed nothing and left them at {self.path}"
            )
        self._migrate()

    def _migrate(self):
        with self._transaction():
            (version,) = self._db.execute("PRAGMA user_version").fetchone()
            if version > len(MIGRATIONS):
                raise ActionError("These records were made by a newer Downkeep. Update Downkeep and try again.")
            for statement in (line for step in MIGRATIONS[version:] for line in step):
                self._db.execute(statement)
            self._db.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        with self._guard:
            self._db.close()

    def _run(self, sql, *values):
        try:
            with self._guard:
                return self._db.execute(sql, values).fetchall()
        except sqlite3.Error as error:
            raise ActionError(f"Downkeep couldn't use its own records: {error}") from error

    @contextmanager
    def _transaction(self):
        with self._guard:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    @contextmanager
    def lock(self) -> Iterator[None]:
        """The system drops the lock when the process ends, even if it was killed."""
        try:
            descriptor = os.open(self.folder / "downkeep.lock", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise ActionError(f"Downkeep can't lock its records in {self.folder}: {error}") from error
        try:
            _grab(descriptor)
            try:
                yield
            finally:
                _let_go(descriptor)
        finally:
            os.close(descriptor)

    def get(self, key: str, default: object = None) -> object:
        rows = self._run("SELECT value FROM settings WHERE key = ?", key)
        return json.loads(rows[0][0]) if rows else default

    def set(self, key: str, value: object) -> None:
        self._run(
            "INSERT INTO settings VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            key, json.dumps(value),
        )

    def begin(self, kind: str, folder: str | os.PathLike, undoes: int | None = None) -> int:
        _expect(kind, KINDS)
        ((batch,),) = self._run(
            "INSERT INTO batches (kind, folder, undoes, started_ns) VALUES (?, ?, ?, ?) RETURNING id",
            kind, os.fsencode(folder), undoes, time.time_ns(),
        )
        return batch

    def end(self, batch: int) -> None:
        self._run("UPDATE batches SET ended_ns = ? WHERE id = ? AND ended_ns IS NULL", time.time_ns(), batch)

    def batch(self, batch: int) -> Batch | None:
        for number, kind, folder, undoes, started_ns in self._run(
            "SELECT id, kind, folder, undoes, started_ns FROM batches WHERE id = ?", batch
        ):
            return Batch(number, kind, os.fsdecode(folder), undoes, started_ns)
        return None

    def intent(self, batch: int, action: str, source: str, dest: str | None, identity: Identity | None) -> int:
        return self._add(batch, action, source, dest, identity, "INTENT", "")

    def skip(self, batch: int, source: str, why: str) -> int:
        """Remembers a file that was left alone before anything was tried."""
        return self._add(batch, "move", source, None, None, "ABORTED", why)

    def _add(self, batch, action, source, dest, identity, state, reason):
        _expect(action, ACTIONS)
        ((op,),) = self._run(
            "INSERT INTO operations (batch, action, source, dest, device, inode, size, mtime_ns, state, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            batch, action, os.fsencode(source), _bytes(dest), *_columns(identity), state, reason,
        )
        return op

    def finish(
        self, op: int, state: str, reason: str = "", dest: str | None = None, identity: Identity | None = None,
        other_file: bool = False,
    ) -> None:
        """other_file: a different file than the one checked went to dest, and identity is that file's."""
        _expect(state, FINISHED)
        device, inode, size, mtime_ns = _columns(identity)
        changed = self._run(
            "UPDATE operations SET state = ?, reason = ?, other_file = ?, dest = COALESCE(?, dest),"
            " device = COALESCE(?, device),"
            " inode = COALESCE(?, inode), size = COALESCE(?, size), mtime_ns = COALESCE(?, mtime_ns)"
            " WHERE id = ? AND state = 'INTENT' RETURNING id",
            state, reason, int(other_file), _bytes(dest), device, inode, size, mtime_ns, op,
        )
        if not changed:
            raise ValueError(f"operation {op} is not open")

    def operations(self, batch: int) -> list[Op]:
        return self._ops("batch = ?", batch)

    def open_intents(self) -> list[Op]:
        return self._ops("state = 'INTENT'")

    def _ops(self, where, *values):
        rows = self._run(
            "SELECT id, batch, action, source, dest, device, inode, size, mtime_ns, state, reason, other_file"
            f" FROM operations WHERE {where} ORDER BY id", *values,
        )
        return [_op(row) for row in rows]

    def to_undo(self, batch: int) -> list[Op]:
        """Finished moves of this batch that no undo has brought back yet, newest first.
        A different file that was moved in place of the checked one can be brought back too."""
        back = {
            row[0]
            for row in self._run(
                "SELECT dest FROM operations JOIN batches ON batches.id = operations.batch"
                " WHERE undoes = ? AND action = 'move' AND state = 'DONE'", batch,
            )
        }
        moved = [
            op for op in self.operations(batch)
            if op.action in ("move", "trash") and (op.state == "DONE" or (op.state == "CONFLICT" and op.other_file))
        ]
        return [op for op in reversed(moved) if os.fsencode(op.source) not in back]

    def history(self, limit: int = 30) -> list[dict]:
        """Newest first. Batches that moved nothing and hit no conflict are left out."""
        rows = self._run(
            "SELECT id, kind, folder, started_ns FROM batches WHERE EXISTS (SELECT 1 FROM operations"
            " WHERE batch = batches.id AND (state = 'CONFLICT' OR (state = 'DONE' AND action IN ('move', 'trash'))))"
            " ORDER BY id DESC LIMIT ?", limit,
        )
        return [self._story(*row) for row in rows]

    def _story(self, batch, kind, folder, started_ns):
        ops = self.operations(batch)
        done = [op for op in ops if op.state == "DONE" and op.action in ("move", "trash")]
        names = [os.path.basename(op.dest if kind == "undo" else op.source) for op in done[:NAMES_SHOWN]]
        left = [
            {"name": os.path.basename(op.source), "why": op.reason}
            for op in ops if op.state in ("ABORTED", "CONFLICT") and op.reason
        ]
        waiting = [] if kind == "undo" else self.to_undo(batch)
        return {
            "batch": batch, "kind": kind, "when": started_ns // 1_000_000_000, "folder": os.fsdecode(folder),
            "count": len(done), "names": names, "left": left,
            "undone": bool(done) and kind != "undo" and not waiting,
            "can_undo": any(op.dest for op in waiting),
        }

    def first_seen(self, file: Identity | FileRecord) -> int | None:
        row = self._find(file)
        return row[1] if row else None

    def remember(self, files: Iterable[Identity | FileRecord], ns: int) -> None:
        with self._changing():
            for file in files:
                self._note(file, ns)

    def leave(self, file: Identity | FileRecord) -> None:
        with self._changing():
            self._run("UPDATE seen SET stays = 1 WHERE id = ?", self._note(file, time.time_ns()))

    def is_left(self, file: Identity | FileRecord) -> bool:
        row = self._find(file)
        return bool(row and row[2])

    def prune(self, files: Iterable[Identity | FileRecord]) -> None:
        """Forgets every file that is not in the list."""
        with self._changing():
            keep = {row[0] for row in map(self._find, files) if row}
            for (number,) in self._run("SELECT id FROM seen"):
                if number not in keep:
                    self._run("DELETE FROM seen WHERE id = ?", number)

    @contextmanager
    def _changing(self):
        try:
            with self._transaction():
                yield
        except sqlite3.Error as error:
            raise ActionError(f"Downkeep couldn't use its own records: {error}") from error

    def _find(self, file):
        """Inode numbers are reused: a file with a new date and a new name is a new file."""
        name = _name(file)
        for number, first, stays, device, mtime_ns, old in self._run(
            "SELECT id, first_ns, stays, device, mtime_ns, name FROM seen WHERE inode = ? ORDER BY device = ? DESC",
            str(file.inode), str(file.device),
        ):
            same_time = mtime_ns == file.mtime_ns
            same_name = name is None or old is None or name == old
            # some disks get a new device number after a restart
            here = device == str(file.device)
            known = same_name and name is not None and old is not None
            if known or (same_time and (here or same_name)) or (here and same_name):
                return number, first, stays
        return None

    def _note(self, file, ns):
        self._run("DELETE FROM seen WHERE id <= (SELECT max(id) FROM seen) - ?", MOST_SEEN - 1)
        row = self._find(file)
        place = str(file.device), str(file.inode)
        self._run("DELETE FROM seen WHERE device = ? AND inode = ? AND id IS NOT ?", *place, row and row[0])
        if row:
            self._run(
                "UPDATE seen SET device = ?, mtime_ns = ?, name = COALESCE(?, name) WHERE id = ?",
                place[0], file.mtime_ns, _name(file), row[0],
            )
            return row[0]
        ((number,),) = self._run(
            "INSERT INTO seen (device, inode, mtime_ns, name, first_ns) VALUES (?, ?, ?, ?, ?) RETURNING id",
            *place, file.mtime_ns, _name(file), ns,
        )
        return number


def _private(folder):
    if sys.platform != "win32" and folder.stat().st_uid == os.geteuid():
        folder.chmod(0o700)


def _grab(descriptor):
    try:
        if sys.platform == "win32":
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise Busy("Downkeep is already moving files. Try again in a moment.") from error
        raise ActionError(f"Downkeep can't lock its records: {error}") from error


def _let_go(descriptor):
    if sys.platform == "win32":
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _expect(value, allowed):
    if value not in allowed:
        raise ValueError(f"{value!r} is not one of {allowed}")


def _bytes(path):
    return None if path is None else os.fsencode(path)


def _name(file):
    return _bytes(getattr(file, "name", None))


def _columns(identity):
    if identity is None:
        return None, None, None, None
    return str(identity.device), str(identity.inode), identity.size, identity.mtime_ns


def _op(row):
    number, batch, action, source, dest, device, inode, size, mtime_ns, state, reason, other_file = row
    identity = None if inode is None else Identity(int(device), int(inode), size, mtime_ns)
    return Op(
        number, batch, action, os.fsdecode(source), None if dest is None else os.fsdecode(dest),
        identity, state, reason, bool(other_file),
    )
