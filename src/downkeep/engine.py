import contextlib
import ctypes
import errno
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence

from downkeep import origin
from downkeep.model import ActionError, Identity, Move, Result, Stay
from downkeep.scan import ScanError, _classify, check_folder
from downkeep.store import Op, Store

if sys.platform == "win32":
    fcntl = None
else:
    import fcntl

RENAME_NOREPLACE = 1
MOVEFILE_WRITE_THROUGH = 8
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE = ctypes.c_void_p(-1).value
LONG_PATH = 240

WHY = {
    "links": "It is a link.",
    "folders": "It is a folder.",
    "special files": "It is not a normal file.",
    "hidden or system files": "It is hidden or belongs to the system.",
    "unfinished downloads": "It looks like an unfinished download.",
    "empty files": "It is empty.",
    "changed in the last minute": "It changed in the last minute.",
}
GONE = "It is no longer there."
CHANGED = "It changed after the check."
BUSY = "Another program may be using it."
TWO_NAMES = "It has a second name on this disk (a hard link)."
OTHER_DISK = "It is on another disk."
NOT_YET = "Moving files is not available on this system yet."
RESERVED = {"con", "prn", "aux", "nul", *(f"{port}{n}" for port in ("com", "lpt") for n in range(1, 10))}

ABSENT, MATCH, OTHER = "absent", "match", "other"


class _Recorded(ActionError):
    """A skip that is already in the records."""


def _checkpoint(step: str) -> None:
    """Does nothing. The crash tests replace it to stop the program at every step."""


def _rename_function():
    if sys.platform != "linux":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function


_renameat2 = _rename_function()
SUPPORTED = sys.platform == "win32" or _renameat2 is not None


def move_no_replace(source: str, dest: str) -> None:
    """Moves within one disk, or fails without changing anything. Never follows a link at the end of either folder."""
    if "\0" in source or "\0" in dest:
        raise ActionError("The name has a character that is not allowed.")
    if not SUPPORTED:
        raise ActionError(NOT_YET)
    if sys.platform == "win32":
        _windows_move(source, dest)
    else:
        _linux_move(source, dest)


def _linux_move(source, dest):
    with _held(os.path.dirname(source)) as source_folder, _held(os.path.dirname(dest)) as dest_folder:
        failed = _renameat2(
            source_folder, os.fsencode(os.path.basename(source)),
            dest_folder, os.fsencode(os.path.basename(dest)), RENAME_NOREPLACE,
        )
        if failed:
            raise _linux_error(ctypes.get_errno(), source, dest)


def _linux_error(code, source, dest):
    if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
        return ActionError("This disk has no safe way to move a file without replacing another.")
    if code == errno.EXDEV:
        return ActionError(OTHER_DISK)
    return OSError(code, os.strerror(code), source, None, dest)


@contextlib.contextmanager
def _held(folder) -> Iterator[int]:
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _kernel32():
    from ctypes import wintypes

    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    library.MoveFileExW.restype = wintypes.BOOL
    library.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    library.CreateFileW.restype = wintypes.HANDLE
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    library.CloseHandle.restype = wintypes.BOOL
    return library


def _last_error():
    return ctypes.get_last_error()


def _windows_move(source, dest):
    if not _kernel32().MoveFileExW(_long(source), _long(dest), MOVEFILE_WRITE_THROUGH):
        raise _windows_error(_last_error(), source, dest)


def _windows_error(code, source, dest):
    if code in (80, 183):
        return FileExistsError(errno.EEXIST, "already exists", dest)
    if code in (2, 3):
        return FileNotFoundError(errno.ENOENT, "not found", source)
    if code == 5:
        return PermissionError(errno.EACCES, "access denied", source)
    if code == 17:
        return ActionError(OTHER_DISK)
    if code in (32, 33):
        return ActionError(BUSY)
    return OSError(errno.EIO, f"Windows error {code}", source)


def _long(path):
    if len(path) < LONG_PATH or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def in_use(path: str) -> bool:
    """True if another program may still be writing to it, or if that can't be known."""
    try:
        return _windows_in_use(path) if sys.platform == "win32" else _linux_in_use(path)
    except (OSError, ValueError, AttributeError):
        return True


def _windows_in_use(path):
    library = _kernel32()
    handle = library.CreateFileW(_long(path), GENERIC_READ, 0, None, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle in (None, INVALID_HANDLE):
        return True
    library.CloseHandle(handle)
    return False


def _linux_in_use(path):
    """A read lease is only given when nothing has the file open for writing, even through a memory map."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOATIME
    descriptor = os.open(path, flags)
    try:
        # the default signal for a broken lease would end this program
        fcntl.fcntl(descriptor, fcntl.F_SETSIG, signal.SIGURG)
        fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_RDLCK)
        fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        return False
    finally:
        os.close(descriptor)


def site_of(path: str) -> str | None:
    """The download address the desktop remembers for this path. A move would lose it."""
    program = shutil.which("gio") if sys.platform == "linux" else None
    if not program or not (path.isprintable() or origin._only_odd_bytes(path)):
        return None
    done = _gio([program, "info", "-a", origin.GIO_ATTRIBUTE, "--", os.fsencode(path)])
    prefix = b"  " + origin.GIO_ATTRIBUTE + b": "
    for line in done.splitlines():
        if line.startswith(prefix):
            url = line[len(prefix):].decode("utf-8", "replace")
            return url if len(url) < origin.LIMIT and origin.host_of(url) else None
    return None


def save_site(path: str, url: str) -> None:
    program = shutil.which("gio") if sys.platform == "linux" else None
    if program:
        _gio([program, "set", "--", os.fsencode(path), origin.GIO_ATTRIBUTE, url])


def _gio(command):
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"}, timeout=3,
        ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return b""


def apply_moves(store: Store, folder: str, moves: Sequence[Move], kind: str = "tidy") -> Result:
    if not SUPPORTED:
        raise ActionError(NOT_YET)
    result = Result()
    if not moves:
        return result
    with store.lock():
        _recover(store)
        try:
            root = str(check_folder(folder))
        except ScanError as error:
            raise ActionError(str(error)) from error
        result.batch = store.begin(kind, root)
        _checkpoint("batch started")
        for move in moves:
            source = os.path.join(root, move.file.name)
            with _each(store, result, move.file.name, source):
                _apply(store, result.batch, root, move)
        _remove_empty(store, result.batch, result.batch)
        store.end(result.batch)
        _checkpoint("batch ended")
    return result


def _apply(store, batch, root, move):
    file = move.file
    _check_file(root, file)
    if in_use(file.path):
        raise ActionError(BUSY)
    target = _ready_folder(store, batch, root, move.folder, file.device)
    _check_free(target, file.name)
    _move(store, batch, file.path, os.path.join(target, file.name), Identity.of(file))


def _check_file(root, file):
    """The rules of the scan, again, on what is there now."""
    if os.path.basename(file.name) != file.name or file.path != os.path.join(root, file.name):
        raise ActionError("It is not directly inside the folder.")
    with os.scandir(root) as listing:
        entries = {entry.name: entry for entry in listing}
    if file.name not in entries:
        raise ActionError(GONE)
    reason, info = _classify(entries[file.name], {name.lower() for name in entries}, time.time_ns())
    if reason:
        raise ActionError(WHY[reason])
    if info.st_nlink != 1:
        raise ActionError(TWO_NAMES)
    if _identity(info) != Identity.of(file):
        raise ActionError(CHANGED)


def _ready_folder(store, batch, root, name, device):
    _check_folder_name(name)
    path = os.path.join(root, name)
    if not os.path.lexists(path):
        _make_folder(store, batch, path)
    info = _real_folder(path)
    if info is None:
        raise ActionError(f"{name} is not a normal folder.")
    if info.st_dev != device:
        raise ActionError(OTHER_DISK)
    return path


def _check_folder_name(name):
    odd = not name or name != name.strip() or name.startswith(".") or name.endswith(".")
    unsafe = not name.isprintable() or any(char in '<>:"/\\|?*' for char in name)
    if odd or unsafe or name.split(".")[0].rstrip().lower() in RESERVED:
        raise ActionError(f"{name!r} can't be used as a folder name.")


def _real_folder(path):
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return info if stat.S_ISDIR(info.st_mode) and not os.path.isjunction(path) else None


def _make_folder(store, batch, path):
    op = store.intent(batch, "mkdir", path, None, None)
    _checkpoint("folder planned")
    try:
        os.mkdir(path)
    except OSError:
        store.finish(op, "ABORTED")
        raise
    _checkpoint("folder made")
    _sync(os.path.dirname(path))
    store.finish(op, "DONE", identity=_identity(os.lstat(path)))
    _checkpoint("folder saved")


def _check_free(folder, name):
    """Two names that differ only in case or accents would clash on another system."""
    wanted = _plain(name)
    with os.scandir(folder) as listing:
        if any(_plain(entry.name) == wanted for entry in listing):
            raise ActionError(_taken(folder))


def _plain(name):
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def _taken(folder):
    return f"{os.path.basename(folder)} already has that name."


def _move(store, batch, source, dest, identity, move: Callable[[], None] | None = None):
    site = site_of(source)
    op = store.intent(batch, "move", source, dest, identity)
    _checkpoint("move planned")
    why = ""
    try:
        if _now(source) != identity:
            raise ActionError(CHANGED)
        (move or (lambda: move_no_replace(source, dest)))()
    except FileExistsError:
        why = _taken(os.path.dirname(dest))
    except (ActionError, OSError) as error:
        why = _reason(error)
    _checkpoint("moved")
    try:
        state, reason = ("ABORTED", why) if why and _look(dest, identity) != MATCH else _outcome(source, dest, identity)
        _checkpoint("move checked")
        store.finish(op, state, reason)
    except (ActionError, OSError) as error:
        raise _Recorded(f"Downkeep will check it again the next time it opens. {_reason(error)}") from error
    _checkpoint("move saved")
    if state != "DONE":
        raise _Recorded(reason)
    if site:
        save_site(dest, site)


def _outcome(source, dest, identity):
    state, reason = _judge(_look(source, identity), _look(dest, identity))
    if state == "DONE":
        _sync(os.path.dirname(dest))
        _sync(os.path.dirname(source))
    return state, f"{reason} Look in: {source} and {dest}" if state == "CONFLICT" else reason


def _judge(source: str, dest: str) -> tuple[str, str]:
    """The crash table: what the two places say about a planned move."""
    if source == MATCH and dest != MATCH:
        return "ABORTED", "The move was interrupted. It is still in its place."
    if source != MATCH and dest == MATCH:
        return "DONE", ""
    if source == MATCH:
        return "CONFLICT", "It is in two places, so Downkeep touched neither."
    return "CONFLICT", "Downkeep can't find it where it should be."


def _look(path, identity):
    """Ignores the device: some disks get a new device number after a restart."""
    if _real_folder(os.path.dirname(path)) is None:
        return ABSENT
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT
    same = (info.st_ino, info.st_size, info.st_mtime_ns) == (identity.inode, identity.size, identity.mtime_ns)
    return MATCH if same and stat.S_ISREG(info.st_mode) else OTHER


def _identity(info):
    return Identity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _now(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ActionError(CHANGED)
    if info.st_nlink != 1:
        raise ActionError(TWO_NAMES)
    return _identity(info)


def _sync(folder):
    if sys.platform == "win32":
        return
    with _held(folder) as descriptor:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise


@contextlib.contextmanager
def _each(store, result, name, source):
    """One file that fails never stops the rest."""
    try:
        yield
        result.done.append(name)
    except (ActionError, OSError) as error:
        why = _reason(error)
        result.skipped.append(Stay(name, why))
        if not isinstance(error, _Recorded):
            with contextlib.suppress(ActionError):
                store.skip(result.batch, source, why)


def _reason(error):
    if isinstance(error, ActionError):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return GONE
    if isinstance(error, PermissionError):
        return "Downkeep isn't allowed to move it."
    return f"The system said: {error.strerror or error}."


def _remove_empty(store, batch, made_by):
    for op in store.operations(made_by):
        if op.action == "mkdir" and op.state == "DONE" and op.identity:
            with contextlib.suppress(ActionError, OSError):
                _remove_folder(store, batch, op)


def _remove_folder(store, batch, op):
    if not _ours_and_empty(op):
        return
    removal = store.intent(batch, "rmdir", op.source, None, op.identity)
    _checkpoint("folder removal planned")
    try:
        os.rmdir(op.source)
    except OSError:
        store.finish(removal, "ABORTED")
        return
    _checkpoint("folder removed")
    _sync(os.path.dirname(op.source))
    store.finish(removal, "DONE")


def _ours_and_empty(op):
    info = _real_folder(op.source)
    if info is None or info.st_ino != op.identity.inode:
        return False
    with os.scandir(op.source) as listing:
        return next(listing, None) is None


def undo(store: Store, batch: int) -> Result:
    if not SUPPORTED:
        raise ActionError(NOT_YET)
    result = Result()
    with store.lock():
        _recover(store)
        before = store.batch(batch)
        if before is None or before.kind == "undo":
            raise ActionError("There is no such change to undo.")
        waiting = store.to_undo(batch)
        if not waiting:
            raise ActionError("Everything from that change is already back, or can't be brought back.")
        result.batch = store.begin("undo", before.folder, undoes=batch)
        _checkpoint("batch started")
        for op in waiting:
            with _each(store, result, os.path.basename(op.source), op.dest or op.source):
                _restore(store, result.batch, op) if op.action == "trash" else _put_back(store, result.batch, op)
        _remove_empty(store, result.batch, batch)
        store.end(result.batch)
        _checkpoint("batch ended")
    return result


def _put_back(store, batch, op):
    here, home = os.path.dirname(op.dest), os.path.dirname(op.source)
    if _real_folder(here) is None:
        raise ActionError(f"It is no longer in {os.path.basename(here)}.")
    identity = _still_it(op)
    folder = _real_folder(home)
    if folder is None or os.path.realpath(home) != home:
        raise ActionError("The folder it came from is no longer there.")
    if folder.st_dev != identity.device:
        raise ActionError(OTHER_DISK)
    if in_use(op.dest):
        raise ActionError(BUSY)
    _check_free(home, os.path.basename(op.source))
    # first, so a crash can't let automatic move it again
    store.leave(identity)
    _move(store, batch, op.dest, op.source, identity)


def _still_it(op):
    if not os.path.lexists(op.dest):
        raise ActionError(f"It is no longer in {os.path.basename(os.path.dirname(op.dest))}.")
    identity = _now(op.dest)
    # an edit keeps the inode, and the device number can change after a restart, but not both
    if identity.inode != op.identity.inode or (
        identity.device != op.identity.device and _look(op.dest, op.identity) != MATCH
    ):
        raise ActionError("A different file is there now.")
    return identity


def _restore(store, batch, op):
    from downkeep import trash

    if not op.dest or not getattr(trash, "CAN_RESTORE", False):
        raise ActionError("Downkeep can't bring it back here. Open the Trash or Recycle Bin to restore it.")
    identity = _still_it(op)
    _move(store, batch, op.dest, op.source, identity, lambda: trash.restore(op.source, op.dest, identity))


def recover(store: Store) -> Result:
    """Settles what a crash left open. Only reads the files; a move is never continued or reversed."""
    with store.lock():
        return _recover(store)


def _recover(store):
    result = Result()
    for op in store.open_intents():
        name = os.path.basename(op.source)
        try:
            state, reason, dest = _settle(op)
        except OSError as error:
            result.skipped.append(Stay(name, _reason(error)))
            continue
        _checkpoint("settled")
        store.finish(op.id, state, reason, dest)
        _checkpoint("settlement saved")
        if state == "DONE":
            result.done.append(name)
        elif reason:
            result.skipped.append(Stay(name, reason))
        store.end(op.batch)
    return result


def _settle(op: Op) -> tuple[str, str, str | None]:
    if op.action == "mkdir":
        return _settle_mkdir(op)
    if op.action == "rmdir":
        info = _real_folder(op.source)
        return ("ABORTED" if info and info.st_ino == op.identity.inode else "DONE"), "", None
    if op.action == "trash" and not op.dest:
        return _settle_trash(op)
    return *_outcome(op.source, op.dest, op.identity), None


def _settle_mkdir(op):
    if os.path.lexists(op.source):
        return "CONFLICT", "Downkeep can't be sure it made this folder, so it will never remove it.", None
    return "ABORTED", "", None


def _settle_trash(op):
    from downkeep import trash

    if _look(op.source, op.identity) == MATCH:
        return "ABORTED", "It was not moved to the Trash. It is still in its place.", None
    find = getattr(trash, "find", None)
    place = find(op.source, op.identity) if find else None
    if place and _look(place, op.identity) == MATCH:
        return "DONE", "", place
    return "CONFLICT", f"It was being moved to the Trash and may be there now. It was at {op.source}", None
