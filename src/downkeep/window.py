import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Chromium first: the system's package manager updates it, so it never asks to be updated itself
LINUX_BROWSERS = (
    "chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
    "brave-browser", "brave", "microsoft-edge-stable", "microsoft-edge",
)
MAC_BROWSERS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")
WINDOWS_BROWSERS = (
    r"Microsoft\Edge\Application\msedge.exe",
    r"Google\Chrome\Application\chrome.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
)


# A profile of Downkeep's own, so the window brings no sign-in, sync, extensions, first-run or update screens
APP_FLAGS = (
    "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-extensions",
    "--disable-default-apps", "--disable-background-networking", "--disable-component-update",
    "--disable-features=Translate,MediaRouter,OptimizationHints,SigninInterception", "--password-store=basic",
    "--window-size=1080,780",
)


def open_window(url: str, avoid: str | None = None):
    """A window without tabs where possible, otherwise the normal browser. Returns the window's own browser process
    when there is one, so the caller can tell when it has gone, else whether a browser opened."""
    program = _app_browser()
    if program:
        profile = browser_profile(program, avoid)
        flags = [f"--user-data-dir={profile}", *APP_FLAGS] if profile else ["--window-size=1080,780"]
        try:
            return _start([program, f"--app={app_link(url)}", *flags])
        except OSError:
            pass
    return webbrowser.open(url)


def app_link(url: str) -> str:
    """Marks the link as opened in Downkeep's own window: only there may the page close itself."""
    page, mark, code = url.partition("#")
    return f"{page.split('?')[0]}?window=app{mark}{code}"


def browser_profile(program: str, avoid: str | None = None) -> Path | None:
    """A private folder for the window's browser, never a link and never inside the folder being tidied."""
    from downkeep.store import data_folder

    snap = _snap_name(program)
    base = Path.home() / "snap" / snap / "common" / "downkeep" if snap else data_folder()
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        place = base / "browser"
        try:
            os.mkdir(place, 0o700)
        except FileExistsError:
            pass
        if not all(_plain_folder(path) for path in (base, place)):
            return None
        if hasattr(os, "geteuid"):
            os.chmod(place, 0o700, follow_symlinks=False)
        if avoid and _inside(os.path.realpath(place), os.path.realpath(avoid)):
            return None
        return place
    except OSError:
        return None


def _plain_folder(path):
    info = os.lstat(path)
    mine = not hasattr(os, "geteuid") or info.st_uid == os.geteuid()
    return stat.S_ISDIR(info.st_mode) and not _is_link(Path(path)) and mine


def _inside(path, folder):
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:
        return False


def _snap_name(program):
    """Snap browsers can't reach hidden folders, so their profile goes where the snap may write."""
    if sys.platform != "linux":
        return None
    if program.startswith("/snap/bin/"):
        return os.path.basename(program)
    try:
        if os.path.realpath(program) == "/usr/bin/snap":
            return os.path.basename(program)
        with open(program, "rb") as file:
            head = file.read(4096)
    except OSError:
        return None
    found = re.search(rb"/snap/bin/([a-z0-9-]+)", head) if b"\0" not in head else None
    return found.group(1).decode() if found else None


def show_in_folder(path: str) -> None:
    """Selects the file in the file manager. Never opens the file itself."""
    if sys.platform == "win32":
        _start(f'explorer.exe /select,"{path}"')
    elif sys.platform == "darwin":
        _start(["/usr/bin/open", "-R", path])
    elif not _ask_file_manager(path):
        _start(["xdg-open", os.path.dirname(path)])


def write_link(place: str, url: str) -> None:
    """For testing the packaged app: the link goes to a new private file instead of a browser."""
    path = Path(os.path.abspath(place))
    # Windows has no O_NOFOLLOW, and there O_EXCL follows a dangling link, so links are refused first
    if os.path.lexists(path) or any(_is_link(part) for part in (path, path.parent)):
        raise FileExistsError(errno.EEXIST, "already exists or is a link", str(path))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with open(descriptor, "w", encoding="utf-8") as file:
        file.write(url)
        made = os.fstat(file.fileno())
    if os.path.realpath(path) != os.path.join(os.path.realpath(path.parent), path.name):
        try:
            if os.path.samestat(os.stat(path), made):
                os.unlink(path)
        except OSError:
            pass
        raise FileExistsError(errno.EEXIST, "the link file went somewhere else", str(path))


def _is_link(path):
    return path.is_symlink() or path.is_junction()


def tell(text: str) -> None:
    """Shows a message without a terminal, and always keeps a copy in the log."""
    log(text)
    try:
        _message(text)
    except OSError:
        pass


def log(text: str) -> None:
    """Only this account can read the log: it can name files."""
    try:
        place = _log_file()
        place.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(place, flags, 0o600)
        with open(descriptor, "a", encoding="utf-8") as file:
            if hasattr(os, "fchmod"):
                os.fchmod(file.fileno(), 0o600)
            file.write(f"{datetime.now():%Y-%m-%d %H:%M} {text}\n")
    except OSError:
        pass


def _log_file():
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local", "Downkeep", "log.txt")
    if sys.platform == "darwin":
        return home / "Library" / "Logs" / "Downkeep.log"
    base = Path(os.environ.get("XDG_STATE_HOME") or "")
    return (base if base.is_absolute() else home / ".local" / "state") / "downkeep" / "log.txt"


def _message(text):
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Downkeep", 0)
    elif sys.platform == "darwin":
        script = 'on run argv\ndisplay dialog (item 1 of argv) with title "Downkeep" buttons {"OK"}\nend run'
        _start(["/usr/bin/osascript", "-e", script, text])
    elif shutil.which("zenity"):
        _start(["zenity", "--info", "--no-markup", "--title=Downkeep", f"--text={text}"])
    elif shutil.which("kdialog"):
        _start(["kdialog", "--title", "Downkeep", "--msgbox", text])
    elif shutil.which("notify-send"):
        _start(["notify-send", "Downkeep", text])


def _app_browser():
    if sys.platform == "win32":
        bases = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        places = [Path(base, name) for name in WINDOWS_BROWSERS for base in bases if base]
    elif sys.platform == "darwin":
        places = [Path("/Applications", f"{name}.app", "Contents/MacOS", name) for name in MAC_BROWSERS]
    else:
        return next(filter(None, map(shutil.which, LINUX_BROWSERS)), None)
    return next((str(place) for place in places if place.is_file()), None)


def _ask_file_manager(path):
    if not shutil.which("dbus-send"):
        return False
    # dbus-send splits lists at commas, so those are escaped too
    uri = "file://" + quote(os.fsencode(path), safe="/")
    command = [
        "dbus-send", "--session", "--print-reply", "--reply-timeout=3000", "--dest=org.freedesktop.FileManager1",
        "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
        f"array:string:{uri}", "string:",
    ]
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _start(command):
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
