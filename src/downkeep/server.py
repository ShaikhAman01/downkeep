import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from downkeep.evidence import find_duplicates, find_extracted
from downkeep.origin import find_hosts
from downkeep.report import build, clean
from downkeep.scan import ScanError, scan

HOST = "127.0.0.1"
MAX_BODY = 16_384
CODE_SECONDS = 120
CLOSE_SECONDS = 4
BEAT_SECONDS = 1

PAGES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class App:
    """One folder, one window. The launch code works once and is swapped for a key the page keeps."""

    def __init__(self, folder: str, show: Callable[[str], None], clock: Callable[[], float] = time.monotonic):
        self.folder = folder
        self.show = show
        self.clock = clock
        self.code = secrets.token_urlsafe(32)
        self.key: str | None = None
        self.paths: dict[str, str] = {}
        self.pages = 0
        self.started = self.seen = clock()
        self.working = 0
        self.lock = threading.Lock()
        self.scanning = threading.Lock()

    def open_session(self, code: object) -> str | None:
        with self.lock:
            fresh = self.clock() - self.started <= CODE_SECONDS
            if self.key or not fresh or not isinstance(code, str) or not _equal(code, self.code):
                return None
            self.key = secrets.token_urlsafe(32)
            self.seen = self.clock()
            return self.key

    def allows(self, key: str | None) -> bool:
        with self.lock:
            return bool(self.key and key and _equal(key, self.key))

    def arrive(self) -> None:
        with self.lock:
            self.pages += 1

    def leave(self) -> None:
        with self.lock:
            self.pages -= 1
            self.seen = self.clock()

    def finished(self) -> bool:
        with self.lock:
            now = self.clock()
            if self.working:
                return False
            if self.key is None:
                return now - self.started > CODE_SECONDS
            return self.pages == 0 and now - self.seen > CLOSE_SECONDS

    def report(self) -> dict:
        with self.lock:
            self.working += 1
        try:
            with self.scanning:
                snapshot = scan(self.folder)
                result = build(
                    snapshot,
                    hosts=find_hosts(file.path for file in snapshot.files),
                    duplicates=find_duplicates(snapshot.files),
                    extracted=find_extracted(snapshot.files, snapshot.folder),
                )
            with self.lock:
                self.paths = {file.name: file.path for file in snapshot.files}
            return result
        finally:
            with self.lock:
                self.working -= 1
                self.seen = self.clock()

    def show_file(self, name: object) -> bool:
        with self.lock:
            path = self.paths.get(name) if isinstance(name, str) else None
        if path is None or not os.path.lexists(path):
            return False
        self.show(path)
        return True


def make_server(app: App) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((HOST, 0), handler)
    server.daemon_threads = True
    return server


def _equal(a, b):
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode())


class _Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "downkeep"
    sys_version = ""
    timeout = 10

    def do_GET(self):
        if not self._from_this_page():
            return
        page = PAGES.get(self.path)
        if page is None:
            return self._json(404, {"error": "not found"})
        name, kind = page
        self._send(200, files("downkeep").joinpath("page", name).read_bytes(), kind)

    def do_POST(self):
        if not self._from_this_page():
            return
        body = self._body()
        if body is None:
            return
        if self.path == "/api/session":
            key = self.app.open_session(body.get("code"))
            return self._json(200, {"key": key}) if key else self._json(403, {"error": "link expired"})
        if not self.app.allows(self.headers.get("X-Downkeep-Key")):
            return self._json(403, {"error": "not allowed"})
        action = {
            "/api/report": self._report,
            "/api/show": self._show,
            "/api/stay": self._stay,
        }.get(self.path)
        if action is None:
            return self._json(404, {"error": "not found"})
        action(body)

    def _report(self, body):
        try:
            self._json(200, self.app.report())
        except ScanError as error:
            self._json(409, {"error": clean(str(error))})
        except Exception:
            self._json(500, {"error": "Couldn't finish checking"})

    def _show(self, body):
        try:
            found = self.app.show_file(body.get("name"))
        except OSError:
            return self._json(500, {"error": "Your file manager didn't open"})
        self._json(200, {}) if found else self._json(404, {"error": "That file isn't there any more"})

    def _stay(self, body):
        # The page keeps this open. When it closes, however it closes, the next write fails.
        self.app.arrive()
        try:
            self._head(200, "text/plain")
            while True:
                self.wfile.write(b".")
                time.sleep(BEAT_SECONDS)
        except OSError:
            pass
        finally:
            self.app.leave()

    def _from_this_page(self):
        here = f"{HOST}:{self.server.server_address[1]}"
        hosts = self.headers.get_all("Host") or []
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        # A changed Host means DNS rebinding; another Origin or site means another page is asking
        if hosts != [here] or origin not in (None, f"http://{here}") or site not in (None, "same-origin", "none"):
            self._json(403, {"error": "not allowed"})
            return False
        return True

    def _body(self):
        length = self.headers.get("Content-Length", "")
        if self.headers.get_content_type() != "application/json" or not (length.isascii() and length.isdigit()):
            self._json(400, {"error": "bad request"})
            return None
        if int(length) > MAX_BODY:
            self._json(413, {"error": "too large"})
            return None
        try:
            body = json.loads(self.rfile.read(int(length)))
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            self._json(400, {"error": "bad request"})
            return None
        return body

    def _json(self, status, value):
        self._send(status, json.dumps(value).encode(), "application/json")

    def _send(self, status, content, kind):
        try:
            self._head(status, kind, len(content))
            self.wfile.write(content)
        except ConnectionError:
            pass

    def _head(self, status, kind, length=None):
        self.close_connection = True
        self.send_response(status)
        for name, value in HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", kind)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.end_headers()

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "bad request"})

    def log_message(self, format, *args):
        pass
