"""The weekly report: how Scout did against simply holding Bitcoin, and an honest verdict.

Built from what the demo loop records (equity snapshots with the BTC price, closed positions,
mood readings, risk events). Sent by iMessage on Sunday evening and saved as a text file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scout.version import APP_VERSION

DAY_MS = 86_400_000
MIN_TRADES_TO_JUDGE = 30


@dataclass(frozen=True)
class TradeLine:
    id: int
    coin: str
    pnl_usd: float
    pnl_pct: float
    close_reason: str


@dataclass(frozen=True)
class WeeklyReport:
    start_ms: int
    end_ms: int
    mode: str
    week_return_pct: float | None
    week_btc_pct: float | None
    total_return_pct: float | None
    total_btc_pct: float | None
    equity_usd: float | None
    initial_usd: float | None
    drawdown_pct: float
    btc_drawdown_pct: float
    trades: list[TradeLine]
    all_trades: int
    all_wins: int
    mood_share: dict[str, float]
    daily_limit_hits: int
    kill_switch_hits: int
    open_positions: int
    verdict: list[str] = field(default_factory=list)

    @property
    def best(self) -> TradeLine | None:
        return max(self.trades, key=lambda t: t.pnl_usd, default=None)

    @property
    def worst(self) -> TradeLine | None:
        return min(self.trades, key=lambda t: t.pnl_usd, default=None)

    @property
    def wins(self) -> int:
        return sum(t.pnl_usd > 0 for t in self.trades)


def _snapshot_at(conn: sqlite3.Connection, ts: int, before: bool = True) -> sqlite3.Row | None:
    if before:
        row = conn.execute("SELECT * FROM equity_snapshots WHERE ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1", (ts,)).fetchone()
        if row is not None:
            return row
    return conn.execute("SELECT * FROM equity_snapshots WHERE ts_ms >= ? ORDER BY ts_ms LIMIT 1", (ts,)).fetchone()


def _change(a: float | None, b: float | None) -> float | None:
    return (b / a - 1) * 100 if a and b else None


def _drawdown(values: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak:
            worst = max(worst, (peak - v) / peak * 100)
    return worst


def mood_share(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> dict[str, float]:
    """% of the time in each mood between start and end (each reading lasts until the next)."""
    rows = conn.execute(
        "SELECT ts_ms, regime FROM regime_history WHERE ts_ms < ? ORDER BY ts_ms", (end_ms,)
    ).fetchall()
    durations: dict[str, float] = {}
    for i, row in enumerate(rows):
        begin = max(row["ts_ms"], start_ms)
        finish = min(rows[i + 1]["ts_ms"] if i + 1 < len(rows) else end_ms, end_ms)
        if finish > begin:
            durations[row["regime"]] = durations.get(row["regime"], 0) + finish - begin
    total = sum(durations.values())
    return {mood: 100 * ms / total for mood, ms in durations.items()} if total else {}


def weekly_report(conn: sqlite3.Connection, end_ms: int, days: int = 7) -> WeeklyReport:
    start_ms = end_ms - days * DAY_MS
    state = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM bot_state")}
    first, last = _snapshot_at(conn, start_ms, before=True), _snapshot_at(conn, end_ms, before=True)
    initial = float(state["initial_equity_usd"]) if state.get("initial_equity_usd") else None
    btc_start = float(state["btc_start_price"]) if state.get("btc_start_price") else None
    snapshots = conn.execute("SELECT equity_usd, btc_price FROM equity_snapshots WHERE ts_ms <= ? ORDER BY ts_ms",
                             (end_ms,)).fetchall()
    closed = conn.execute(
        "SELECT id, coin, qty, entry_price, pnl_usd, close_reason FROM demo_positions WHERE status = 'closed' "
        "AND closed_ts_ms >= ? AND closed_ts_ms < ? ORDER BY closed_ts_ms", (start_ms, end_ms),
    ).fetchall()
    totals = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_usd > 0), 0) FROM demo_positions "
                          "WHERE status = 'closed' AND closed_ts_ms < ?", (end_ms,)).fetchone()

    def count(pattern: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM events_log WHERE category = 'risk' AND message LIKE ? "
                            "AND ts_ms >= ? AND ts_ms < ?", (pattern, start_ms, end_ms)).fetchone()[0]

    report = WeeklyReport(
        start_ms=start_ms, end_ms=end_ms, mode=(state.get("mode") or "demo").upper(),
        week_return_pct=_change(first["equity_usd"] if first else None, last["equity_usd"] if last else None),
        week_btc_pct=_change(first["btc_price"] if first else None, last["btc_price"] if last else None),
        total_return_pct=_change(initial, last["equity_usd"] if last else None),
        total_btc_pct=_change(btc_start, last["btc_price"] if last else None),
        equity_usd=last["equity_usd"] if last else None, initial_usd=initial,
        drawdown_pct=_drawdown([r["equity_usd"] for r in snapshots]),
        btc_drawdown_pct=_drawdown([r["btc_price"] for r in snapshots if r["btc_price"]]),
        trades=[TradeLine(r["id"], r["coin"], r["pnl_usd"], r["pnl_usd"] / (r["qty"] * r["entry_price"]) * 100,
                          r["close_reason"] or "") for r in closed],
        all_trades=totals[0], all_wins=totals[1],
        mood_share=mood_share(conn, start_ms, end_ms),
        daily_limit_hits=count("Daily loss limit hit%"), kill_switch_hits=count("KILL SWITCH%"),
        open_positions=conn.execute("SELECT COUNT(*) FROM demo_positions WHERE status = 'open'").fetchone()[0],
    )
    return replace(report, verdict=verdict(report))


def verdict(r: WeeklyReport) -> list[str]:
    """Plain, honest conclusions. The last line is the one-word answer."""
    lines = []
    if r.total_return_pct is not None and r.total_btc_pct is not None:
        gap = r.total_return_pct - r.total_btc_pct
        if gap < -2:
            lines.append(f"Since the start Scout is {abs(gap):.1f} points BEHIND simply holding Bitcoin "
                         f"({r.total_return_pct:+.1f}% vs {r.total_btc_pct:+.1f}%).")
        elif gap > 2:
            lines.append(f"Since the start Scout is {gap:.1f} points AHEAD of simply holding Bitcoin "
                         f"({r.total_return_pct:+.1f}% vs {r.total_btc_pct:+.1f}%).")
        else:
            lines.append(f"Since the start Scout is about level with holding Bitcoin "
                         f"({r.total_return_pct:+.1f}% vs {r.total_btc_pct:+.1f}%).")
    lines.append(f"Worst fall so far: Scout {r.drawdown_pct:.1f}% vs Bitcoin {r.btc_drawdown_pct:.1f}%.")
    off = r.mood_share.get("RISK_OFF", 0)
    if off >= 60:
        lines.append(f"The market was RISK_OFF {off:.0f}% of the week, so Scout mostly sat in cash. That's by design.")

    if r.all_trades < MIN_TRADES_TO_JUDGE:
        lines.append(f"Verdict: TOO EARLY TO TELL. Only {r.all_trades} trade(s) so far; a fair judgement needs "
                     f"at least {MIN_TRADES_TO_JUDGE}, over several weeks and different market moods.")
        return lines
    ahead = (r.total_return_pct or 0) > (r.total_btc_pct or 0)
    safer = r.drawdown_pct < r.btc_drawdown_pct
    if ahead and safer:
        lines.append("Verdict: WORKING SO FAR: ahead of Bitcoin with smaller falls. Keep watching; results can change.")
    elif ahead:
        lines.append("Verdict: MIXED: ahead of Bitcoin, but with falls as big as Bitcoin's.")
    elif safer:
        lines.append("Verdict: MIXED: safer than holding Bitcoin, but it has made less. Holding Bitcoin would have "
                     "earned more; Scout's case rests on avoiding big crashes.")
    else:
        lines.append("Verdict: NOT WORKING: holding Bitcoin did better on both return and risk. Don't move to real "
                     "money; rethink the strategy.")
    return lines


def _aud(usd: float | None, usd_per_aud: float, signed: bool = False) -> str:
    if usd is None:
        return "—"
    value = usd / usd_per_aud
    sign = ("+" if value >= 0 else "−") if signed else ("−" if value < 0 else "")
    return f"{sign}A${abs(value):,.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f}%"


def _moods(share: dict[str, float]) -> str:
    order = ("RISK_ON", "NEUTRAL", "RISK_OFF")
    return ", ".join(f"{m} {share[m]:.0f}%" for m in order if m in share) or "no readings"


def report_message(r: WeeklyReport, usd_per_aud: float, tz: ZoneInfo) -> str:
    """The short iMessage version."""
    end = datetime.fromtimestamp(r.end_ms / 1000, tz)
    lines = [
        f"🗓️ Weekly report (week to {end:%a %d %b})",
        f"This week: Scout {_pct(r.week_return_pct)} vs BTC {_pct(r.week_btc_pct)}",
        f"Since start: Scout {_pct(r.total_return_pct)} vs BTC {_pct(r.total_btc_pct)} · "
        f"balance {_aud(r.equity_usd, usd_per_aud)}",
        f"Trades: {len(r.trades)} closed ({r.wins} won)" + (
            f" · best {r.best.coin} {_aud(r.best.pnl_usd, usd_per_aud, True)}, worst {r.worst.coin} "
            f"{_aud(r.worst.pnl_usd, usd_per_aud, True)}" if r.trades else ""),
        f"Mood: {_moods(r.mood_share)}",
    ]
    if r.daily_limit_hits or r.kill_switch_hits:
        lines.append(f"Risk: daily limit hit {r.daily_limit_hits}×, kill switch {r.kill_switch_hits}×")
    lines.append(r.verdict[-1] if r.verdict else "")
    return "\n".join(lines)


def report_text(r: WeeklyReport, usd_per_aud: float, tz: ZoneInfo) -> str:
    """The full version saved to a file."""
    start = datetime.fromtimestamp(r.start_ms / 1000, tz)
    end = datetime.fromtimestamp(r.end_ms / 1000, tz)
    out = [
        f"Scout weekly report ({r.mode}) — {start:%a %d %b %Y} to {end:%a %d %b %Y}",
        f"Generated by Scout v{APP_VERSION}",
        "",
        "PERFORMANCE",
        f"  This week:        Scout {_pct(r.week_return_pct)}   vs holding BTC {_pct(r.week_btc_pct)}",
        f"  Since the start:  Scout {_pct(r.total_return_pct)}   vs holding BTC {_pct(r.total_btc_pct)}",
        f"  Balance:          {_aud(r.equity_usd, usd_per_aud)} (started {_aud(r.initial_usd, usd_per_aud)})",
        f"  Worst fall:       Scout {r.drawdown_pct:.1f}%   vs BTC {r.btc_drawdown_pct:.1f}%",
        f"  Open positions:   {r.open_positions}",
        "",
        f"TRADES CLOSED THIS WEEK: {len(r.trades)} ({r.wins} winners) — {r.all_trades} in total, "
        f"{r.all_wins} winners",
    ]
    for label, trade in (("Best", r.best), ("Worst", r.worst)):
        if trade:
            out.append(f"  {label}: #{trade.id} {trade.coin} {_aud(trade.pnl_usd, usd_per_aud, True)} "
                       f"({trade.pnl_pct:+.1f}%) — {trade.close_reason}")
    out += ["", "MARKET MOOD THIS WEEK (share of the time)"]
    out += [f"  {m:<9} {v:5.1f}%" for m, v in sorted(r.mood_share.items(), key=lambda kv: -kv[1])] or ["  no readings"]
    out += ["", "RISK EVENTS", f"  Daily loss limit hit: {r.daily_limit_hits}", f"  Kill switch: {r.kill_switch_hits}",
            "", "VERDICT"]
    out += [f"  {line}" for line in r.verdict]
    out += ["", "For the story behind any trade: uv run scout explain <id>"]
    return "\n".join(out) + "\n"


def save_report(r: WeeklyReport, folder: Path, usd_per_aud: float, tz: ZoneInfo) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    end = datetime.fromtimestamp(r.end_ms / 1000, tz)
    path = folder / f"weekly_{end:%Y-%m-%d}.txt"
    path.write_text(report_text(r, usd_per_aud, tz), encoding="utf-8")
    return path
