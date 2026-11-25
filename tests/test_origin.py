import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from downkeep import origin
from downkeep.origin import find_hosts, host_of
from tests.helpers import FolderTest


class HostTest(unittest.TestCase):
    def test_only_the_site_name_is_kept(self):
        cases = {
            "https://Example.COM/a/b?token=secret#part": "example.com",
            "https://user:password@files.example.com:8443/x": "files.example.com",
            "  http://example.com.  ": "example.com",
            "blob:https://app.example.com/1b9d-uuid": "app.example.com",
            "https://b\u00fccher.example/x": "xn--bcher-kva.example",
            "http://127.0.0.1:3000/x": "127.0.0.1",
            "http://[::1]:3000/x": "::1",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(host_of(url), expected)

    def test_everything_else_is_unknown(self):
        for url in [
            "", "about:internet", "data:text/plain,hi", "file:///etc/passwd", "blob:null/1b9d", "ftp://example.com/x",
            "https://", "https:///path", "http://[broken", "https://bad host/x", "https://evil\x1b[2J.com/",
            "https://" + "a" * 300 + ".com", "https://\udcff.com", "javascript:alert(1)",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(host_of(url))


class ParserTest(unittest.TestCase):
    def test_gio(self):
        output = (
            b"uri: file:///d/sp%20ace%2520.txt\nlocal path: /d/sp ace%20.txt\nattributes:\n"
            b"  metadata::download-uri: https://one.example/x?t=1\n"
            b"uri: file:///d/none.txt\nattributes:\n"
            b"uri: file:///d/bad%FF.txt\nattributes:\n  metadata::download-uri: https://two.example/\n"
            b"uri: file:///d/data.txt\nattributes:\n  metadata::download-uri: data:text/plain,x\n"
        )
        self.assertEqual(origin._parse_gio(output), {b"/d/sp ace%20.txt": "one.example", b"/d/bad\xff.txt": "two.example"})
        self.assertEqual(origin._parse_gio(b"  metadata::download-uri: https://lost.example/\n"), {})

    def test_names_with_line_breaks_never_reach_gio(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"")
        with mock.patch("shutil.which", return_value="/bin/gio"), mock.patch("subprocess.run", return_value=done) as run:
            origin._gio(["/d/ok.txt", "/d/evil\n  metadata::download-uri: https://fake.example/", "/d/odd\udcff.txt"])
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        sent = run.call_args.args[0][5:]
        self.assertEqual(sent, [os.fsencode("/d/ok.txt"), os.fsencode("/d/odd\udcff.txt")])

    def test_gio_is_called_in_batches(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"")
        with mock.patch("shutil.which", return_value="/bin/gio"), mock.patch("subprocess.run", return_value=done) as run:
            origin._gio([f"/d/{index}" for index in range(450)])
        self.assertEqual([len(call.args[0]) - 5 for call in run.call_args_list], [200, 200, 50])

    def test_gio_problems_mean_unknown(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(origin._gio(["/d/a"]), {})
        for error in [OSError(), subprocess.TimeoutExpired("gio", 20)]:
            with mock.patch("shutil.which", return_value="/bin/gio"), mock.patch("subprocess.run", side_effect=error):
                self.assertEqual(origin._gio(["/d/a"]), {})

    def test_where_froms(self):
        pack = lambda value: plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
        self.assertEqual(origin._parse_where_froms(pack(["https://cdn.example/x", "https://page.example/"])), "cdn.example")
        self.assertEqual(origin._parse_where_froms(pack(["", "https://page.example/"])), "page.example")
        for raw in [b"", b"junk", b"bplist00broken", pack([]), pack({"https://x.example/": 1}), pack([1, b"x"])]:
            with self.subTest(raw=raw):
                self.assertIsNone(origin._parse_where_froms(raw))

    def test_zone_identifier(self):
        zone = "[ZoneTransfer]\r\nZoneId=3\r\nReferrerUrl=https://page.example/\r\nHostUrl=https://cdn.example/f?t=1\r\n"
        self.assertEqual(origin._parse_zone(zone), "cdn.example")
        self.assertEqual(origin._parse_zone("HostUrl=about:internet\nReferrerUrl=https://page.example/"), "page.example")
        for text in ["", "ZoneId=3", "HostUrl", "\0\0"]:
            with self.subTest(text=text):
                self.assertIsNone(origin._parse_zone(text))


class ThisSystemTest(FolderTest):
    def test_unmarked_and_missing_files(self):
        path = self.add("plain.txt")
        self.assertEqual(find_hosts([str(path), str(self.folder / "missing")]), {})
        self.assertEqual(find_hosts([]), {})

    @unittest.skipUnless(sys.platform == "linux", "Linux only")
    def test_linux_attribute(self):
        path = self.add("a.txt")
        try:
            os.setxattr(path, "user.xdg.origin.url", b"https://attr.example/x?t=1")
        except OSError:
            self.skipTest("this filesystem has no user attributes")
        self.assertEqual(find_hosts([str(path)]), {str(path): "attr.example"})

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("gio"), "needs gio")
    def test_linux_gio(self):
        cache = Path.home() / ".cache"
        if not cache.is_dir():
            self.skipTest("no cache folder")
        with tempfile.TemporaryDirectory(dir=cache, prefix="downkeep-test-") as holder:
            path = Path(holder, "sp ace%20\u00e9.txt")
            path.write_text("x")
            subprocess.run(["gio", "set", "-t", "string", path, "metadata::download-uri", "https://gio.example/x?t=1"], capture_output=True)
            shown = subprocess.run(["gio", "info", "-a", "metadata::download-uri", path], capture_output=True).stdout
            if b"gio.example" not in shown:
                self.skipTest("no metadata service here")
            self.assertEqual(find_hosts([str(path)]), {str(path): "gio.example"})

    @unittest.skipUnless(sys.platform == "darwin", "macOS only")
    def test_macos(self):
        path = self.add("caf\u00e9.txt")
        value = plistlib.dumps(["https://cdn.example/x?t=1", "https://page.example/"], fmt=plistlib.FMT_BINARY).hex()
        subprocess.run(["xattr", "-wx", "com.apple.metadata:kMDItemWhereFroms", value, path], check=True)
        self.assertEqual(find_hosts([str(path)]), {str(path): "cdn.example"})

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows(self):
        path = self.add("caf\u00e9.txt")
        with open(f"{path}:Zone.Identifier", "w", encoding="utf-8") as stream:
            stream.write("[ZoneTransfer]\nZoneId=3\nHostUrl=https://cdn.example/x?t=1\n")
        self.assertEqual(find_hosts([str(path)]), {str(path): "cdn.example"})


if __name__ == "__main__":
    unittest.main()
