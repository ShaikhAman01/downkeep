import http.client
import io
import json
import os
import re
import socket
import threading
import time
import unittest
import zipfile
from importlib.resources import files
from unittest import mock

from downkeep import server, window
from downkeep.server import App, make_server
from tests.helpers import FolderTest


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
        self.app = App(str(self.folder), self.shown.append, self.clock, home=self.folder.parent)
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
        for path, kind in {"/": "text/html", "/style.css": "text/css", "/app.js": "text/javascript", "/icon.svg": "image/svg+xml"}.items():
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
        other = App(str(self.folder), self.shown.append, self.clock)
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
        for name in ["index.html", "style.css", "app.js", "icon.svg"]:
            text = files("downkeep").joinpath("page", name).read_text(encoding="utf-8")
            text = text.replace("http://www.w3.org/2000/svg", "")
            with self.subTest(name=name):
                self.assertNotRegex(text, r"https?://|//cdn|@import|url\(")
                self.assertNotRegex(text, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|localStorage")
        page = files("downkeep").joinpath("page", "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)|\sstyle=|\son\w+=", page))


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
        browser.assert_called_once()
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

    def test_app_window(self):
        with mock.patch("downkeep.window._app_browser", return_value="/bin/chrome"), mock.patch("downkeep.window._start") as start:
            window.open_window("http://127.0.0.1:1/#x")
        self.assertEqual(start.call_args.args[0][:2], ["/bin/chrome", "--app=http://127.0.0.1:1/#x"])


if __name__ == "__main__":
    unittest.main()
