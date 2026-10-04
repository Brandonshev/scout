"""The experiment: a separate fake account testing copy trading (70%) and high-risk small coins (30%).

It runs the same DemoEngine and loop as the main demo (risk manager, fills, fees, funding, alerts,
sleep handling, backups), in its own database, with these differences:
- no stop losses: copied trades close when the wallet closes; high-risk trades close at the profit
  target or the time limit;
- no single position bigger than max_trade_pct of the account; leverage still 1x;
- a last-resort kill switch at -50%;
- trade alerts are bundled hourly, plus a daily scorecard per strategy;
- crypto news headlines are a safety check: a serious one about a coin (hack, rug pull, delisting...)
  blocks buying it and sells a long we hold (see news.py).
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, closing
from datetime import datetime
from pathlib import Path

from scout import copytrade, highrisk, news
from scout.config import Settings
from scout.copytrade import WalletScore
from scout.data import (
    DAY_MS,
    HyperliquidClient,
    HyperliquidError,
    fetch_leaderboard,
    format_price,
)
from scout.demo import DemoEngine, DemoPosition, Fill, Job
from scout.news import Headline, Verdict
from scout.notify import Priority, aud, due_update

log = logging.getLogger(__name__)
HOUR_MS = 3_600_000
TAGS = {"copy": "[COPY]", "high_risk": "[HIGH-RISK]"}

ClientFactory = Callable[[Settings], AbstractAsyncContextManager[HyperliquidClient]]
Leaderboard = Callable[[], Awaitable[list[dict]]]
NewsFetcher = Callable[[], Awaitable[tuple[list[Headline], dict[str, str]]]]


def experiment_settings(settings: Settings) -> Settings:
    """The main settings adjusted for the experiment account (its own database and risk rules)."""
    e = settings.experiment
    return settings.model_copy(update={
        "app": settings.app.model_copy(update={"db_path": e.db_path}),
        "demo": settings.demo.model_copy(update={"starting_balance_aud": e.starting_balance_aud}),
        "risk": settings.risk.model_copy(update={
            "require_stop": False,
            "max_position_pct": e.max_trade_pct,
            "max_open_positions": 25,
            "max_total_exposure_pct": 100.0,
            "daily_loss_limit_pct": 20.0,
            "kill_switch_drawdown_pct": e.kill_switch_drawdown_pct,
        }),
        "signals": settings.signals.model_copy(update={"allow_shorts": e.copy_trading.allow_shorts}),
    })


def floor_to(value: float, decimals: int) -> float:
    step = 10 ** decimals
    return math.floor(value * step) / step


class Experiment:
    def __init__(self, engine: DemoEngine, client_factory: ClientFactory,
                 leaderboard: Leaderboard | None = None, echo: Callable[[str], None] = print,
                 news_fetcher: NewsFetcher | None = None) -> None:
        self.engine = engine
        self.settings = engine.settings
        self.cfg = self.settings.experiment
        self.client_factory = client_factory
        self.leaderboard = leaderboard or fetch_leaderboard
        self.echo = echo
        self.snapshots: dict[str, dict | None] = {}
        self.last_poll: dict[str, int] = {}
        self.sz_decimals: dict[str, int] = {}
        self.names: dict[str, str] = {}
        self.refreshing: asyncio.Task | None = None
        self.news_fetcher = news_fetcher or (lambda: news.fetch_headlines(self.settings.news))
        self.headlines: list[Headline] = news.recent(engine.conn, self._news_since())
        engine.summaries = False  # the experiment sends its own scorecard

    # ---------------------------------------------------------------- state

    @property
    def followed(self) -> list[WalletScore]:
        return copytrade.wallets_from_json(self.engine.state.get("followed_wallets"))

    def jobs(self) -> list[Job]:
        reading = [Job("news", self.settings.news.refresh_minutes * 60, lambda _: self.refresh_news())]
        return [
            *(reading if self.settings.news.enabled else []),
            Job("wallet check", self.cfg.copy_trading.poll_seconds, lambda _: self.poll_wallets()),
            Job("high-risk scan", self.cfg.high_risk.scan_minutes * 60, lambda _: self.scan_high_risk()),
            Job("high-risk exits", 60, lambda _: self.high_risk_exits()),
            Job("wallet refresh", 300, lambda _: self.refresh_if_due()),
            Job("scorecard", 60, lambda _: self.scorecard_if_due()),
        ]

    def equity(self) -> float:
        return self.engine.account.equity(self.engine.prices.prices)

    def exposure(self, strategy: str) -> float:
        prices = self.engine.prices.prices
        return sum(p.qty * prices.get(p.coin, p.entry_price) for p in self.engine.account.positions()
                   if p.strategy == strategy)

    def room(self, strategy: str) -> float:
        """US$ still available to this strategy under its share of the account."""
        share = self.cfg.copy_pct if strategy == "copy" else self.cfg.high_risk_pct
        return max(0.0, self.equity() * share / 100 - self.exposure(strategy))

    # ---------------------------------------------------------------- news

    def _news_since(self) -> int:
        return self.engine.clock() - self.settings.news.lookback_hours * HOUR_MS

    async def refresh_news(self) -> None:
        """Read the feeds, store new headlines, and sell any long that's now in trouble."""
        headlines, errors = await self.news_fetcher()
        now = self.engine.clock()
        with self.engine.conn:
            new = news.save(self.engine.conn, headlines, now)
            if errors and not headlines:
                self.engine.event("WARNING", "news", "No news feed could be read: " + "; ".join(
                    f"{k}: {v}" for k, v in errors.items()))
        self.headlines = news.recent(self.engine.conn, self._news_since())
        log.info("news: %d new headlines, %d in the last %dh", new, len(self.headlines),
                 self.settings.news.lookback_hours)
        await self.news_exits()

    def verdict(self, coin: str, name: str | None = None) -> Verdict:
        if not self.settings.news.enabled:
            return Verdict(coin, [], [])
        since = self._news_since()
        return news.judge(coin, name, [h for h in self.headlines if h.published_ms >= since])

    def _veto(self, coin: str, name: str, strategy: str, verdict: Verdict) -> None:
        """Skip a buy because of the news; recorded once a day per coin, to check later."""
        engine = self.engine
        if news.decided_recently(engine.conn, coin, "veto", engine.clock() - DAY_MS):
            return
        with engine.conn:
            news.record_decision(engine.conn, engine.clock(), coin, name, strategy, "veto",
                                 engine.prices.prices.get(coin, 0.0), verdict.serious[0])
            engine.event("INFO", "news", f"{TAGS[strategy]} Not buying {name}: {verdict.why}")

    async def news_exits(self) -> None:
        """Sell longs with a serious headline against them (shorts are left alone: bad news helps them)."""
        engine = self.engine
        for p in engine.account.positions():
            if p.strategy not in TAGS or not p.long or p.coin not in engine.prices.prices:
                continue
            name = p.source if p.strategy == "high_risk" else p.coin
            verdict = self.verdict(p.coin, name)
            if not verdict.danger:
                continue
            price = engine.prices.prices[p.coin]
            reason = f"{TAGS[p.strategy]} 📰 Selling {name} early: {verdict.why}"
            with engine.conn:
                news.record_decision(engine.conn, engine.clock(), p.coin, name, p.strategy, "exit", price,
                                     verdict.serious[0])
            if p.strategy == "high_risk":
                await self._sell_high_risk(p, reason)
            else:
                await engine.close(p, reason)
            engine.notify(reason + f"\n{verdict.serious[0].url}", "trade", Priority.NORMAL)

    # ------------------------------------------------------- picking wallets

    async def refresh_if_due(self) -> None:
        last = self.engine.state.get_float("wallets_refreshed_ms", 0)
        due = self.engine.clock() - last >= self.cfg.wallet_refresh_hours * HOUR_MS
        if due and (self.refreshing is None or self.refreshing.done()):
            self.refreshing = asyncio.create_task(self._refresh_logged())

    async def _refresh_logged(self) -> None:
        try:
            await self.refresh_wallets()
        except Exception as exc:  # retried at the next check (every 5 minutes)
            self.engine.event("ERROR", "experiment", f"Picking wallets failed ({exc}); trying again soon.")

    async def refresh_wallets(self) -> list[WalletScore]:
        """Re-rank wallets on their real trades and store the ones to follow."""
        cfg = self.cfg.copy_trading
        rows = await self.leaderboard()
        candidates = copytrade.leaderboard_candidates(rows, cfg)
        now = self.engine.clock()
        scores = []
        async with self.client_factory(self.settings) as client:
            for wallet, account in candidates:
                try:
                    fills = await client.fills_of(wallet, now - cfg.lookback_days * DAY_MS)
                    open_pnl = sum(p.unrealized_usd for p in await client.positions_of(wallet))
                except HyperliquidError as exc:
                    log.warning("trades for %s unavailable: %s", wallet, exc)
                    continue
                scores.append(copytrade.score_wallet(wallet, account, fills, now, cfg, open_pnl))
        chosen = copytrade.pick_wallets(scores, cfg.wallets)
        with self.engine.conn:
            self.engine.state.set("followed_wallets", copytrade.wallets_to_json(chosen))
            self.engine.state.set("wallets_refreshed_ms", now)
            self.engine.state.set("wallets_checked", len(scores))
            self.engine.event("INFO", "experiment", f"Checked {len(scores)} wallets' trades; following {len(chosen)}: "
                              + ", ".join(s.summary for s in chosen))
        for s in chosen:
            self.snapshots.setdefault(s.wallet, None)
        self.echo(f"Following {len(chosen)} wallets (checked {len(scores)}).")
        return chosen

    # --------------------------------------------------------- copy trading

    async def poll_wallets(self) -> None:
        followed = {s.wallet: s for s in self.followed}
        mine = [p for p in self.engine.account.positions() if p.strategy == "copy"]
        wallets = list(dict.fromkeys([*followed, *(p.source for p in mine if p.source)]))
        if not wallets:
            return
        now = self.engine.clock()
        async with self.client_factory(self.settings) as client:
            for wallet in wallets:
                current = await client.positions_of(wallet)
                # Too long since the last look (sleep, network drop): treat as a first look, so we don't
                # copy positions opened while we weren't watching.
                stale = now - self.last_poll.get(wallet, 0) > 3 * self.cfg.copy_trading.poll_seconds * 1000
                previous = None if stale else self.snapshots.get(wallet)
                score = followed.get(wallet)
                changes = copytrade.diff_positions(wallet, previous, current,
                                                   score.account_usd if score else 0.0, self.cfg.copy_trading)
                self.snapshots[wallet] = copytrade.snapshot(current)
                self.last_poll[wallet] = now
                await self._reconcile(wallet, self.snapshots[wallet])
                for change in changes:
                    if change.action == "open" and score is not None:
                        await self._copy_open(change, score)

    async def _reconcile(self, wallet: str, holding: dict) -> None:
        """Close our copies of this wallet's positions that it no longer holds (same side)."""
        for p in self.engine.account.positions():
            if p.strategy != "copy" or p.source != wallet:
                continue
            theirs = holding.get(p.coin)
            if theirs is None or theirs.side != p.side:
                verb = "Selling" if p.long else "Closing the short in"
                await self.engine.close(p, f"{TAGS['copy']} {verb} {p.coin}: the wallet we copied "
                                           f"({wallet[:6]}…{wallet[-4:]}) closed its position.")

    async def _copy_open(self, change: copytrade.Change, score: WalletScore) -> None:
        engine = self.engine
        if any(p.coin == change.coin for p in engine.account.positions()):
            return  # already holding it (maybe copying another wallet)
        price = engine.prices.prices.get(change.coin)
        if price is None:
            return
        spend = min(self.equity() * self.cfg.copy_trading.position_pct / 100, self.room("copy"))
        qty = floor_to(spend / price, self.sz_decimals.get(change.coin, 4))
        if qty * price < self.settings.risk.min_order_usd:
            engine.event("INFO", "experiment", f"{TAGS['copy']} Not copying {change.coin}: the copy budget is used up.")
            return
        long = change.side == "long"
        verdict = self.verdict(change.coin)
        if long and verdict.danger:
            self._veto(change.coin, change.coin, "copy", verdict)
            return
        text = (f"{TAGS['copy']} {'🟢 BOUGHT' if long else '🟣 SHORTED'} {qty:g} {change.coin} at ${format_price(price)} "
                f"({aud(qty * price, self.settings.demo.aud_to_usdc_rate)}), copying {score.summary}"
                + (f". {verdict.why[0].upper()}{verdict.why[1:]}" if verdict.why else ""))
        await engine.open_position(change.coin, change.side, qty, text, strategy="copy", source=change.wallet,
                                   message=text)

    # ------------------------------------------------------ high-risk coins

    async def scan_high_risk(self) -> None:
        cfg = self.cfg.high_risk
        engine = self.engine
        async with self.client_factory(self.settings) as client:
            perps = await client.market()
            spot = await client.spot_market()
            engine.funding_rates = {c.coin: c.funding_rate for c in perps}
            self.sz_decimals.update({c.coin: c.sz_decimals for c in perps})
            self.sz_decimals.update({c.pair: c.sz_decimals for c in spot})
            self.names.update(highrisk.names(spot))
            held = {p.coin for p in engine.account.positions()}
            since = engine.clock() - DAY_MS
            recent = {r["coin"] for r in engine.conn.execute(
                "SELECT coin FROM demo_positions WHERE strategy = 'high_risk' AND opened_ts_ms >= ?", (since,))}
            candidates = highrisk.prefilter(spot, perps, cfg, held | recent, self.settings.scanner.min_24h_volume_usd)
            for candidate in candidates[:8]:
                if self.room("high_risk") < self.settings.risk.min_order_usd:
                    return
                book = await client.l2_book(candidate.coin)
                now = engine.clock()
                candles = await client.candles(candidate.coin, "1d", now - 8 * DAY_MS, now - DAY_MS)
                ok, detailed, why_not = highrisk.check(candidate, book, [c.volume * c.close for c in candles], cfg)
                if not ok:
                    log.info("high-risk: skipped %s: %s", candidate.name, why_not)
                    continue
                verdict = self.verdict(candidate.coin, candidate.name)
                if verdict.danger:
                    self._veto(candidate.coin, candidate.name, "high_risk", verdict)
                    continue
                spend = min(self.equity() * cfg.position_pct / 100, self.room("high_risk"))
                filled = highrisk.fill_buy(book, spend)
                if filled is None or candidate.coin not in engine.prices.prices:
                    continue
                qty = floor_to(filled[0], self.sz_decimals.get(candidate.coin, 2))
                if qty * filled[1] < self.settings.risk.min_order_usd:
                    continue
                fee = qty * filled[1] * self.settings.risk.taker_fee_pct / 100
                fill = Fill(candidate.coin, "buy", qty, filled[1], fee, engine.clock())
                text = (f"{TAGS['high_risk']} 🎲 BOUGHT {qty:g} {candidate.name} at ${format_price(filled[1])} "
                        f"({aud(qty * filled[1], self.settings.demo.aud_to_usdc_rate)}): {detailed.why}. Sells at "
                        f"+{cfg.take_profit_pct:g}% or after {cfg.max_hold_hours:g}h."
                        + (f" {verdict.why[0].upper()}{verdict.why[1:]}." if verdict.why else ""))
                await engine.open_position(candidate.coin, "long", qty, text, strategy="high_risk",
                                           source=candidate.name, fill=fill, message=text)

    async def high_risk_exits(self) -> None:
        cfg = self.cfg.high_risk
        engine = self.engine
        due: list[tuple[DemoPosition, str]] = []
        for p in engine.account.positions():
            if p.strategy != "high_risk":
                continue
            price = engine.prices.prices.get(p.coin)
            if price is None:
                continue
            held = (engine.clock() - p.opened_ts_ms) / HOUR_MS
            reason = highrisk.exit_reason(p.entry_price, price, held, cfg)
            if reason:
                due.append((p, reason))
        if not due:
            return
        for p, reason in due:
            await self._sell_high_risk(p, f"{TAGS['high_risk']} Selling {p.source or p.coin}: {reason}.")

    async def _sell_high_risk(self, p: DemoPosition, reason: str) -> None:
        """Sell into the real order book (thin coins sell below the quoted price)."""
        async with self.client_factory(self.settings) as client:
            book = await client.l2_book(p.coin)
        average, enough = highrisk.fill_sell(book, p.qty)
        note = "" if enough else " Not enough buyers: the rest was assumed sold at half price."
        await self.engine.close(p, reason + note, price=self.engine.prices.prices[p.coin], fill_price=average)

    # ---------------------------------------------------------- scorecard

    async def scorecard_if_due(self) -> None:
        engine = self.engine
        now = engine.clock()
        due = due_update(now, self.settings.app.tz, self.settings.notify.update_times,
                         engine.state.get("last_update_slot"), int(engine.state.get_float("opened_ms", 0)))
        if due is None:
            return
        with engine.conn:
            engine.state.set("last_update_slot", due[0])
        if due[1]:
            engine.notify(self.scorecard(), "report", Priority.NORMAL)

    def scorecard(self, core_db: Path | None = None) -> str:
        return scorecard_text(self.engine, self.followed, core_db)


def strategy_stats(conn: sqlite3.Connection, prices: dict[str, float], strategy: str) -> dict:
    closed = conn.execute("SELECT pnl_usd FROM demo_positions WHERE status = 'closed' AND strategy = ?",
                          (strategy,)).fetchall()
    open_rows = conn.execute("SELECT * FROM demo_positions WHERE status = 'open' AND strategy = ?",
                             (strategy,)).fetchall()
    unrealised = 0.0
    for p in open_rows:
        price = prices.get(p["coin"], p["last_price"] or p["entry_price"])
        unrealised += p["qty"] * (price - p["entry_price"]) * (1 if p["side"] == "long" else -1) - p["fees_usd"] \
            - p["funding_usd"]
    realised = sum(r["pnl_usd"] for r in closed)
    return {"closed": len(closed), "wins": sum(r["pnl_usd"] > 0 for r in closed), "realised": realised,
            "open": len(open_rows), "unrealised": unrealised, "total": realised + unrealised}


def scorecard_text(engine: DemoEngine, followed: list[WalletScore], core_db: Path | None = None) -> str:
    settings = engine.settings
    rate = settings.demo.aud_to_usdc_rate
    equity = engine.account.equity(engine.prices.prices)
    start = engine.state.get_float("initial_equity_usd", equity)
    lines = [f"🧪 {settings.experiment.name} scorecard ({datetime.now(settings.app.tz):%a %d %b %H:%M})",
             f"Balance {aud(equity, rate)} ({(equity / start - 1) * 100:+.2f}% since the start)"]
    for strategy, label in (("copy", "Copy trading"), ("high_risk", "High-risk coins")):
        s = strategy_stats(engine.conn, engine.prices.prices, strategy)
        lines.append(f"{label}: {aud(s['total'], rate, True)} · {s['closed']} closed ({s['wins']} won), "
                     f"{s['open']} open ({aud(s['unrealised'], rate, True)} so far)")
    lines.append(f"Following {len(followed)} wallets" + (f", best: {followed[0].summary}" if followed else ""))
    if settings.news.enabled:
        n = news.hindsight(engine.conn, engine.prices.prices)
        line = f"News: blocked {n['vetoes']} buys, sold {n['exits']} early"
        if n["avg_move_pct"] is not None:
            line += (f"; since then {n['fell']} of {n['checked']} of those coins fell, average "
                     f"{n['avg_move_pct']:+.1f}% (falls = the news was right)")
        lines.append(line)
    if core_db is not None and core_db.exists():
        with closing(sqlite3.connect(f"file:{core_db}?mode=ro", uri=True)) as core:
            row = core.execute("SELECT equity_usd FROM equity_snapshots ORDER BY ts_ms DESC LIMIT 1").fetchone()
            first = core.execute("SELECT value FROM bot_state WHERE key = 'initial_equity_usd'").fetchone()
        if row and first:
            lines.append(f"{settings.demo.name} for comparison: {aud(row[0], rate)} "
                         f"({(row[0] / float(first[0]) - 1) * 100:+.2f}%)")
    return "\n".join(lines)
