import hashlib
import os
import stat
import struct
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath

from downkeep.scan import FileRecord

HEAD = 65_536
BLOCK = 1024 * 1024
MAX_MEMBERS = 10_000
MAX_DIRECTORY = 16 * 1024 * 1024
MAX_INDEX = 200_000
MAX_CANDIDATES = 20
EXAMPLES = 3
NEVER_ENTER = {".git", ".hg", ".venv", "__pycache__", "node_modules", "venv"}


@dataclass(frozen=True, slots=True)
class Duplicates:
    files: tuple[FileRecord, ...]

    @property
    def wasted(self) -> int:
        return sum(file.size for file in self.files[1:])


@dataclass(frozen=True, slots=True)
class Extracted:
    archive: FileRecord
    folder: str
    members: int
    names: tuple[str, ...] = ()


class Changed(Exception):
    pass


def find_duplicates(files: Iterable[FileRecord]) -> list[Duplicates]:
    groups = _same(_one_per_inode(files), lambda file: file.size)
    groups = [smaller for group in groups for smaller in _same(group, lambda file: _hash(file, HEAD))]
    groups = [same for group in groups for same in _same(group, _hash)]
    found = [Duplicates(tuple(sorted(group, key=lambda file: (file.mtime_ns, file.name)))) for group in groups]
    return sorted(found, key=lambda group: (-group.wasted, group.files[0].name))


def find_extracted(files: Iterable[FileRecord], folder: str) -> list[Extracted]:
    archives = [file for file in files if file.extension == ".zip"]
    if not archives:
        return []
    sizes = _index(folder)
    by_name = defaultdict(list)
    for path, size in sizes.items():
        by_name[os.path.basename(path), size].append(path)
    found = []
    for archive in archives:
        members = _members(archive)
        where = _find_folder(members, sizes, by_name) if members else None
        if where:
            examples = tuple(parts[-1] for parts in sorted(members)[:EXAMPLES])
            found.append(Extracted(archive, where, len(members), examples))
    return sorted(found, key=lambda item: (-item.archive.size, item.archive.name))


def _one_per_inode(files):
    return list({(file.device, file.inode): file for file in files}.values())


def _same(files, key):
    groups = defaultdict(list)
    for file in files:
        try:
            groups[key(file)].append(file)
        except (Changed, OSError):
            continue
    return [group for group in groups.values() if len(group) > 1]


def _hash(file, limit=None):
    digest = hashlib.blake2b(digest_size=32)
    with _open(file) as descriptor:
        for block in _blocks(descriptor, limit):
            digest.update(block)
    return digest.digest()


def _blocks(descriptor: int, limit: int | None = None) -> Iterator[bytes]:
    left = limit
    while left is None or left > 0:
        block = os.read(descriptor, BLOCK if left is None else min(BLOCK, left))
        if not block:
            return
        yield block
        left = None if left is None else left - len(block)


@contextmanager
def _open(file: FileRecord) -> Iterator[int]:
    descriptor = _open_quietly(file.path)
    try:
        _check(file, descriptor)
        yield descriptor
        _check(file, descriptor)
    finally:
        os.close(descriptor)


def _open_quietly(path):
    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    try:
        return os.open(path, flags | getattr(os, "O_NOATIME", 0))
    except PermissionError:
        return os.open(path, flags)


def _check(file, descriptor):
    info = os.fstat(descriptor)
    seen = (info.st_size, info.st_mtime_ns, info.st_ino)
    if not stat.S_ISREG(info.st_mode) or seen != (file.size, file.mtime_ns, file.inode):
        raise Changed(file.path)


def _members(archive):
    """Every file a zip would create, as {path: (size, checksum)}, or None if it can't be trusted."""
    try:
        with _open(archive) as descriptor:
            with open(descriptor, "rb", closefd=False) as stream:
                if not _small_directory(stream, archive.size):
                    return None
                with zipfile.ZipFile(stream) as opened:
                    entries = opened.infolist()
    except (Changed, OSError, ValueError, NotImplementedError, zipfile.BadZipFile, struct.error):
        return None
    members = {}
    for entry in entries:
        if entry.is_dir() or entry.filename.startswith("__MACOSX/"):
            continue
        # zipfile tidies entry.filename (and differently on Windows), so judge the original
        name = entry.orig_filename
        parts = PurePosixPath(name).parts
        unsafe = not parts or ".." in parts or name.startswith("/") or set("\\:\0") & set(name)
        special = stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
        if unsafe or special or entry.flag_bits & 1 or parts in members:
            return None
        members[parts] = (entry.file_size, entry.CRC)
    return members


def _small_directory(stream, size):
    length = min(size, 22 + 65_535)
    stream.seek(size - length)
    tail = stream.read(length)
    stream.seek(0)
    start = tail.rfind(b"PK\x05\x06")
    if start < 0 or len(tail) - start < 22 or b"PK\x06\x07" in tail:
        return False
    count, directory = struct.unpack_from("<HL", tail, start + 10)
    return count <= MAX_MEMBERS and directory <= MAX_DIRECTORY


def _index(folder):
    sizes = {}
    for parent, folders, names in os.walk(folder):
        folders[:] = sorted(name for name in folders if name not in NEVER_ENTER)
        for name in sorted(names):
            path = os.path.join(parent, name)
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                sizes[path] = info.st_size
            if len(sizes) >= MAX_INDEX:
                return sizes
    return sizes


def _find_folder(members, sizes, by_name):
    largest = max(members, key=lambda parts: (members[parts][0], parts))
    size, _ = members[largest]
    tail = os.path.join(*largest)
    for path in by_name[largest[-1], size][:MAX_CANDIDATES]:
        if not path.endswith(os.sep + tail):
            continue
        folder = path[: -len(tail) - 1]
        wanted = {os.path.join(folder, *parts): value for parts, value in members.items()}
        if any(sizes.get(place) != value[0] for place, value in wanted.items()):
            continue
        if all(_checksum(place, value[0]) == value[1] for place, value in wanted.items()):
            return folder
    return None


def _checksum(path, size):
    try:
        descriptor = _open_quietly(path)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            return None
        value = 0
        for block in _blocks(descriptor):
            value = zlib.crc32(block, value)
        after = os.fstat(descriptor)
        same = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        return value if same else None
    except OSError:
        return None
    finally:
        os.close(descriptor)
