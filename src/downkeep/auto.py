import inspect
import os
import time
from dataclasses import replace

from downkeep import engine, schedule
from downkeep.model import ActionError, Busy, Result
from downkeep.origin import find_hosts
from downkeep.scan import FileRecord, ScanError, check_folder, scan
from downkeep.sorting import RULES_KEY, match, plan, saved_rules

DAY_NS = 86_400 * 1_000_000_000
STEADY_NS = 60 * 1_000_000_000
HOUR_NS = 3_600 * 1_000_000_000
# Time between two runs counts for at most this much, so a clock that jumps ahead can't make files old. A file
# waiting N days has been seen by more than N * 6 runs. A computer that is off overnight still gets there, later.
LONGEST_GAP = 4 * HOUR_NS
MOST = 200
MOST_UNDONE = 5000
NOT_HERE = "Moving files is not available on this system yet"

# Each setting has one writer, so a run and the window never overwrite each other's:
STATE = "auto"  # turn_on and turn_off: {"on", "folder", "inode", "since"}
LEDGER = "auto_files"  # turn_on and run, holding the lock: {"folder", "last", "ahead", "files": {name: {"left", "inode", ...}}}
LOG = "auto_log"  # run: {"last", "waiting", "problem"}
UNDONE = "auto_undone"  # note_undo: {"names", "strikes": {rule: [batch]}, "stopped": [rule]}


def status(store, folder: str | os.PathLike | None = None) -> dict:
    """Plain values for the window. "folder" is the one being tidied, which may not be the one shown. "note" is what
    the person should know, or ""."""
    state, log = store.get(STATE) or {}, store.get(LOG) or {}
    available, why = _available()
    on = bool(state.get("on"))
    notes, upcoming = [], None
    if on:
        if log.get("problem"):
            notes.append(log["problem"])
        if available and not schedule.installed_command():
            notes.append("The hourly check is missing. Turn automatic tidying off and on again.")
        elif available:
            upcoming = _next(log.get("last"), time.time_ns())
    try:
        rules = saved_rules(store)
    except ActionError:
        rules = []
    notes += _stopped(rules, (store.get(UNDONE) or {}).get("stopped", ()))
    return {
        "available": available, "why": why, "on": on, "next": upcoming,
        "waiting": log.get("waiting", 0) if on else 0, "folder": state.get("folder") if on else None,
        "note": " ".join(notes), "summary": _summary(on, rules),
    }


def _summary(on, rules):
    if not on:
        return "Off. Nothing moves by itself."
    days = sorted({rule["days"] for rule in rules if rule["on"] and rule["auto"]})
    if not days:
        return "On, but no folder is set to take new files by itself, so nothing moves."
    if days[-1] == 0:
        return "On. Every hour, new files move to their folder, even when Downkeep is closed."
    if len(days) > 1:
        return (f"On. Every hour, new files move to their folder once they are older than its wait ({days[0]} to "
                f"{days[-1]} days), even when Downkeep is closed.")
    return f"On. Every hour, new files older than {_days(days[0])} move to their folder, even when Downkeep is closed."


def _days(count):
    return "1 day" if count == 1 else f"{count} days"


def turn_on(store, folder: str | os.PathLike) -> None:
    """Everything in the folder now counts as already here and is never moved automatically."""
    available, why = _available()
    if not available:
        raise ActionError(why)
    try:
        root = str(check_folder(folder))
    except ScanError as error:
        raise ActionError(str(error)) from error
    saved_rules(store)
    now, inode = time.time_ns(), os.stat(root).st_ino
    state = store.get(STATE) or {}
    again = state.get("on") and (state["folder"], state["inode"]) == (root, inode)
    if not again:
        try:
            with store.lock():
                store.set(LEDGER, _all_left(root, now))
        except OSError as error:
            raise ActionError(f"Can't read {root}: {error.strerror or error}") from error
        store.set(LOG, {})
    schedule.install(schedule.command())
    if not again:
        store.set(STATE, {"on": True, "folder": root, "inode": inode, "since": now})


def turn_off(store) -> None:
    """Off is saved first: even if the schedule can't be removed, a run then does nothing."""
    store.set(STATE, (store.get(STATE) or {}) | {"on": False})
    schedule.remove()


def run(store, now_ns: int | None = None) -> Result:
    """One scheduled pass. A failure is kept for status and the next pass starts fresh."""
    now = time.time_ns() if now_ns is None else now_ns
    state = store.get(STATE) or {}
    if not state.get("on"):
        return Result()
    log = {"last": now, "waiting": (store.get(LOG) or {}).get("waiting", 0), "problem": ""}
    result = Result()
    try:
        moves, rules, log["waiting"] = _choose(store, state, now)
        if moves and store.get(STATE) == state:
            result = _apply(store, state, moves)
            log["waiting"] -= len(result.done)
            if result.batch is not None:
                store.set(_batch_key(result.batch), {name: rules[name] for name in result.done})
    except Busy:
        return result
    except (ActionError, ScanError) as error:
        log["problem"] = f"The last check stopped: {error}"
    except OSError as error:
        log["problem"] = f"The last check stopped: {error.strerror or type(error).__name__}"
    except Exception as error:
        log["problem"] = f"The last check stopped unexpectedly ({type(error).__name__})."
        raise
    finally:
        store.set(LOG, log)
    return result


def note_undo(store, batch: int) -> None:
    """Undone files stay for good. Two undone automatic passes turn a rule's automatic moves off."""
    record = {"names": [], "strikes": {}, "stopped": []} | (store.get(UNDONE) or {})
    folder = (store.get(STATE) or {}).get("folder")
    moved_by = store.get(_batch_key(batch)) or {}
    rules = set()
    for op in store.operations(batch):
        info = _came_back(op)
        if not info:
            continue
        name = os.path.basename(op.source)
        store.leave(FileRecord(name, op.source, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, info.st_nlink))
        rules |= {moved_by.pop(name, None)} - {None}
        if os.path.dirname(op.source) == folder and name not in record["names"]:
            record["names"] = [*record["names"], name][-MOST_UNDONE:]
    for rule in sorted(rules):
        batches = {*record["strikes"].get(rule, ()), batch}
        record["strikes"][rule] = sorted(batches)
        if len(batches) >= 2:
            del record["strikes"][rule]
            record["stopped"] = sorted({*record["stopped"], rule})
            _stop(store, rule)
    store.set(UNDONE, record)
    store.set(_batch_key(batch), moved_by)


# Choosing


def _choose(store, state, now):
    """The moves that are due, the rule behind each, and how many files are waiting (those included)."""
    root = str(check_folder(state["folder"]))
    if root != state["folder"] or os.stat(root).st_ino != state["inode"]:
        raise ActionError("the folder was replaced. Turn automatic tidying off and on again.")
    if now < state["since"]:
        raise ActionError("the computer's clock is set to a date before automatic tidying was turned on.")
    rules = saved_rules(store)
    with store.lock():
        snapshot = scan(root, now)
        ledger = _update(store, snapshot, now)
        fresh = [file for file in snapshot.files if not ledger["files"].get(file.name, {"left": True})["left"]]
        needs_sites = any(rule["on"] and rule["sites"] for rule in rules)
        hosts = find_hosts(file.path for file in fresh) if needs_sites else {}
        found = [(file, match(file, rules, hosts.get(file.path))) for file in fresh]
        waiting = {file: hit[0] for file, hit in found if hit and hit[0]["auto"]}
        due = [file for file, rule in waiting.items() if _due(file, ledger["files"][file.name], rule["days"], now)]
        moves, _ = plan(replace(snapshot, files=tuple(due)), rules, hosts)
        # Files that couldn't be moved last time go to the back, so they never block the others
        moves.sort(key=lambda move: (ledger["files"][move.file.name]["tried"], move.file.name))
        for move in moves[:MOST]:
            ledger["files"][move.file.name] |= {"tried": now}
        store.set(LEDGER, ledger)
    return moves[:MOST], {file.name: rule["id"] for file, rule in waiting.items()}, len(waiting)


def _apply(store, state, moves):
    """Stops between two files as soon as automatic tidying is turned off or moved to another folder."""
    extra = {}
    if "should_stop" in inspect.signature(engine.apply_moves).parameters:
        extra["should_stop"] = lambda: store.get(STATE) != state
    return engine.apply_moves(store, state["folder"], moves, "auto", **extra)


def _due(file, entry, days, now):
    """Old enough both by the time Downkeep watched it and by its own date, and unchanged for a minute."""
    wait = days * DAY_NS
    return entry["waited"] >= wait and now - file.mtime_ns >= wait and now - entry["same"] >= STEADY_NS


def _update(store, snapshot, now):
    """Notes new files and what changed. A file that is already here is known by its name or its inode, because
    editing can change either one. Device numbers are not used: some disks get new ones after a restart."""
    ledger = store.get(LEDGER)
    if not ledger or ledger["folder"] != snapshot.folder:
        return _all_left(snapshot.folder, now)
    before, finished = ledger["files"], {file.name: file for file in snapshot.files}
    left_inodes = {entry["inode"] for entry in before.values() if entry["left"]} - {0}
    undone = set((store.get(UNDONE) or {}).get("names", ()))
    # After the clock went back, time already counted once is not counted again
    last, ahead = ledger["last"], ledger.get("ahead", ledger["last"])
    gap = min(max(now - (ahead if last < ahead <= now else last), 0), LONGEST_GAP)
    behind = now < ahead
    files = {}
    for name, inode in _names(snapshot.folder).items():
        entry, file = before.get(name), finished.get(name)
        if (entry and entry["left"]) or inode in left_inodes or name in undone or (file and store.is_left(file)):
            files[name] = {"left": True, "inode": inode}
        elif file:
            files[name] = _seen(entry, file, now, gap, behind)
    return {"folder": snapshot.folder, "last": now, "ahead": max(ahead, now), "files": files}


def _all_left(root, now):
    files = {name: {"left": True, "inode": inode} for name, inode in _names(root).items()}
    return {"folder": root, "last": now, "ahead": now, "files": files}


def _seen(entry, file, now, gap, behind):
    if not entry or entry["inode"] != file.inode:
        return {"left": False, "inode": file.inode, "size": file.size, "mtime": file.mtime_ns,
                "waited": 0, "same": now, "tried": 0, "behind": behind}
    # First seen while the clock was behind: the time back to the last good check may be from before it came
    counted = 0 if entry["behind"] and not behind else gap
    entry = entry | {"waited": entry["waited"] + counted, "behind": entry["behind"] and behind}
    # A clock that went back would make the file look steady for longer than it was
    if (entry["size"], entry["mtime"]) != (file.size, file.mtime_ns) or entry["same"] > now:
        return entry | {"size": file.size, "mtime": file.mtime_ns, "same": now}
    return entry


def _names(root):
    with os.scandir(root) as listing:
        return {entry.name: entry.inode() for entry in listing if not entry.is_dir(follow_symlinks=False)}


# Undo


def _batch_key(batch):
    return f"auto_batch_{int(batch)}"


def _came_back(op):
    if op.action != "move" or op.state != "DONE" or not op.identity:
        return None
    try:
        info = os.lstat(op.source)
    except OSError:
        return None
    return info if info.st_ino == op.identity.inode else None


def _stop(store, rule):
    rules = saved_rules(store)
    store.set(RULES_KEY, [item | {"auto": item["auto"] and item["id"] != rule} for item in rules])


def _stopped(rules, stopped):
    return [f"New files no longer go to {rule['folder']} by themselves, because you undid that twice."
            for rule in rules if rule["id"] in stopped and rule["on"] and not rule["auto"]]


# Words for the person


def _available():
    return schedule.available() if engine.SUPPORTED else (False, NOT_HERE)


def _next(last, now):
    if not last or now < last:
        return "within the next hour"
    minutes = (last + HOUR_NS - now) // (60 * 1_000_000_000)
    return f"in about {minutes} minutes" if minutes > 1 else "in a few minutes"
