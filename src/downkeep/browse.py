import os
import stat
from pathlib import Path

from downkeep.scan import ScanError, check_folder, is_hidden

MAX_DEPTH = 30


def subfolders(folder: Path) -> list[str]:
    """Names of the real, visible folders directly inside. Links and hidden folders are left out."""
    names = []
    try:
        with os.scandir(folder) as listing:
            for entry in listing:
                try:
                    info = os.stat(entry.path, follow_symlinks=False)
                    link = entry.is_symlink() or entry.is_junction()
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode) and not link and not is_hidden(entry.name, info):
                    names.append(entry.name)
    except OSError as error:
        raise ScanError(f"Can't read {folder}: {error.strerror or error}") from error
    return sorted(names, key=lambda name: (name.lower(), name))


def inside(home: Path, parts: object) -> Path:
    """The folder reached by these names, each of which must be listed in the one before it."""
    if not isinstance(parts, list) or len(parts) > MAX_DEPTH:
        raise ScanError("That folder isn't available")
    current = home.resolve()
    for part in parts:
        if not isinstance(part, str) or part not in subfolders(current):
            raise ScanError("That folder isn't available")
        current = current / part
    return current


def choose(home: Path, parts: object) -> Path:
    wanted = inside(home, parts)
    found = check_folder(wanted)
    # A link swapped in after the listing would lead somewhere else
    if found != wanted or found == home.resolve():
        raise ScanError("That folder isn't available")
    return found
