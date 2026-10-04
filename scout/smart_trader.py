"""SMART:3 live: runs the strategy in scout/smart.py on live prices in its own fake account.

Once a day, just after the daily candle closes (00:10 UTC = 10:10/11:10am Sydney), it downloads the last
~4 months of daily candles for the most-traded coins, works out the plan, and trades the difference:
sells what's no longer wanted, buys/shorts what's new. Positions keep their size in between. The usual
engine handles prices, emergency stops, funding, the kill switch, alerts and sleep.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout import smart
from scout.config import Settings
from scout.data import DAY_MS, HyperliquidClient, format_price
from scout.demo import DemoEngine, Job
from scout.notify import Priority, aud, due_update

log = logging.getLogger(__name__)
TAG = "[SMART]"
STRATEGY = "smart"
HISTORY_DAYS = 130  # enough for the 60-day measures, the 50-day average and the 30-day volume ranking

ClientFactory = Callable[[Settings], AbstractAsyncContextManager[HyperliquidClient]]


def smart_settings(settings: Settings) -> Settings:
    """The main settings adjusted for the SMART:3 account (its own database and risk rules)."""
    s = settings.smart
    return settings.model_copy(update={
        "app": settings.app.model_copy(update={"db_path": s.db_path}),
        "demo": settings.demo.model_copy(update={"starting_balance_aud": s.starting_balance_aud}),
        "risk": settings.risk.model_copy(update={
            "require_stop": False,
            "max_position_pct": max(s.btc_pct, s.long_pct, s.short_pct) + 1,
            "max_open_positions": 2 * s.picks + 1,
            "max_total_exposure_pct": 100.0,
            "daily_loss_limit_pct": 10.0,
            "kill_switch_drawdown_pct": s.kill_switch_drawdown_pct,
        }),
        "signals": settings.signals.model_copy(update={"allow_shorts": s.short_pct > 0}),
    })


def floor_to(value: float, decimals: int) -> float:
    step = 10 ** decimals
    return math.floor(value * step) / step


class SmartTrader:
    def __init__(self, engine: DemoEngine, client_factory: ClientFactory, echo: Callable[[str], None] = print,
                 others: dict[str, Path] | None = None) -> None:
        self.engine = engine
        self.settings = engine.settings
        self.cfg = self.settings.smart
        self.client_factory = client_factory
        self.echo = echo
        self.others = others or {}  # name -> database, for the comparison in the daily update
        engine.summaries = False  # SMART:3 sends its own daily update

    def jobs(self) -> list[Job]:
        return [Job("SMART rebalance", 300, lambda _: self.rebalance_if_due()),
                Job("SMART daily update", 60, lambda _: self.update_if_due())]

    # ------------------------------------------------------------ rebalance

    async def rebalance_if_due(self) -> None:
        now = datetime.fromtimestamp(self.engine.clock() / 1000, UTC)
        today = now.strftime("%Y-%m-%d")
        if now.time() < self.cfg.rebalance_after_utc or self.engine.state.get("smart_rebalanced_day") == today:
            return
        await self.rebalance(today)

    async def download(self) -> tuple[dict, dict[str, int]]:
        """Daily candles for the most-traded coins (finished candles only), and each coin's size decimals."""
        now = self.engine.clock()
        async with self.client_factory(self.settings) as client:
            market = [c for c in await client.market() if not c.is_delisted and ":" not in c.coin]
            self.engine.funding_rates = {c.coin: c.funding_rate for c in market}
            market.sort(key=lambda c: -c.volume_24h_usd)
            chosen = [c.coin for c in market[: self.cfg.fetch_coins]]
            if smart.BTC not in chosen:
                chosen.append(smart.BTC)
            daily = {}
            for coin in chosen:
                candles = await client.candles(coin, "1d", now - HISTORY_DAYS * DAY_MS, now)
                daily[coin] = smart.frames_from_candles([c for c in candles if c.is_closed(now)])
        return daily, {c.coin: c.sz_decimals for c in market}

    async def rebalance(self, today: str) -> None:
        engine = self.engine
        daily, decimals = await self.download()
        panels = smart.build_panels(daily, self.cfg)
        day = panels.close.index[-1]
        if day.strftime("%Y-%m-%d") != (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"):
            log.info("SMART: yesterday's daily candle isn't available yet (latest %s); trying again soon", day)
            return
        mine = [p for p in engine.account.positions() if p.strategy == STRATEGY]
        plan = smart.plan(panels, day, {p.coin: p.side for p in mine}, self.cfg)
        sold, opened = [], []
        for p in mine:
            if plan.wants(p.coin, p.side):
                continue
            if p.coin == smart.BTC:
                why = f"{TAG} Selling BTC: {smart.btc_reason(plan, self.cfg)}."
            else:
                why = f"{TAG} {'Selling' if p.long else 'Closing the short in'} {p.coin}: " + \
                      self._dropped_reason(panels, day, p.coin, p.side)
            if await engine.close(p, why) is not None:
                sold.append(f"{p.coin} ({'long' if p.long else 'short'})")
        prices = engine.prices.prices
        equity = engine.account.equity(prices)
        held = {p.coin for p in engine.account.positions()}
        wanted = ([(smart.BTC, "long", None)] if plan.btc_on else []) + [(k.coin, k.side, k) for k in plan.picks]
        rate = self.settings.demo.aud_to_usdc_rate
        for coin, side, pick in sorted(wanted, key=lambda w: w[1] != "short"):  # shorts first: they need cover
            if coin in held or coin not in prices:
                continue
            price = prices[coin]
            qty = floor_to(smart.target_usd(plan, pick, equity, self.cfg) / price, decimals.get(coin, 4))
            if qty * price < self.settings.risk.min_order_usd:
                continue
            stop = smart.emergency_stop(price, float(panels.atr.at[day, coin]), side, self.cfg)
            why = pick.reason if pick else smart.btc_reason(plan, self.cfg)
            verb = "🟢 BOUGHT" if side == "long" else "🟣 SHORTED"
            stop_text = f" Safety stop ${format_price(stop)}." if stop else ""
            text = f"{TAG} {verb} {qty:g} {coin} at ${format_price(price)} ({aud(qty * price, rate)}): {why}.{stop_text}"
            if await engine.open_position(coin, side, qty, text, strategy=STRATEGY, source=STRATEGY,
                                          stop=stop) is not None:
                opened.append(f"{'+' if side == 'long' else '−'}{coin}")
        with engine.conn:
            engine.state.set("smart_rebalanced_day", today)
            engine.state.set("smart_last_plan", self._plan_summary(plan))
        summary = self._rebalance_message(plan, sold, opened)
        engine.event("INFO", STRATEGY, summary.replace("\n", " | "))
        engine.notify(summary, "trade", Priority.NORMAL)
        self.echo(summary)

    def _dropped_reason(self, panels: smart.Panels, day, coin: str, side: str) -> str:
        scores = panels.score.loc[day].dropna()
        if coin not in scores:
            return "it's no longer among the most-traded coins, so it isn't ranked."
        order = (scores if side == "long" else -scores).rank(ascending=False, method="first")
        where = "best" if side == "long" else "worst"
        return f"it slipped to #{int(order[coin])} {where} (it's kept only while in the {where} {self.cfg.keep_within})."

    def _plan_summary(self, plan: smart.Plan) -> str:
        return (f"BTC {'on' if plan.btc_on else 'off'}; long " + ", ".join(p.coin for p in plan.longs)
                + "; short " + ", ".join(p.coin for p in plan.shorts))

    def _rebalance_message(self, plan: smart.Plan, sold: list[str], opened: list[str]) -> str:
        lines = [f"🧠 {self.cfg.name} daily rebalance ({plan.day:%a %d %b} close)",
                 f"Bitcoin half: {'HOLD' if plan.btc_on else 'CASH'} ({smart.btc_reason(plan, self.cfg)})",
                 "Long (best ranked): " + (", ".join(p.coin for p in plan.longs) or "none"),
                 "Short (worst ranked): " + (", ".join(p.coin for p in plan.shorts) or "none")]
        changes = ([f"opened {', '.join(opened)}"] if opened else []) + ([f"closed {', '.join(sold)}"] if sold else [])
        lines.append("Changes: " + ("; ".join(changes) if changes else "none (same picks as yesterday)"))
        return "\n".join(lines)

    # ------------------------------------------------------------ daily update

    async def update_if_due(self) -> None:
        engine = self.engine
        due = due_update(engine.clock(), self.settings.app.tz, self.settings.notify.update_times,
                         engine.state.get("last_update_slot"), int(engine.state.get_float("opened_ms", 0)))
        if due is None:
            return
        with engine.conn:
            engine.state.set("last_update_slot", due[0])
        if due[1]:
            engine.notify(update_text(engine, self.others), "report", Priority.NORMAL)


def account_line(db: Path, name: str, rate: float) -> str | None:
    """'NAME: A$x (+y%)' from another account's database (read-only), or None if it has no data yet."""
    if not db.exists():
        return None
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT equity_usd FROM equity_snapshots ORDER BY ts_ms DESC LIMIT 1").fetchone()
        first = conn.execute("SELECT value FROM bot_state WHERE key = 'initial_equity_usd'").fetchone()
    if not row or not first:
        return None
    return f"{name}: {aud(row[0], rate)} ({(row[0] / float(first[0]) - 1) * 100:+.2f}%)"


def update_text(engine: DemoEngine, others: dict[str, Path] | None = None) -> str:
    settings = engine.settings
    cfg = settings.smart
    rate = settings.demo.aud_to_usdc_rate
    prices = engine.prices.prices
    equity = engine.account.equity(prices)
    start = engine.state.get_float("initial_equity_usd", equity)
    lines = [f"🧠 {cfg.name} daily update ({datetime.now(settings.app.tz):%a %d %b %H:%M})",
             f"Balance {aud(equity, rate)} ({(equity / start - 1) * 100:+.2f}% since the start)"]
    positions = [p for p in engine.account.positions() if p.strategy == STRATEGY]

    def pnl(p) -> str:
        price = prices.get(p.coin, p.entry_price)
        value = p.unrealised_usd(price) - p.fees_usd - p.funding_usd
        return f"{p.coin} {value / (p.qty * p.entry_price) * 100:+.1f}%"

    btc = [p for p in positions if p.coin == smart.BTC]
    lines.append("Bitcoin half: " + (f"holding ({pnl(btc[0])})" if btc else "in cash (Bitcoin's trend is down)"))
    longs = [p for p in positions if p.long and p.coin != smart.BTC]
    shorts = [p for p in positions if not p.long]
    lines.append("Long: " + (", ".join(pnl(p) for p in longs) or "none"))
    lines.append("Short: " + (", ".join(pnl(p) for p in shorts) or "none"))
    closed = engine.conn.execute("SELECT pnl_usd FROM demo_positions WHERE status = 'closed' AND strategy = ?",
                                 (STRATEGY,)).fetchall()
    if closed:
        total = sum(r[0] for r in closed)
        lines.append(f"Closed: {len(closed)} trades, {sum(r[0] > 0 for r in closed)} won, {aud(total, rate, True)}")
    for name, db in (others or {}).items():
        line = account_line(db, name, rate)
        if line:
            lines.append(line)
    return "\n".join(lines)
