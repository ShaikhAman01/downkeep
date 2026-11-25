import os
import stat
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

UNFINISHED = {
    ".!qb", ".!ut", ".aria2", ".crdownload", ".download",
    ".opdownload", ".part", ".partial", ".tmp", ".ytdl",
}
SIDECARS = (".aria2", ".crdownload", ".part", ".partial")
SYSTEM = {"desktop.ini", "thumbs.db"}
SETTLE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class FileRecord:
    name: str
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    links: int

    @property
    def extension(self) -> str:
        stem, last = os.path.splitext(self.name.lower())
        return ".tar" + last if last and stem.endswith(".tar") else last


@dataclass(frozen=True, slots=True)
class Snapshot:
    folder: str
    taken_ns: int
    files: tuple[FileRecord, ...]
    skipped: dict[str, int]


class ScanError(Exception):
    pass


def check_folder(folder: str | os.PathLike) -> Path:
    path = Path(folder).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ScanError(f"Can't open {path}: {error}") from error
    if not resolved.is_dir():
        raise ScanError(f"{path} is not a folder")
    if resolved == Path(resolved.anchor):
        raise ScanError("Choose a folder, not a whole drive")
    if resolved == Path.home().resolve():
        raise ScanError("Choose a folder inside your home folder, not the home folder itself")
    return resolved


def scan(folder: str | os.PathLike, now_ns: int | None = None) -> Snapshot:
    root = check_folder(folder)
    now_ns = time.time_ns() if now_ns is None else now_ns
    try:
        with os.scandir(root) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
    except OSError as error:
        raise ScanError(f"Can't read {root}: {error}") from error

    names = {entry.name.lower() for entry in entries}
    files = []
    skipped = Counter()
    for entry in entries:
        try:
            reason, info = _classify(entry, names, now_ns)
        except FileNotFoundError:
            skipped["disappeared during the scan"] += 1
            continue
        except OSError:
            skipped["couldn't be read"] += 1
            continue
        if reason:
            skipped[reason] += 1
            continue
        files.append(
            FileRecord(
                name=entry.name,
                path=entry.path,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                device=info.st_dev,
                inode=info.st_ino,
                links=info.st_nlink,
            )
        )
    return Snapshot(str(root), now_ns, tuple(files), dict(skipped))


def _classify(entry, names, now_ns):
    # DirEntry.stat() leaves inode and link count empty on Windows
    info = os.stat(entry.path, follow_symlinks=False)
    name = entry.name.lower()
    if entry.is_symlink() or entry.is_junction():
        return "links", info
    if stat.S_ISDIR(info.st_mode):
        return "folders", info
    if not stat.S_ISREG(info.st_mode):
        return "special files", info
    if _hidden(name, info) or name in SYSTEM:
        return "hidden or system files", info
    if _unfinished(name, names):
        return "unfinished downloads", info
    if info.st_size == 0:
        return "empty files", info
    if 0 <= now_ns - info.st_mtime_ns < SETTLE_SECONDS * 1_000_000_000:
        return "changed in the last minute", info
    return None, info


def _hidden(name, info):
    attributes = getattr(info, "st_file_attributes", 0)
    flags = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
    return name.startswith((".", "~$")) or bool(attributes & flags)


def _unfinished(name, names):
    if os.path.splitext(name)[1] in UNFINISHED:
        return True
    return any(name + suffix in names for suffix in SIDECARS)
