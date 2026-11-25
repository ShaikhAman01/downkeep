import os
import tempfile
import time
import unittest
from pathlib import Path

DAY = 86_400


class FolderTest(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name).resolve() / "Downloads"
        self.folder.mkdir()

    def add(self, name, content=b"x", days=10.0):
        path = self.folder / name
        path.write_bytes(content)
        when = time.time() - days * DAY
        os.utime(path, (when, when))
        return path

    def symlink(self, name, target):
        try:
            (self.folder / name).symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not allowed here")
