"""Weekly report and tax export, on a small hand-made history with known numbers."""

import csv
from contextlib import closing
from datetime import date, datetime

import pytest

from scout.db import open_db
from scout.reports import report_message, report_text, save_report, verdict, weekly_report
from scout.tax import (
    COLUMNS,
    financial_year,
    fy_bounds,
    parse_rba_csv,
    rate_on,
    save_rates,
    summary,
    tax_rows,
    write_notes,
    write_tax_csv,
)

DAY = 86_400_000
USD_PER_AUD = 0.65


def ms(text: str, tz) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=tz).timestamp() * 1000)


def add_position(conn, *, id, coin, side, qty, entry, exit_price, opened, closed, pnl, fees, funding=0.0,
                 entry_fee=None, reason="Selling: test."):
    conn.execute(
        """INSERT INTO demo_positions (id, coin, side, status, opened_ts_ms, closed_ts_ms, qty, entry_price,
               stop_price, exit_price, fees_usd, funding_usd, pnl_usd, open_reason, close_reason, app_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'Buying: test.', ?, 'test')""",
        (id, coin, side, "closed" if closed else "open", opened, closed, qty, entry, exit_price, fees, funding,
         pnl, reason if closed else None),
    )
    if entry_fee is not None:
        conn.execute("INSERT INTO demo_orders (ts_ms, coin, side, order_type, qty, price, fill_price, fee_usd, "
                     "status, reason, position_id, app_version) VALUES (?, ?, 'buy', 'market', ?, ?, ?, ?, "
                     "'filled', 'x', ?, 'test')", (opened, coin, qty, entry, entry, entry_fee, id))


@pytest.fixture
def conn(settings):
    with closing(open_db(settings.app.db_path)) as connection:
        yield connection


# ------------------------------------------------------------------- RBA rates

RBA_SAMPLE = """﻿F11.1  EXCHANGE RATES
Title,A$1=USD,Trade-weighted Index May 1970 = 100
Units,USD,Index
Series ID,FXRUSD,FXRTWI
03-Jan-2025,0.6216,60.1
06-Jan-2025,0.6250,60.3
07-Jan-2025,,60.2
Notes,see website,
"""


def test_parse_rba_csv():
    assert parse_rba_csv(RBA_SAMPLE) == {"2025-01-03": 0.6216, "2025-01-06": 0.6250}


def test_weekend_uses_the_last_business_day(conn):
    save_rates(conn, parse_rba_csv(RBA_SAMPLE))
    assert rate_on(conn, "2025-01-04") == (0.6216, "2025-01-03")  # Saturday -> Friday
    assert rate_on(conn, "2025-01-06") == (0.6250, "2025-01-06")
    with pytest.raises(LookupError):
        rate_on(conn, "2024-12-31")


def test_financial_years():
    assert financial_year(date(2025, 6, 30)) == 2025
    assert financial_year(date(2025, 7, 1)) == 2026


def test_fy_bounds_use_sydney_midnight(settings):
    tz = settings.app.tz
    start, end = fy_bounds(2026, tz)
    assert datetime.fromtimestamp(start / 1000, tz).isoformat() == "2025-07-01T00:00:00+10:00"
    assert datetime.fromtimestamp(end / 1000, tz).isoformat() == "2026-07-01T00:00:00+10:00"


# ------------------------------------------------------------------ tax export


def test_tax_rows_convert_each_side_at_its_own_rate(conn, settings):
    tz = settings.app.tz
    save_rates(conn, {"2025-08-01": 0.65, "2025-08-10": 0.60})
    conn.execute("INSERT INTO bot_state (key, value, updated_ms) VALUES ('mode', 'demo', 0)")
    # Bought 1 ETH at $2,000, sold at $2,100: +$100 gross, $2 fees (1.0 in, 1.0 out), $0.5 funding
    add_position(conn, id=1, coin="ETH", side="long", qty=1.0, entry=2000.0, exit_price=2100.0,
                 opened=ms("2025-08-01T10:00", tz), closed=ms("2025-08-10T15:30", tz), pnl=97.5, fees=2.0,
                 funding=0.5, entry_fee=1.0)
    start, end = fy_bounds(2026, tz)
    rows = tax_rows(conn, tz, start, end)
    opened, closed = rows
    assert (opened["action"], opened["side"], opened["date_sydney"], opened["time_sydney"]) == \
        ("open", "buy", "2025-08-01", "10:00:00")
    assert opened["value_aud"] == pytest.approx(2000 / 0.65)
    assert opened["fee_usd"] == 1.0 and closed["fee_usd"] == 1.0
    assert closed["realised_pnl_usd"] == 97.5
    expected_aud = 2100 / 0.60 - 2000 / 0.65 - 1.0 / 0.65 - 1.0 / 0.60 - 0.5 / 0.60
    assert closed["realised_pnl_aud"] == pytest.approx(expected_aud)
    assert closed["financial_year"] == "FY2026" and closed["account"] == "DEMO"


def test_short_trades_and_financial_year_filter(conn, settings):
    tz = settings.app.tz
    save_rates(conn, {"2025-06-01": 0.65})
    add_position(conn, id=1, coin="SOL", side="short", qty=2.0, entry=100.0, exit_price=90.0,
                 opened=ms("2025-06-20T09:00", tz), closed=ms("2025-07-02T09:00", tz), pnl=19.8, fees=0.2)
    fy25 = tax_rows(conn, tz, *fy_bounds(2025, tz))
    fy26 = tax_rows(conn, tz, *fy_bounds(2026, tz))
    assert [r["action"] for r in fy25] == ["open"] and fy25[0]["side"] == "sell"
    assert [r["action"] for r in fy26] == ["close"] and fy26[0]["side"] == "buy"
    assert fy26[0]["realised_pnl_aud"] == pytest.approx((200 / 0.65 - 180 / 0.65) - 0.1 / 0.65 - 0.1 / 0.65)


def test_tax_csv_file(conn, settings, tmp_path):
    tz = settings.app.tz
    save_rates(conn, {"2025-08-01": 0.65})
    add_position(conn, id=1, coin="ETH", side="long", qty=0.5, entry=2000.0, exit_price=1900.0,
                 opened=ms("2025-08-01T10:00", tz), closed=ms("2025-08-02T10:00", tz), pnl=-51.0, fees=1.0)
    add_position(conn, id=2, coin="BTC", side="long", qty=0.01, entry=80000.0, exit_price=None,
                 opened=ms("2025-08-03T10:00", tz), closed=None, pnl=None, fees=0.4)
    rows = tax_rows(conn, tz, *fy_bounds(2026, tz))
    path = write_tax_csv(rows, tmp_path / "tax.csv")
    with path.open() as handle:
        records = list(csv.DictReader(handle))
    assert list(records[0]) == COLUMNS
    assert [(r["trade_id"], r["action"]) for r in records] == [("1", "open"), ("1", "close"), ("2", "open")]
    assert float(records[1]["realised_pnl_usd"]) == -51.0
    assert records[0]["realised_pnl_aud"] == ""  # only closes realise a result
    totals = summary(rows)
    assert totals["closes"] == 1 and totals["realised_usd"] == -51.0
    notes = write_notes(tmp_path / "README.txt").read_text()
    assert "not a tax adviser" in notes and "NOT taxable" in notes


def test_missing_rate_is_an_error_not_a_guess(conn, settings):
    tz = settings.app.tz
    add_position(conn, id=1, coin="ETH", side="long", qty=1.0, entry=2000.0, exit_price=2100.0,
                 opened=ms("2025-08-01T10:00", tz), closed=ms("2025-08-02T10:00", tz), pnl=98.0, fees=2.0)
    with pytest.raises(LookupError):
        tax_rows(conn, tz, *fy_bounds(2026, tz))


# ---------------------------------------------------------------- weekly report


def snapshot(conn, ts, equity, btc):
    conn.execute("INSERT INTO equity_snapshots (ts_ms, equity_usd, cash_usd, positions_value_usd, btc_price, mode, "
                 "app_version) VALUES (?, ?, ?, 0, ?, 'demo', 'test')", (ts, equity, equity, btc))


def build_week(conn, tz, *, trades=3, scout_end=670.0, btc_end=88_000.0):
    end = ms("2026-06-14T19:00", tz)  # a Sunday evening
    start = end - 7 * DAY
    for key, value in {"mode": "demo", "initial_equity_usd": 650, "btc_start_price": 80_000}.items():
        conn.execute("INSERT INTO bot_state (key, value, updated_ms) VALUES (?, ?, 0)", (key, str(value)))
    snapshot(conn, start - DAY, 650.0, 80_000)
    snapshot(conn, start, 660.0, 84_000)
    snapshot(conn, start + 3 * DAY, 600.0, 70_000)  # a dip
    snapshot(conn, end - 1, scout_end, btc_end)
    results = [12.0, -6.0, 3.0, -2.0, 5.0][:trades]
    for i, pnl in enumerate(results, start=1):
        add_position(conn, id=i, coin=f"C{i}", side="long", qty=1.0, entry=100.0, exit_price=100 + pnl,
                     opened=start + i * DAY - 3_600_000, closed=start + i * DAY, pnl=pnl, fees=0.1,
                     reason=f"Selling C{i}: test.")
    conn.execute("INSERT INTO regime_history (ts_ms, regime, risk_level, score, reason, app_version) "
                 "VALUES (?, 'RISK_ON', 'NORMAL', 4, 'x', 't')", (start - DAY,))
    conn.execute("INSERT INTO regime_history (ts_ms, regime, risk_level, score, reason, app_version) "
                 "VALUES (?, 'RISK_OFF', 'NORMAL', -4, 'x', 't')", (start + int(5.25 * DAY),))
    conn.execute("INSERT INTO events_log (ts_ms, level, category, message, app_version) VALUES "
                 "(?, 'WARNING', 'risk', 'Daily loss limit hit: down 3.1%', 't')", (start + 3 * DAY,))
    conn.commit()
    return end


def test_weekly_report_numbers(conn, settings):
    tz = settings.app.tz
    end = build_week(conn, tz)
    r = weekly_report(conn, end)
    assert r.week_return_pct == pytest.approx((670 / 660 - 1) * 100)
    assert r.week_btc_pct == pytest.approx((88_000 / 84_000 - 1) * 100)
    assert r.total_return_pct == pytest.approx((670 / 650 - 1) * 100)
    assert r.total_btc_pct == pytest.approx(10.0)
    assert (r.best.coin, r.worst.coin) == ("C1", "C2")
    assert r.wins == 2 and len(r.trades) == 3
    assert r.mood_share == pytest.approx({"RISK_ON": 75.0, "RISK_OFF": 25.0})
    assert r.daily_limit_hits == 1 and r.kill_switch_hits == 0
    assert r.drawdown_pct == pytest.approx((660 - 600) / 660 * 100)
    assert r.btc_drawdown_pct == pytest.approx((84_000 - 70_000) / 84_000 * 100)


def test_weekly_report_verdict_is_honest(conn, settings):
    tz = settings.app.tz
    r = weekly_report(conn, build_week(conn, tz))
    assert any("BEHIND simply holding Bitcoin" in line for line in r.verdict)  # +3.1% vs +10%
    assert r.verdict[-1].startswith("Verdict: TOO EARLY TO TELL")


@pytest.mark.parametrize(
    ("total", "btc", "dd", "btc_dd", "expected"),
    [
        (20.0, 10.0, 5.0, 30.0, "WORKING SO FAR"),
        (20.0, 10.0, 40.0, 30.0, "MIXED: ahead"),
        (5.0, 10.0, 5.0, 30.0, "MIXED: safer"),
        (-5.0, 10.0, 35.0, 30.0, "NOT WORKING"),
    ],
)
def test_verdict_once_there_are_enough_trades(conn, settings, total, btc, dd, btc_dd, expected):
    from dataclasses import replace

    r = weekly_report(conn, build_week(conn, settings.app.tz))
    r = replace(r, all_trades=40, total_return_pct=total, total_btc_pct=btc, drawdown_pct=dd, btc_drawdown_pct=btc_dd)
    assert verdict(r)[-1].startswith(f"Verdict: {expected}")


def test_weekly_message_and_file(conn, settings, tmp_path):
    tz = settings.app.tz
    r = weekly_report(conn, build_week(conn, tz))
    message = report_message(r, USD_PER_AUD, tz)
    assert message.startswith("🗓️ Weekly report (week to Sun 14 Jun)")
    assert "best C1 +A$18.46, worst C2 −A$9.23" in message
    assert "Mood: RISK_ON 75%, RISK_OFF 25%" in message
    assert "daily limit hit 1×" in message
    assert len(message) <= settings.notify.max_length
    text = report_text(r, USD_PER_AUD, tz)
    assert "VERDICT" in text and "MARKET MOOD THIS WEEK" in text
    path = save_report(r, tmp_path, USD_PER_AUD, tz)
    assert path.name == "weekly_2026-06-14.txt" and path.read_text() == text
