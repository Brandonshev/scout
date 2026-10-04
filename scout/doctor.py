"""`scout doctor`: check everything Scout needs to run unattended, with a fix for each problem."""

from __future__ import annotations

import asyncio
import email.utils
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import httpx

from scout.backup import latest_backup
from scout.config import Settings
from scout.data import parse_mids, stream_mids
from scout.db import SCHEMA_VERSION
from scout.service import Launchctl, heartbeat_path, read_heartbeat, run_launchctl
from scout.service import status as service_status
from scout.tax import latest_rate, parse_rba_csv
from scout.version import APP_VERSION

OK, WARN, FAIL = "ok", "warn", "fail"
Runner = Callable[[Sequence[str]], tuple[int, str, str]]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def run_command(args: Sequence[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout, result.stderr


# ------------------------------------------------------------- local checks


def check_env_file(env_file: Path) -> Check:
    if not env_file.exists():
        return Check(".env secrets file", WARN, f"{env_file} doesn't exist", "cp .env.example .env")
    if env_file.stat().st_mode & 0o077:
        return Check(".env secrets file", WARN, "other users on this Mac can read it", f"chmod 600 {env_file}")
    return Check(".env secrets file", OK, "only you can read it")


def check_database(settings: Settings) -> Check:
    path = settings.app.db_path
    if not path.exists():
        return Check("Database", WARN, f"{path.name} doesn't exist yet", "uv run scout init-db")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        return Check("Database", FAIL, f"can't open {path.name}: {exc}", "restore the latest file from data/backups")
    size = path.stat().st_size / 1e6
    if result != "ok":
        return Check("Database", FAIL, f"integrity check failed: {result}", "restore the latest file from data/backups")
    note = "" if version >= SCHEMA_VERSION else f" (schema {version}; it upgrades on the next start)"
    return Check("Database", OK, f"{path.name}, {size:.1f} MB, integrity OK{note}")


def check_disk(settings: Settings, min_free_gb: float = 1.0) -> Check:
    free = shutil.disk_usage(settings.app.db_path.parent if settings.app.db_path.parent.exists() else Path.home()).free
    free_gb = free / 1e9
    status = OK if free_gb >= min_free_gb else FAIL
    return Check("Disk space", status, f"{free_gb:.1f} GB free", "" if status == OK else "free up some disk space")


def check_logs(settings: Settings) -> Check:
    folder = settings.app.log_dir
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("Logs", FAIL, f"can't write to {folder}: {exc}", f"check the permissions of {folder}")
    size = sum(p.stat().st_size for p in folder.glob("*") if p.is_file()) / 1e6
    status = OK if size < 500 else WARN
    return Check("Logs", status, f"{folder.name}/ is writable, {size:.1f} MB (rotated automatically)",
                 "" if status == OK else "delete old files in logs/")


def check_backups(settings: Settings, now: float | None = None) -> Check:
    latest = latest_backup(settings.app.backup_dir, settings.app.db_path.stem)
    if latest is None:
        return Check("Backups", WARN, "no backups yet (the demo makes one each day)", "uv run scout backup")
    age_days = ((now or time.time()) - latest.stat().st_mtime) / 86400
    status = OK if age_days < 2 else WARN
    return Check("Backups", status, f"latest {latest.name}, {age_days:.1f} days old",
                 "" if status == OK else "uv run scout backup")


def check_service(settings: Settings, launchctl: Launchctl = run_launchctl, now: float | None = None) -> Check:
    s = service_status(settings, launchctl)
    beat = read_heartbeat(heartbeat_path(settings))
    age = None if beat is None else ((now or time.time()) * 1000 - beat["ts_ms"]) / 1000
    alive = age is not None and age < 120
    if not s.installed:
        extra = f"; a demo loop IS running (heartbeat {age:.0f}s ago)" if alive else ""
        return Check("Background service", WARN, f"not installed{extra}", "uv run scout service install")
    if not s.loaded:
        return Check("Background service", WARN, "installed but stopped", "uv run scout service start")
    if not alive:
        return Check("Background service", FAIL, f"loaded (pid {s.pid}, last exit {s.last_exit}) but no recent "
                     "heartbeat", "look at logs/service.err.log, then: uv run scout service stop && "
                     "uv run scout service start")
    return Check("Background service", OK, f"running (pid {s.pid}), heartbeat {age:.0f}s ago, v{beat['version']}")


def check_sleep(settings: Settings, runner: Runner = run_command) -> Check:
    code, out, _ = runner(["pmset", "-g"])
    if code != 0:
        return Check("Sleep", WARN, "couldn't read the power settings (pmset)")
    sleep_minutes = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "sleep" and parts[1].isdigit():
            sleep_minutes = int(parts[1])
    prevented = "sleep prevented by" in out
    if sleep_minutes == 0 or prevented:
        return Check("Sleep", OK, "system sleep is off or being prevented right now")
    detail = f"the Mac sleeps after {sleep_minutes} idle minutes" if sleep_minutes else "the Mac may sleep"
    how = ("keep_awake is on, so Scout prevents sleep while it runs on mains power"
           if settings.service.keep_awake else "turn on service.keep_awake in config.yaml")
    return Check("Sleep", WARN, f"{detail}; {how}. A closed lid always sleeps",
                 "System Settings → Battery → Options → 'Prevent automatic sleeping on power adapter when the "
                 "display is off'")


def check_rate(settings: Settings, conn: sqlite3.Connection | None) -> Check | None:
    """Compare the manual AUD rate in config.yaml with the latest RBA rate, if we have one stored."""
    if conn is None:
        return None
    latest = latest_rate(conn)
    if latest is None:
        return None
    rate, day = latest
    configured = settings.demo.aud_to_usdc_rate
    gap = abs(configured / rate - 1) * 100
    status = OK if gap < 3 else WARN
    return Check("AUD rate in config", status, f"config {configured} vs RBA {rate} on {day} ({gap:.0f}% apart)",
                 "" if status == OK else f"set demo.aud_to_usdc_rate: {rate} in config.yaml (changes AUD figures)")


# ------------------------------------------------------------ network checks


async def check_api(settings: Settings, http: httpx.AsyncClient) -> list[Check]:
    started = time.monotonic()
    try:
        response = await http.post(settings.data.api_url, json={"type": "allMids"})
        response.raise_for_status()
        mids = parse_mids(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        return [Check("Hyperliquid API", FAIL, f"not reachable: {exc}", "check the internet connection")]
    latency = (time.monotonic() - started) * 1000
    checks = [Check("Hyperliquid API", OK, f"{len(mids)} prices in {latency:.0f} ms (BTC ${mids.get('BTC', 0):,.0f})")]
    server = response.headers.get("date")
    if server:
        drift = abs(time.time() - email.utils.parsedate_to_datetime(server).timestamp())
        status = OK if drift < 5 else WARN
        checks.append(Check("Clock", status, f"Mac clock is {drift:.1f}s from the server's",
                            "" if status == OK else "System Settings → General → Date & Time → set automatically"))
    return checks


async def check_websocket(settings: Settings, timeout: float = 15.0) -> Check:
    async def first() -> dict[str, float]:
        async for mids in stream_mids(settings.data.ws_url):
            return mids
        return {}

    try:
        mids = await asyncio.wait_for(first(), timeout)
    except TimeoutError:
        return Check("Live price feed", FAIL, f"no prices within {timeout:.0f}s", "check the internet connection")
    return Check("Live price feed", OK, f"websocket connected, {len(mids)} live prices")


async def check_fx(settings: Settings, http: httpx.AsyncClient) -> Check:
    try:
        response = await http.get(settings.tax.fx_url)
        response.raise_for_status()
        rates = parse_rba_csv(response.text)
    except (httpx.HTTPError, ValueError) as exc:
        return Check("RBA exchange rates", WARN, f"not available: {exc}", "only needed for `scout tax-export`")
    day = max(rates)
    configured = settings.demo.aud_to_usdc_rate
    gap = abs(configured / rates[day] - 1) * 100
    if gap >= 3:
        return Check("RBA exchange rates", WARN, f"latest A$1 = US${rates[day]} ({day}), but config.yaml uses "
                     f"{configured} ({gap:.0f}% apart), so AUD figures are off by about that much",
                     f"set demo.aud_to_usdc_rate: {rates[day]} in config.yaml")
    return Check("RBA exchange rates", OK, f"latest A$1 = US${rates[day]} ({day}); config.yaml's {configured} is close")


async def check_ntfy(settings: Settings, http: httpx.AsyncClient) -> Check:
    n = settings.notify
    if not n.ntfy_enabled:
        if n.imessage_enabled:
            return Check("ntfy push alerts", OK, "off (iMessage is used instead)")
        return Check("ntfy push alerts", WARN, "off, and iMessage is off too, so you get no phone alerts",
                     "uv run scout ntfy-setup")
    try:
        response = await http.get(f"{n.ntfy_server.rstrip('/')}/v1/health")
        healthy = response.status_code == 200 and response.json().get("healthy") is True
    except (httpx.HTTPError, ValueError) as exc:
        return Check("ntfy push alerts", FAIL, f"{n.ntfy_server} not reachable: {exc}", "check the internet connection")
    if not healthy:
        return Check("ntfy push alerts", FAIL, f"{n.ntfy_server} says it isn't healthy", "try again later")
    return Check("ntfy push alerts", OK, f"on, {n.ntfy_server} is up; run `scout notify-test` to send a real test")


async def check_imessage(settings: Settings, runner: Runner = run_command) -> Check:
    if not settings.notify.imessage_enabled:
        return Check("iMessage", OK if settings.notify.ntfy_enabled else WARN,
                     "off" + (" (ntfy is used instead)" if settings.notify.ntfy_enabled else ""))
    script = 'tell application "Messages" to get name of 1st account whose service type = iMessage'
    code, out, err = await asyncio.to_thread(runner, ["osascript", "-e", script])
    if code != 0:
        return Check("iMessage", FAIL, f"can't use Messages: {err.strip()[:200]}",
                     "System Settings → Privacy & Security → Automation → allow Terminal (and python) to "
                     "control Messages; make sure Messages is signed in")
    return Check("iMessage", OK, f"Messages is signed in ({out.strip() or 'iMessage account found'}); "
                                 "run `scout notify-test` to send a real test")


# ---------------------------------------------------------------- all of it


async def run_checks(settings: Settings, env_file: Path, *, launchctl: Launchctl = run_launchctl,
                     runner: Runner = run_command, http: httpx.AsyncClient | None = None,
                     network: bool = True) -> list[Check]:
    checks = [Check("Scout", OK, f"v{APP_VERSION}, config valid, mode {settings.mode.value.upper()}"),
              check_env_file(env_file), check_database(settings), check_disk(settings), check_logs(settings),
              check_backups(settings), check_service(settings, launchctl), check_sleep(settings, runner)]
    if settings.app.db_path.exists():
        with closing(sqlite3.connect(f"file:{settings.app.db_path}?mode=ro", uri=True)) as conn:
            try:
                rate = check_rate(settings, conn)
            except sqlite3.Error:
                rate = None
        if rate:
            checks.append(rate)
    if network:
        own = http is None
        http = http or httpx.AsyncClient(timeout=15, headers={"User-Agent": f"Scout/{APP_VERSION}"})
        try:
            checks += await check_api(settings, http)
            checks.append(await check_websocket(settings))
            checks.append(await check_fx(settings, http))
            checks.append(await check_ntfy(settings, http))
        finally:
            if own:
                await http.aclose()
    checks.append(await check_imessage(settings, runner))
    return checks
