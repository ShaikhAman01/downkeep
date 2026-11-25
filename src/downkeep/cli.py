import argparse
import os
import sys

from downkeep import __version__
from downkeep.evidence import find_duplicates, find_extracted
from downkeep.folders import downloads_folder
from downkeep.origin import find_hosts
from downkeep.report import as_json, as_text, build, clean
from downkeep.scan import ScanError, scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="downkeep", description="Careful cleanup for your Downloads folder")
    parser.add_argument("--version", action="version", version=f"downkeep {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="show what is in the folder; changes nothing")
    report.add_argument("folder", nargs="?", help="defaults to your Downloads folder")
    report.add_argument("--json", action="store_true", help="print JSON")
    report.add_argument("--top", type=_top, default=10, metavar="N", help="how many big files to list (default 10)")
    report.add_argument("--quick", action="store_true", help="don't look for sites, copies or extracted zips")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        result = _report(args)
    except ScanError as error:
        print(f"downkeep: {clean(str(error))}", file=sys.stderr)
        return 2
    try:
        print(as_json(result) if args.json else as_text(result))
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def _report(args):
    snapshot = scan(args.folder or downloads_folder())
    if args.quick:
        return build(snapshot, args.top)
    return build(
        snapshot,
        args.top,
        hosts=find_hosts(file.path for file in snapshot.files),
        duplicates=find_duplicates(snapshot.files),
        extracted=find_extracted(snapshot.files, snapshot.folder),
    )


def _top(value):
    if not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError("must be a whole number, 0 or more")
    return int(value)
