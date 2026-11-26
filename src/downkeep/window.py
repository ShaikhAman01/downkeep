import errno
import os
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

LINUX_BROWSERS = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
    "microsoft-edge-stable", "microsoft-edge", "brave", "brave-browser",
)
MAC_BROWSERS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")
WINDOWS_BROWSERS = (
    r"Microsoft\Edge\Application\msedge.exe",
    r"Google\Chrome\Application\chrome.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
)


def open_window(url: str) -> bool:
    """A window without tabs where possible, otherwise the normal browser."""
    program = _app_browser()
    if program:
        try:
            _start([program, f"--app={url}", "--window-size=1080,780"])
            return True
        except OSError:
            pass
    return webbrowser.open(url)


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
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
