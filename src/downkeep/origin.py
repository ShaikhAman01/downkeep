import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Iterable
from urllib.parse import unquote_to_bytes, urlsplit

LIMIT = 65_536
BATCH = 200
GIO_ATTRIBUTE = b"metadata::download-uri"


def find_hosts(paths: Iterable[str]) -> dict[str, str]:
    """Best guess at the site each file came from. A replaced file can keep its old site."""
    paths = list(paths)
    if sys.platform == "win32":
        return _one_by_one(paths, _windows)
    if sys.platform == "darwin":
        return _one_by_one(paths, _macos)
    found = _one_by_one(paths, _linux_attribute)
    found |= _gio([path for path in paths if path not in found])
    return found


def host_of(url: str) -> str | None:
    """Only the site name is kept: paths and queries can hold private tokens."""
    try:
        parts = urlsplit(url.strip().removeprefix("blob:"))
        host = (parts.hostname or "").rstrip(".")
        host = host.encode("idna").decode("ascii") if not host.isascii() else host
    except (UnicodeError, ValueError):
        return None
    if parts.scheme not in ("http", "https") or not host or len(host) > 253:
        return None
    return host if all(char.isalnum() or char in "-._:" for char in host) else None


def _one_by_one(paths, read):
    found = {}
    for path in paths:
        host = read(path)
        if host:
            found[path] = host
    return found


def _first_host(urls):
    return next((host for host in map(host_of, urls) if host), None)


def _linux_attribute(path):
    try:
        raw = os.getxattr(path, "user.xdg.origin.url", follow_symlinks=False)
    except (AttributeError, OSError):
        return None
    return host_of(raw[:LIMIT].decode("utf-8", "replace"))


def _gio(paths):
    program = shutil.which("gio")
    if not program:
        return {}
    # gio prints names as they are, so a line break in one could fake a result
    by_bytes = {os.fsencode(path): path for path in paths if path.isprintable() or _only_odd_bytes(path)}
    keys = list(by_bytes)
    found = {}
    for start in range(0, len(keys), BATCH):
        command = [program, "info", "-a", GIO_ATTRIBUTE.decode(), "--", *keys[start : start + BATCH]]
        try:
            done = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "LC_ALL": "C"},
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            break
        for key, host in _parse_gio(done.stdout).items():
            if key in by_bytes:
                found[by_bytes[key]] = host
    return found


def _only_odd_bytes(path):
    return all(char.isprintable() or "\udc80" <= char <= "\udcff" for char in path)


def _parse_gio(output):
    found = {}
    current = None
    for line in output.split(b"\n"):
        if line.startswith(b"uri: file://"):
            current = unquote_to_bytes(line[len(b"uri: file://") :])
        elif current and line.startswith(b"  " + GIO_ATTRIBUTE + b": "):
            url = line[len(GIO_ATTRIBUTE) + 4 :]
            host = host_of(url[:LIMIT].decode("utf-8", "replace"))
            if host:
                found[current] = host
            current = None
    return found


def _macos(path):
    import ctypes

    read = ctypes.CDLL(None, use_errno=True).getxattr
    read.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
    ]
    read.restype = ctypes.c_ssize_t
    buffer = ctypes.create_string_buffer(LIMIT)
    no_follow = 1
    size = read(os.fsencode(path), b"com.apple.metadata:kMDItemWhereFroms", buffer, LIMIT, 0, no_follow)
    return _parse_where_froms(buffer.raw[:size]) if size > 0 else None


def _parse_where_froms(raw):
    try:
        values = plistlib.loads(raw)
    except Exception:
        return None
    if not isinstance(values, list):
        return None
    return _first_host(value for value in values if isinstance(value, str))


def _windows(path):
    try:
        with open(path + ":Zone.Identifier", encoding="utf-8", errors="replace") as stream:
            return _parse_zone(stream.read(LIMIT))
    except (OSError, ValueError):
        return None


def _parse_zone(text):
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        values.setdefault(key.strip().lower(), value.strip())
    return _first_host(values[key] for key in ("hosturl", "referrerurl") if key in values)
