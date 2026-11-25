import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from downkeep import folders
from downkeep.cli import main
from tests.helpers import FolderTest


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(args))
        except SystemExit as error:
            code = error.code
    return code, out.getvalue(), err.getvalue()


class CliTest(FolderTest):
    def test_text_report(self):
        self.add("a.zip", b"x" * 2000)
        code, out, err = run("report", str(self.folder))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 file, 2.0 KB", out)

    def test_json_report(self):
        self.add("a.zip")
        code, out, _ = run("report", str(self.folder), "--json", "--top", "1")
        self.assertEqual((code, json.loads(out)["files"]), (0, 1))

    def test_default_folder(self):
        self.add("a.zip")
        with mock.patch("downkeep.cli.downloads_folder", return_value=self.folder):
            self.assertIn("1 file", run("report")[1])

    def test_errors_go_to_stderr(self):
        code, out, err = run("report", str(self.folder / "missing"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("downkeep: Can't open", err)

    def test_bad_arguments(self):
        for args in [(), ("report", "--top", "-1"), ("report", "--top", "x"), ("delete",)]:
            with self.subTest(args=args):
                self.assertEqual(run(*args)[0], 2)

    def test_version(self):
        code, out, _ = run("--version")
        self.assertEqual((code, out.startswith("downkeep ")), (0, True))

    def test_module_with_a_limited_terminal(self):
        self.add("\u0928\u092e\u0938\u094d\u0924\u0947.txt")
        env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONPATH": os.pathsep.join(sys.path)}
        done = subprocess.run([sys.executable, "-m", "downkeep", "report", str(self.folder)], env=env, capture_output=True)
        self.assertEqual((done.returncode, done.stderr), (0, b""))


class DownloadsFolderTest(unittest.TestCase):
    def xdg(self, text):
        with tempfile.TemporaryDirectory() as base:
            if text is not None:
                Path(base, "user-dirs.dirs").write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": base}):
                return folders._xdg()

    def test_xdg(self):
        absolute = str(Path.home().resolve().parent / "shared")
        cases = {
            None: None,
            "": None,
            'XDG_DOWNLOAD_DIR="$HOME/Saved files"': Path.home() / "Saved files",
            f'# XDG_DOWNLOAD_DIR="$HOME/no"\nXDG_DOWNLOAD_DIR="{absolute}"': Path(absolute),
            'XDG_DOWNLOAD_DIR="$HOME"': None,
            'XDG_DOWNLOAD_DIR="$HOME/"': None,
            'XDG_DOWNLOAD_DIR="relative"': None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.xdg(text), expected)

    def test_this_system_gives_an_absolute_folder(self):
        self.assertTrue(folders.downloads_folder().is_absolute())


if __name__ == "__main__":
    unittest.main()
