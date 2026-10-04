"""Running unattended: launchd service, heartbeat, backups, restart alerts, clean stops and `scout doctor`.

launchctl, pmset, osascript and the network are all faked: nothing here changes the Mac.
"""

import asyncio
import os
import plistlib
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from scout import doctor, service
from scout.backup import backup_db, latest_backup
from scout.db import open_db
from scout.demo import DemoEngine, DemoRunner

pytestmark = pytest.mark.anyio
T0 = 1_780_000_000_000


class FakeLaunchctl:
    def __init__(self, loaded=False, pid=None):
        self.calls = []
        self.loaded = loaded
        self.pid = pid

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "bootstrap":
            self.loaded = True
            return 0, "", ""
        if args[0] == "bootout":
            was, self.loaded = self.loaded, False
            return (0, "", "") if was else (3, "", "No such process")
        if args[0] == "print":
            if not self.loaded:
                return 113, "", "Could not find service"
            return 0, f"state = running\n\tpid = {self.pid}\n\tlast exit code = 0\n", ""
        return 1, "", "unknown"


class Clock:
    def __init__(self, ms=T0):
        self.ms = ms

    def __call__(self):
        return self.ms


# ------------------------------------------------------------ launchd


def test_plist_starts_at_login_and_restarts_after_crashes(settings, tmp_path):
    plist = service.build_plist(settings, Path("/venv/bin/scout"), tmp_path, tmp_path / "config.yaml",
                                tmp_path / ".env")
    assert plist["ProgramArguments"] == ["/venv/bin/scout", "demo", "--config", str(tmp_path / "config.yaml"),
                                         "--env-file", str(tmp_path / ".env")]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}  # a crash restarts it; a clean stop doesn't
    assert plist["ThrottleInterval"] >= 10
    assert plist["EnvironmentVariables"]["LAUNCHED_BY_SCOUT_SERVICE"] == "1"
    assert plist["StandardErrorPath"].endswith("service.err.log")


def test_install_status_stop_uninstall(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "agents_dir", lambda: tmp_path / "LaunchAgents")
    launchctl = FakeLaunchctl(pid=4321)
    path = service.install(settings, Path("/venv/bin/scout"), tmp_path, tmp_path / "config.yaml", tmp_path / ".env",
                           launchctl)
    assert path == tmp_path / "LaunchAgents" / "au.scout.demo.plist"
    assert plistlib.loads(path.read_bytes())["Label"] == "au.scout.demo"
    assert launchctl.calls[-1] == ["bootstrap", f"gui/{os.getuid()}", str(path)]
    status = service.status(settings, launchctl)
    assert (status.installed, status.loaded, status.pid) == (True, True, 4321)
    assert service.stop(settings, launchctl)
    assert not service.status(settings, launchctl).loaded
    assert service.start(settings, launchctl) == (True, "")
    assert service.start(settings, launchctl) == (False, "it's already running")
    assert service.uninstall(settings, launchctl)
    assert not path.exists()
    assert not service.uninstall(settings, launchctl)  # already gone


def test_install_reports_launchctl_errors(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "agents_dir", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="couldn't load"):
        service.install(settings, Path("/x"), tmp_path, tmp_path, tmp_path,
                        lambda args: (5, "", "Input/output error"))


def test_heartbeat_round_trip(tmp_path):
    path = tmp_path / "heartbeat.json"
    assert service.read_heartbeat(path) is None
    service.write_heartbeat(path, pid=12, version="0.10.0", state="RUNNING", ts_ms=T0, positions=2)
    assert service.read_heartbeat(path) == {"pid": 12, "version": "0.10.0", "state": "RUNNING", "ts_ms": T0,
                                            "open_positions": 2}


def test_service_logs_rotate(tmp_path):
    big = tmp_path / "service.err.log"
    big.write_bytes(b"x" * 200)
    service.rotate_service_logs(tmp_path, max_bytes=100)
    assert not big.exists() and (tmp_path / "service.err.log.1").stat().st_size == 200


# ------------------------------------------------------------ backups


def test_daily_backup_is_a_working_copy_and_old_ones_are_pruned(settings, tmp_path):
    db = settings.app.db_path
    with closing(open_db(db)) as conn:
        conn.execute("INSERT INTO bot_state (key, value, updated_ms) VALUES ('cash_usd', '650', 0)")
        conn.commit()
    for day in range(5):
        backup_db(db, tmp_path / "backups", T0 + day * 86_400_000, settings.app.tz, keep=3)
    backups = sorted((tmp_path / "backups").glob("scout-*.db"))
    assert len(backups) == 3
    assert latest_backup(tmp_path / "backups") == backups[-1]
    with closing(sqlite3.connect(backups[-1])) as copy:
        assert copy.execute("SELECT value FROM bot_state WHERE key = 'cash_usd'").fetchone()[0] == "650"


# ------------------------------------------------------- restarts and stops


def engine_at(settings, clock):
    conn = open_db(settings.app.db_path)
    return DemoEngine(settings, conn, clock=clock), conn


class Outbox:
    enabled = True

    def __init__(self):
        self.messages = []

    def notify(self, text, category, priority=None):
        self.messages.append((category, priority, text))


def test_restart_after_a_crash_sends_an_alert(settings):
    from scout.notify import Priority

    clock = Clock()
    engine, conn = engine_at(settings, clock)
    with closing(conn):
        assert engine.on_start(by_service=True) == "First start."
        with conn:
            engine.state.set("last_tick_ms", clock())
        engine.record_crash("ConnectionError: boom")
        clock.ms += 3 * 60_000
        engine.notifier = Outbox()
        line = engine.on_start(by_service=True)
        assert line.startswith("🔄 Scout restarted after an unexpected stop")
        assert "Reason: ConnectionError: boom" in line and "3m ago" in line
        [(category, priority, text)] = engine.notifier.messages
        assert category == "service" and priority is Priority.NORMAL


def test_unexplained_stop_is_reported_too(settings):
    clock = Clock()
    engine, conn = engine_at(settings, clock)
    with closing(conn):
        with conn:
            engine.state.set("last_tick_ms", clock())  # was running, then the power went off
        clock.ms += 3_600_000
        assert "power loss, a Mac restart" in engine.on_start(by_service=True)


def test_clean_restart_is_only_a_minor_note(settings):
    from scout.notify import Priority

    clock = Clock()
    engine, conn = engine_at(settings, clock)
    with closing(conn):
        with conn:
            engine.state.set("last_tick_ms", clock())
            engine.state.set("loop_stopped_ms", clock())
        clock.ms += 8 * 3_600_000
        engine.notifier = Outbox()
        assert engine.on_start(by_service=True).startswith("Scout started (service). Last stopped cleanly")
        assert engine.notifier.messages[0][1] is Priority.BATCH


def test_crash_loop_is_detected(settings):
    clock = Clock()
    engine, conn = engine_at(settings, clock)
    with closing(conn):
        counts = []
        for _ in range(5):
            counts.append(engine.recent_starts())
            clock.ms += 5 * 60_000
        assert counts == [1, 2, 3, 4, 5]
        clock.ms += 2 * 3_600_000
        assert engine.recent_starts() == 1  # old starts drop out of the window


async def test_stop_signal_ends_the_loop_cleanly_with_heartbeat_and_backup(settings):
    clock = Clock()
    engine, conn = engine_at(settings, clock)
    stop = asyncio.Event()
    ticks = []

    async def stream():
        while True:
            yield {"BTC": 80_000.0}
            await asyncio.sleep(0)

    async def cycle(_):
        pass

    async def sleep(seconds):
        clock.ms += int(seconds * 1000)
        ticks.append(clock.ms)
        if len(ticks) > 20:
            stop.set()  # what SIGTERM from launchd does
        await asyncio.sleep(0)

    runner = DemoRunner(engine, cycle, stream, wall_clock=lambda: clock.ms / 1000, sleep=sleep, echo=lambda _: None)
    with closing(conn):
        await asyncio.wait_for(runner.run(stop=stop), timeout=10)
        assert engine.state.get_float("loop_stopped_ms") >= engine.state.get_float("last_tick_ms")  # clean stop
        beat = service.read_heartbeat(service.heartbeat_path(settings))
        assert beat["pid"] == os.getpid() and beat["state"] == "RUNNING"
        assert latest_backup(settings.app.backup_dir) is not None


# ------------------------------------------------------------ scout doctor


def test_doctor_local_checks(settings, tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    env.chmod(0o644)
    assert doctor.check_env_file(env).status == "warn"
    env.chmod(0o600)
    assert doctor.check_env_file(env).status == "ok"
    assert doctor.check_database(settings).status == "warn"  # no database yet
    open_db(settings.app.db_path).close()
    assert doctor.check_database(settings).status == "ok"
    assert doctor.check_logs(settings).status == "ok"
    assert doctor.check_backups(settings).status == "warn"


def test_doctor_sleep_and_service(settings):
    awake = doctor.check_sleep(settings, lambda args: (0, " sleep                0\n", ""))
    asleep = doctor.check_sleep(settings, lambda args: (0, " sleep                10\n", ""))
    assert awake.status == "ok" and asleep.status == "warn" and "10 idle minutes" in asleep.detail
    assert doctor.check_service(settings, FakeLaunchctl()).status == "warn"  # not installed


async def test_doctor_network_checks_with_a_fake_server(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"BTC": "80000", "ETH": "2000"},
                                  headers={"date": "Mon, 28 Sep 2026 12:00:00 GMT"})
        return httpx.Response(200, text="Series ID,FXRUSD\n28-Sep-2026,0.7023\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = await doctor.check_api(settings, http)
        fx = await doctor.check_fx(settings, http)
    assert api[0].status == "ok" and "2 prices" in api[0].detail
    assert fx.status == "warn" and "0.65" in fx.detail  # config's manual rate is 7% off the RBA's


async def test_doctor_reports_an_unreachable_api(settings):
    def down(request):
        raise httpx.ConnectError("no internet")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        [check] = await doctor.check_api(settings, http)
    assert check.status == "fail" and "internet" in check.fix


async def test_stop_request_abandons_a_slow_hourly_check(settings):
    clock = Clock()
    engine, conn = engine_at(settings, clock)
    stop = asyncio.Event()

    async def stream():
        while True:
            yield {"BTC": 80_000.0}
            await asyncio.sleep(0)

    async def slow_cycle(_):
        stop.set()  # SIGTERM arrives while the check is downloading...
        await asyncio.sleep(3600)  # ...which would take ages

    runner = DemoRunner(engine, slow_cycle, stream, wall_clock=lambda: clock.ms / 1000,
                        sleep=lambda s: asyncio.sleep(0), echo=lambda _: None)
    with closing(conn):
        await asyncio.wait_for(runner.run(stop=stop), timeout=5)  # returns quickly, not after an hour
        assert engine.state.get("loop_stopped_ms") is not None


def test_the_service_environment_loads_cleanly(settings, write_config, no_env_file, monkeypatch, tmp_path):
    """Regression: an env var named SCOUT_SERVICE was read as the `service:` setting and broke the config."""
    from scout.config import load_settings

    plist = service.build_plist(settings, Path("/x"), tmp_path, tmp_path, tmp_path)
    for name, value in plist["EnvironmentVariables"].items():
        monkeypatch.setenv(name, value)
    assert load_settings(write_config(""), no_env_file).service.label == "au.scout.demo"


def test_start_retries_when_launchd_is_still_busy(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "agents_dir", lambda: tmp_path)
    replies = iter([(5, "", "Bootstrap failed: 5: Input/output error"), (0, "", "")])
    calls = []

    def launchctl(args):
        calls.append(args[0])
        if args[0] == "print":
            return 113, "", ""
        return next(replies)

    assert service.start(settings, launchctl, wait=0) == (True, "")
    assert calls.count("bootstrap") == 2


def test_start_isnt_fooled_by_a_service_still_shutting_down(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "agents_dir", lambda: tmp_path)
    calls = []

    def launchctl(args):
        calls.append(args[0])
        if args[0] == "print":
            return 0, "state = not running\n", ""  # listed, but no process yet
        return 0, "", ""

    assert service.start(settings, launchctl, wait=0) == (True, "")
    assert "bootstrap" in calls
