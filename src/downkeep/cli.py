import argparse
import os
import sys
import threading
from urllib.parse import quote

from downkeep import __version__
from downkeep.evidence import find_duplicates, find_extracted
from downkeep.folders import downloads_folder
from downkeep.origin import find_hosts
from downkeep.report import as_json, as_text, build, clean
from downkeep.scan import ScanError, check_folder, scan
from downkeep.server import HOST, App, make_server
from downkeep.window import open_window, show_in_folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="downkeep", description="Careful cleanup for your Downloads folder")
    parser.add_argument("--version", action="version", version=f"downkeep {__version__}")
    commands = parser.add_subparsers(dest="command")
    window = commands.add_parser("open", help="open the window (what plain 'downkeep' does)")
    window.add_argument("folder", nargs="?", help="defaults to your Downloads folder")
    window.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    report = commands.add_parser("report", help="show what is in the folder; changes nothing")
    report.add_argument("folder", nargs="?", help="defaults to your Downloads folder")
    report.add_argument("--json", action="store_true", help="print JSON")
    report.add_argument("--top", type=_top, default=10, metavar="N", help="how many big files to list (default 10)")
    report.add_argument("--quick", action="store_true", help="don't look for sites, copies or extracted zips")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if args.command != "report":
        return _open(getattr(args, "folder", None), getattr(args, "no_browser", False))
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


def _open(folder, no_browser):
    try:
        root = check_folder(folder or downloads_folder())
    except ScanError as error:
        print(f"downkeep: {clean(str(error))}", file=sys.stderr)
        return 2
    app = App(str(root), show_in_folder)
    server = make_server(app)
    url = f"http://{HOST}:{server.server_address[1]}/#{quote(app.code)}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if no_browser:
            print(url, flush=True)
        elif not open_window(url):
            print(f"Couldn't open a browser. Open this link within 2 minutes:\n{url}", flush=True)
        else:
            print("Downkeep is open in a window. Close it to stop, or press Ctrl+C here.", flush=True)
        done = threading.Event()
        while not app.finished():
            done.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
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
