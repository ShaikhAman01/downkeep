import json
import os
from collections import Counter

from downkeep.evidence import Duplicates, Extracted
from downkeep.scan import Snapshot

DAY_NS = 86_400 * 1_000_000_000
AGES = (
    ("under a week", 7),
    ("a week to a month", 30),
    ("1 to 3 months", 90),
    ("3 months to a year", 365),
    ("over a year", None),
)


def build(
    snapshot: Snapshot,
    top: int = 10,
    hosts: dict[str, str] | None = None,
    duplicates: list[Duplicates] | None = None,
    extracted: list[Extracted] | None = None,
) -> dict:
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
    result = {
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
    if hosts is not None:
        result["sites"] = _sites(snapshot, hosts)
    if duplicates is not None:
        result["duplicates"] = [
            {"names": [file.name for file in group.files], "bytes_each": group.files[0].size, "wasted": group.wasted}
            for group in duplicates
        ]
    if extracted is not None:
        result["extracted"] = [
            {
                "name": item.archive.name,
                "bytes": item.archive.size,
                "folder": os.path.relpath(item.folder, snapshot.folder),
                "files": item.members,
            }
            for item in extracted
        ]
    return result


def _sites(snapshot, hosts):
    count, size = Counter(), Counter()
    for file in snapshot.files:
        site = hosts.get(file.path)
        count[site] += 1
        size[site] += file.size
    known = sorted((site for site in count if site), key=lambda site: (-count[site], site))
    return {
        "known": [{"site": site, "files": count[site], "bytes": size[site]} for site in known],
        "unknown": {"files": count[None], "bytes": size[None]},
    }


def as_json(report: dict) -> str:
    return json.dumps(report, indent=2)


def as_text(report: dict, types: int = 8, rows: int = 8) -> str:
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
        lines += _site_lines(report.get("sites"), rows)
        lines += _duplicate_lines(report.get("duplicates"), rows)
        lines += _extracted_lines(report.get("extracted"), rows)
    if report["skipped"]:
        lines += ["", "Left out"]
        lines += [f"  {count:>6}  {reason}" for reason, count in report["skipped"].items()]
    return "\n".join(lines)


def _site_lines(sites, rows):
    if sites is None:
        return []
    lines = ["", "Probably from"]
    lines += [_row(clean(row["site"]), row) for row in sites["known"][:rows]]
    rest = sites["known"][rows:]
    if rest:
        lines.append(_row(f"{len(rest)} other sites", {key: sum(row[key] for row in rest) for key in ("files", "bytes")}))
    if sites["unknown"]["files"]:
        lines.append(_row("unknown", sites["unknown"]))
    return lines


def _duplicate_lines(groups, rows):
    if not groups:
        return []
    extra = sum(len(group["names"]) - 1 for group in groups)
    wasted = human(sum(group["wasted"] for group in groups))
    lines = ["", f"Exact copies: {extra} extra, {wasted} (contents compared)"]
    for group in groups[:rows]:
        lines.append(f"  {human(group['wasted']):>9}  " + "  =  ".join(clean(name) for name in group["names"]))
    return lines + _more(groups, rows)


def _extracted_lines(archives, rows):
    if not archives:
        return []
    size = human(sum(item["bytes"] for item in archives))
    lines = ["", f"Zips already extracted: {len(archives)}, {size} (every file checked)"]
    for item in archives[:rows]:
        place = "this folder" if item["folder"] == "." else clean(item["folder"])
        lines.append(f"  {human(item['bytes']):>9}  {clean(item['name'])}  ({_files(item['files'])} in {place})")
    return lines + _more(archives, rows)


def _more(items, rows):
    hidden = len(items) - rows
    return [f"  and {hidden} more (see --json)"] if hidden > 0 else []


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
