import os
import shutil
import subprocess
import sys
import webbrowser
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
