import json
from collections import Counter

from downkeep.scan import Snapshot

DAY_NS = 86_400 * 1_000_000_000
AGES = (
    ("under a week", 7),
    ("a week to a month", 30),
    ("1 to 3 months", 90),
    ("3 months to a year", 365),
    ("over a year", None),
)


def build(snapshot: Snapshot, top: int = 10) -> dict:
    count_by_type, size_by_type = Counter(), Counter()
    count_by_age, size_by_age = Counter(), Counter()
    for file in snapshot.files:
        kind = file.extension or "no extension"
        count_by_type[kind] += 1
        size_by_type[kind] += file.size
        age = _age(snapshot.taken_ns - file.mtime_ns)
        count_by_age[age] += 1
        size_by_age[age] += file.size

    total = sum(file.size for file in snapshot.files)
    biggest = sorted(snapshot.files, key=lambda file: (-file.size, file.name))[:top]
    return {
        "folder": snapshot.folder,
        "files": len(snapshot.files),
        "bytes": total,
        "types": [
            {"type": kind, "files": count_by_type[kind], "bytes": size_by_type[kind]}
            for kind in sorted(count_by_type, key=lambda kind: (-size_by_type[kind], kind))
        ],
        "ages": [
            {"age": label, "files": count_by_age[label], "bytes": size_by_age[label]}
            for label, _ in AGES
        ],
        "biggest": [
            {
                "name": file.name,
                "bytes": file.size,
                "days_old": max(0, (snapshot.taken_ns - file.mtime_ns) // DAY_NS),
            }
            for file in biggest
        ],
        "biggest_share": _percent(sum(file.size for file in biggest), total),
        "skipped": dict(sorted(snapshot.skipped.items())),
    }


def as_json(report: dict) -> str:
    return json.dumps(report, indent=2)


def as_text(report: dict, types: int = 8) -> str:
    lines = [
        clean(report["folder"]),
        f"{_files(report['files'])}, {human(report['bytes'])}. Nothing was changed.",
    ]
    if report["files"]:
        lines += ["", "By type"]
        shown = report["types"][:types]
        lines += [_row(clean(row["type"]), row) for row in shown]
        rest = report["types"][types:]
        if rest:
            other = {key: sum(row[key] for row in rest) for key in ("files", "bytes")}
            lines.append(_row(f"{len(rest)} other types", other))

        lines += ["", "By last change"]
        lines += [_row(row["age"], row) for row in report["ages"] if row["files"]]

        count = len(report["biggest"])
        if count:
            heading = "Biggest file" if count == 1 else f"Biggest {count}"
            lines += ["", f"{heading} ({report['biggest_share']}% of the space)"]
            for file in report["biggest"]:
                lines.append(f"  {human(file['bytes']):>9}  {_days(file['days_old']):>11}  {clean(file['name'])}")
    if report["skipped"]:
        lines += ["", "Left out"]
        lines += [f"  {count:>6}  {reason}" for reason, count in report["skipped"].items()]
    return "\n".join(lines)


def human(size: int) -> str:
    if size < 1000:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 999.95 or unit == "TB":
            return f"{value:.1f} {unit}"


def clean(text: str) -> str:
    return "".join(char if char.isprintable() else "\N{REPLACEMENT CHARACTER}" for char in text)


def _age(elapsed_ns):
    days = elapsed_ns / DAY_NS
    for label, limit in AGES:
        if limit is None or days < limit:
            return label


def _row(label, row):
    return f"  {label:<20} {row['files']:>6}  {human(row['bytes']):>9}"


def _files(count):
    return "1 file" if count == 1 else f"{count} files"


def _days(days):
    return "today" if days == 0 else "1 day" if days == 1 else f"{days} days"


def _percent(part, whole):
    return round(100 * part / whole) if whole else 0
