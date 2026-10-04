"""`scout explain <trade_id>`: a plain-English walkthrough of one trade, from the records the bot kept.

Works on the demo account or a replay (both use the same tables). Read-only.
"""

from __future__ import annotations

import sqlite3
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

from scout.data import format_price
from scout.notify import duration


def list_trades(conn: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, coin, side, status, opened_ts_ms, closed_ts_ms, pnl_usd, qty, entry_price
           FROM demo_positions ORDER BY id DESC LIMIT ?""", (limit,)
    ).fetchall()


def _one(conn: sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def explain_trade(conn: sqlite3.Connection, trade_id: int, tz: ZoneInfo, usd_per_aud: float,
                  width: int = 100) -> str:
    p = _one(conn, "SELECT * FROM demo_positions WHERE id = ?", (trade_id,))
    if p is None:
        raise KeyError(f"there is no trade #{trade_id}")

    def when(ms: int | None) -> str:
        return "—" if ms is None else datetime.fromtimestamp(ms / 1000, tz).strftime("%a %d %b %Y %H:%M")

    def money(usd: float, signed: bool = False) -> str:
        value = usd / usd_per_aud
        sign = ("+" if value >= 0 else "−") if signed else ("−" if value < 0 else "")
        return f"{sign}A${abs(value):,.2f}"

    long = p["side"] == "long"
    closed = p["status"] == "closed"
    end = p["closed_ts_ms"]
    out: list[str] = []

    def para(text: str, indent: str = "   ") -> None:
        out.append(textwrap.fill(text, width, initial_indent=indent, subsequent_indent=indent))

    mode = (_one(conn, "SELECT value FROM bot_state WHERE key = 'mode'", ()) or {"value": "demo"})["value"]
    out.append(f"Trade #{trade_id} — {p['coin']}, {'long (bet on a rise)' if long else 'short (bet on a fall)'} "
               f"[{mode.upper()} account]")
    size = p["qty"] * p["entry_price"]
    if closed:
        out.append(f"Opened {when(p['opened_ts_ms'])} · closed {when(end)} (held {duration(end - p['opened_ts_ms'])}) · "
                   f"result {money(p['pnl_usd'], True)} ({p['pnl_usd'] / size * 100:+.1f}%)")
    else:
        out.append(f"Opened {when(p['opened_ts_ms'])} · still open")
    out.append("")

    # 1. The market
    mood = _one(conn, "SELECT * FROM regime_history WHERE ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1", (p["opened_ts_ms"],))
    out.append("1. The market mood when it opened")
    if mood:
        para(f"{mood['regime']}, volatility {mood['risk_level']} (checked {when(mood['ts_ms'])}). {mood['reason']}")
    else:
        para("No mood reading was recorded before this trade.")
    out.append("")

    # 2. The shortlist
    scan = _one(conn, "SELECT * FROM scan_results WHERE coin = ? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1",
                (p["coin"], p["opened_ts_ms"]))
    out.append(f"2. Why {p['coin']} was being watched")
    if scan and scan["passed"]:
        total = _one(conn, "SELECT COUNT(*) AS n FROM scan_results WHERE ts_ms = ? AND passed = 1", (scan["ts_ms"],))["n"]
        para(f"The scanner ranked it #{scan['rank']} of {total} coins that passed its checks "
             f"(score {scan['score']:+.1f}). {scan['reason'].split('). ', 1)[-1]}")
    elif scan:
        para(f"Scanner note: {scan['reason']}")
    else:
        para("No scan was recorded for this coin before the trade.")
    out.append("")

    # 3. The signal
    entry_action = "ENTER_LONG" if long else "ENTER_SHORT"
    signal = _one(conn, "SELECT * FROM signals WHERE coin = ? AND action = ? AND ts_ms <= ? ORDER BY acted DESC, "
                        "ts_ms DESC LIMIT 1", (p["coin"], entry_action, p["opened_ts_ms"]))
    out.append("3. The signal")
    para(signal["reason"] if signal else p["open_reason"])
    out.append("")

    # 4. The order
    orders = conn.execute(
        "SELECT * FROM demo_orders WHERE coin = ? AND status = 'filled' AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms",
        (p["coin"], p["opened_ts_ms"], end if end is not None else 2**62),
    ).fetchall()
    out.append("4. The order")
    if orders:
        o = orders[0]
        slip = abs(o["fill_price"] - o["price"]) * o["qty"]
        para(f"The risk manager checked the order first (account size, open positions, cash, today's losses, "
             f"the kill switch, a stop loss on the right side) and approved it. It filled {o['qty']:g} {p['coin']} at "
             f"${format_price(o['fill_price'])} against a market price of ${format_price(o['price'])}: slippage cost "
             f"{money(slip)} and the fee was {money(o['fee_usd'])}. Position size {money(size)}.")
    else:
        para(f"Filled {p['qty']:g} {p['coin']} at ${format_price(p['entry_price'])} (position size {money(size)}).")
    out.append("")

    # 5. While open
    moves = conn.execute(
        "SELECT ts_ms, stop_price FROM signals WHERE coin = ? AND action = 'MOVE_STOP' AND acted = 1 AND ts_ms >= ? "
        "AND ts_ms <= ? ORDER BY ts_ms", (p["coin"], p["opened_ts_ms"], end if end is not None else 2**62),
    ).fetchall()
    initial_stop = signal["stop_price"] if signal and signal["stop_price"] else p["stop_price"]
    out.append("5. While it was open")
    para(f"The stop loss started at ${format_price(initial_stop)} "
         f"({abs(p['entry_price'] - initial_stop) / p['entry_price'] * 100:.1f}% {'below' if long else 'above'} the entry).")
    if moves:
        para(f"As the price rose, the trailing stop was raised {len(moves)} time(s), from "
             f"${format_price(initial_stop)} to ${format_price(moves[-1]['stop_price'])}:")
        shown = list(moves) if len(moves) <= 5 else [*moves[:2], None, *moves[-2:]]
        for m in shown:
            if m is None:
                para(f"… {len(moves) - 4} more …", indent="     ")
                continue
            para(f"{when(m['ts_ms'])}: ${format_price(m['stop_price'])}", indent="   • ")
    if not moves:
        para("The stop never moved (the price didn't rise far enough for the trailing stop to follow).")
    if p["funding_usd"]:
        para(f"Funding (the fee for holding a perp) came to {money(p['funding_usd'])}.")
    out.append("")

    # 6. The end
    out.append("6. How it ended")
    if closed:
        para(p["close_reason"] or "Closed.")
        gross = p["qty"] * (p["exit_price"] - p["entry_price"]) * (1 if long else -1)
        funding = f", funding {money(-p['funding_usd'], True)}" if abs(p["funding_usd"]) >= 0.005 else ""
        para(f"Exit ${format_price(p['exit_price'])}. Price move {money(gross, True)}, fees {money(-p['fees_usd'], True)}"
             f"{funding} → result {money(p['pnl_usd'], True)} ({p['pnl_usd'] / size * 100:+.1f}% of the position).")
    else:
        last = p["last_price"] or p["entry_price"]
        unrealised = p["qty"] * (last - p["entry_price"]) * (1 if long else -1) - p["fees_usd"] - p["funding_usd"]
        para(f"Still open. Last price ${format_price(last)} ({when(p['last_price_ms'])}): {money(unrealised, True)} "
             f"so far after fees. Current stop ${format_price(p['stop_price'])}.")
    out.append("")

    # 7. The lesson
    out.append("7. In plain English")
    para(_lesson(p, signal, closed, money))
    return "\n".join(out)


def _lesson(p: sqlite3.Row, signal: sqlite3.Row | None, closed: bool, money) -> str:
    if not closed:
        return ("The trade is still running. The stop loss caps how much it can lose; if the price keeps "
                "rising, the trailing stop will follow it up and protect part of the gain.")
    reason = (p["close_reason"] or "").lower()
    planned = f" (planned risk was {money(signal['risk_usd'])})" if signal and signal["risk_usd"] else ""
    if p["pnl_usd"] < 0:
        if "stop loss" in reason:
            return (f"A loss of {money(-p['pnl_usd'])}{planned}, and that's the plan working: the stop loss sold "
                    "automatically at a price chosen before buying, so the loss stayed small. Most breakouts fail; "
                    "the strategy only works because the winners are bigger than these small, controlled losses.")
        if "trend broke" in reason or "risk_off" in reason:
            return (f"A loss of {money(-p['pnl_usd'])}{planned}, taken early: Scout sold before the stop was hit "
                    "because the reason for buying had gone. Getting out of a trade that isn't working usually costs "
                    "less than waiting for the stop.")
        if "kill switch" in reason:
            return "Closed by the kill switch, which sells everything when the account has fallen too far."
        return f"A loss of {money(-p['pnl_usd'])}{planned}."
    if "stop loss" in reason:
        return (f"A win of {money(p['pnl_usd'])}: the trailing stop followed the price up and sold once it turned "
                "down, locking in most of the gain. Letting winners run like this is where the strategy makes its money.")
    if "trend broke" in reason:
        return (f"A win of {money(p['pnl_usd'])}: Scout took the profit when the uptrend weakened, rather than "
                "waiting for the price to fall to the stop.")
    return f"A result of {money(p['pnl_usd'], True)}."
