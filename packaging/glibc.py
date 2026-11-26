"""Finds the oldest glibc a built Linux program runs on, from the versions its binaries ask for."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

NEEDS = re.compile(r"\bGLIBC_(\d+)\.(\d+)")


def binaries(folder: Path) -> list[Path]:
    return [path for path in sorted(folder.rglob("*")) if path.is_file() and path.read_bytes()[:4] == b"\x7fELF"]


def needed(folder: Path) -> dict[Path, tuple[int, int]]:
    found = {}
    for path in binaries(folder):
        text = subprocess.run(["objdump", "-T", str(path)], capture_output=True, text=True, check=True).stdout
        found[path] = max(((int(major), int(minor)) for major, minor in NEEDS.findall(text)), default=(0, 0))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--at-most", metavar="2.35")
    args = parser.parse_args(argv)
    found = needed(args.folder)
    if not found:
        raise SystemExit(f"No binaries in {args.folder}")
    for path, (major, minor) in found.items():
        print(f"{major}.{minor:<3} {path.relative_to(args.folder)}")
    highest = max(found.values())
    print(f"Needs glibc {highest[0]}.{highest[1]} or newer")
    if args.at_most and highest > tuple(int(part) for part in args.at_most.split(".")):
        print(f"That is newer than {args.at_most}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
