import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
SOURCE = ROOT / "src"
PACKAGE = SOURCE / "downkeep"
VERSION = re.search(r'^__version__ = "([^"]+)"$', (PACKAGE / "__init__.py").read_text(encoding="utf-8"), re.MULTILINE)[1]
MODULES = sorted(f"downkeep.{path.stem}" for path in PACKAGE.glob("*.py") if not path.stem.startswith("__"))
WINDOWS = sys.platform == "win32"

# Nothing here talks to the internet, so there is no TLS and no OpenSSL to keep patched.
EXCLUDES = [
    "_hashlib", "_ssl", "ssl",
    "_tkinter", "tkinter", "turtle", "turtledemo", "idlelib",
    "test", "_testcapi", "_testinternalcapi", "unittest", "doctest", "pdb",
    "pydoc", "pydoc_data", "lib2to3", "ensurepip", "venv", "distutils", "setuptools", "pkg_resources",
    "asyncio", "concurrent.interpreters", "curses", "_curses", "readline", "xmlrpc", "multiprocessing",
]

if not (WINDOWS or sys.platform.startswith("linux")):
    raise SystemExit("Downkeep is packaged for Windows and Linux only")


def version_info():
    from PyInstaller.utils.win32 import versioninfo as info

    numbers = (tuple(int(part) for part in VERSION.split(".")) + (0, 0, 0, 0))[:4]
    words = {
        "CompanyName": "Downkeep",
        "FileDescription": "Downkeep",
        "FileVersion": VERSION,
        "InternalName": "Downkeep",
        "LegalCopyright": "MIT License",
        "OriginalFilename": "Downkeep.exe",
        "ProductName": "Downkeep",
        "ProductVersion": VERSION,
    }
    return info.VSVersionInfo(
        ffi=info.FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            info.StringFileInfo([info.StringTable("040904B0", [info.StringStruct(*word) for word in words.items()])]),
            info.VarFileInfo([info.VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


analysis = Analysis(
    [str(ROOT / "packaging" / "start.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=[(str(PACKAGE / "page"), "downkeep/page")],
    hiddenimports=MODULES,
    excludes=EXCLUDES,
    noarchive=False,
)
exe = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Downkeep" if WINDOWS else "downkeep",
    console=False,
    icon=str(ROOT / "packaging" / "downkeep.ico") if WINDOWS else None,
    version=version_info() if WINDOWS else None,
    uac_admin=False,
    strip=False,
    upx=False,
)
COLLECT(exe, analysis.binaries, analysis.datas, name="Downkeep" if WINDOWS else "downkeep", strip=False, upx=False)
