import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from downkeep.model import ActionError

PLATFORM = sys.platform
NAME = "Downkeep"
NO_SESSION = "Automatic tidying needs the systemd user service, which isn't running here"
NO_TASKS = "Automatic tidying needs the Windows Task Scheduler, which isn't available here"
NOT_HERE = "Automatic tidying is not available on this system yet"
Runner = Callable[[list[str]], subprocess.CompletedProcess]

# Only options that need no new namespace: those fail for user services on systems that restrict namespaces.
# An AppImage gets none, because it has to mount itself with a setuid helper.
HARDENING = """\
NoNewPrivileges=yes
RestrictAddressFamilies=AF_UNIX
RestrictRealtime=yes
LockPersonality=yes
"""
# systemd refuses to start a program with a quote or backslash in its path
VIA_SHELL = ["/bin/sh", "-c", 'exec "$@"', "downkeep"]
SERVICE = """\
[Unit]
Description=Downkeep automatic tidying

[Service]
Type=oneshot
ExecStart={command}
WorkingDirectory=/
{environment}Nice=10
IOSchedulingClass=idle
TimeoutStartSec=10min
{hardening}"""
TIMER = """\
[Unit]
Description=Downkeep automatic tidying, every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
"""
TASK_SPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{space}">
  <RegistrationInfo>
    <Description>Downkeep automatic tidying, every hour</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT1H</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2025-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{program}</Command>
{arguments}    </Exec>
  </Actions>
</Task>
"""


def run_command(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ActionError("Downkeep couldn't reach the system's scheduler") from error


def command() -> list[str]:
    program = sys.executable
    if not program:
        raise ActionError("Downkeep couldn't find its own program, so it can't be scheduled")
    image, mounted = os.environ.get("APPIMAGE"), os.environ.get("APPDIR")
    # Inside an AppImage the program runs from a mount that is gone later. Another AppImage's values can be inherited.
    if image and (not mounted or Path(program).is_relative_to(mounted)):
        return [image, "--auto-run"]
    if getattr(sys, "frozen", False):
        return [program, "--auto-run"]
    if PLATFORM == "win32":
        windowless = ntpath.join(ntpath.dirname(program), "pythonw.exe")
        program = windowless if os.path.isfile(windowless) else program
    return [program, "-m", "downkeep", "auto-run"]


def available(run: Runner = run_command) -> tuple[bool, str]:
    if PLATFORM == "win32":
        return (True, "") if os.path.isfile(_schtasks()) else (False, NO_TASKS)
    if not PLATFORM.startswith("linux"):
        return False, NOT_HERE
    if not shutil.which("systemctl"):
        return False, NO_SESSION
    try:
        answered = run(["systemctl", "--user", "show", "--property=Version"]).returncode == 0
    except ActionError:
        answered = False
    return (True, "") if answered else (False, NO_SESSION)


def install(command: list[str], name: str = NAME, run: Runner = run_command) -> None:
    ready, why = available(run)
    if not ready:
        raise ActionError(why)
    if not all(part.isprintable() for part in command):
        raise ActionError("Downkeep is in a folder whose name has characters that can't be scheduled. Move it first.")
    _check_name(name)
    if PLATFORM == "win32":
        _install_task(command, name, run)
    else:
        _install_units(command, name, run)


def remove(name: str = NAME, run: Runner = run_command) -> None:
    _check_name(name)
    if PLATFORM == "win32":
        if _query(name, run) is not None and run([_schtasks(), "/Delete", "/TN", name, "/F"]).returncode:
            raise ActionError("Windows didn't let Downkeep remove its hourly check")
    elif PLATFORM.startswith("linux"):
        _remove_units(name, run)


def installed_command(name: str = NAME, run: Runner = run_command) -> list[str] | None:
    """The command the schedule runs now, or None if there is no complete schedule."""
    _check_name(name)
    if PLATFORM == "win32":
        try:
            found = _query(name, run)
        except ActionError:
            return None
        return read_task(found) if found is not None else None
    service, timer, link = _units(name)
    try:
        return read_unit(service.read_text(encoding="utf-8")) if timer.is_file() and link.is_symlink() else None
    except (OSError, UnicodeError):
        return None


def _check_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,60}", name):
        raise ValueError(f"{name!r} can't name a schedule")


# Linux


def _units(name):
    base = Path(os.environ.get("XDG_CONFIG_HOME") or "")
    folder = (base if base.is_absolute() else Path.home() / ".config") / "systemd" / "user"
    timer = f"{name.lower()}.timer"
    return folder / f"{name.lower()}.service", folder / timer, folder / "timers.target.wants" / timer


def make_unit(command: list[str], environment: dict[str, str] | None = None) -> str:
    # ":" makes systemd leave every "$" as it is, also in the program's own path
    words = [*VIA_SHELL, *command] if re.search(r"[\"'\\]", command[0]) else command
    words = [_quote(part) for part in (":" + words[0], *words[1:])]
    lines = [f"Environment={_quote(f'{key}={value}')}\n" for key, value in (environment or {}).items()]
    hardening = "" if command[0].lower().endswith(".appimage") or command[0] == os.environ.get("APPIMAGE") else HARDENING
    return SERVICE.format(command=" ".join(words), environment="".join(lines), hardening=hardening)


def read_unit(text: str) -> list[str] | None:
    line = next((line for line in text.splitlines() if line.startswith("ExecStart=")), "")
    words = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
    words = [re.sub(r"\\(.)", r"\1", word).replace("%%", "%") for word in words]
    words = [words[0].removeprefix(":"), *words[1:]] if words else []
    return words[4:] if words[:4] == VIA_SHELL else words or None


def _quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _install_units(command, name, run):
    service, timer, _ = _units(name)
    # The service doesn't get the person's shell settings, so a moved records folder is passed on
    kept = {key: os.environ[key] for key in ("DOWNKEEP_DATA", "XDG_STATE_HOME") if os.environ.get(key)}
    kept = {key: value for key, value in kept.items() if value.isprintable()}
    try:
        service.parent.mkdir(parents=True, exist_ok=True)
        _write(service, make_unit(command, kept))
        _write(timer, TIMER)
        for step in (["daemon-reload"], ["enable", "--now", timer.name]):
            if run(["systemctl", "--user", *step]).returncode:
                raise ActionError("systemd didn't accept the hourly check")
    except (ActionError, OSError) as error:
        _remove_units(name, run)
        if isinstance(error, ActionError):
            raise
        raise ActionError(f"Downkeep couldn't save the hourly check: {error.strerror or error}") from error


def _write(path, text):
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def _remove_units(name, run):
    service, timer, link = _units(name)
    if not any(map(os.path.lexists, (service, timer, link))):
        return
    _quietly(run, ["systemctl", "--user", "disable", "--now", timer.name])
    try:
        for path in (link, timer, service):
            path.unlink(missing_ok=True)
    except OSError as error:
        raise ActionError(f"Downkeep couldn't remove the hourly check: {error.strerror or error}") from error
    _quietly(run, ["systemctl", "--user", "daemon-reload"])


def _quietly(run, command):
    try:
        run(command)
    except ActionError:
        pass


# Windows


def _schtasks():
    return ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "schtasks.exe")


def make_task(command: list[str]) -> str:
    arguments = escape(subprocess.list2cmdline(command[1:]))
    line = f"      <Arguments>{arguments}</Arguments>\n" if arguments else ""
    return TASK.format(space=TASK_SPACE, program=escape(command[0]), arguments=line)


def read_task(text: str) -> list[str] | None:
    try:
        # The text says UTF-16 even where it was printed in another encoding
        action = ElementTree.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text)).find(f".//{{{TASK_SPACE}}}Exec")
    except ElementTree.ParseError:
        return None
    program = action.findtext(f"{{{TASK_SPACE}}}Command") if action is not None else None
    if not program:
        return None
    return [program, *_split(action.findtext(f"{{{TASK_SPACE}}}Arguments") or "")]


def _split(line):
    """The reverse of subprocess.list2cmdline."""
    words, word, quoted = [], None, False
    for slashes, char in re.findall(r"(\\*)([^\\]|$)", line):
        if char == '"':
            word = (word or "") + slashes[: len(slashes) // 2]
            if len(slashes) % 2:
                word += char
            else:
                quoted = not quoted
        elif char in (" ", "\t") and not quoted:
            word = (word or "") + slashes if slashes else word
            words += [word] if word is not None else []
            word = None
        elif slashes or char:
            word = (word or "") + slashes + char
    return words if word is None else [*words, word]


def _install_task(command, name, run):
    handle, path = tempfile.mkstemp(prefix="downkeep-", suffix=".xml")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(make_task(command).encode("utf-16"))
        if run([_schtasks(), "/Create", "/TN", name, "/XML", path, "/F"]).returncode:
            raise ActionError("Windows didn't let Downkeep add its hourly check")
    finally:
        os.unlink(path)


def _query(name, run):
    done = run([_schtasks(), "/Query", "/TN", name, "/XML"])
    return _decode(done.stdout) if done.returncode == 0 else None


def _decode(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    if raw[1:2] == b"\0":
        return raw.decode("utf-16-le", "replace")
    for encoding in ("utf-8", "oem"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    return raw.decode("latin-1")
