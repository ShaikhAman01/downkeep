import os
import sys

from downkeep.gui import main


def clean_environment() -> None:
    """Programs started from the packaged app, such as the browser, must not load its bundled libraries."""
    if not getattr(sys, "frozen", False):
        return
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(None)
        return
    before = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if before is None:
        os.environ.pop("LD_LIBRARY_PATH", None)
    else:
        os.environ["LD_LIBRARY_PATH"] = before


if __name__ == "__main__":
    clean_environment()
    sys.exit(main(sys.argv[1:]))
