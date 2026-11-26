from dataclasses import dataclass, field

from downkeep.scan import FileRecord


class ActionError(Exception):
    """Something the person should be told, in plain words. Nothing was changed by the failed step."""


class Busy(ActionError):
    """Another Downkeep is moving files right now."""


@dataclass(frozen=True, slots=True)
class Identity:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, file: FileRecord) -> "Identity":
        return cls(file.device, file.inode, file.size, file.mtime_ns)


@dataclass(frozen=True, slots=True)
class Move:
    file: FileRecord
    folder: str
    why: str


@dataclass(frozen=True, slots=True)
class Stay:
    name: str
    why: str


@dataclass(slots=True)
class Result:
    batch: int | None = None
    done: list[str] = field(default_factory=list)
    skipped: list[Stay] = field(default_factory=list)
