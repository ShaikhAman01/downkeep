"""Packs what PyInstaller built into the file people download. Same input, same bytes."""

import argparse
import gzip
import hashlib
import io
import os
import re
import sys
import sysconfig
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
MIT = ROOT / "LICENSE"
EARLIEST = 315_532_800  # zip can't hold a date before 1980


def version() -> str:
    text = (ROOT / "src" / "downkeep" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', text, re.MULTILINE)[1]


def check_tag(kind: str | None, name: str | None) -> None:
    if kind == "tag" and name != f"v{version()}":
        raise SystemExit(f"The tag {name} doesn't match version {version()}")


def python_license() -> bytes:
    for folder in (sysconfig.get_path("stdlib"), sys.base_prefix):
        place = Path(folder, "LICENSE.txt")
        if place.is_file():
            return place.read_bytes()
    raise SystemExit("Python's license file wasn't found")


def built_files(dist: Path) -> dict[str, tuple[bytes, bool]]:
    files = {}
    for path in sorted(dist.rglob("*")):
        if path.is_dir():
            if path.is_symlink():
                raise SystemExit(f"{path} is a link to a folder")
            continue
        if not path.resolve().is_relative_to(dist.resolve()):
            raise SystemExit(f"{path} leads outside the build")
        files[path.relative_to(dist).as_posix()] = (path.read_bytes(), os.access(path, os.X_OK))
    return files


def extra_files(system: str) -> dict[str, tuple[bytes, bool]]:
    def text(path: Path, ending: str = "\n") -> bytes:
        return path.read_text(encoding="utf-8").replace("\n", ending).encode()

    ending = "\r\n" if system == "windows" else "\n"
    files = {
        "Read me first.txt": (text(PACKAGING / system / "Read me first.txt", ending), False),
        "LICENSE.txt": (text(MIT, ending), False),
        "licenses/Python.txt": (python_license(), False),
    }
    if system == "linux":
        files["downkeep.desktop"] = (text(PACKAGING / "linux" / "downkeep.desktop"), False)
        files["downkeep.png"] = ((PACKAGING / "downkeep.png").read_bytes(), False)
        for name in ("install.sh", "uninstall.sh"):
            files[name] = (text(PACKAGING / "linux" / name), True)
    return files


def make_zip(files: dict[str, tuple[bytes, bool]], folder: str, epoch: int) -> bytes:
    buffer = io.BytesIO()
    date = time.gmtime(max(epoch, EARLIEST))[:6]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (data, _) in sorted(files.items()):
            entry = zipfile.ZipInfo(f"{folder}/{name}", date)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return buffer.getvalue()


def make_tar(files: dict[str, tuple[bytes, bool]], folder: str, epoch: int) -> bytes:
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=epoch) as packed,
        tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for name, (data, runs) in sorted(files.items()):
            entry = tarfile.TarInfo(f"{folder}/{name}")
            entry.size, entry.mtime, entry.mode = len(data), epoch, 0o755 if runs else 0o644
            archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, help="the folder PyInstaller built")
    parser.add_argument("out", type=Path, help="a new or empty folder for the result")
    args = parser.parse_args(argv)
    check_tag(os.environ.get("GITHUB_REF_TYPE"), os.environ.get("GITHUB_REF_NAME"))

    system = "windows" if (args.dist / "Downkeep.exe").is_file() else "linux"
    if system == "linux" and not (args.dist / "downkeep").is_file():
        raise SystemExit(f"No built program in {args.dist}")
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    files = built_files(args.dist)
    extra = extra_files(system)
    if files.keys() & extra.keys():
        raise SystemExit(f"Built files in the way: {sorted(files.keys() & extra.keys())}")
    files |= extra
    if system == "windows":
        name, data = f"Downkeep-{version()}-windows-x64.zip", make_zip(files, "Downkeep", epoch)
    else:
        name, data = f"downkeep-{version()}-linux-x64.tar.gz", make_tar(files, "downkeep", epoch)

    args.out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    with open(args.out / name, "xb") as target:
        target.write(data)
    with open(args.out / f"SHA256SUMS-{system}.txt", "x", encoding="utf-8", newline="\n") as target:
        target.write(f"{digest}  {name}\n")
    print(f"{name}  {len(data):,} bytes  {len(files)} files  {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
