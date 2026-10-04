from typer.testing import CliRunner

from scout import APP_VERSION
from scout.cli import app

runner = CliRunner()


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"Scout v{APP_VERSION}" in result.output


def test_config_check_passes_and_prints_version(write_config, no_env_file):
    result = runner.invoke(app, ["config-check", "-c", str(write_config("")), "--env-file", str(no_env_file)])
    assert result.exit_code == 0, result.output
    assert f"Scout v{APP_VERSION} | DEMO mode" in result.output
    assert "Config OK" in result.output


def test_config_check_reports_problems(write_config, no_env_file):
    config = write_config("mode: live\nrisk:\n  max_leverage: 3\n")
    result = runner.invoke(app, ["config-check", "-c", str(config), "--env-file", str(no_env_file)])
    assert result.exit_code == 1
    assert "2 problem(s)" in result.output
    assert "mode" in result.output and "risk.max_leverage" in result.output


def test_init_db_creates_database(write_config, no_env_file, tmp_path):
    result = runner.invoke(app, ["init-db", "-c", str(write_config("")), "--env-file", str(no_env_file)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "scout.db").is_file()
    assert (tmp_path / "logs" / "scout.log").is_file()


def _offline(monkeypatch, fake):
    import scout.cli as cli
    from scout.data import HyperliquidClient

    monkeypatch.setattr(cli, "make_client",
                        lambda settings: HyperliquidClient(transport=fake.transport(), weight_per_minute=1200))


def test_market_shows_coins_by_volume_and_saves_snapshot(write_config, no_env_file, tmp_path, monkeypatch):
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=1_790_000_000_000))
    result = runner.invoke(app, ["market", "-n", "5", "-c", str(write_config("")), "--env-file", str(no_env_file)])
    assert result.exit_code == 0, result.output
    assert "BTC" in result.output
    assert "delisted hidden" in result.output
    import sqlite3

    with sqlite3.connect(tmp_path / "data" / "scout.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] > 0


def test_fetch_stores_candles(write_config, no_env_file, tmp_path, monkeypatch):
    from scout.db import now_ms
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=now_ms()))
    args = ["fetch", "--coin", "btc", "--interval", "4h", "--days", "30", "-c", str(write_config("")),
            "--env-file", str(no_env_file)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "BTC" in result.output
    import sqlite3

    with sqlite3.connect(tmp_path / "data" / "scout.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM candles WHERE coin = 'BTC'").fetchone()[0] >= 179


def test_fetch_rejects_unknown_coin(write_config, no_env_file, monkeypatch):
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=1_790_000_000_000))
    args = ["fetch", "--coin", "NOTACOIN", "-c", str(write_config("")), "--env-file", str(no_env_file)]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "NOTACOIN" in result.output


def test_mood_shows_regime_and_saves_it(write_config, no_env_file, tmp_path, monkeypatch):
    from scout.db import now_ms
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=now_ms()))
    result = runner.invoke(app, ["mood", "-c", str(write_config("")), "--env-file", str(no_env_file)])
    assert result.exit_code == 0, result.output
    assert "Market mood:" in result.output
    assert "Rules for trading" in result.output
    import sqlite3

    with sqlite3.connect(tmp_path / "data" / "scout.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM regime_history").fetchone()[0] == 1


def test_mood_history_saves_chart(write_config, no_env_file, tmp_path, monkeypatch):
    from scout.db import now_ms
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=now_ms()))
    out = tmp_path / "chart.png"
    args = ["mood-history", "--days", "60", "-o", str(out), "-c", str(write_config("")), "--env-file", str(no_env_file)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Chart saved" in result.output
    assert out.is_file()


def test_scan_prints_shortlist_and_saves(write_config, no_env_file, tmp_path, monkeypatch):
    from scout.db import now_ms
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=now_ms()))
    config = write_config("scanner:\n  min_24h_volume_usd: 1000\n  min_open_interest_usd: 0\n")
    result = runner.invoke(app, ["scan", "-c", str(config), "--env-file", str(no_env_file)])
    assert result.exit_code == 0, result.output
    assert "Why each shortlisted coin is there" in result.output
    assert "Excluded" in result.output  # the thin-book coin
    import sqlite3

    with sqlite3.connect(tmp_path / "data" / "scout.db") as conn:
        passed = conn.execute("SELECT COUNT(*) FROM scan_results WHERE passed = 1").fetchone()[0]
        excluded = conn.execute("SELECT COUNT(*) FROM scan_results WHERE passed = 0").fetchone()[0]
    assert passed > 0 and excluded > 0


# ------------------------------------------------------------ demo trading


def _demo_config(write_config):
    return write_config("demo:\n  tick_seconds: 0.05\n")


def _run(args, config, no_env_file):
    return runner.invoke(app, [*args, "-c", str(config), "--env-file", str(no_env_file)])


def _open_demo_position(config, no_env_file):
    """Put one ETH position in the demo account directly."""
    import asyncio
    from contextlib import closing

    from scout.config import load_settings
    from scout.db import now_ms, open_db
    from scout.demo import DemoEngine
    from scout.signals import Action, Signal

    settings = load_settings(config, no_env_file)
    with closing(open_db(settings.app.db_path)) as conn:
        engine = DemoEngine(settings, conn)
        engine.prices.update({"ETH": 2000.0}, now_ms())
        signal = Signal("ETH", Action.ENTER_LONG, 0, 0, "4h", 2000.0, 1900.0, "RISK_ON", "Buying ETH: test.", qty=0.05)
        assert asyncio.run(engine.open_from_signal(signal)) is not None


def test_status_positions_and_controls(write_config, no_env_file, monkeypatch):
    from scout.db import now_ms
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=now_ms()))
    config = _demo_config(write_config)
    _open_demo_position(config, no_env_file)

    result = _run(["status"], config, no_env_file)
    assert result.exit_code == 0, result.output
    assert "State: RUNNING" in result.output and "A$" in result.output and "ETH" in result.output

    result = _run(["positions", "--closed", "5"], config, no_env_file)
    assert result.exit_code == 0, result.output
    assert "Buying ETH: test." in result.output

    assert "Paused" in _run(["pause"], config, no_env_file).output
    assert "State: PAUSED" in _run(["status"], config, no_env_file).output
    assert "Resumed" in _run(["resume"], config, no_env_file).output

    result = _run(["kill", "--yes"], config, no_env_file)
    assert result.exit_code == 0, result.output
    assert "KILLED: closed 1 position(s)" in result.output
    assert "State: KILLED" in _run(["status"], config, no_env_file).output
    assert _run(["resume"], config, no_env_file).exit_code == 1  # resume can't undo a kill

    result = _run(["reset-kill", "--yes"], config, no_env_file)
    assert "Kill switch reset" in result.output
    assert "State: RUNNING" in _run(["status"], config, no_env_file).output


def test_demo_runs_a_cycle_on_live_prices(write_config, no_env_file, tmp_path, monkeypatch):
    import asyncio
    import sqlite3

    import scout.cli as cli
    from scout.db import now_ms
    from tests.fake_hyperliquid import FakeHyperliquid

    _offline(monkeypatch, FakeHyperliquid(now_ms=now_ms()))

    async def fake_stream():
        while True:
            yield {"BTC": 80_000.0, "ETH": 2000.0}
            await asyncio.sleep(0.01)

    monkeypatch.setattr(cli, "price_stream", lambda settings: fake_stream())
    result = _run(["demo", "--minutes", "0.02"], _demo_config(write_config), no_env_file)
    assert result.exit_code == 0, result.output
    assert "Mood" in result.output and "shortlist" in result.output
    with sqlite3.connect(tmp_path / "data" / "scout.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM regime_history").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] >= 1
        messages = [r[0] for r in conn.execute("SELECT message FROM events_log WHERE category = 'demo'")]
    assert any("Demo trading started" in m for m in messages)
    assert "demo loop NOT running" in _run(["status"], _demo_config(write_config), no_env_file).output


# ------------------------------------------------------------ notifications


def test_notify_test_sends_one_message_with_a_mocked_osascript(write_config, tmp_path, monkeypatch):
    import scout.cli as cli
    from scout.notify import IMessageBackend

    calls = []

    async def fake_osascript(args, timeout):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(cli, "make_backends", lambda settings, include_disabled=False: [
        IMessageBackend(settings.notify.imessage_recipient.get_secret_value(), runner=fake_osascript)])
    env = tmp_path / ".env"
    env.write_text('SCOUT_NOTIFY__IMESSAGE_RECIPIENT="+61412345678"\n')
    result = runner.invoke(app, ["notify-test", "-c", str(write_config("")), "--env-file", str(env)])
    assert result.exit_code == 0, result.output
    assert "imessage: sent" in result.output
    assert "+61412345678" not in result.output  # never printed
    [args] = calls
    assert args[3] == "+61412345678" and args[4].startswith("Scout v")


def test_notify_test_explains_a_failure(write_config, tmp_path, monkeypatch):
    import scout.cli as cli
    from scout.notify import IMessageBackend

    async def refusing(args, timeout):
        return 1, "", "Not authorised to send Apple events to Messages."

    monkeypatch.setattr(cli, "make_backends", lambda settings, include_disabled=False: [
        IMessageBackend("+61412345678", runner=refusing)])
    env = tmp_path / ".env"
    env.write_text('SCOUT_NOTIFY__IMESSAGE_RECIPIENT="+61412345678"\n')
    result = runner.invoke(app, ["notify-test", "-c", str(write_config("")), "--env-file", str(env)])
    assert result.exit_code == 1
    assert "Not authorised" in result.output and "Automation" in result.output


def test_notify_test_without_recipient(write_config, no_env_file):
    result = runner.invoke(app, ["notify-test", "-c", str(write_config("")), "--env-file", str(no_env_file)])
    assert result.exit_code == 1
    assert "SCOUT_NOTIFY__IMESSAGE_RECIPIENT" in result.output


# ------------------------------------------------------- replay and explain


def test_replay_then_explain(write_config, no_env_file, tmp_path, monkeypatch):
    import pandas as pd

    import scout.cli as cli
    from scout.backtest import History, build_plans
    from tests.test_backtest import make_candles, to_daily

    candles = make_candles()
    history = History(to_daily(candles), candles)

    async def fake_history(settings, start, end, refresh):
        return history, build_plans(history, settings, start, end)

    monkeypatch.setattr(cli, "_load_history", fake_history)
    monkeypatch.setattr(cli, "_period", lambda settings, a, b: (pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC")))
    config = write_config("scanner:\n  min_24h_volume_usd: 100000\n  universe_size: 6\n  max_coins: 4\n"
                          "regime:\n  breadth_min_coins: 3\n")
    args = ["-c", str(config), "--env-file", str(no_env_file)]
    result = runner.invoke(app, ["replay", "--from", "2025-01-01", "--to", "2025-02-01", "--speed", "max", *args])
    assert result.exit_code == 0, result.output
    assert "Replay finished" in result.output
    assert "BOUGHT" in result.output  # trades are printed as they happen
    assert (tmp_path / "data" / "replay.db").is_file()
    assert not (tmp_path / "data" / "scout.db").exists()  # the real demo account is never touched

    listing = runner.invoke(app, ["explain", "--replay", *args])
    assert listing.exit_code == 0 and "#1" in listing.output
    story = runner.invoke(app, ["explain", "1", "--replay", *args])
    assert story.exit_code == 0, story.output
    assert "1. The market mood when it opened" in story.output and "7. In plain English" in story.output
    assert runner.invoke(app, ["explain", "999", "--replay", *args]).exit_code == 1


def test_replay_rejects_a_bad_speed(write_config, no_env_file):
    result = runner.invoke(app, ["replay", "--from", "2025-01-01", "--to", "2025-02-01", "--speed", "fast",
                                 "-c", str(write_config("")), "--env-file", str(no_env_file)])
    assert result.exit_code == 1 and "500x" in result.output


# -------------------------------------------------------------- unattended


def test_backup_report_and_tax_export_commands(write_config, no_env_file, tmp_path, settings):
    import asyncio
    from contextlib import closing

    from scout.db import open_db
    from scout.demo import DemoEngine
    from scout.signals import Action, Signal
    from scout.tax import save_rates

    args = ["-c", str(write_config("")), "--env-file", str(no_env_file)]
    with closing(open_db(settings.app.db_path)) as conn:
        engine = DemoEngine(settings, conn)
        engine.prices.update({"ETH": 2000.0, "BTC": 80_000.0}, engine.clock())
        asyncio.run(engine.housekeeping())
        signal = Signal("ETH", Action.ENTER_LONG, 0, 0, "4h", 2000.0, 1900.0, "RISK_ON", "Buying ETH: test.", qty=0.05)
        asyncio.run(engine.open_from_signal(signal))
        [position] = engine.account.positions()
        asyncio.run(engine.close(position, "Selling ETH: test.", 2100.0))
        save_rates(conn, {"2020-01-01": 0.70})

    result = runner.invoke(app, ["backup", *args])
    assert result.exit_code == 0 and "Backed up" in result.output

    result = runner.invoke(app, ["report", "weekly", *args])
    assert result.exit_code == 0, result.output
    assert "VERDICT" in result.output and "TOO EARLY TO TELL" in result.output

    result = runner.invoke(app, ["tax-export", "--no-refresh", *args])
    assert result.exit_code == 0, result.output
    assert "1 opened, 1 closed" in result.output and "fake-money" in result.output
    [csv_file] = (tmp_path / "reports" / "tax").glob("scout_trades_FY*_demo.csv")
    assert "ETH" in csv_file.read_text()


def test_service_status_command(write_config, no_env_file, monkeypatch):
    from scout import service

    monkeypatch.setattr(service, "run_launchctl", lambda args: (113, "", "not found"))
    monkeypatch.setattr(service, "status", lambda settings, launchctl=None, folder=None: service.ServiceStatus(
        False, False, None, None, service.plist_path(settings)))
    result = runner.invoke(app, ["service", "status", "-c", str(write_config("")), "--env-file", str(no_env_file)])
    assert result.exit_code == 0 and "Installed: no" in result.output and "Heartbeat: none yet" in result.output
