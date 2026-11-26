import ctypes
import itertools
import ntpath
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from urllib.parse import quote, unquote_to_bytes

from downkeep.evidence import is_trash
from downkeep.model import ActionError, Identity

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32" and sys.maxsize > 2**32
SUPPORTED = LINUX or WINDOWS
CAN_RESTORE = LINUX

NOTE = ".trashinfo"
NAME_BYTES = 255
TRIES = 10_000


def send(path: str, identity: Identity) -> str:
    """Moves one file to the Trash and says where it went ("" in the Windows Recycle Bin). Never deletes."""
    if not SUPPORTED:
        raise ActionError("The Trash is not available on this system yet.")
    _check(path, identity)
    return _recycle(path, identity) if WINDOWS else _to_trash(path, identity)


def restore(original: str, place: str, identity: Identity) -> None:
    if not CAN_RESTORE:
        raise ActionError("Open the Recycle Bin to put it back.")
    files, name = os.path.split(place)
    if os.path.basename(files) != "files" or not is_same(place, identity):
        raise ActionError("It isn't in the Trash any more.")
    folder = os.path.dirname(original)
    if not os.path.isdir(folder) or not _plain_folder(original):
        raise ActionError("Its folder isn't there any more.")
    try:
        _move(place, original, lambda moved: is_same(moved, identity))
    except FileExistsError as error:
        raise ActionError("Another file has that name now.") from error
    _flush(folder)
    _flush(files)
    _drop_note(os.path.dirname(files), name, original)


def drop_notes(original: str) -> None:
    """After a crash between writing a note and the move: removes notes for this file that has no file in the Trash."""
    for trash in _trashes(original) if LINUX else ():
        try:
            names = sorted(os.listdir(os.path.join(trash, "info")))
        except OSError:
            continue
        for name in names:
            if name.endswith(NOTE):
                _drop_note(trash, name.removesuffix(NOTE), original)


def _drop_note(trash, name, original):
    note = os.path.join(trash, "info", name + NOTE)
    if not os.path.lexists(os.path.join(trash, "files", name)) and _noted(note) == original:
        os.unlink(note)


def find(original: str, identity: Identity) -> str | None:
    """Where this exact file is in our Trash, for recovery after a crash."""
    for trash in _trashes(original) if LINUX else ():
        try:
            for name in sorted(os.listdir(os.path.join(trash, "files"))):
                place = os.path.join(trash, "files", name)
                found = _identity(place)
                # Some disks get a new device number after a restart
                if found and replace(found, device=identity.device) == identity:
                    return place
        except OSError:
            continue
    return None


def holds(path: str) -> bool:
    """True if this path is inside any Trash, so it can't count as a copy that stays."""
    path = os.path.realpath(path)
    home = os.path.realpath(_home_trash())
    return path.startswith(home + os.sep) or path == home or any(map(is_trash, path.split(os.sep)))


def is_same(path: str, identity: Identity) -> bool:
    found = _identity(path)
    return found is not None and (found.device, found.inode) == (identity.device, identity.inode)


def _identity(path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return Identity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _check(path, identity):
    if not os.path.lexists(path):
        raise ActionError("It isn't there any more.")
    if not _plain_folder(path):
        raise ActionError("Its folder is reached through a link.")
    if _identity(path) != identity:
        raise ActionError("It changed since it was checked.")
    if os.lstat(path).st_nlink != 1:
        raise ActionError("It has more than one name on this disk.")


def _plain_folder(path):
    folder = os.path.dirname(path)
    return os.path.isabs(path) and os.path.normcase(os.path.realpath(folder)) == os.path.normcase(folder)


# Linux: the freedesktop.org Trash specification


def _to_trash(path, identity):
    trash, top = _trash_for(path, identity.device)
    files, info = os.path.join(trash, "files"), os.path.join(trash, "info")
    recorded = os.path.relpath(path, top) if top else path
    date = time.strftime("%Y-%m-%dT%H:%M:%S")
    text = f"[Trash Info]\nPath={quote(os.fsencode(recorded))}\nDeletionDate={date}\n".encode()
    for name in _names(os.path.basename(path)):
        place, note = os.path.join(files, name), os.path.join(info, name + NOTE)
        if not _write_new(note, text):
            continue
        _flush(info)
        try:
            if os.path.lexists(place):
                raise FileExistsError(place)
            _check(path, identity)
            _move(path, place, lambda moved: _identity(moved) == identity)
        except FileExistsError:
            os.unlink(note)
            continue
        except BaseException:
            if not os.path.lexists(place):
                os.unlink(note)
            raise
        _flush(files)
        _flush(os.path.dirname(path))
        return place
    raise ActionError("The Trash has too many files with this name.")


def _move(source: str, dest: str, is_right: Callable[[str], bool]) -> None:
    """A move that never replaces a file, and is taken back if something else turns out to have been moved."""
    try:
        from downkeep.engine import move_no_replace
    except ImportError as error:
        raise ActionError("Moving files safely is not available here.") from error
    move_no_replace(source, dest)
    if is_right(dest):
        return
    try:
        move_no_replace(dest, source)
    except OSError as error:
        raise ActionError(f"It changed at the last moment and is now at {dest}.") from error
    raise ActionError("It changed at the last moment, so it was put back.")


def _home_trash():
    data = os.environ.get("XDG_DATA_HOME", "")
    return os.path.join(data if os.path.isabs(data) else os.path.expanduser("~/.local/share"), "Trash")


def _trashes(path):
    top, user = _top(path), os.getuid()
    return [_home_trash(), os.path.join(top, ".Trash", str(user)), os.path.join(top, f".Trash-{user}")]


def _trash_for(path, device):
    """The Trash folder on the same disk, and the disk's top folder unless it is the home Trash."""
    home, shared, own = _trashes(path)
    if os.stat(_existing(home)).st_dev == device:
        return _ready(home, device), None
    top = _top(path)
    choices = [shared, own] if _is_shared(os.path.dirname(shared)) else [own]
    for trash in choices:
        try:
            return _ready(trash, device), top
        except (ActionError, OSError):
            continue
    raise ActionError("This disk has no Trash.")


def _is_shared(folder):
    try:
        mode = os.lstat(folder).st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode) and bool(mode & stat.S_ISVTX)


def _ready(trash, device):
    os.makedirs(os.path.dirname(trash), 0o700, exist_ok=True)
    for folder in (trash, os.path.join(trash, "files"), os.path.join(trash, "info")):
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(folder)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_dev != device:
            raise ActionError("The Trash folder is not set up in the usual way.")
    return trash


def _existing(path):
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    return path


def _top(path):
    here = _existing(os.path.dirname(path))
    device = os.stat(here).st_dev
    while here != os.path.dirname(here) and os.stat(os.path.dirname(here)).st_dev == device:
        here = os.path.dirname(here)
    return here


def _names(name: str) -> Iterator[str]:
    stem, last = os.path.splitext(name)
    if len(os.fsencode(last)) > 20:
        stem, last = name, ""
    for number in itertools.islice(itertools.count(1), TRIES):
        tail = f" {number}{last}" if number > 1 else last
        short = stem
        while len(os.fsencode(short + tail + NOTE)) > NAME_BYTES:
            short = short[:-1]
        yield short + tail


def _write_new(note, text):
    try:
        descriptor = os.open(note, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return False
    try:
        with open(descriptor, "wb") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(descriptor)
    except BaseException:
        os.unlink(note)
        raise
    return True


def _noted(note):
    try:
        with open(note, "rb") as stream:
            path = re.search(rb"^Path=(.+)$", stream.read(), re.MULTILINE)
    except OSError:
        return None
    return os.path.join(_top(note), os.fsdecode(unquote_to_bytes(path[1]))) if path else None


def _flush(folder):
    try:
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


# Windows: the Recycle Bin, through IFileOperation

DRIVE_FIXED = 3
EXPLORER = r"Software\Microsoft\Windows\CurrentVersion"
FLAGS = (
    0x0004  # FOF_SILENT
    | 0x0010  # FOF_NOCONFIRMATION
    | 0x0040  # FOF_ALLOWUNDO
    | 0x0400  # FOF_NOERRORUI
    | 0x4000  # FOF_WANTNUKEWARNING
    | 0x00080000  # FOFX_RECYCLEONDELETE
)
WILL_RECYCLE = 0x80  # TSF_DELETE_RECYCLE_IF_POSSIBLE
S_OK, E_ABORT, E_NOINTERFACE = 0, -0x7FFFBFFC, -0x7FFFBFFE
UNKNOWN = "The Recycle Bin settings of this disk couldn't be read."
APARTMENT, ANY_CONTEXT, FILE_PATH = 0x2 | 0x4, 0x17, 0x80058000


@dataclass(frozen=True, slots=True)
class Bin:
    """What Windows says about the Recycle Bin of one disk. None means it couldn't be read."""

    fixed: bool
    limited: bool | None
    off: int | None
    capacity: int | None
    used: int | None


def why_not(path: str, size: int, info: Bin) -> str | None:
    """Windows deletes for good when the bin can't take a file, so anything unknown is a no."""
    if not _plain_windows_path(path):
        return "Its name or place is one the Recycle Bin can't be trusted with."
    if not info.fixed:
        return "It isn't on a built-in disk, so Windows might delete it for good."
    if None in (info.limited, info.off, info.capacity, info.used):
        return UNKNOWN
    if info.off or info.limited:
        return "The Recycle Bin is turned off or limited on this computer."
    if size > info.capacity:
        return "It is larger than the Recycle Bin allows."
    if info.used + size > info.capacity:
        return "The Recycle Bin is too full, and Windows would delete older things from it."
    return None


def _plain_windows_path(path):
    drive, rest = ntpath.splitdrive(path)
    parts = rest.split("\\")[1:]
    return (
        bool(re.fullmatch("[A-Za-z]:", drive))
        and rest.startswith("\\")
        and len(path) <= 259
        and all(parts)
        and not any(part[-1] in ". " or set(part) & set('<>:"/|?*') or min(part) < " " for part in parts)
    )


def _read_bin(path):
    kernel32, shell32 = _dll("kernel32"), _dll("shell32")
    root = ntpath.splitdrive(path)[0] + "\\"
    volume = ctypes.create_unicode_buffer(64)
    mount = ctypes.create_unicode_buffer(300)
    if not kernel32.GetVolumePathNameW(ctypes.c_wchar_p(path), mount, 300) or mount.value.lower() != root.lower():
        return Bin(False, None, None, None, None)
    fixed = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_FIXED
    off = capacity = None
    if kernel32.GetVolumeNameForVolumeMountPointW(ctypes.c_wchar_p(root), volume, 64):
        key = rf"{EXPLORER}\Explorer\BitBucket\Volume\{volume.value[volume.value.find('{'):].rstrip('\\')}"
        off = _number("HKEY_CURRENT_USER", key, "NukeOnDelete")
        capacity = _number("HKEY_CURRENT_USER", key, "MaxCapacity")
    query = _BinQuery(ctypes.sizeof(_BinQuery), 0, 0)
    asked = shell32.SHQueryRecycleBinW(ctypes.c_wchar_p(root), ctypes.byref(query))
    megabyte = 1024 * 1024
    return Bin(fixed, _limited(), off, capacity and capacity * megabyte, query.size if asked == S_OK else None)


def _limited():
    try:
        return any(
            _number(hive, rf"{EXPLORER}\Policies\Explorer", name) not in fine
            for hive in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE")
            for name, fine in (("NoRecycleFiles", (None, 0)), ("RecycleBinSize", (None,)))
        )
    except OSError:
        return None


def _number(hive, key, name):
    import winreg

    try:
        access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        with winreg.OpenKey(getattr(winreg, hive), key, 0, access) as opened:
            value, kind = winreg.QueryValueEx(opened, name)
    except FileNotFoundError:
        return None
    if kind != winreg.REG_DWORD:
        raise OSError("not a number")
    return value


def _recycle(path, identity):
    try:
        reason = why_not(path, identity.size, _read_bin(path))
    except OSError:
        reason = UNKNOWN
    if reason:
        raise ActionError(reason)
    recycled = _delete_to_bin(path, identity)
    if os.path.lexists(path):
        raise ActionError("Windows left it where it was.")
    if not recycled:
        raise ActionError("Windows removed it but didn't say it is in the Recycle Bin.")
    return ""


def _delete_to_bin(path, identity):
    ole32, shell32 = _dll("ole32"), _dll("shell32")
    _ok(ole32.CoInitializeEx(None, APARTMENT))
    operation, item = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        _ok(
            ole32.CoCreateInstance(
                _guid("3ad05575-8857-4850-9277-11b85bdb8e09"), None, ANY_CONTEXT,
                _guid("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"), ctypes.byref(operation),
            )
        )
        _ok(
            shell32.SHCreateItemFromParsingName(
                ctypes.c_wchar_p(path), None, _guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), ctypes.byref(item)
            )
        )
        if _identity(_item_path(ole32, item)) != identity:
            raise ActionError("It changed since it was checked.")
        sink, cookie, aborted = _Sink(), ctypes.c_uint32(), ctypes.c_int32(1)
        _ok(_com(operation, 5, (ctypes.c_uint32,), FLAGS))  # SetOperationFlags
        _ok(_com(operation, 3, (ctypes.c_void_p, ctypes.c_void_p), sink.pointer, ctypes.byref(cookie)))  # Advise
        try:
            _ok(_com(operation, 18, (ctypes.c_void_p, ctypes.c_void_p), item, None))  # DeleteItem
            _check(path, identity)
            _ok(_com(operation, 21))  # PerformOperations
            _ok(_com(operation, 22, (ctypes.c_void_p,), ctypes.byref(aborted)))  # GetAnyOperationsAborted
        finally:
            _com(operation, 4, (ctypes.c_uint32,), cookie)  # Unadvise
        return sink.recycled and not aborted.value
    finally:
        for thing in (item, operation):
            if thing:
                _com(thing, 2)  # Release
        ole32.CoUninitialize()


def _item_path(ole32, item):
    text = ctypes.c_void_p()
    _ok(_com(item, 5, (ctypes.c_uint32, ctypes.c_void_p), FILE_PATH, ctypes.byref(text)))  # GetDisplayName
    try:
        return ctypes.wstring_at(text.value)
    finally:
        ole32.CoTaskMemFree(text)


class _Sink:
    """The COM object Windows reports to. It refuses anything that would not go to the Recycle Bin."""

    METHODS = (
        "QueryInterface:pp AddRef Release StartOperations FinishOperations:i PreRenameItem:upp PostRenameItem:uppip "
        "PreMoveItem:uppp PostMoveItem:upppip PreCopyItem:uppp PostCopyItem:upppip PreDeleteItem:up "
        "PostDeleteItem:upip PreNewItem:upp PostNewItem:upppuip UpdateProgress:uu ResetTimer PauseTimer ResumeTimer"
    ).split()
    TYPES = {"p": ctypes.c_void_p, "u": ctypes.c_uint32, "i": ctypes.c_int32}
    KNOWN = ("00000000-0000-0000-c000-000000000046", "04b0f1a7-9490-44bc-96e1-4296a31252e2")
    kept = []  # Windows may hold on to a sink after the work is done, so they are never freed

    def __init__(self):
        self.recycled = False
        self.calls = []
        for method in self.METHODS:
            name, _, kinds = method.partition(":")
            arguments = [self.TYPES[kind] for kind in kinds]
            self.calls.append(_prototype(*arguments)(getattr(self, name, self.other)))
        self.table = (ctypes.c_void_p * len(self.calls))(*(ctypes.cast(call, ctypes.c_void_p) for call in self.calls))
        self.object = ctypes.c_void_p(ctypes.addressof(self.table))
        self.pointer = ctypes.addressof(self.object)
        self.kept.append(self)

    def QueryInterface(self, this, wanted, answer):
        known = bytes((ctypes.c_char * 16).from_address(wanted)) in [uuid.UUID(name).bytes_le for name in self.KNOWN]
        ctypes.c_void_p.from_address(answer).value = this if known else None
        return S_OK if known else E_NOINTERFACE

    def AddRef(self, this):
        return 1

    Release = AddRef

    def PreDeleteItem(self, this, flags, item):
        return S_OK if flags & WILL_RECYCLE else E_ABORT

    def PostDeleteItem(self, this, flags, item, result, new):
        self.recycled = result >= 0 and bool(new)
        return S_OK

    def other(self, this, *rest):
        return S_OK


class _BinQuery(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("size", ctypes.c_int64), ("count", ctypes.c_int64)]


def _dll(name):
    return ctypes.WinDLL(name)


def _prototype(*arguments):
    make = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    return make(ctypes.c_int32, ctypes.c_void_p, *arguments)


def _com(thing, index, types=(), *arguments):
    table = ctypes.cast(thing, ctypes.POINTER(ctypes.c_void_p))[0]
    method = ctypes.cast(table, ctypes.POINTER(ctypes.c_void_p))[index]
    return _prototype(*types)(method)(thing, *arguments)


def _guid(text):
    return ctypes.create_string_buffer(uuid.UUID(text).bytes_le, 16)


def _ok(result):
    if result < 0:
        raise ActionError(f"Windows couldn't move it to the Recycle Bin (code {result & 0xFFFFFFFF:08x}).")
