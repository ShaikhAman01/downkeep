import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from downkeep import trash
from downkeep.evidence import Extracted, find_duplicates, find_extracted, verify_copy, verify_extracted
from downkeep.model import ActionError, Identity, Result, Stay
from downkeep.scan import FileRecord, ScanError, scan


@dataclass(slots=True)
class Case:
    """One file with proof: the zip it is, if every file in it was found, and its identical copies."""

    file: FileRecord
    extracted: Extracted | None = None
    copies: tuple[FileRecord, ...] = ()


def apply_clear(store, folder: str, names: Sequence[str]) -> Result:
    if not trash.SUPPORTED:
        raise ActionError("Moving to the Trash is not available on this system yet.")
    from downkeep.engine import _recover

    with store.lock():
        _recover(store)
        try:
            snapshot = scan(folder)
        except ScanError as error:
            raise ActionError(str(error)) from error
        cases = _cases(snapshot)
        known = {file.name for file in snapshot.files}
        result = Result()
        for name in dict.fromkeys(names):
            if name not in known:
                result.skipped.append(Stay(name, "It isn't there any more, or it just changed."))
            elif name not in cases:
                result.skipped.append(Stay(name, "There is no proof for it now, so it stays."))
        todo = [case for name, case in cases.items() if name in names]
        if todo:
            result.batch = store.begin("trash", snapshot.folder)
            try:
                _clear(store, snapshot.folder, todo, result)
            finally:
                store.end(result.batch)
        return result


def _cases(snapshot):
    """Everything proven, in the order it is shown: zips first. The first of a group of copies stays."""
    cases = {item.archive.name: Case(item.archive, item) for item in find_extracted(snapshot.files, snapshot.folder)}
    for group in find_duplicates(snapshot.files):
        for file in group.files:
            if file.name in cases or file is not group.files[0]:
                cases.setdefault(file.name, Case(file)).copies = group.files
    return cases


def _clear(store, folder, todo, result):
    waiting = {case.file.name for case in todo}
    needed: dict[tuple[int, int], str] = {}
    for case in todo:
        name = case.file.name
        waiting.discard(name)
        try:
            staying = _ready(case, folder, waiting, needed)
            needed |= dict.fromkeys(staying, name)
            _send(store, result.batch, case.file)
            result.done.append(name)
        except (ActionError, OSError, ValueError, ScanError) as error:
            result.skipped.append(Stay(name, _reason(error)))


def _ready(case, folder, waiting, needed):
    """The files that must stay once this one is gone. Raises ActionError if it has to stay itself."""
    file = case.file
    reliant = needed.get((file.device, file.inode))
    if reliant:
        raise ActionError(f"It proves that nothing is lost with {reliant}, so it stays.")
    if file.links != 1:
        raise ActionError("It has more than one name on this disk.")
    staying = next(filter(None, (_outside(paths, file) for paths in _proofs(case, waiting))), None)
    if not staying:
        raise ActionError("The proof doesn't hold any more, so it stays.")
    if file not in scan(folder).files:
        raise ActionError("It just changed.")
    if _in_use(file.path):
        raise ActionError("It is open in another program.")
    return staying


def _proofs(case: Case, waiting: set[str]) -> Iterator[tuple[str, ...]]:
    if case.extracted:
        yield verify_extracted(case.extracted)
    for other in case.copies:
        if other.name not in waiting:
            yield verify_copy(case.file, other)


def _outside(paths, file):
    """The files behind a proof, or nothing if one is in the Trash, on another disk or the file itself."""
    found = [os.lstat(path) for path in paths]
    keys = {(info.st_dev, info.st_ino) for info in found}
    if (file.device, file.inode) in keys or any(device != file.device for device, _ in keys):
        return set()
    return set() if any(map(trash.holds, paths)) else keys


def _in_use(path):
    try:
        from downkeep.engine import in_use
    except ImportError as error:
        raise ActionError("Downkeep can't tell if it is in use.") from error
    return in_use(path)


def _send(store, batch, file):
    identity = Identity.of(file)
    operation = store.intent(batch, "trash", file.path, None, identity)
    try:
        place = trash.send(file.path, identity)
    except (ActionError, OSError, ValueError) as error:
        place = trash.find(file.path, identity)
        if place is None:
            state = "ABORTED" if os.path.lexists(file.path) else "CONFLICT"
            store.finish(operation, state, _reason(error))
            raise
    store.finish(operation, "DONE", dest=place or None)


def _reason(error):
    if isinstance(error, (ActionError, ScanError)):
        return str(error)
    return f"It couldn't be moved to the Trash ({getattr(error, 'strerror', None) or error})."
