"""Notification tests. osascript is always mocked: no real message is ever sent."""

from contextlib import closing
from datetime import time
from types import SimpleNamespace

import pandas as pd
import pytest

from scout.db import open_db
from scout.notify import (
    APPLESCRIPT,
    IMessageBackend,
    Notifier,
    NotifyBackend,
    NotifyError,
    Priority,
    clean_text,
    daily_summary,
    in_quiet_hours,
    mood_changed,
    trade_closed,
    trade_opened,
)
from scout.regime import Regime, Volatility
from scout.version import APP_VERSION

pytestmark = pytest.mark.anyio

NOON = int(pd.Timestamp("2026-06-10 02:00", tz="UTC").timestamp() * 1000)  # 12:00 Sydney
NIGHT = int(pd.Timestamp("2026-06-10 14:00", tz="UTC").timestamp() * 1000)  # 00:00 Sydney (quiet)
MORNING = int(pd.Timestamp("2026-06-10 21:00", tz="UTC").timestamp() * 1000)  # 07:00 Sydney
USD_PER_AUD = 0.65


class FakeOsascript:
    """Stands in for the osascript program."""

    def __init__(self, fail_times: int = 0, stderr: str = "Messages got an error") -> None:
        self.calls: list[list[str]] = []
        self.fail_times = fail_times
        self.stderr = stderr

    async def __call__(self, args, timeout):
        self.calls.append(list(args))
        if len(self.calls) <= self.fail_times:
            return 1, "", self.stderr
        return 0, "", ""


class Recorder(NotifyBackend):
    def __init__(self, name="recorder", fail=False):
        self.name = name
        self.fail = fail
        self.sent: list[str] = []

    async def send(self, text, priority=None):
        self.priorities = getattr(self, "priorities", []) + [priority]
        if self.fail:
            raise NotifyError("down")
        self.sent.append(text)


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, seconds):
        self.ms += int(seconds * 1000)


@pytest.fixture
def conn(tmp_path):
    with closing(open_db(tmp_path / "scout.db")) as connection:
        yield connection


def notifier(conn, settings, backends, start=NOON):
    clock = Clock(start)
    return Notifier(conn, settings, backends, clock=clock), clock


def statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM notifications ORDER BY id")]


# ------------------------------------------------------------ osascript


async def test_text_and_recipient_are_arguments_not_part_of_the_script():
    fake = FakeOsascript()
    tricky = 'He said "hi" \\ end tell\n" & do shell script "rm -rf ~" & "'
    await IMessageBackend("+61412345678", runner=fake).send(tricky)
    [args] = fake.calls
    assert args[:3] == ["osascript", "-e", APPLESCRIPT]
    assert args[3] == "+61412345678"
    assert args[4] == tricky  # passed through untouched, as data
    assert "on run argv" in APPLESCRIPT and "rm -rf" not in APPLESCRIPT


async def test_osascript_failure_raises_with_the_reason():
    backend = IMessageBackend("+61412345678", runner=FakeOsascript(fail_times=1, stderr="Not authorised"))
    with pytest.raises(NotifyError, match="Not authorised"):
        await backend.send("hi")


def test_clean_text():
    assert clean_text("a\x07b\r\n\n\n\nc", 100) == "ab\n\nc"
    assert clean_text("x" * 50, 10) == "x" * 9 + "…"


@pytest.mark.parametrize(
    ("hhmm", "quiet"), [("23:00", True), ("23:30", True), ("03:00", True), ("06:59", True), ("07:00", False),
                        ("12:00", False), ("22:59", False)],
)
def test_quiet_hours_wrap_past_midnight(hhmm, quiet):
    assert in_quiet_hours(time.fromisoformat(hhmm), time(23), time(7)) is quiet


def test_same_start_and_end_means_no_quiet_hours():
    assert not in_quiet_hours(time(3), time(0), time(0))


# ------------------------------------------------------------ the queue


async def test_every_message_carries_version_name_and_mode(conn, settings):
    backend = Recorder()
    n, _ = notifier(conn, settings, [backend])
    n.notify("hello", "test")
    assert await n.deliver() == 1
    assert backend.sent == [f"Scout v{APP_VERSION} · Breakout:1 · DEMO\nhello"]
    assert statuses(conn) == ["sent"]


async def test_messages_are_spaced_out(conn, settings):
    backend = Recorder()
    n, clock = notifier(conn, settings, [backend])
    n.notify("one", "test")
    n.notify("two", "test")
    await n.deliver()
    await n.deliver()  # too soon
    assert len(backend.sent) == 1
    clock.advance(settings.notify.min_seconds_between)
    await n.deliver()
    assert len(backend.sent) == 2


async def test_quiet_hours_hold_messages_but_not_the_kill_switch(conn, settings):
    backend = Recorder()
    n, clock = notifier(conn, settings, [backend], start=NIGHT)
    n.notify("bought ETH", "trade")
    n.notify("daily limit hit", "risk")
    n.notify("KILL SWITCH", "risk", Priority.CRITICAL)
    await n.deliver()
    assert len(backend.sent) == 1 and "KILL SWITCH" in backend.sent[0]
    clock.ms = MORNING
    await n.deliver()
    assert len(backend.sent) == 2
    assert "While you were asleep:" in backend.sent[1]
    assert "• bought ETH" in backend.sent[1] and "• daily limit hit" in backend.sent[1]
    assert statuses(conn).count("merged") == 2


async def test_minor_updates_are_batched_hourly(conn, settings):
    backend = Recorder()
    n, clock = notifier(conn, settings, [backend])
    n.notify("ETH stop moved", "trade", Priority.BATCH)
    clock.advance(600)
    n.notify("SOL stop moved", "trade", Priority.BATCH)
    await n.deliver()
    assert backend.sent == []
    clock.advance(settings.notify.batch_minutes * 60)
    await n.deliver()
    assert len(backend.sent) == 1
    assert "Updates:" in backend.sent[0] and "ETH stop moved" in backend.sent[0] and "SOL" in backend.sent[0]


async def test_a_backlog_is_merged_into_one_digest(conn, settings):
    backend = Recorder()
    n, _ = notifier(conn, settings, [backend])
    for i in range(6):
        n.notify(f"trade {i}", "trade")
    await n.deliver()
    assert len(backend.sent) == 1
    assert "6 updates:" in backend.sent[0]


async def test_hourly_cap(conn, settings):
    backend = Recorder()
    capped = settings.model_copy(update={"notify": settings.notify.model_copy(update={"max_per_hour": 2})})
    n, clock = notifier(conn, capped, [backend])
    for i in range(3):
        n.notify(f"m{i}", "trade")
        await n.deliver()
        clock.advance(60)
    assert len(backend.sent) == 2  # the third waits for the next hour
    clock.advance(3600)
    await n.deliver()
    assert len(backend.sent) == 3


async def test_retries_then_succeeds(conn, settings):
    fake = FakeOsascript(fail_times=2)
    n, clock = notifier(conn, settings, [IMessageBackend("+61412345678", runner=fake)])
    n.notify("hello", "test")
    for _ in range(3):
        await n.deliver()
        clock.advance(600)
    assert len(fake.calls) == 3
    assert statuses(conn) == ["sent"]


async def test_gives_up_after_retries_and_logs_it(conn, settings):
    n, clock = notifier(conn, settings, [Recorder(fail=True)])
    n.notify("hello", "test")
    for _ in range(settings.notify.retry_attempts + 2):
        await n.deliver()
        clock.advance(3600)
    assert statuses(conn) == ["failed"]
    event = conn.execute("SELECT level, message FROM events_log WHERE category = 'notify'").fetchone()
    assert event["level"] == "ERROR" and "Couldn't send a message" in event["message"]


async def test_falls_back_to_the_next_backend(conn, settings):
    fallback = Recorder("ntfy")
    n, _ = notifier(conn, settings, [Recorder("imessage", fail=True), fallback])
    n.notify("hello", "test")
    await n.deliver()
    assert len(fallback.sent) == 1
    assert conn.execute("SELECT backend FROM notifications").fetchone()[0] == "ntfy"


async def test_disabled_messages_are_recorded_but_not_sent(conn, settings):
    n, _ = notifier(conn, settings, [])
    n.notify("hello", "test")
    assert await n.deliver() == 0
    assert statuses(conn) == ["disabled"]


# ------------------------------------------------------------ message texts


def test_trade_opened_message_is_short_and_in_aud():
    text = trade_opened("ETH", "long", 0.08, 2001.0, 1900.0, 5.47, 160.08,
                        "ETH broke above its 3.3-day high of $1,980.00 on 2.1x normal volume, RSI 58", "RISK_ON",
                        USD_PER_AUD)
    assert text.startswith("🟢 BOUGHT 0.08 ETH at $2,001.00 (A$246.28)")
    assert "Why: ETH broke above" in text and "Market mood RISK_ON." in text
    assert "Stop $1,900.00 (5.0% below) · at risk A$8.42" in text
    assert len(text) < 400


def test_trade_closed_message_shows_result_in_aud():
    text = trade_closed("ETH", "long", 2000.0, 1890.0, 0.08, -9.3,
                        "Selling ETH: the stop loss at $1,900.00 was hit (price $1,890.00).", 2 * 86_400_000 + 3_600_000,
                        USD_PER_AUD)
    assert text.startswith("🔻 SOLD ETH: −A$14.31 (-5.8%) after fees")
    assert "held 2d 1h" in text
    assert "Why: the stop loss at $1,900.00 was hit" in text


def reading(regime, volatility="NORMAL", btc=60_000.0, slow_ma=70_000.0, breadth=30.0, score=-4):
    return SimpleNamespace(regime=Regime(regime), volatility=Volatility(volatility), btc_price=btc, slow_ma=slow_ma,
                           breadth_pct=breadth, score=score)


def test_mood_change_message_matches_the_example():
    text = mood_changed("RISK_ON", "NORMAL", reading("RISK_OFF"), 200)
    assert text == ("Market mood changed to RISK_OFF (was RISK_ON) — Bitcoin dropped below its 200-day average. "
                    "No new buys until it recovers.")


def test_no_message_when_nothing_changed_or_on_first_check():
    assert mood_changed("RISK_OFF", "NORMAL", reading("RISK_OFF"), 200) is None
    assert mood_changed(None, None, reading("RISK_OFF"), 200) is None


def test_wild_volatility_is_announced():
    assert "half size" in mood_changed("RISK_ON", "NORMAL", reading("RISK_ON", "WILD", 80_000.0, 70_000.0, 70, 5), 200)


def test_daily_summary():
    text = daily_summary("Wed 10 Jun", 670.0, 650.0, 660.0, 2, [("ETH", 3.25, 2.0)], "RISK_ON, volatility CALM",
                         1.2, 5.0, USD_PER_AUD)
    assert text.splitlines()[0] == "📊 Update (Wed 10 Jun)"
    assert "Balance A$1,030.77 (+3.08% since the start)" in text
    assert "Today +A$15.38 (+1.52%) · 2 trade(s)" in text
    assert "Open: ETH +A$5.00 (+2.0%)" in text
    assert "Since the start: Scout +3.1% vs holding BTC +5.0%" in text


def test_updates_at_8am_and_8pm_without_late_ones():
    from datetime import time as t
    from zoneinfo import ZoneInfo

    from scout.notify import due_update
    syd = ZoneInfo("Australia/Sydney")
    times = [t(8, 0), t(20, 0)]

    def at(text):
        return int(pd.Timestamp(text, tz=syd).timestamp() * 1000)

    assert due_update(at("2026-10-02 07:59"), syd, times, None) is None
    assert due_update(at("2026-10-02 08:00"), syd, times, None) == ("2026-10-02 08:00", True)
    assert due_update(at("2026-10-02 09:00"), syd, times, "2026-10-02 08:00") is None  # already sent
    assert due_update(at("2026-10-02 20:01"), syd, times, "2026-10-02 08:00") == ("2026-10-02 20:00", True)
    assert due_update(at("2026-10-02 14:00"), syd, times, "2026-10-01 20:00") == ("2026-10-02 08:00", False)  # asleep
    assert due_update(at("2026-10-02 10:30"), syd, times, None) == ("2026-10-02 08:00", True)  # woke 2.5h late
    assert due_update(at("2026-10-02 09:00"), syd, times, None, opened_ms=at("2026-10-02 08:30"))[1] is False
