"""Running unattended on a Mac with launchd.

- A LaunchAgent (~/Library/LaunchAgents/<label>.plist) starts `scout demo` when you log in and
  starts it again if it crashes (KeepAlive: SuccessfulExit = false). A clean stop (exit 0, e.g.
  `scout service stop`) is not restarted. ThrottleInterval stops a broken setup restarting
  more than once every 30 seconds.
- While running, Scout starts `caffeinate -s -w <pid>`: the Mac won't go to sleep while Scout runs
  and it's on mains power. It can't stop a MacBook sleeping when the lid is closed.
- A heartbeat file (data/heartbeat.json) is rewritten every ~30 seconds, so `scout service status`
  and `scout doctor` can tell whether the loop is alive without touching the database.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from scout.config import Settings

Launchctl = Callable[[Sequence[str]], tuple[int, str, str]]


def run_launchctl(args: Sequence[str]) -> tuple[int, str, str]:
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout, result.stderr


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(settings: Settings, folder: Path | None = None, label: str | None = None) -> Path:
    return (folder or agents_dir()) / f"{label or settings.service.label}.plist"


def experiment_label(settings: Settings) -> str:
    return f"{settings.service.label}.experiment"


def smart_label(settings: Settings) -> str:
    return f"{settings.service.label}.smart"


def status_label(settings: Settings) -> str:
    return f"{settings.service.label}.status"


def log_name(settings: Settings, label: str | None) -> str:
    """launchd's log files: service.* for the main demo, <last part of the label>.* for the others."""
    if label is None or label == settings.service.label:
        return "service"
    return label.rsplit(".", 1)[-1]


def domain() -> str:
    return f"gui/{os.getuid()}"


def build_plist(settings: Settings, scout_bin: Path, project: Path, config: Path, env_file: Path,
                command: Sequence[str] = ("demo",), label: str | None = None, every_seconds: int | None = None) -> dict:
    """A LaunchAgent that keeps Scout running, or (every_seconds) runs a short job on a timer."""
    plist = _plist(settings, scout_bin, project, config, env_file, command, label)
    if every_seconds:
        del plist["KeepAlive"], plist["ExitTimeOut"]
        plist["StartInterval"] = every_seconds
    return plist


def _plist(settings: Settings, scout_bin: Path, project: Path, config: Path, env_file: Path,
           command: Sequence[str], label: str | None) -> dict:
    logs = settings.app.log_dir
    name = log_name(settings, label)
    return {
        "Label": label or settings.service.label,
        "ProgramArguments": [str(scout_bin), *command, "--config", str(config), "--env-file", str(env_file)],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,  # start at login
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a clean stop
        "ThrottleInterval": 30,
        "ExitTimeOut": 30,  # seconds to stop cleanly after SIGTERM before launchd forces it
        "StandardOutPath": str(logs / f"{name}.out.log"),
        "StandardErrorPath": str(logs / f"{name}.err.log"),
        "EnvironmentVariables": {
            "LAUNCHED_BY_SCOUT_SERVICE": "1",  # no SCOUT_ prefix: those are settings
            "PYTHONUNBUFFERED": "1",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_AU.UTF-8",
        },
    }


@dataclass(frozen=True)
class ServiceStatus:
    installed: bool
    loaded: bool
    pid: int | None
    last_exit: str | None
    plist: Path


def install(settings: Settings, scout_bin: Path, project: Path, config: Path, env_file: Path,
            launchctl: Launchctl = run_launchctl, folder: Path | None = None,
            command: Sequence[str] = ("demo",), label: str | None = None, every_seconds: int | None = None) -> Path:
    """Write the LaunchAgent and load it (which starts Scout now, and at every login)."""
    label = label or settings.service.label
    path = plist_path(settings, folder, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    settings.app.log_dir.mkdir(parents=True, exist_ok=True)
    launchctl(["bootout", f"{domain()}/{label}"])  # replace an older copy (errors are fine)
    with path.open("wb") as handle:
        plistlib.dump(build_plist(settings, scout_bin, project, config, env_file, command,
                                  None if label == settings.service.label else label, every_seconds), handle)
    code, _, err = launchctl(["bootstrap", domain(), str(path)])
    if code != 0:
        raise RuntimeError(f"launchctl couldn't load the service: {err.strip() or code}")
    return path


def uninstall(settings: Settings, launchctl: Launchctl = run_launchctl, folder: Path | None = None,
              label: str | None = None) -> bool:
    """Stop Scout and remove the LaunchAgent. Returns False if it wasn't installed."""
    label = label or settings.service.label
    path = plist_path(settings, folder, label)
    launchctl(["bootout", f"{domain()}/{label}"])
    if not path.exists():
        return False
    path.unlink()
    return True


def stop(settings: Settings, launchctl: Launchctl = run_launchctl) -> bool:
    """Stop until the next login (or `scout service start`). Scout exits cleanly, so it isn't restarted."""
    code, _, _ = launchctl(["bootout", f"{domain()}/{settings.service.label}"])
    return code == 0


def start(settings: Settings, launchctl: Launchctl = run_launchctl, folder: Path | None = None,
          attempts: int = 5, wait: float = 2.0) -> tuple[bool, str]:
    """Load the service. Right after a stop, launchd may refuse for a few seconds, so try a few times.
    Returns (started, why not)."""
    current = status(settings, launchctl, folder)
    if current.loaded and current.pid:
        return False, "it's already running"
    error = ""
    for attempt in range(attempts):
        # Right after a stop, launchd can still list the service (without a process) for a moment.
        code, _, err = launchctl(["bootstrap", domain(), str(plist_path(settings, folder))])
        if code == 0:
            return True, ""
        error = err.strip() or f"launchctl exit code {code}"
        if attempt + 1 < attempts:
            time.sleep(wait)
    return False, error


def status(settings: Settings, launchctl: Launchctl = run_launchctl, folder: Path | None = None,
           label: str | None = None) -> ServiceStatus:
    label = label or settings.service.label
    path = plist_path(settings, folder, label)
    code, out, _ = launchctl(["print", f"{domain()}/{label}"])
    pid = last_exit = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            pid = int(line.split("=", 1)[1])
        elif line.startswith("last exit code = "):
            last_exit = line.split("=", 1)[1].strip()
    return ServiceStatus(path.exists(), code == 0, pid, last_exit, path)


# ---------------------------------------------------------------- heartbeat


def heartbeat_path(settings: Settings) -> Path:
    """data/heartbeat.json for the main demo; data/heartbeat-<name>.json for others (e.g. the experiment)."""
    db = settings.app.db_path
    return db.parent / ("heartbeat.json" if db.stem == "scout" else f"heartbeat-{db.stem}.json")


def write_heartbeat(path: Path, *, pid: int, version: str, state: str, ts_ms: int, positions: int) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": pid, "version": version, "state": state, "ts_ms": ts_ms,
                               "open_positions": positions}))
    tmp.replace(path)


def read_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------- keep awake


def keep_awake(pid: int) -> subprocess.Popen | None:
    """`caffeinate -s -w pid`: no system sleep while `pid` runs and the Mac is on mains power."""
    try:
        return subprocess.Popen(["/usr/bin/caffeinate", "-s", "-w", str(pid)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


# ---------------------------------------------------------- service logs


def rotate_service_logs(log_dir: Path, max_bytes: int = 5_000_000, keep: int = 3) -> None:
    """launchd's own output files never rotate, so rotate them when Scout starts."""
    for name in [f"{n}.{kind}.log" for n in ("service", "experiment", "smart", "status") for kind in ("out", "err")]:
        path = log_dir / name
        if not path.exists() or path.stat().st_size < max_bytes:
            continue
        for n in range(keep - 1, 0, -1):
            older = log_dir / f"{name}.{n}"
            if older.exists():
                older.replace(log_dir / f"{name}.{n + 1}")
        path.replace(log_dir / f"{name}.1")
