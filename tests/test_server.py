import http.client
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from importlib.resources import files
from pathlib import Path
from unittest import mock

from downkeep import server, window
from downkeep.server import App, make_server
from tests.helpers import FolderTest
from tests.test_actions import FakeActions


SCRIPTS = ["app.js", "ask.js", "auto.js", "clutter.js", "core.js", "rules.js", "runs.js", "tidy.js"]


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class ServerTest(FolderTest):
    def setUp(self):
        super().setUp()
        self.add("a.txt", b"same")
        self.add("b.txt", b"same")
        self.shown = []
        self.clock = Clock()
        patch = mock.patch("downkeep.server.find_hosts", return_value={})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("downkeep.server.BEAT_SECONDS", 0.02)
        patch.start()
        self.addCleanup(patch.stop)
        self.app = App(str(self.folder), self.shown.append, self.clock, home=self.folder.parent, actions=FakeActions(), same_user=lambda client, port: True)
        self.server = make_server(self.app)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.here = f"127.0.0.1:{self.port}"

    def ask(self, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        content = raw if raw is not None else None if body is None else json.dumps(body).encode()
        base = {} if content is None else {"Content-Type": "application/json"}
        connection.request(method, path, content, {**base, **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read(), response.headers

    def key(self):
        status, content, _ = self.ask("POST", "/api/session", {"code": self.app.code})
        self.assertEqual(status, 200)
        return json.loads(content)["key"]

    def call(self, path, key, body=None, **headers):
        return self.ask("POST", path, body or {}, {"X-Downkeep-Key": key, **headers})

    def test_only_this_computer_can_connect(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_pages(self):
        scripts = {f"/{name}": "text/javascript" for name in SCRIPTS}
        for path, kind in {"/": "text/html", "/style.css": "text/css", **scripts, "/icon.svg": "image/svg+xml"}.items():
            with self.subTest(path=path):
                status, content, headers = self.ask("GET", path)
                self.assertEqual((status, headers.get_content_type()), (200, kind))
                self.assertTrue(content)

    def test_every_answer_is_locked_down(self):
        for method, path, body in [("GET", "/", None), ("GET", "/missing", None), ("POST", "/api/report", {}), ("PUT", "/", None)]:
            with self.subTest(method=method, path=path):
                headers = self.ask(method, path, body)[2]
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe", headers["Content-Security-Policy"])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertFalse([name for name in headers if name.lower().startswith("access-control")])

    def test_pages_hold_nothing_private(self):
        for path in server.PAGES:
            self.assertNotIn(b"a.txt", self.ask("GET", path)[1])
            self.assertNotIn(self.app.code.encode(), self.ask("GET", path)[1])

    def test_other_paths_are_not_found(self):
        for path in ["/../server.py", "/%2e%2e/server.py", "//etc/passwd", "/index.html", "/app.js?x", "/api/report"]:
            with self.subTest(path=path):
                self.assertEqual(self.ask("GET", path)[0], 404)

    def test_wrong_host_is_refused(self):
        key = self.key()
        for host in ["evil.example", f"localhost:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port}.evil.example", ""]:
            with self.subTest(host=host):
                self.assertEqual(self.ask("GET", "/", headers={"Host": host})[0], 403)
                self.assertEqual(self.call("/api/report", key, Host=host)[0], 403)

    def test_two_hosts_or_no_host_are_refused(self):
        for lines in ["", f"Host: {self.here}\r\nHost: evil.example\r\n"]:
            with self.subTest(lines=lines), socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
                raw.sendall(f"GET / HTTP/1.1\r\n{lines}\r\n".encode())
                self.assertIn(b" 403 ", raw.recv(200))

    def test_other_sites_are_refused(self):
        key = self.key()
        for headers in [
            {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": f"http://localhost:{self.port}"},
            {"Origin": f"https://{self.here}"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.call("/api/report", key, **headers)[0], 403)
                self.assertEqual(self.ask("GET", "/", headers=headers)[0], 403)

    def test_this_page_is_allowed(self):
        key = self.key()
        self.assertEqual(self.call("/api/report", key, **{"Origin": f"http://{self.here}", "Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_preflight_is_refused(self):
        status, _, headers = self.ask("OPTIONS", "/api/report", headers={"Origin": "https://evil.example"})
        self.assertGreaterEqual(status, 400)
        self.assertIsNone(headers["Access-Control-Allow-Origin"])

    def test_code_works_once(self):
        self.key()
        self.assertEqual(self.ask("POST", "/api/session", {"code": self.app.code})[0], 403)

    def test_wrong_codes(self):
        for code in ["", "x", None, 5, [], {}, self.app.code[:-1], self.app.code + "x", "\u00e9\udcff"]:
            with self.subTest(code=code):
                self.assertEqual(self.ask("POST", "/api/session", {"code": code})[0], 403)
        self.key()

    def test_code_expires(self):
        self.clock.now += server.CODE_SECONDS + 1
        self.assertEqual(self.ask("POST", "/api/session", {"code": self.app.code})[0], 403)

    def test_nothing_works_without_the_key(self):
        self.assertEqual(self.call("/api/report", "")[0], 403)
        key = self.key()
        for path in ["/api/report", "/api/show", "/api/folders", "/api/choose", "/api/stay", "/api/unknown"]:
            for wrong in ["", "x", self.app.code, key[:-1]]:
                with self.subTest(path=path, wrong=wrong):
                    self.assertEqual(self.call(path, wrong)[0], 403)
            self.assertEqual(self.ask("POST", path, {})[0], 403)
        self.assertEqual(self.call("/api/unknown", key)[0], 404)

    def test_bodies_must_be_small_json_objects(self):
        key = self.key()
        for raw in [b"", b"junk", b"[]", b"null", b"[" * 10_000, b"\xff"]:
            with self.subTest(raw=raw[:10]):
                self.assertEqual(self.ask("POST", "/api/show", raw=raw, headers={"X-Downkeep-Key": key})[0], 400)
        for kind in ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]:
            with self.subTest(kind=kind):
                self.assertEqual(self.call("/api/show", key, **{"Content-Type": kind})[0], 400)
        self.assertEqual(self.call("/api/show", key, **{"Content-Length": "99999999"})[0], 413)
        for length in ["-1", "+5", "1_0", "x"]:
            with self.subTest(length=length):
                self.assertEqual(self.call("/api/show", key, **{"Content-Length": length})[0], 400)

    def test_report(self):
        status, content, _ = self.call("/api/report", self.key())
        result = json.loads(content)
        self.assertEqual((status, result["files"]), (200, 2))
        self.assertEqual(result["duplicates"][0]["names"], ["a.txt", "b.txt"])
        self.assertEqual(result["proven"], {"files": 1, "bytes": 4})

    def test_a_file_proven_twice_is_counted_once(self):
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr("x.txt", b"inside")
        (self.folder / "pack").mkdir()
        self.add("pack/x.txt", b"inside")
        self.add("pack.zip", zipped.getvalue())
        self.add("pack (1).zip", zipped.getvalue())
        result = json.loads(self.call("/api/report", self.key())[1])
        self.assertEqual(len(result["extracted"]), 2)
        self.assertEqual(result["extracted"][0]["examples"], ["x.txt"])
        self.assertEqual(result["proven"], {"files": 3, "bytes": 4 + 2 * len(zipped.getvalue())})

    def test_unreadable_folder(self):
        self.app.folder = str(self.folder / "gone")
        status, content, _ = self.call("/api/report", self.key())
        self.assertEqual(status, 409)
        self.assertIn("Can't open", json.loads(content)["error"])

    def test_folders(self):
        key = self.key()
        (self.folder / "inner").mkdir()
        (self.folder.parent / "Music").mkdir()
        status, content, _ = self.call("/api/folders", key, {"path": []})
        self.assertEqual((status, json.loads(content)), (200, {"folders": ["Downloads", "Music"]}))
        self.assertEqual(json.loads(self.call("/api/folders", key, {"path": ["Downloads"]})[1]), {"folders": ["inner"]})
        for path in [[".."], ["missing"], None, "Music", ["Downloads", ".."]]:
            with self.subTest(path=path):
                self.assertEqual(self.call("/api/folders", key, {"path": path})[0], 409)
                self.assertEqual(self.call("/api/choose", key, {"path": path})[0], 409)
        self.assertEqual(self.call("/api/choose", key, {"path": []})[0], 409)
        self.assertEqual(self.app.folder, str(self.folder))

    def test_choosing_a_folder(self):
        key = self.key()
        other = self.folder.parent / "Music"
        other.mkdir()
        song = other / "song.mp3"
        song.write_bytes(b"la")
        os.utime(song, (1, 1))
        self.call("/api/report", key)
        self.assertEqual(self.call("/api/choose", key, {"path": ["Music"]})[0], 200)
        self.assertEqual(self.call("/api/show", key, {"name": "a.txt"})[0], 404)
        result = json.loads(self.call("/api/report", key)[1])
        self.assertEqual((result["folder"], result["files"]), (str(other), 1))
        self.assertEqual(self.call("/api/show", key, {"name": "song.mp3"})[0], 200)
        self.assertEqual(self.shown, [str(song)])

    def test_choosing_during_a_scan_does_not_mix_folders(self):
        key = self.key()
        (self.folder.parent / "Music").mkdir()
        real = server.find_duplicates

        def switch(files):
            self.app.choose(["Music"])
            return real(files)

        with mock.patch("downkeep.server.find_duplicates", switch):
            self.call("/api/report", key)
        self.assertEqual(self.call("/api/show", key, {"name": "a.txt"})[0], 404)

    def test_two_windows_are_separate(self):
        other = App(str(self.folder), self.shown.append, self.clock, actions=FakeActions())
        second = make_server(other)
        self.addCleanup(second.server_close)
        self.assertNotEqual(second.server_address[1], self.port)
        self.assertEqual(self.ask("POST", "/api/session", {"code": other.code})[0], 403)
        self.assertFalse(other.allows(self.key()))

    def test_session_tells_the_version(self):
        content = self.ask("POST", "/api/session", {"code": self.app.code})[1]
        self.assertRegex(json.loads(content)["version"], r"^\d+\.\d+\.\d+$")

    def test_show_only_knows_names_from_the_report(self):
        key = self.key()
        self.assertEqual(self.call("/api/show", key, {"name": "a.txt"})[0], 404)
        self.call("/api/report", key)
        for name in ["../a.txt", "/etc/passwd", str(self.folder / "a.txt"), "", None, 3, ["a.txt"], "a.txt\0"]:
            with self.subTest(name=name):
                self.assertEqual(self.call("/api/show", key, {"name": name})[0], 404)
        self.assertEqual(self.shown, [])
        self.assertEqual(self.call("/api/show", key, {"name": "a.txt"})[0], 200)
        self.assertEqual(self.shown, [os.path.join(str(self.folder), "a.txt")])

    def test_deleted_file_is_not_shown(self):
        key = self.key()
        self.call("/api/report", key)
        (self.folder / "a.txt").unlink()
        self.assertEqual(self.call("/api/show", key, {"name": "a.txt"})[0], 404)
        self.assertEqual(self.shown, [])

    def test_unexpected_failure_still_answers(self):
        with mock.patch("downkeep.server.find_duplicates", side_effect=OSError("private detail")):
            status, content, _ = self.call("/api/report", self.key())
        self.assertEqual(status, 500)
        self.assertNotIn(b"private", content)

    def test_file_manager_failure(self):
        key = self.key()
        self.call("/api/report", key)
        self.app.show = mock.Mock(side_effect=FileNotFoundError)
        self.assertEqual(self.call("/api/show", key, {"name": "a.txt"})[0], 500)

    def test_stops_when_nobody_opens_it(self):
        self.assertFalse(self.app.finished())
        self.clock.now += server.CODE_SECONDS + 1
        self.assertTrue(self.app.finished())

    def stay(self, key):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", "/api/stay", b"{}", {"Content-Type": "application/json", "X-Downkeep-Key": key})
        page = connection.getresponse()
        self.addCleanup(page.close)
        self.assertEqual((page.status, page.read(2)), (200, b".."))
        return page

    def wait_for_pages(self, count):
        end = time.monotonic() + 5
        while self.app.pages != count and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(self.app.pages, count)

    def test_open_page_keeps_it_running_however_long(self):
        self.stay(self.key())
        self.clock.now += 1_000_000
        self.assertFalse(self.app.finished())

    def test_only_downkeeps_own_window_mark_is_allowed_on_the_page(self):
        self.assertEqual(self.ask("GET", "/?window=app")[0], 200)
        for path in ["/?window=other", "/?window=app&x=1", "/?x"]:
            self.assertEqual(self.ask("GET", path)[0], 404)

    def test_stops_at_once_when_its_own_window_exits(self):
        page = self.stay(self.key())
        self.wait_for_pages(1)
        self.app.window_gone()
        self.assertFalse(self.app.finished())
        page.close()
        self.wait_for_pages(0)
        self.app.window_gone()
        self.assertTrue(self.app.finished())

    def test_a_launcher_that_exits_before_the_page_opens_is_not_the_window(self):
        self.app.window_gone()
        self.clock.now += 1
        self.stay(self.key()).close()
        self.wait_for_pages(0)
        self.assertFalse(self.app.finished())
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertTrue(self.app.finished())

    def test_stops_soon_after_the_page_closes(self):
        self.stay(self.key()).close()
        self.wait_for_pages(0)
        self.assertFalse(self.app.finished())
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertTrue(self.app.finished())

    def test_closing_counts_from_when_the_page_left(self):
        page = self.stay(self.key())
        self.clock.now += 1_000
        page.close()
        self.wait_for_pages(0)
        self.assertFalse(self.app.finished())

    def test_reloading_the_page_keeps_it_running(self):
        key = self.key()
        self.stay(key).close()
        self.wait_for_pages(0)
        self.stay(key)
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertFalse(self.app.finished())

    def test_closing_one_of_two_tabs_keeps_it_running(self):
        key = self.key()
        pages = [self.stay(key), self.stay(key)]
        pages[0].close()
        self.wait_for_pages(1)
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertFalse(self.app.finished())
        pages[1].close()
        self.wait_for_pages(0)
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertTrue(self.app.finished())

    def test_stops_if_the_page_never_stays(self):
        self.key()
        self.assertFalse(self.app.finished())
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertTrue(self.app.finished())

    def test_a_slow_browser_gets_the_full_grace(self):
        self.clock.now += server.CODE_SECONDS - 1
        self.key()
        self.assertFalse(self.app.finished())

    def test_never_stops_during_a_scan(self):
        self.key()
        self.clock.now += server.CLOSE_SECONDS + 1
        self.app.working = 1
        self.assertFalse(self.app.finished())

    def test_staying_is_locked_down_too(self):
        page = self.stay(self.key())
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", page.headers["Content-Security-Policy"])
        self.assertIsNone(page.headers["Access-Control-Allow-Origin"])
        self.assertEqual(self.ask("POST", "/api/stay", {}, {"X-Downkeep-Key": self.app.key, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.app.pages, 1)

    def test_stops_after_a_scan_even_a_failed_one(self):
        key = self.key()
        self.call("/api/report", key)
        self.app.folder = str(self.folder / "gone")
        self.call("/api/report", key)
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertTrue(self.app.finished())

    def test_grace_starts_again_after_a_long_scan(self):
        key = self.key()
        self.clock.now += server.CLOSE_SECONDS + 1
        self.call("/api/report", key)
        self.assertFalse(self.app.finished())

    def test_a_silent_connection_does_not_block_others(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5):
            self.assertEqual(self.ask("GET", "/")[0], 200)

    def test_page_loads_nothing_from_elsewhere(self):
        for name in ["index.html", "style.css", *SCRIPTS, "icon.svg"]:
            text = files("downkeep").joinpath("page", name).read_text(encoding="utf-8")
            text = text.replace("http://www.w3.org/2000/svg", "")
            with self.subTest(name=name):
                self.assertNotRegex(text, r"https?://|//cdn|@import|url\(")
                self.assertNotRegex(text, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|localStorage|new Function|cssText")
                self.assertNotRegex(text, r"\b(alert|confirm|prompt)\(|\u2014|\u2013")
        page = files("downkeep").joinpath("page", "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)|\sstyle=|\son\w+=", page))

    def test_the_header_shows_the_same_logo_as_the_tab(self):
        page = files("downkeep").joinpath("page", "index.html").read_text(encoding="utf-8")
        name = re.search(r'<p class="name">(.*?)</p>', page).group(1)
        self.assertEqual(name, '<img class="logo" src="/icon.svg" alt="" width="26" height="26">Downkeep')
        self.assertIn('<link rel="icon" href="/icon.svg" type="image/svg+xml">', page)

    def test_every_script_of_the_page_is_served_and_nothing_else(self):
        folder = files("downkeep").joinpath("page")
        found = {item.name for item in folder.iterdir() if item.name.endswith(".js")}
        self.assertEqual(found, set(SCRIPTS))
        for name in SCRIPTS:
            for wanted in re.findall(r'from "\./([^"]+)"', folder.joinpath(name).read_text(encoding="utf-8")):
                self.assertIn(f"/{wanted}", server.PAGES)

    def test_every_question_goes_through_one_place(self):
        for name in SCRIPTS:
            text = files("downkeep").joinpath("page", name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertEqual(bool(re.search(r"\bfetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\(", text)), name == "ask.js")

    def test_the_page_asks_only_for_routes_that_exist(self):
        key = self.key()
        self.app.actions = FakeActions()
        asked = set()
        for name in SCRIPTS:
            asked |= set(re.findall(r'"(/api/[a-z/]+)"', files("downkeep").joinpath("page", name).read_text(encoding="utf-8")))
        self.assertGreaterEqual(len(asked), 14)
        for path in asked - {"/api/stay", "/api/session"}:
            with self.subTest(path=path):
                self.assertNotEqual((self.call(path, key)[0], self.call(path, key)[1]), (404, b'{"error": "not found"}'))


FAKE_PAGE = """
const any = new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => "" : k === "then" ? undefined : any), apply: () => any });
function el(tag) {
  const e = { tagName: tag, attrs: {}, children: [], dataset: {}, handlers: {},
    setAttribute(n, v) { this.attrs[n] = String(v); }, getAttribute(n) { return this.attrs[n] ?? null; }, contains() { return false; },
    focus() { globalThis.focused = this; },
    addEventListener(n, f) { (this.handlers[n] ??= []).push(f); },
    append(...c) { for (const child of c) if (typeof child !== "string") child.parent = this; this.children.push(...c); },
    replaceChildren(...c) { this.children = []; this.append(...c); },
    replaceWith(other) { const at = this.parent.children.indexOf(this); other.parent = this.parent; this.parent.children[at] = other; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); },
    querySelectorAll() { return []; },
    get textContent() { return this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); },
    set textContent(t) { this.children = [String(t)]; } };
  return new Proxy(e, { get: (t, k) => (k in t ? t[k] : any) });
}
const picked = {};
globalThis.document = new Proxy({ createElement: el, createElementNS: (ns, tag) => el(tag), querySelector: (s) => (picked[s] ??= el("div")), documentElement: el("html") },
  { get: (t, k) => (k in t ? t[k] : any) });
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
globalThis.SVGElement = class {};
globalThis.addEventListener = () => {};
globalThis.window = { close() { globalThis.closes = (globalThis.closes ?? 0) + 1; } };
const stored = new Map();
Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
  getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, String(v)), removeItem: (k) => stored.delete(k) } });
const asked = [];
const answers = {};
globalThis.fetch = async (path, { body }) => {
  asked.push([path, JSON.parse(body)]);
  const { status = 200, ...value } = answers[path] ?? {};
  return { ok: status === 200, status, json: async () => value };
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const press = async (node, ms = 50) => { for (const f of node.handlers.click ?? []) f({ detail: 0, currentTarget: node }); await sleep(ms); };
const type = async (input, value) => { input.value = value; for (const f of input.handlers.input ?? []) f({}); await sleep(20); };
const page = process.argv[2];
const core = await import(new URL(`file://${page}/core.js`));
const clutter = await import(new URL(`file://${page}/clutter.js`));
const auto = await import(new URL(`file://${page}/auto.js`));
const tidy = await import(new URL(`file://${page}/tidy.js`));
const rules = await import(new URL(`file://${page}/rules.js`));
const panel = document.querySelector("#stage");
const find = (node, test, out = []) => {
  if (typeof node !== "string") { if (test(node)) out.push(node); (node.children ?? []).forEach((child) => find(child, test, out)); }
  return out;
};
const result = await (async () => { %s })();
console.log(JSON.stringify(result));
"""


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class PageTest(unittest.TestCase):
    def run_page(self, body):
        with tempfile.TemporaryDirectory() as folder:
            script = os.path.join(folder, "run.mjs")
            with open(script, "w", encoding="utf-8") as file:
                file.write(FAKE_PAGE % body)
            page = str(files("downkeep").joinpath("page"))
            done = subprocess.run(["node", script, page], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=folder)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_copy_picture_shows_whole_names_and_where_each_one_is(self):
        texts = self.run_page("""
          const lines = (item) => find(core.copyPicture(item, "Downloads"), (node) => node.tagName === "text").map((node) => node.textContent);
          return [
            lines({ name: "Resume_Aman.pdf", keep: "Documents/Resume_Aman.pdf" }),
            lines({ name: "Resume (1).pdf", keep: "Resume.pdf" }),
            lines({ name: "a.pdf", keep: "Work\\\\a.pdf" }),
          ];
        """)
        self.assertEqual(texts, [
            ["Resume_Aman.pdf", "in Downloads", "the copy", "Resume_Aman.pdf", "in Documents", "the one that stays"],
            ["Resume (1).pdf", "in Downloads", "the copy", "Resume.pdf", "in Downloads", "the one that stays"],
            ["a.pdf", "in Downloads", "the copy", "a.pdf", "in Work", "the one that stays"],
        ])

    def test_long_names_wrap_without_losing_a_character(self):
        name = "Quarterly_Report_Final_v3 signed-by-everyone(scan 2).pdf"
        lines = self.run_page("""
          const width = (text) => [...text].length * 10;
          return [%s, "\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9\\u00e9.pdf", "a"].map((name) => core.wrap(name, 100, width));
        """ % json.dumps(name))
        self.assertEqual(["".join(found) for found in lines], [name, "é" * 12 + ".pdf", "a"])
        for found in lines:
            self.assertTrue(all(0 < len(line) <= 10 for line in found), found)
            self.assertFalse(any("…" in line for line in found))
        self.assertIn("Quarterly_", lines[0])
        self.assertTrue(any(line.startswith("(scan") for line in lines[0]), lines[0])

    def test_copy_text_names_the_one_that_stays(self):
        said = self.run_page("""
          return [clutter.copyWhy({ name: "Resume (1).pdf", keep: "Resume.pdf" }), clutter.copyWhy({ name: "a.pdf", keep: "Documents/a.pdf" })];
        """)
        self.assertEqual(said, [
            "Same as Resume.pdf, which stays. Same contents, checked from start to end.",
            "Same as Documents/a.pdf, which stays. Same contents, checked from start to end.",
        ])

    def test_the_file_that_stays_is_marked_in_the_list(self):
        rows = self.run_page("""
          core.state.system = { trash: true, restore: true, tidy: true };
          core.state.report = { folder: "/x/Downloads", files: 3, bytes: 300, extracted: [], skipped: {}, ages: [], types: [],
            sites: { known: [], unknown: { files: 0 } }, duplicates: [{ names: ["Resume.pdf", "Resume (1).pdf"], bytes_each: 100 }],
            biggest: [{ name: "Resume.pdf", bytes: 100 }, { name: "Resume (1).pdf", bytes: 100 }, { name: "a.txt", bytes: 100 }] };
          clutter.showClutter();
          return find(core.list, (node) => node.attrs?.class === "item").map((row) => [
            row.textContent, find(row, (node) => node.attrs?.class === "stays").map((tag) => [tag.tagName, tag.textContent])]);
        """)
        self.assertIn(["Resume.pdfstays100 B", [["small", "stays"]]], rows)
        self.assertIn(["Resume (1).pdf100 B", []], rows)
        self.assertIn(["a.txt100 B", []], rows)

    def test_automatic_intro_follows_the_state(self):
        said = self.run_page("""
          return [
            auto.intro({ on: false }), auto.intro({ on: true, folder: "/x/Downloads" }, false), auto.intro({ on: true, folder: "/x/Other" }, true),
            auto.every([{ days: 1 }]), auto.every([{ days: 0 }]), auto.every([{ days: 1 }, { days: 3 }], "as set for each folder"),
          ];
        """)
        self.assertEqual(said, [
            "Off until you turn it on. It only moves files that arrive after that, using the same folders you approved.",
            "On. It only moves files that arrived after you turned it on, using the folders you approved.",
            "On for Other. It only moves files that arrived there after it was turned on.",
            "Every hour, new files older than 1 day move to their folder, even when Downkeep is closed.",
            "Every hour, new files move to their folder, even when Downkeep is closed.",
            "Every hour, new files old enough, as set for each folder, move to their folder, even when Downkeep is closed.",
        ])

    AUTO = """
      core.state.report = { folder: "/x/Downloads", files: 4 };
      Object.assign(answers, {
        "/api/auto": { available: true, on: %s, folder: "/x/Downloads", waiting: 0, note: "" },
        "/api/rules": { rules: [{ id: "documents", folder: "Documents", on: true, auto: true, days: 1 },
                                { id: "images", folder: "Images", on: true, auto: true, days: 1 },
                                { id: "music", folder: "Music", on: false, auto: true, days: 1 }] },
        "/api/history": { batches: [{ kind: "tidy", batch: 1, when: 1, files: [] }] },
      });
      await auto.showAuto();
      const master = find(panel, (node) => node.attrs?.role === "switch" && node.attrs["aria-label"] === "Automatic tidying")[0];
      const said = () => find(panel, (node) => node.tagName === "p" || node.tagName === "h2").map((node) => node.textContent).join(" | ");
      const first = said();
      const folders = find(panel, (node) => /\\bfolders\\b/.test(node.attrs?.class ?? ""))[0];
      const controls = () => find(folders, (node) => node.tagName === "select" || node.attrs?.role === "switch");
      asked.length = 0;
      await press(master, 300);
    """

    def test_switching_on_asks_first_and_only_turn_on_tells_the_server(self):
        seen = self.run_page(self.AUTO % "false" + """
          const before = asked.map((call) => call[0]);
          const asking = said();
          const buttons = find(panel, (node) => node.tagName === "button" && /main-act|next/.test(node.attrs.class));
          await press(buttons.find((node) => node.textContent === "Turn on"), 300);
          return { first, before, asking, knob: master.attrs["aria-checked"], buttons: buttons.map((node) => node.textContent),
                   after: asked.filter((call) => call[0] === "/api/auto/set"), calls: asked.map((call) => call[0]) };
        """)
        self.assertEqual(seen["knob"], "true")
        self.assertEqual(seen["calls"], ["/api/auto/set", "/api/auto", "/api/rules", "/api/history"])
        self.assertIn("Off until you turn it on.", seen["first"])
        self.assertEqual(seen["before"], [])
        for text in ["Turn on automatic tidying?", "Every hour, new files older than 1 day move to their folder, even when Downkeep is closed.",
                     "Choose which folders take new files, and when:", "to Downloads", "Nothing would move right now.",
                     "The 4 files here now stay where they are."]:
            self.assertIn(text, seen["asking"])
        self.assertNotIn("Music", seen["asking"])
        self.assertEqual(seen["buttons"], ["Turn on", "Cancel"])
        self.assertEqual(seen["after"], [["/api/auto/set", {"on": True, "confirmed": True}]])

    def test_folders_can_change_before_turning_on(self):
        seen = self.run_page(self.AUTO % "false" + """
          core.go.changed = () => {};
          answers["/api/rules/save"] = { rules: [{ id: "documents", folder: "Documents", on: true, auto: false, days: 1 },
                                                 { id: "images", folder: "Images", on: true, auto: false, days: 1 },
                                                 { id: "music", folder: "Music", on: false, auto: true, days: 1 }] };
          const asking = find(panel, (node) => /\\bfolders\\b/.test(node.attrs?.class ?? ""))[0];
          const here = find(asking, (node) => node.tagName === "select" || node.attrs?.role === "switch");
          const turnOn = find(panel, (node) => node.tagName === "button" && node.textContent === "Turn on")[0];
          const enabled = here.map((node) => node.disabled === true);
          await press(here.find((node) => node.attrs.role === "switch" && node.attrs["aria-label"].includes("Images")));
          const now = find(panel, (node) => node.attrs?.class === "now")[0];
          const cancel = find(panel, (node) => node.tagName === "button" && node.textContent === "Cancel")[0];
          await press(cancel);
          const back = find(panel, (node) => node.attrs?.role === "switch" && node.attrs["aria-label"]?.includes("Images"))[0];
          return { enabled, saved: asked.filter((call) => call[0] === "/api/rules/save").length,
                   turnOn: Boolean(turnOn.disabled), now: Boolean(now.hidden), back: back.attrs["aria-checked"] };
        """)
        self.assertEqual(seen["enabled"], [False, False, False, False])
        self.assertEqual(seen["saved"], 1)
        self.assertEqual((seen["turnOn"], seen["now"]), (True, True))
        self.assertEqual(seen["back"], "false")

    def test_folders_wait_and_cannot_change_while_automatic_is_off(self):
        body = """
          core.state.report = { folder: "/x/Downloads", files: 4 };
          Object.assign(answers, {
            "/api/auto": { available: true, on: %s, folder: "/x/Downloads", waiting: 0, note: "" },
            "/api/rules": { rules: [{ id: "documents", folder: "Documents", on: true, auto: true, days: 1 }] },
            "/api/history": { batches: [] },
            "/api/rules/save": { rules: [{ id: "documents", folder: "Documents", on: true, auto: false, days: 1 }] },
          });
          await auto.showAuto();
          const folders = find(panel, (node) => /\\bfolders\\b/.test(node.attrs?.class ?? ""))[0];
          const note = find(panel, (node) => node.attrs?.id === "autos-waiting")[0];
          const controls = find(folders, (node) => node.tagName === "select" || node.attrs?.role === "switch");
          return { list: folders.attrs, note: [note.tagName, note.textContent, Boolean(note.hidden)],
                   controls: controls.map((node) => [node.tagName, Boolean(node.disabled), node.attrs["aria-disabled"]]) };
        """
        off = self.run_page(body % "false")
        self.assertEqual(off["list"], {"class": "folders dim", "aria-describedby": "autos-waiting"})
        self.assertEqual(off["note"], ["p", "These folders are used once you turn automatic tidying on", False])
        self.assertEqual(off["controls"], [["select", True, "true"], ["button", True, "true"]])
        on = self.run_page(body % "true")
        self.assertEqual(on["list"], {"class": "folders"})
        self.assertEqual(on["note"], ["p", "New files that match go to these folders.", False])
        self.assertEqual(on["controls"], [["select", False, "false"], ["button", False, "false"]])

    def test_switching_off_is_one_click_and_changes_the_screen_in_place(self):
        seen = self.run_page(self.AUTO % "true" + """
          const same = find(panel, (node) => /\\bfolders\\b/.test(node.attrs?.class ?? ""))[0] === folders;
          return { first, calls: asked, same, list: folders.attrs, knob: master.attrs["aria-checked"],
                   controls: controls().map((node) => Boolean(node.disabled)), now: said() };
        """)
        self.assertIn("On. It only moves files that arrived after you turned it on", seen["first"])
        self.assertEqual(seen["calls"], [["/api/auto/set", {"on": False}]])
        self.assertTrue(seen["same"])
        self.assertEqual((seen["list"]["class"], seen["knob"]), ("folders dim", "false"))
        self.assertEqual(set(seen["controls"]), {True})
        self.assertIn("Off. Nothing moves by itself.", seen["now"])

    def test_cancel_goes_back_without_asking_the_server(self):
        seen = self.run_page(self.AUTO % "false" + """
          const cancel = find(panel, (node) => node.tagName === "button" && node.textContent === "Cancel")[0];
          await press(cancel);
          const again = find(panel, (node) => node.attrs?.role === "switch" && node.attrs["aria-label"] === "Automatic tidying")[0];
          return { calls: asked, now: said(), knob: again.attrs["aria-checked"] };
        """)
        self.assertEqual(seen["calls"], [])
        self.assertIn("Off until you turn it on.", seen["now"])
        self.assertEqual(seen["knob"], "false")

    SUGGEST = """
      const rule = (id, folder, word) => ({ id, folder, words: [word], kinds: [], extensions: [], sites: [], on: true, auto: true, days: 1 });
      const existing = [rule("own-1", "Bills", "bill"), rule("documents", "Documents", "doc"), rule("images", "Images", "img")];
      const suggested = %s;
      core.state.system = { tidy: true, trash: true };
      core.state.report = { folder: "/x/Downloads", files: 3 };
      core.go.changed = tidy.forgetPlan;
      core.go.focus = () => {};
      Object.assign(answers, {
        "/api/tidy/plan": { plan: "p1", moves: [{ name: "a.pdf", folder: "Documents", why: "A document", bytes: 5 }],
          stays: [], folders: [{ name: "Documents", exists: true }], suggested },
        "/api/rules": { rules: existing },
        "/api/rules/save": %s,
      });
      tidy.forgetPlan();
      await tidy.showTidy();
      const adds = () => find(panel, (node) => node.tagName === "button" && /\\badd/.test(node.attrs.class ?? ""));
      const all = adds().find((node) => node.textContent.startsWith("Add all"));
      const labels = adds().map((node) => node.textContent);
      asked.length = 0;
      globalThis.focused = null;
    """

    @staticmethod
    def suggestion(folder, word, id_):
        return {"folder": folder, "why": f"3 files have {word} in the name", "files": 3,
                "rule": {"id": id_, "folder": folder, "words": [word], "kinds": [], "extensions": [], "sites": [], "on": True, "auto": True, "days": 1}}

    def test_add_all_saves_every_new_suggestion_in_one_call(self):
        suggested = [self.suggestion("Lectures", "lecture", "own-2"), self.suggestion("BILLS", "invoice", "own-3"),
                     self.suggestion("Wedding", "wedding", "own-1"), self.suggestion("lectures", "notes", "own-4")]
        seen = self.run_page(self.SUGGEST % (json.dumps(suggested), "{}") + """
          await press(all);
          await new Promise((r) => setTimeout(r, 100));
          return { labels, asked, toast: document.querySelector("#status").textContent,
                   focused: globalThis.focused?.textContent ?? null, focusedClass: globalThis.focused?.attrs?.class ?? null };
        """)
        self.assertEqual(seen["labels"], ["Add all 4 folders", "Add", "Add", "Add", "Add"])
        saves = [call for call in seen["asked"] if call[0] == "/api/rules/save"]
        self.assertEqual(len(saves), 1)
        rules = saves[0][1]["rules"]
        self.assertEqual([(rule["id"], rule["folder"]) for rule in rules], [
            ("own-2", "Lectures"), ("", "Wedding"), ("own-1", "Bills"), ("documents", "Documents"), ("images", "Images")])
        self.assertEqual(rules[2:], [
            {"id": "own-1", "folder": "Bills", "words": ["bill"], "kinds": [], "extensions": [], "sites": [], "on": True, "auto": True, "days": 1},
            {"id": "documents", "folder": "Documents", "words": ["doc"], "kinds": [], "extensions": [], "sites": [], "on": True, "auto": True, "days": 1},
            {"id": "images", "folder": "Images", "words": ["img"], "kinds": [], "extensions": [], "sites": [], "on": True, "auto": True, "days": 1},
        ])
        self.assertEqual(seen["toast"], "Added 2 folders. To undo, delete them in Change folders.")
        self.assertEqual([call[0] for call in seen["asked"]], ["/api/rules", "/api/rules/save", "/api/tidy/plan"])
        self.assertEqual((seen["focused"], seen["focusedClass"]), ("Tidy 1 file", "main-act"))
        self.assertFalse(any(call[0] == "/api/tidy/apply" for call in seen["asked"]))

    def test_names_match_the_way_the_server_compares_folders(self):
        same = self.run_page("""return ["Stra\\u00dfe", "STRASSE", "Cafe\\u0301", "caf\\u00e9", "\\ufb01les", "FILES"].map(tidy.sameName);""")
        self.assertEqual(same[0], same[1])
        self.assertEqual(same[2], same[3])
        self.assertEqual(same[4], same[5])

    def test_add_all_is_hidden_for_one_suggestion(self):
        seen = self.run_page(self.SUGGEST % (json.dumps([self.suggestion("Lectures", "lecture", "own-2")]), "{}") + "return { labels, all: Boolean(all) };")
        self.assertEqual(seen, {"labels": ["Add"], "all": False})

    def test_add_all_error_gives_the_buttons_back(self):
        suggested = [self.suggestion("Lectures", "lecture", "own-2"), self.suggestion("Wedding", "wedding", "own-3")]
        seen = self.run_page(self.SUGGEST % (json.dumps(suggested), '{ status: 400, error: "There can be at most 100 rules" }') + """
          const during = [];
          const save = globalThis.fetch;
          globalThis.fetch = async (path, init) => { if (path === "/api/rules/save") during.push(adds().map((node) => Boolean(node.disabled))); return save(path, init); };
          await press(all);
          await new Promise((r) => setTimeout(r, 100));
          return { during, after: adds().map((node) => Boolean(node.disabled)), toast: document.querySelector("#status").textContent,
                   asked: asked.map((call) => call[0]), focused: globalThis.focused?.textContent ?? null };
        """)
        self.assertEqual(seen["during"], [[True, True, True]])
        self.assertEqual(seen["after"], [False, False, False])
        self.assertEqual(seen["toast"], "There can be at most 100 rules")
        self.assertEqual(seen["asked"], ["/api/rules", "/api/rules/save"])
        self.assertIsNone(seen["focused"])

    RENAME = """
      const input = (n = 0) => find(panel, (node) => node.tagName === "input")[n];
      const renames = () => find(panel, (node) => node.tagName === "button" && node.textContent === "Rename");
      const problems = () => find(panel, (node) => /\\bproblem\\b/.test(node.attrs?.class ?? "")).map((node) => node.textContent).filter(Boolean);
      const saves = () => asked.filter((call) => call[0] === "/api/rules/save").map((call) => call[1].rules.map((rule) => rule.folder));
    """

    def test_a_suggested_name_can_be_changed_before_adding(self):
        suggested = [self.suggestion("Lectures", "lecture", "own-2"), self.suggestion("Wedding", "wedding", "own-3")]
        seen = self.run_page(self.SUGGEST % (json.dumps(suggested), "{}") + self.RENAME + """
          await press(renames()[0]);
          const opened = [input().value, input().attrs["aria-label"], renames().length];
          const out = {};
          for (const [label, name] of [["slash", "Class/notes"], ["empty", "   "], ["dot", ".hidden"], ["taken", "bills"], ["twin", "wedding"], ["long", "x".repeat(61)]]) {
            await type(input(), name);
            out[label] = [problems()[0] ?? "", input().attrs["aria-invalid"]];
          }
          await type(input(), "wedding");
          await press(all, 150);
          out.blocked = [saves().length, Boolean(all.disabled), globalThis.focused === input()];
          await type(input(), "  Class notes ");
          out.fine = [problems().length, input().attrs["aria-invalid"]];
          await press(all, 150);
          out.saved = saves();
          return { opened, ...out };
        """)
        self.assertEqual(seen["opened"], ["Lectures", "Name for the suggested folder Lectures", 1])
        self.assertEqual(seen["slash"], ['A folder name can\'t contain < > : " / \\ | ? * or invisible characters.', "true"])
        self.assertEqual(seen["empty"][0], "A folder needs a name.")
        self.assertEqual(seen["dot"][0], "A folder name can't start with a dot, a space or ~.")
        self.assertEqual(seen["taken"][0], "You already have a folder named bills.")
        self.assertEqual(seen["twin"][0], "Another suggested folder has this name.")
        self.assertEqual(seen["long"][0], "A folder name can have at most 60 characters.")
        self.assertEqual(seen["blocked"], [0, False, True])
        self.assertEqual(seen["fine"], [0, "false"])
        self.assertEqual(seen["saved"], [["Class notes", "Wedding", "Bills", "Documents", "Images"]])

    def test_one_add_uses_the_typed_name(self):
        suggested = [self.suggestion("Lectures", "lecture", "own-2"), self.suggestion("Wedding", "wedding", "own-3")]
        seen = self.run_page(self.SUGGEST % (json.dumps(suggested), "{}") + self.RENAME + """
          await press(renames()[1]);
          await type(input(), "Our wedding");
          const add = find(panel, (node) => node.tagName === "button" && node.textContent === "Add")[1];
          await press(add, 150);
          return { saved: saves(), toast: document.querySelector("#status").textContent };
        """)
        self.assertEqual(seen["saved"], [["Our wedding", "Bills", "Documents", "Images"]])
        self.assertEqual(seen["toast"], "Added Our wedding. To undo, delete it in Change folders.")

    def test_folder_names_are_checked_like_the_server_does(self):
        from downkeep.sorting import _check_folder

        names = ["", " ", "Bills", "Café", "Straße", "x" * 60, "x" * 61, "é" * 60, "Docu/ments", 'a"b', "a?b",
                 "a​b", "a b", "a\tb", ".hidden", "~x", " lead", "trail.", "trail ", "CON", "con.txt", "COM¹", "Lpt9 .x",
                 "Console", "Class notes", "ملفات"]
        found = self.run_page("return %s.map((name) => rules.nameProblem(name));" % json.dumps(names))
        for name, problem in zip(names, found):
            with self.subTest(name=name):
                try:
                    _check_folder(name)
                    server = ""
                except Exception as error:
                    server = str(error)
                self.assertEqual(bool(problem), bool(server), (problem, server))

    def test_change_folders_says_what_each_link_does(self):
        seen = self.run_page("""
          const rule = (id, folder) => ({ id, folder, words: ["x"], kinds: [], extensions: [], sites: [], on: true, auto: false, days: 7 });
          answers["/api/rules"] = { rules: [rule("own-1", "Bills"), rule("own-2", "Class notes"), rule("documents", "Documents")],
            kinds: [], defaults: [rule("documents", "Documents")] };
          await rules.showRules();
          const tools = find(panel, (node) => /\\btools\\b/.test(node.attrs?.class ?? ""));
          const lead = find(panel, (node) => node.attrs?.class === "lead")[0];
          const box = find(panel, (node) => node.tagName === "input")[0];
          return { lead: lead.textContent, box: box.attrs["aria-label"],
                   links: tools.filter((node) => node.children.length).map((node) => node.children.map((b) => [b.textContent, b.attrs["aria-label"], "disabled" in b.attrs])) };
        """)
        self.assertEqual(seen["lead"], "If a file fits more than one folder, it goes to the highest one.")
        self.assertEqual(seen["box"], "Folder name, type here to rename")
        self.assertEqual(seen["links"], [
            [["Choose files", "Choose which files go to Bills. To rename it, type in its name box.", False],
             ["Move up", "Move Bills up", True], ["Move down", "Move Bills down", False], ["Delete", "Delete Bills", False]],
            [["Choose files", "Choose which files go to Class notes. To rename it, type in its name box.", False],
             ["Move up", "Move Class notes up", False], ["Move down", "Move Class notes down", True], ["Delete", "Delete Class notes", False]],
        ])

    def test_the_closed_screen_shows_the_logo_and_can_close_its_own_window(self):
        seen = self.run_page("""
          const screen = (inApp) => {
            stored.clear();
            if (inApp) stored.set("window", "app");
            return null;
          };
          const look = () => {
            const mark = find(panel, (node) => node.attrs?.class === "mark")[0];
            const buttons = find(panel, (node) => node.tagName === "button").map((node) => node.textContent);
            return { mark: mark ? [mark.tagName, mark.attrs.src, mark.attrs.alt, mark.attrs.width] : null,
                     title: find(panel, (node) => node.tagName === "h1")[0].textContent, buttons };
          };
          screen(true);
          core.closed();
          const closed = look();
          globalThis.closes = 0;
          await sleep(1700);
          const automatically = globalThis.closes;
          core.expired();
          const expired = look();
          await sleep(1700);
          const afterExpired = globalThis.closes;
          await press(find(panel, (node) => node.tagName === "button" && node.textContent === "Close window")[0], 400);
          const hint = find(panel, (node) => node.attrs?.role === "status")[0];
          return { closed, automatically, expired, afterExpired, pressed: globalThis.closes, hint: [hint.textContent, Boolean(hint.hidden)] };
        """)
        self.assertEqual(seen["closed"]["title"], "Downkeep has closed")
        self.assertEqual(seen["closed"]["buttons"], ["Close window"])
        self.assertEqual(seen["automatically"], 1)
        self.assertEqual(seen["expired"]["title"], "This link has expired")
        self.assertEqual(seen["expired"]["buttons"], ["Close window"])
        self.assertEqual(seen["afterExpired"], 1)
        self.assertEqual(seen["pressed"], 2)
        self.assertEqual(seen["hint"], ["Your browser kept this window open. You can close it yourself.", False])
        self.assertEqual(seen["closed"]["mark"], ["img", "/icon.svg", "", "112"])
        self.assertEqual(seen["expired"]["mark"], ["img", "/icon.svg", "", "112"])

    def test_the_window_stays_when_it_is_not_downkeeps_own(self):
        seen = self.run_page("""
          stored.clear();
          globalThis.closes = 0;
          core.closed();
          await sleep(1700);
          return globalThis.closes;
        """)
        self.assertEqual(seen, 0)


class WindowTest(unittest.TestCase):
    def test_file_manager_gets_one_escaped_address(self):
        done = mock.Mock(returncode=0)
        with mock.patch("shutil.which", return_value="/bin/dbus-send"), mock.patch("subprocess.run", return_value=done) as run:
            self.assertTrue(window._ask_file_manager("/d/a,b c%\u00e9\n.txt"))
        self.assertIn("array:string:file:///d/a%2Cb%20c%25%C3%A9%0A.txt", run.call_args.args[0])

    def test_falls_back_to_opening_the_folder(self):
        with (
            mock.patch("downkeep.window.sys.platform", "linux"),
            mock.patch("downkeep.window._ask_file_manager", return_value=False),
            mock.patch("downkeep.window._start") as start,
        ):
            window.show_in_folder("/d/evil.desktop")
        start.assert_called_once_with(["xdg-open", "/d"])

    def test_mac_and_windows_only_select_the_file(self):
        for platform, expected in {"darwin": ["/usr/bin/open", "-R", "/d/-x"], "win32": 'explorer.exe /select,"/d/-x"'}.items():
            with mock.patch("downkeep.window.sys.platform", platform), mock.patch("downkeep.window._start") as start:
                window.show_in_folder("/d/-x")
            start.assert_called_once_with(expected)

    def test_window_falls_back_to_the_browser(self):
        with mock.patch("downkeep.window._app_browser", return_value=None), mock.patch("webbrowser.open") as browser:
            window.open_window("http://127.0.0.1:1/#x")
        browser.assert_called_once_with("http://127.0.0.1:1/#x")
        with (
            mock.patch("downkeep.window._app_browser", return_value="/bin/chrome"),
            mock.patch("downkeep.window._start", side_effect=OSError),
            mock.patch("webbrowser.open") as browser,
        ):
            window.open_window("http://127.0.0.1:1/#x")
        browser.assert_called_once()

    def test_says_whether_a_browser_opened(self):
        for opened in [True, False]:
            with mock.patch("downkeep.window._app_browser", return_value=None), mock.patch("webbrowser.open", return_value=opened):
                self.assertIs(window.open_window("http://127.0.0.1:1/#x"), opened)

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name)
        patch = mock.patch.dict(os.environ, {"DOWNKEEP_DATA": str(self.base / "data")})
        patch.start()
        self.addCleanup(patch.stop)

    def launch(self, program="/usr/bin/chromium", avoid=None):
        process = mock.Mock()
        with mock.patch("downkeep.window._app_browser", return_value=program), mock.patch("downkeep.window._start", return_value=process) as start:
            self.assertIs(window.open_window("http://127.0.0.1:1/#x", avoid), process)
        return start.call_args.args[0]

    def test_app_window_has_a_profile_of_its_own(self):
        command = self.launch()
        profile = self.base / "data" / "browser"
        self.assertEqual(command[:3], ["/usr/bin/chromium", "--app=http://127.0.0.1:1/?window=app#x", f"--user-data-dir={profile}"])
        for flag in ["--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-extensions", "--disable-background-networking"]:
            self.assertIn(flag, command)
        self.assertTrue(profile.is_dir())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)

    def test_the_profile_is_never_a_link_or_inside_the_tidied_folder(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.base / "data").mkdir()
        (self.base / "data" / "browser").symlink_to(elsewhere)
        command = self.launch()
        self.assertFalse(any(part.startswith("--user-data-dir") for part in command))
        self.assertEqual(command[1], "--app=http://127.0.0.1:1/?window=app#x")
        (self.base / "data" / "browser").unlink()
        command = self.launch(avoid=str(self.base))
        self.assertFalse(any(part.startswith("--user-data-dir") for part in command))
        self.assertTrue(any(part.startswith("--user-data-dir") for part in self.launch(avoid=str(elsewhere))))

    def test_a_snap_browser_keeps_its_profile_where_the_snap_may_write(self):
        with mock.patch("downkeep.window.sys.platform", "linux"), mock.patch("downkeep.window.Path.home", return_value=self.base / "home"):
            command = self.launch("/snap/bin/chromium")
            self.assertIn(f"--user-data-dir={self.base / 'home' / 'snap' / 'chromium' / 'common' / 'downkeep' / 'browser'}", command)
            wrapper = self.base / "chromium-browser"
            wrapper.write_text("#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n")
            self.assertEqual(window._snap_name(str(wrapper)), "chromium")
            self.assertIsNone(window._snap_name("/usr/bin/sh"))

    def test_windows_and_mac_browsers_get_the_same_window(self):
        for program in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]:
            with self.subTest(program=program):
                command = self.launch(program)
                self.assertEqual(command[0], program)
                self.assertEqual(command[2], f"--user-data-dir={self.base / 'data' / 'browser'}")
                self.assertEqual(command[3:], list(window.APP_FLAGS))

    def test_linux_prefers_chromium(self):
        found = {"google-chrome-stable": "/usr/bin/google-chrome-stable", "chromium": "/usr/bin/chromium"}
        with mock.patch("downkeep.window.sys.platform", "linux"), mock.patch("downkeep.window.shutil.which", found.get):
            self.assertEqual(window._app_browser(), "/usr/bin/chromium")
        with mock.patch("downkeep.window.sys.platform", "linux"), mock.patch("downkeep.window.shutil.which", {"google-chrome": "/opt/chrome"}.get):
            self.assertEqual(window._app_browser(), "/opt/chrome")

    def test_app_link(self):
        self.assertEqual(window.app_link("http://127.0.0.1:1/#abc"), "http://127.0.0.1:1/?window=app#abc")
        self.assertEqual(window.app_link("http://127.0.0.1:1/?x=1#abc"), "http://127.0.0.1:1/?window=app#abc")
        self.assertEqual(window.app_link("http://127.0.0.1:1/"), "http://127.0.0.1:1/?window=app")


if __name__ == "__main__":
    unittest.main()
