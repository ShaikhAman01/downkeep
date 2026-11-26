import argparse
import os
import shlex
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote

from downkeep import __version__
from downkeep.evidence import find_duplicates, find_extracted
from downkeep.folders import downloads_folder
from downkeep.model import ActionError, Busy, Result, Stay
from downkeep.origin import find_hosts
from downkeep.report import as_json, as_text, build, clean
from downkeep.scan import ScanError, check_folder, scan
from downkeep.server import BUSY, HOST, NO_AUTO, NO_TIDY, Actions, App, make_server, reason
from downkeep.window import log, open_window, show_in_folder, write_link

LINK_FILE = "DOWNKEEP_LINK_FILE"
ROWS = 20
KINDS = {"tidy": "Tidied", "auto": "Tidied automatically", "trash": "Moved to the Recycle Bin" if sys.platform == "win32" else "Moved to the Trash", "undo": "Undid a run"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="downkeep", description="Careful cleanup for your Downloads folder")
    parser.add_argument("--version", action="version", version=f"downkeep {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")
    window = commands.add_parser("open", help="open the window (what plain 'downkeep' does)")
    window.add_argument("folder", nargs="?", help="defaults to your Downloads folder")
    window.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    report = commands.add_parser("report", help="show what is in the folder; changes nothing")
    report.add_argument("folder", nargs="?", help="defaults to your Downloads folder")
    report.add_argument("--json", action="store_true", help="print JSON")
    report.add_argument("--top", type=_top, default=10, metavar="N", help="how many big files to list (default 10)")
    report.add_argument("--quick", action="store_true", help="don't look for sites, copies or extracted zips")
    tidy = commands.add_parser("tidy", help="show how files would be sorted into folders; changes nothing")
    tidy.add_argument("folder", nargs="?", help="defaults to your Downloads folder")
    tidy.add_argument("--apply", action="store_true", help="move the files")
    commands.add_parser("history", help="show what was moved")
    undo = commands.add_parser("undo", help="put the files of a run back")
    undo.add_argument("batch", nargs="?", type=_top, help="a number from the history; defaults to the last run")
    auto = commands.add_parser("auto", help="tidy new files automatically")
    auto.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    commands.add_parser("auto-run")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if args.command == "auto-run":
        return auto_run()
    work = {"report": _report, "tidy": _tidy, "history": _history, "undo": _undo, "auto": _auto}.get(args.command)
    if work is None:
        return open_app(getattr(args, "folder", None), getattr(args, "no_browser", False))
    try:
        code, text = work(args)
    except (ActionError, ScanError) as error:
        print(f"downkeep: {clean(str(error)) or BUSY}", file=sys.stderr)
        return 2
    try:
        print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return code


def auto_run() -> int:
    """One scheduled pass. Nobody is watching, so it says nothing and problems go to the log."""
    actions = Actions()
    try:
        if actions.able()["tidy"]:
            actions.auto_run()
        return 0
    except Busy:
        return 0
    except ActionError as error:
        log(f"Automatic tidying: {clean(str(error))}")
    except Exception as error:
        log(f"Automatic tidying failed ({type(error).__name__}).")
    return 1


def open_app(folder: str | None, no_browser: bool = False, tell: Callable[[str], None] | None = None) -> int:
    try:
        # A missing Downloads folder is shown in the window, where another one can be chosen
        root = check_folder(folder) if folder else downloads_folder()
    except ScanError as error:
        print(f"downkeep: {clean(str(error))}", file=sys.stderr)
        return 2
    app = App(str(root), show_in_folder)
    app.recover()
    server = make_server(app)
    url = f"http://{HOST}:{server.server_address[1]}/#{quote(app.code)}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        link_file = os.environ.get(LINK_FILE)
        if link_file:
            try:
                write_link(link_file, url)
            except OSError:
                print(f"downkeep: {LINK_FILE} must name a file that doesn't exist yet", file=sys.stderr)
                return 2
        elif no_browser:
            print(url, flush=True)
        elif open_window(url):
            print("Downkeep is open in a window. Close it to stop, or press Ctrl+C here.", flush=True)
        elif tell:
            tell("Downkeep couldn't open a web browser. Install one, then open Downkeep again.")
            return 1
        else:
            print(f"Couldn't open a browser. Open this link within 2 minutes:\n{url}", flush=True)
        done = threading.Event()
        while not app.finished():
            done.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        app.settle()
    return 0


def _report(args):
    snapshot = scan(args.folder or downloads_folder())
    found = {}
    if not args.quick:
        found = {
            "hosts": find_hosts(file.path for file in snapshot.files),
            "duplicates": find_duplicates(snapshot.files),
            "extracted": find_extracted(snapshot.files, snapshot.folder),
        }
    result = build(snapshot, args.top, **found)
    return 0, as_json(result) if args.json else as_text(result)


def _tidy(args):
    actions = _able()
    folder = str(check_folder(args.folder or downloads_folder()))
    if args.apply:
        actions.recover()
    moves, stays, _ = actions.plan(folder)
    if not moves:
        return 0, "\n".join([clean(folder), "Nothing to tidy. Nothing was changed.", *_left(stays)])
    if args.apply:
        return _done(actions.tidy(folder, moves), "Moved")
    into = {}
    for move in moves:
        into.setdefault(move.folder, []).append(f"{clean(move.file.name)}  ({clean(reason(move.file.name, move.why))})")
    lines = [clean(folder), f"{_count(len(moves))} would move into {_count(len(into), 'folder')}. Nothing was changed."]
    for name, rows in into.items():
        lines += ["", f"{clean(name)} ({_count(len(rows))})", *(f"  {row}" for row in rows)]
    again = " ".join(["downkeep tidy", *([shlex.quote(clean(args.folder))] if args.folder else []), "--apply"])
    return 0, "\n".join([*lines, *_left(stays), "", f"To do this: {again}"])


def _history(args):
    runs = _able().history()
    lines = []
    for run in runs:
        when = datetime.fromtimestamp(run["when"])
        note = " (undone)" if run["undone"] else "" if run["can_undo"] else " (can't be undone here)"
        what = KINDS.get(run["kind"], clean(run["kind"]))
        lines.append(f"{run['batch']:>5}  {when:%Y-%m-%d %H:%M}  {what}, {_count(run['count'])}{note}")
    return 0, "\n".join(lines) or "Nothing has been moved yet."


def _undo(args):
    actions = _able()
    actions.recover()
    batch = args.batch
    if batch is None:
        batch = next((run["batch"] for run in actions.history(1000) if run["can_undo"] and not run["undone"]), None)
        if batch is None:
            return 0, "There is nothing to undo."
    return _done(actions.undo(batch), "Put back")


def _auto(args):
    actions = _able(NO_AUTO)
    folder = str(downloads_folder())
    if args.state == "on":
        available, why = actions.schedule()
        if not available:
            raise ActionError(why or NO_AUTO)
        folder = str(check_folder(folder))
    if args.state != "status":
        actions.set_auto(folder, args.state == "on")
    status = actions.auto(folder)
    if not status["on"]:
        why = "" if status["available"] else f" {clean(status['why'])}."
        return 0, f"Automatic tidying is off.{why}"
    names = [clean(rule["folder"]) for rule in actions.rules()["rules"] if rule["on"] and rule["auto"]]
    lines = [f"Automatic tidying is on for {clean(status['folder'] or folder)}"]
    lines.append(f"Folders: {', '.join(names)}" if names else "No folder is switched on for it, so nothing will move.")
    lines.append(f"Waiting: {_count(status['waiting'])}. It never uses the Trash.")
    return 0, "\n".join(lines)


def _able(why=NO_TIDY):
    actions = Actions()
    if not actions.able()["tidy"]:
        raise ActionError(why)
    return actions


def _done(result: Result, verb):
    lines = [f"{verb} {_count(len(result.done))}."] if result.done else ["Nothing was moved."]
    lines += _left(result.skipped)
    if verb == "Moved" and result.done and result.batch is not None:
        lines += ["", f"To undo this: downkeep undo {result.batch}"]
    return (1 if result.skipped and not result.done else 0), "\n".join(lines)


def _left(stays: list[Stay]):
    if not stays:
        return []
    lines = ["", f"Left alone ({_count(len(stays))})"]
    lines += [f"  {clean(stay.name)}  ({clean(stay.why)})" for stay in stays[:ROWS]]
    return lines + ([f"  and {len(stays) - ROWS} more"] if len(stays) > ROWS else [])


def _count(count, one="file"):
    return f"{count} {one}" if count == 1 else f"{count} {one}s"


def _top(value):
    if not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError("must be a whole number, 0 or more")
    return int(value)
