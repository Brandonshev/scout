"""Tax export: every trade as CSV rows, in USD and AUD, ready for an accountant.

- Exchange rates: the Reserve Bank of Australia's daily "A$1 = US$" rate (table F11.1), stored in
  the fx_rates table. Weekends and public holidays use the latest earlier business day's rate.
- One row when a position opens and one when it closes. Each row is converted to AUD at the rate
  for its own Sydney date, so the AUD result of a trade is (AUD value at close) − (AUD value at
  open) − fees − funding. That's the usual way to convert foreign-currency trades, but ask your
  accountant how they want Hyperliquid perps treated.
- Australia's financial year runs 1 July to 30 June: FY2026 = 1 Jul 2025 – 30 Jun 2026.

Scout is not a tax adviser; this file is a record, not advice.
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from scout.version import APP_VERSION

SOURCE = "RBA F11.1"


# ------------------------------------------------------------------ rates


def parse_rba_csv(text: str) -> dict[str, float]:
    """RBA F11.1 CSV -> {"YYYY-MM-DD": US$ per A$1}."""
    rows = list(csv.reader(text.lstrip("﻿").splitlines()))
    header = next(i for i, row in enumerate(rows) if row and row[0] == "Series ID")
    column = rows[header].index("FXRUSD")
    rates = {}
    for row in rows[header + 1:]:
        if len(row) <= column or not row[column].strip():
            continue
        try:
            day = datetime.strptime(row[0], "%d-%b-%Y").date()
            rates[day.isoformat()] = float(row[column])
        except ValueError:
            continue  # notes or footers
    if not rates:
        raise ValueError("no AUD/USD rates found in the RBA file")
    return rates


def save_rates(conn: sqlite3.Connection, rates: dict[str, float], source: str = SOURCE) -> int:
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO fx_rates (date, usd_per_aud, source) VALUES (?, ?, ?) "
        "ON CONFLICT (date) DO UPDATE SET usd_per_aud = excluded.usd_per_aud, source = excluded.source",
        [(day, rate, source) for day, rate in rates.items()],
    )
    conn.commit()
    return conn.total_changes - before


async def refresh_rates(conn: sqlite3.Connection, url: str, timeout: float = 30.0) -> int:
    """Download the RBA table and store every daily rate. Returns how many rows were written."""
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": f"Scout/{APP_VERSION}"}) as http:
        response = await http.get(url)
        response.raise_for_status()
    return save_rates(conn, parse_rba_csv(response.text))


def rate_on(conn: sqlite3.Connection, day: str) -> tuple[float, str]:
    """(US$ per A$1, the date that rate is from) for `day`, falling back to the last earlier business day."""
    row = conn.execute("SELECT date, usd_per_aud FROM fx_rates WHERE date <= ? ORDER BY date DESC LIMIT 1",
                       (day,)).fetchone()
    if row is None:
        raise LookupError(f"no AUD/USD rate on or before {day}: run `scout tax-export` with internet access")
    return row[1], row[0]


def latest_rate(conn: sqlite3.Connection) -> tuple[float, str] | None:
    row = conn.execute("SELECT usd_per_aud, date FROM fx_rates ORDER BY date DESC LIMIT 1").fetchone()
    return None if row is None else (row[0], row[1])


# --------------------------------------------------------- financial year


def financial_year(day: date) -> int:
    """The Australian financial year a date falls in, named by the year it ends (FY2026 = Jul 2025–Jun 2026)."""
    return day.year + 1 if day.month >= 7 else day.year


def fy_bounds(fy: int, tz: ZoneInfo) -> tuple[int, int]:
    """[start, end) of a financial year in UTC milliseconds, using Sydney midnights."""
    start = datetime(fy - 1, 7, 1, tzinfo=tz)
    end = datetime(fy, 7, 1, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ------------------------------------------------------------------ export

COLUMNS = [
    "date_sydney", "time_sydney", "financial_year", "account", "trade_id", "coin", "action", "side", "quantity",
    "price_usd", "value_usd", "fee_usd", "funding_usd", "realised_pnl_usd", "usd_per_aud", "rate_date",
    "value_aud", "fee_aud", "funding_aud", "realised_pnl_aud", "note",
]


def _fees(conn: sqlite3.Connection, position: sqlite3.Row) -> tuple[float, float]:
    """(entry fee, exit fee) in US$. Uses the linked orders; older records split the total evenly."""
    rows = conn.execute("SELECT fee_usd FROM demo_orders WHERE position_id = ? AND status = 'filled' ORDER BY ts_ms",
                        (position["id"],)).fetchall()
    if rows:
        entry = rows[0]["fee_usd"]
        return entry, max(0.0, position["fees_usd"] - entry)
    half = position["fees_usd"] / (2 if position["status"] == "closed" else 1)
    return half, (position["fees_usd"] - half if position["status"] == "closed" else 0.0)


def tax_rows(conn: sqlite3.Connection, tz: ZoneInfo, start_ms: int, end_ms: int) -> list[dict]:
    """Every open and close in [start_ms, end_ms), oldest first."""
    account = (conn.execute("SELECT value FROM bot_state WHERE key = 'mode'").fetchone() or ["demo"])[0].upper()
    positions = conn.execute(
        """SELECT * FROM demo_positions
           WHERE (opened_ts_ms >= ? AND opened_ts_ms < ?) OR (closed_ts_ms >= ? AND closed_ts_ms < ?)
           ORDER BY opened_ts_ms""", (start_ms, end_ms, start_ms, end_ms),
    ).fetchall()
    rows: list[tuple[int, int, dict]] = []
    for p in positions:
        long = p["side"] == "long"
        entry_fee, exit_fee = _fees(conn, p)
        open_local = datetime.fromtimestamp(p["opened_ts_ms"] / 1000, tz)
        open_rate, open_rate_day = rate_on(conn, open_local.date().isoformat())
        open_value = p["qty"] * p["entry_price"]
        if start_ms <= p["opened_ts_ms"] < end_ms:
            rows.append((p["opened_ts_ms"], 0, {
                "date_sydney": open_local.date().isoformat(), "time_sydney": open_local.strftime("%H:%M:%S"),
                "financial_year": f"FY{financial_year(open_local.date())}", "account": account, "trade_id": p["id"],
                "coin": p["coin"], "action": "open", "side": "buy" if long else "sell", "quantity": p["qty"],
                "price_usd": p["entry_price"], "value_usd": open_value, "fee_usd": entry_fee, "funding_usd": 0.0,
                "realised_pnl_usd": "", "usd_per_aud": open_rate, "rate_date": open_rate_day,
                "value_aud": open_value / open_rate, "fee_aud": entry_fee / open_rate, "funding_aud": 0.0,
                "realised_pnl_aud": "", "note": f"opened {'long' if long else 'short'}",
            }))
        if p["status"] != "closed" or not (start_ms <= p["closed_ts_ms"] < end_ms):
            continue
        close_local = datetime.fromtimestamp(p["closed_ts_ms"] / 1000, tz)
        close_rate, close_rate_day = rate_on(conn, close_local.date().isoformat())
        close_value = p["qty"] * p["exit_price"]
        # AUD result: each side converted at its own date's rate, minus all costs in AUD
        gross_aud = (close_value / close_rate - open_value / open_rate) * (1 if long else -1)
        pnl_aud = gross_aud - entry_fee / open_rate - exit_fee / close_rate - p["funding_usd"] / close_rate
        rows.append((p["closed_ts_ms"], 1, {
            "date_sydney": close_local.date().isoformat(), "time_sydney": close_local.strftime("%H:%M:%S"),
            "financial_year": f"FY{financial_year(close_local.date())}", "account": account, "trade_id": p["id"],
            "coin": p["coin"], "action": "close", "side": "sell" if long else "buy", "quantity": p["qty"],
            "price_usd": p["exit_price"], "value_usd": close_value, "fee_usd": exit_fee,
            "funding_usd": p["funding_usd"], "realised_pnl_usd": p["pnl_usd"], "usd_per_aud": close_rate,
            "rate_date": close_rate_day, "value_aud": close_value / close_rate, "fee_aud": exit_fee / close_rate,
            "funding_aud": p["funding_usd"] / close_rate, "realised_pnl_aud": pnl_aud,
            "note": f"held since {open_local.date().isoformat()}",
        }))
    return [row for _, _, row in sorted(rows, key=lambda r: (r[0], r[1]))]


def write_tax_csv(rows: Sequence[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([_fmt(row[c]) for c in COLUMNS])
    return path


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".") if abs(value) < 1 else f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def summary(rows: Sequence[dict]) -> dict:
    closes = [r for r in rows if r["action"] == "close"]
    return {
        "opens": sum(r["action"] == "open" for r in rows),
        "closes": len(closes),
        "realised_usd": sum(r["realised_pnl_usd"] for r in closes),
        "realised_aud": sum(r["realised_pnl_aud"] for r in closes),
        "fees_aud": sum(r["fee_aud"] for r in rows),
    }


NOTES = """Scout tax export — notes for your accountant
=============================================

* One row when a position opens and one when it closes. trade_id links the two.
* Prices, values, fees and funding are in US dollars (Hyperliquid settles in USDC, a US-dollar
  stablecoin), converted to AUD with the Reserve Bank of Australia's daily rate (table F11.1,
  "A$1 = USD", 4pm Sydney). usd_per_aud is US$ per A$1; AUD = USD / usd_per_aud. Weekends and
  public holidays use the latest earlier business day's rate (rate_date shows which).
* realised_pnl_aud on a close row = AUD value at close − AUD value at open (each at its own date's
  rate) − entry and exit fees − funding, all in AUD. realised_pnl_usd is the same result in USD.
* funding is the hourly fee paid (or received) for holding a perpetual futures position.
* The positions are Hyperliquid perpetual futures (derivatives), not coins held in a wallet.
  How these are taxed (e.g. capital gains or ordinary income) depends on your circumstances.
* account = DEMO or REPLAY means fake-money trades: those are NOT real and NOT taxable.
* Times are Sydney local time (AEST/AEDT). Financial years run 1 July – 30 June (FY2026 = Jul 2025 – Jun 2026).

Generated by Scout v{version}. Scout is not a tax adviser; this is a record of trades, not advice.
"""


def write_notes(path: Path) -> Path:
    path.write_text(NOTES.format(version=APP_VERSION), encoding="utf-8")
    return path
