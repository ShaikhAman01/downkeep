import os
import re
import sys
from pathlib import Path


def downloads_folder() -> Path:
    if sys.platform == "win32":
        found = _windows()
    elif sys.platform == "darwin":
        found = None
    else:
        found = _xdg()
    return found or Path.home() / "Downloads"


def _xdg():
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    try:
        text = Path(base, "user-dirs.dirs").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = re.search(r'^\s*XDG_DOWNLOAD_DIR="([^"]+)"', text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1)
    if value == "$HOME" or value.startswith("$HOME/"):
        path = Path.home() / value[len("$HOME"):].lstrip("/")
    else:
        path = Path(value)
    return path if path.is_absolute() and path != Path.home() else None


def _windows():
    import ctypes
    import uuid
    from ctypes import wintypes

    folder_id = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
    buffer = ctypes.create_string_buffer(folder_id, 16)
    result = ctypes.c_wchar_p()
    shell = ctypes.windll.shell32.SHGetKnownFolderPath
    shell.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.restype = ctypes.c_long
    release = ctypes.windll.ole32.CoTaskMemFree
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    try:
        status = shell(buffer, 0, None, ctypes.byref(result))
        return Path(result.value) if status == 0 and result.value else None
    finally:
        release(ctypes.cast(result, ctypes.c_void_p))
