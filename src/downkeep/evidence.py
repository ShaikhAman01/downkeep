import hashlib
import os
import re
import stat
import struct
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from downkeep.scan import FileRecord, ScanError, scan

HEAD = 65_536
BLOCK = 1024 * 1024
MAX_MEMBERS = 10_000
MAX_DIRECTORY = 16 * 1024 * 1024
MAX_INDEX = 200_000
MAX_CANDIDATES = 20
EXAMPLES = 3
NEVER_ENTER = {".git", ".hg", ".venv", "__pycache__", "node_modules", "venv"}
STORED, DEFLATED = 0, 8
# "a (1).pdf", "a - Copy.pdf", "a copy 2.pdf", "a (another copy).pdf", "Copy of a.pdf"
COPY_NAME = re.compile(r"^copy of |\s\(\d+\)$|(\s-)?\s\(?((\d+(st|nd|rd|th)|another)\s)?copy(\s\d+)?\)?(\s\(\d+\))?$")


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


def find_duplicates(files: Iterable[FileRecord], keepers: Iterable[FileRecord] = ()) -> list[Duplicates]:
    """Groups of identical files, the one that stays first. Keepers (from subfolders) can only stay."""
    groups = _same(_one_per_inode([*keepers, *files]), lambda file: file.size)
    groups = [smaller for group in groups for smaller in _same(group, lambda file: _hash(file, HEAD))]
    groups = [same for group in groups for same in _same(group, _hash)]
    found = []
    for group in groups:
        group = sorted(group, key=_keep_first)
        group = group[:1] + [file for file in group[1:] if "/" not in file.name]
        if len(group) > 1:
            found.append(Duplicates(tuple(group)))
    return sorted(found, key=lambda group: (-group.wasted, group.files[0].name))


def find_keepers(folder: str, names: Iterable[str]) -> list[FileRecord]:
    """Files directly inside these subfolders, named "Documents/a.pdf". Links and other disks are left out."""
    found = []
    device = os.stat(folder).st_dev
    for name in dict.fromkeys(name for name in names if isinstance(name, str)):
        if name in ("", ".", "..") or set(name) & {"/", "\\", "\0"} or is_trash(name):
            continue
        path = os.path.join(folder, name)
        try:
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or os.path.realpath(path) != path or info.st_dev != device:
                continue
            files = scan(path).files
        except (OSError, ScanError):
            continue
        found += [replace(file, name=f"{name}/{file.name}") for file in files if file.device == device]
    return found


def is_copy_name(name: str) -> bool:
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    return bool(COPY_NAME.search(stem.removesuffix(".tar")))


def _keep_first(file):
    return "/" not in file.name, is_copy_name(file.name), file.mtime_ns, file.name


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
        where = _find_folder(archive, members, sizes, by_name) if members else None
        if where:
            examples = tuple(parts[-1] for parts in sorted(members)[:EXAMPLES])
            found.append(Extracted(archive, where, len(members), examples))
    return sorted(found, key=lambda item: (-item.archive.size, item.archive.name))


def verify_copy(file: FileRecord, other: FileRecord) -> tuple[str, ...]:
    """Reads both again in full. The files that must stay for this proof to hold, or () if it doesn't."""
    if (file.device, file.inode) == (other.device, other.inode):
        return ()
    return (other.path,) if _same((file, other), _hash) else ()


def verify_extracted(item: Extracted) -> tuple[str, ...]:
    """Reads the zip's listing and every extracted file again. The files that must stay, or ()."""
    members = _members(item.archive) or {}
    wanted = {os.path.join(item.folder, *parts): value for parts, value in members.items()}
    # A link anywhere on the way would make a file outside the folder count as proof
    if any(os.path.realpath(place) != place for place in wanted):
        return ()
    if not all(_checksum(place, size) == crc for place, (size, crc) in wanted.items()):
        return ()
    return tuple(wanted) if _same_contents(item.archive, item.folder) else ()


def is_trash(name: str) -> bool:
    return name == ".Trash" or name.startswith(".Trash-") or name.lower() == "$recycle.bin"


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
    # Anything in front of the first file, such as another zip, is not in this listing
    if entries and min(entry.header_offset for entry in entries) != 0:
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
        folders[:] = sorted(name for name in folders if name not in NEVER_ENTER and not is_trash(name))
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


def _find_folder(archive, members, sizes, by_name):
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
        same = all(_checksum(place, value[0]) == value[1] for place, value in wanted.items())
        if same and _same_contents(archive, folder):
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


def _same_contents(archive, folder):
    """Unpacks each file in memory, a block at a time, and compares it with the one on disk.

    A checksum alone can be matched on purpose. Every packed byte must be used and the output may not pass
    the declared size, which already equals the file on disk, so neither hidden data nor a zip bomb gets through.
    """
    try:
        with _open(archive) as descriptor, open(descriptor, "rb", closefd=False) as stream:
            with zipfile.ZipFile(stream) as opened:
                entries = [entry for entry in opened.infolist() if not entry.is_dir()]
            for entry in entries:
                place = os.path.join(folder, *PurePosixPath(entry.orig_filename).parts)
                if entry.filename.startswith("__MACOSX/"):
                    continue
                if _unpacked(stream, entry) != _digest(place, entry.file_size):
                    return False
        return True
    except (Changed, OSError, ValueError, EOFError, zlib.error, struct.error, zipfile.BadZipFile):
        return False


def _unpacked(stream, entry):
    """The digest of one member's contents, or None if it holds more or less than it says."""
    if entry.compress_type not in (STORED, DEFLATED):
        return None
    stream.seek(entry.header_offset)
    header = stream.read(30)
    if len(header) < 30 or header[:4] != b"PK\x03\x04":
        return None
    stream.seek(sum(struct.unpack_from("<HH", header, 26)), os.SEEK_CUR)
    unpack = zlib.decompressobj(-15) if entry.compress_type == DEFLATED else None
    digest, out, left = hashlib.blake2b(digest_size=32), 0, entry.compress_size
    while True:
        if unpack and unpack.unconsumed_tail:
            packed = unpack.unconsumed_tail
        elif left:
            packed = stream.read(min(BLOCK, left))
            left -= len(packed)
            if not packed:
                return None
        elif unpack and not unpack.eof:
            packed = b""
        else:
            break
        block = unpack.decompress(packed, BLOCK) if unpack else packed
        if not (packed or block):
            break
        out += len(block)
        if out > entry.file_size:
            return None
        digest.update(block)
    finished = unpack is None or (unpack.eof and not unpack.unused_data)
    return digest.digest() if finished else None


def _digest(place, size):
    descriptor = _open_quietly(place)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise Changed(place)
        digest = hashlib.blake2b(digest_size=32)
        for block in _blocks(descriptor):
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise Changed(place)
        return digest.digest()
    finally:
        os.close(descriptor)
