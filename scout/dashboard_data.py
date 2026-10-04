"""Everything the dashboard shows, read from SQLite through a READ-ONLY connection.

The connection is opened with SQLite's mode=ro, so the database itself refuses any write:
the dashboard can't place, change or close a trade even by accident.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path} yet")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _state(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM bot_state")}


def _float(state: dict, key: str) -> float | None:
    value = state.get(key)
    return None if value in (None, "") else float(value)


def _times(ms: pd.Series, tz: str) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert(tz)


def overview(conn: sqlite3.Connection, usd_per_aud: float) -> dict:
    state = _state(conn)
    snap = conn.execute("SELECT * FROM equity_snapshots ORDER BY ts_ms DESC LIMIT 1").fetchone()
    written = conn.execute("SELECT app_version FROM events_log ORDER BY id DESC LIMIT 1").fetchone()
    equity = snap["equity_usd"] if snap else _float(state, "cash_usd")
    initial = _float(state, "initial_equity_usd") or equity
    day_start = _float(state, "day_start_equity_usd") or equity
    btc_start, btc_now = _float(state, "btc_start_price"), (snap["btc_price"] if snap else None)
    return {
        "mode": (state.get("mode") or (snap["mode"] if snap else "demo")).upper(),
        "state": state.get("state", "RUNNING"),
        "written_by": written["app_version"] if written else None,
        "as_of_ms": snap["ts_ms"] if snap else None,
        "equity_aud": equity / usd_per_aud if equity is not None else None,
        "initial_aud": initial / usd_per_aud if initial else None,
        "today_pct": (equity / day_start - 1) * 100 if equity and day_start else None,
        "today_aud": (equity - day_start) / usd_per_aud if equity and day_start else None,
        "total_pct": (equity / initial - 1) * 100 if equity and initial else None,
        "btc_pct": (btc_now / btc_start - 1) * 100 if btc_now and btc_start else None,
        "daily_limit_hit": state.get("daily_limit_hit") == "1",
        "killed_reason": state.get("killed_reason"),
        "replay": {
            "from_ms": _float(state, "replay_from_ms"), "to_ms": _float(state, "replay_to_ms"),
            "now_ms": _float(state, "sim_now_ms"), "status": state.get("replay_status"),
            "speed": state.get("replay_speed"), "paused": state.get("replay_paused") == "1",
        } if state.get("replay_from_ms") else None,
    }


def latest_mood(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM regime_history ORDER BY ts_ms DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    details = json.loads(row["details_json"] or "{}")
    return {"ts_ms": row["ts_ms"], "regime": row["regime"], "volatility": row["risk_level"],
            "summary": row["reason"], "reasons": details.get("reasons", []), "score": row["score"]}


def mood_history(conn: sqlite3.Connection, tz: str) -> pd.DataFrame:
    rows = conn.execute("SELECT ts_ms, regime, risk_level, details_json FROM regime_history ORDER BY ts_ms").fetchall()
    frame = pd.DataFrame(
        [{"ts_ms": r["ts_ms"], "mood": r["regime"], "volatility": r["risk_level"],
          "btc_price": json.loads(r["details_json"] or "{}").get("btc_price")} for r in rows],
        columns=["ts_ms", "mood", "volatility", "btc_price"],
    )
    frame["time"] = _times(frame["ts_ms"], tz)
    frame["until"] = frame["time"].shift(-1).fillna(frame["time"] + pd.Timedelta(hours=1))
    return frame


def shortlist(conn: sqlite3.Connection, max_coins: int, min_score: float = float("-inf")) -> tuple[int | None, pd.DataFrame]:
    latest = conn.execute("SELECT MAX(ts_ms) FROM scan_results").fetchone()[0]
    columns = ["rank", "coin", "score", "note", "shortlisted"]
    if latest is None:
        return None, pd.DataFrame(columns=columns)
    rows = conn.execute("SELECT rank, coin, score, reason, passed FROM scan_results WHERE ts_ms = ? "
                        "ORDER BY rank IS NULL, rank", (latest,)).fetchall()
    frame = pd.DataFrame([{
        "rank": r["rank"], "coin": r["coin"], "score": r["score"],
        "note": r["reason"].split("). ", 1)[-1] if r["passed"] else r["reason"],
        "shortlisted": bool(r["passed"] and r["rank"] and r["rank"] <= max_coins and (r["score"] or 0) >= min_score),
    } for r in rows], columns=columns)
    return latest, frame


def open_positions(conn: sqlite3.Connection, usd_per_aud: float, tz: str) -> pd.DataFrame:
    rows = conn.execute("SELECT * FROM demo_positions WHERE status = 'open' ORDER BY opened_ts_ms").fetchall()
    records = []
    for p in rows:
        price = p["last_price"] or p["entry_price"]
        sign = 1 if p["side"] == "long" else -1
        pnl = p["qty"] * (price - p["entry_price"]) * sign - p["fees_usd"] - p["funding_usd"]
        records.append({
            "id": p["id"], "coin": p["coin"], "side": p["side"], "qty": p["qty"], "entry": p["entry_price"],
            "now": price, "stop": p["stop_price"], "pnl_aud": pnl / usd_per_aud,
            "pnl_pct": pnl / (p["qty"] * p["entry_price"]) * 100, "opened_ms": p["opened_ts_ms"],
            "why": p["open_reason"],
        })
    frame = pd.DataFrame(records, columns=["id", "coin", "side", "qty", "entry", "now", "stop", "pnl_aud", "pnl_pct",
                                           "opened_ms", "why"])
    frame["opened"] = _times(frame["opened_ms"], tz)
    return frame


def trade_history(conn: sqlite3.Connection, usd_per_aud: float, tz: str) -> pd.DataFrame:
    rows = conn.execute("SELECT * FROM demo_positions WHERE status = 'closed' ORDER BY closed_ts_ms DESC").fetchall()
    frame = pd.DataFrame([{
        "id": p["id"], "coin": p["coin"], "side": p["side"], "opened_ms": p["opened_ts_ms"],
        "closed_ms": p["closed_ts_ms"], "entry": p["entry_price"], "exit": p["exit_price"],
        "pnl_aud": p["pnl_usd"] / usd_per_aud, "pnl_pct": p["pnl_usd"] / (p["qty"] * p["entry_price"]) * 100,
        "why_opened": p["open_reason"], "why_closed": p["close_reason"],
    } for p in rows], columns=["id", "coin", "side", "opened_ms", "closed_ms", "entry", "exit", "pnl_aud", "pnl_pct",
                               "why_opened", "why_closed"])
    frame["opened"] = _times(frame["opened_ms"], tz)
    frame["closed"] = _times(frame["closed_ms"], tz)
    return frame


def equity_curve(conn: sqlite3.Connection, usd_per_aud: float, tz: str) -> pd.DataFrame:
    rows = conn.execute("SELECT ts_ms, equity_usd, btc_price FROM equity_snapshots ORDER BY ts_ms").fetchall()
    frame = pd.DataFrame([dict(r) for r in rows], columns=["ts_ms", "equity_usd", "btc_price"])
    frame["time"] = _times(frame["ts_ms"], tz)
    frame["Scout"] = frame["equity_usd"] / usd_per_aud
    first = frame["btc_price"].dropna()
    if len(first) and len(frame):
        start_equity = frame["Scout"].iloc[0]
        frame["Just holding BTC"] = start_equity * frame["btc_price"] / first.iloc[0]
    else:
        frame["Just holding BTC"] = float("nan")
    return frame


def messages(conn: sqlite3.Connection, tz: str, limit: int = 15) -> pd.DataFrame:
    rows = conn.execute("SELECT created_ms, category, status, text FROM notifications ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    frame = pd.DataFrame([dict(r) for r in rows], columns=["created_ms", "category", "status", "text"])
    frame["time"] = _times(frame["created_ms"], tz)
    return frame


def risk_events(conn: sqlite3.Connection, tz: str, limit: int = 10) -> pd.DataFrame:
    rows = conn.execute("SELECT ts_ms, level, message FROM events_log WHERE category IN ('risk', 'control') "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    frame = pd.DataFrame([dict(r) for r in rows], columns=["ts_ms", "level", "message"])
    frame["time"] = _times(frame["ts_ms"], tz)
    return frame


# Every term shown on the dashboard, in plain English.
GLOSSARY: list[tuple[str, str]] = [
    ("DEMO / REPLAY / LIVE", "DEMO trades fake money on real live prices. REPLAY runs the same bot over past "
     "prices at high speed so you can watch what it would have done. LIVE would be real money (not built)."),
    ("Bot state: RUNNING / PAUSED / KILLED", "RUNNING trades normally. PAUSED opens nothing new but still manages "
     "open positions. KILLED means the kill switch went off: everything was sold, and nothing happens until you "
     "reset it by hand."),
    ("Balance", "What the account is worth right now: cash plus the current value of open positions, in AUD."),
    ("P&L (profit and loss)", "How much money was made (+) or lost (−). 'Today' counts from midnight Sydney time; "
     "'total' counts from when the account started."),
    ("vs holding BTC", "What the same starting money would be worth if you had simply bought Bitcoin and done "
     "nothing. If Scout can't beat this after costs, it isn't worth running."),
    ("Market mood (regime)", "Scout's read of the whole crypto market. RISK_ON = uptrend, new buys allowed. "
     "NEUTRAL = no clear direction, buys allowed with care. RISK_OFF = weak or falling, no new buys."),
    ("Volatility: CALM / NORMAL / WILD", "How much Bitcoin's price is swinging compared with the past year. "
     "When it's WILD, Scout halves the size of new positions."),
    ("Moving average", "The average closing price over the last N days (e.g. 50 or 200). Price above it = buyers "
     "have been in control over that period."),
    ("Breadth", "The % of the biggest coins trading above their own 50-day average. High breadth means the whole "
     "market is rising, not just a few coins."),
    ("Score", "The mood score adds up points from trend, breadth and crowding. The coin score adds up points for "
     "beating Bitcoin, rising volume, an uptrend, and not being too jumpy."),
    ("Shortlist", "The best-scoring coins from the scanner: the only coins Scout is allowed to buy."),
    ("Relative strength (vs BTC)", "A coin's % change minus Bitcoin's over the same week or month. Positive = the "
     "coin is doing better than Bitcoin."),
    ("Volume / volume surge", "How much of a coin was traded. '2.0x normal' means twice its usual daily trading, "
     "a sign people are paying attention."),
    ("Spread and depth", "The spread is the gap between the best buying and selling prices; depth is how much money "
     "is waiting to trade near the price. Tight spreads and deep books mean you get fair prices."),
    ("Breakout", "When a price closes above its recent high (the highest point of the last 20 four-hour candles). "
     "Scout buys breakouts that come with extra volume."),
    ("RSI", "A 0–100 gauge of how one-sided recent moves were. Above ~75, a coin has risen so fast it often pulls "
     "back, so Scout skips it."),
    ("ATR (average true range)", "The typical size of a price swing per candle. Scout uses it to place stops "
     "outside normal noise."),
    ("Stop loss", "A price, set before buying, where Scout sells automatically to cap the loss."),
    ("Trailing stop", "A stop that moves up as the price rises (never down), locking in part of the gain."),
    ("Entry / Now / Stop", "The price Scout bought at, the latest price, and where it will sell if things go wrong."),
    ("Position size / risk per trade", "How much to buy is worked out so that if the stop is hit, the account loses "
     "about 1% (A$10 of A$1,000), including fees."),
    ("Long / short", "Long = bet the price rises (buy first). Short = bet it falls (sell first). Scout is long-only "
     "by default."),
    ("Fees, slippage, funding", "Costs of trading: the exchange's fee per trade, paying a little more (or getting a "
     "little less) than the screen price, and the hourly fee for holding a perp position."),
    ("Daily loss limit", "If the account falls 3% in one day, Scout opens nothing new until midnight Sydney time."),
    ("Kill switch", "If the account falls 15% from its highest point, Scout sells everything and stops until you "
     "reset it by hand."),
    ("Drawdown", "How far the account is below its highest value so far."),
    ("Equity curve", "A chart of the account's value over time, here drawn next to simply holding Bitcoin."),
    ("Replay speed", "How much faster than real time a replay runs. 500x means one real second shows about eight "
     "minutes of market time."),
    ("Messages", "The iMessages Scout sent (or, in a replay or with iMessage off, would have sent)."),
]
