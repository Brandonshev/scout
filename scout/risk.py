"""Risk management.

- Position sizing: how much to buy so a stopped-out trade loses a fixed, small slice.
- The RiskManager: every order passes through `check` before it can be executed.
  Orders that only close positions are always allowed (blocking an exit makes things worse).
  New positions must pass every rule; each rejection comes with a plain-English reason.
- Alerts: the daily loss limit and the kill switch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from scout.config import RiskSettings


@dataclass(frozen=True)
class PositionSize:
    qty: float  # coins to buy (or sell short)
    notional_usd: float  # qty × entry price
    risk_usd: float  # expected loss if the stop is hit, including fees and slippage
    target_risk_usd: float  # what we aimed to risk before any cap
    capped_by: str | None = None  # why the size was reduced below the target, if it was
    problem: str | None = None  # why no trade is possible, if so

    @property
    def ok(self) -> bool:
        return self.problem is None


def size_position(
    equity_usd: float,
    cash_usd: float,
    entry: float,
    stop: float,
    cfg: RiskSettings,
    size_multiplier: float = 1.0,
    sz_decimals: int = 4,
) -> PositionSize:
    """Size a trade so that hitting the stop loses about risk_per_trade_pct of the account.

    loss per coin = distance to the stop + fees and slippage on the way in and out
    coins         = amount to risk / loss per coin
    Then capped so one position is at most max_position_pct of the account and never
    more than the cash available (no borrowing: leverage stays at 1x).
    `size_multiplier` < 1 shrinks everything (e.g. 0.5 when volatility is wild).
    """
    if entry <= 0 or stop <= 0 or entry == stop:
        return PositionSize(0, 0, 0, 0, problem="invalid entry or stop price")
    costs_pct = (cfg.taker_fee_pct + cfg.slippage_pct) / 100
    loss_per_coin = abs(entry - stop) + (entry + stop) * costs_pct
    target_risk = equity_usd * cfg.risk_per_trade_pct / 100 * size_multiplier
    qty = target_risk / loss_per_coin

    capped_by = None
    max_notional = equity_usd * cfg.max_position_pct / 100 * size_multiplier
    if qty * entry > max_notional:
        qty, capped_by = max_notional / entry, f"{cfg.max_position_pct:g}% of the account per position"
    spendable = cash_usd / (1 + costs_pct)
    if qty * entry > spendable:
        qty, capped_by = spendable / entry, "the cash available"

    step = 10**sz_decimals
    qty = math.floor(qty * step) / step  # the exchange only accepts sizes to sz_decimals places
    notional = qty * entry
    size = PositionSize(qty, notional, qty * loss_per_coin, target_risk, capped_by)
    if notional < cfg.min_order_usd:
        return PositionSize(
            qty, notional, size.risk_usd, target_risk, capped_by,
            problem=f"the position would be only US${notional:,.2f}, below the US${cfg.min_order_usd:g} minimum order",
        )
    return size


# ------------------------------------------------------------ risk manager


class BotState(StrEnum):
    RUNNING = "RUNNING"  # trading normally
    PAUSED = "PAUSED"  # no new trades; open positions are still managed (you paused it)
    KILLED = "KILLED"  # kill switch: everything closed, nothing new until a manual reset


class Alert(StrEnum):
    KILL = "KILL"  # drawdown from the peak reached the kill switch level
    DAILY_LIMIT = "DAILY_LIMIT"  # today's loss reached the daily limit


@dataclass(frozen=True)
class Order:
    coin: str
    side: str  # "buy" or "sell"
    qty: float
    price: float  # latest price, used for the checks
    reduce_only: bool  # True = only closes an existing position
    reason: str
    stop_price: float | None = None  # required when opening

    @property
    def notional_usd(self) -> float:
        return self.qty * self.price

    @property
    def position_side(self) -> str:
        """The side of the position this order opens (or closes)."""
        opens_long = self.side == "buy"
        return ("short" if opens_long else "long") if self.reduce_only else ("long" if opens_long else "short")


@dataclass(frozen=True)
class HeldPosition:
    coin: str
    side: str
    notional_usd: float


@dataclass(frozen=True)
class RiskContext:
    state: BotState
    equity_usd: float
    free_cash_usd: float
    peak_equity_usd: float
    day_start_equity_usd: float
    daily_limit_hit: bool
    positions: tuple[HeldPosition, ...]
    price_age_seconds: float | None  # age of the latest price for the order's coin; None = no price
    allow_shorts: bool

    @property
    def drawdown_pct(self) -> float:
        return max(0.0, (self.peak_equity_usd - self.equity_usd) / self.peak_equity_usd * 100)

    @property
    def daily_loss_pct(self) -> float:
        return max(0.0, (self.day_start_equity_usd - self.equity_usd) / self.day_start_equity_usd * 100)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


class RiskManager:
    def __init__(self, cfg: RiskSettings) -> None:
        self.cfg = cfg

    def check(self, order: Order, ctx: RiskContext) -> RiskDecision:
        """Approve or reject an order. Every rejection explains itself."""
        c = self.cfg
        if order.reduce_only:
            return RiskDecision(True, "closing a position is always allowed")

        def no(why: str) -> RiskDecision:
            return RiskDecision(False, f"Not opening {order.coin}: {why}.")

        if order.qty <= 0 or order.price <= 0:
            return no("the order size or price is invalid")
        if ctx.state is BotState.KILLED:
            return no("the kill switch is on. Nothing new is traded until you run `scout reset-kill`")
        if ctx.state is BotState.PAUSED:
            return no("Scout is paused (run `scout resume` to continue)")
        if ctx.price_age_seconds is None:
            return no("there is no live price for it yet")
        if ctx.price_age_seconds > c.stale_price_seconds:
            return no(f"the latest price is {ctx.price_age_seconds:.0f} seconds old (limit {c.stale_price_seconds}s). "
                      "The price feed may have dropped, and trading on old prices is guesswork")
        if ctx.daily_limit_hit or ctx.daily_loss_pct >= c.daily_loss_limit_pct:
            return no(f"today's loss limit was hit (down {ctx.daily_loss_pct:.1f}% since midnight Sydney time, "
                      f"limit {c.daily_loss_limit_pct:g}%). New trades resume after midnight")
        if ctx.drawdown_pct >= c.kill_switch_drawdown_pct:
            return no(f"the account is {ctx.drawdown_pct:.1f}% below its peak "
                      f"(kill switch level {c.kill_switch_drawdown_pct:g}%)")
        if order.position_side == "short" and not ctx.allow_shorts:
            return no("short selling is switched off")
        if order.stop_price is None and c.require_stop:
            return no("it has no stop loss, and every position must have one")
        if order.stop_price is not None and (
            (order.position_side == "long" and order.stop_price >= order.price)
            or (order.position_side == "short" and order.stop_price <= order.price)
        ):
            return no(f"the stop ${order.stop_price:,.4g} is on the wrong side of the price ${order.price:,.4g}")
        if any(p.coin == order.coin for p in ctx.positions):
            return no("there is already a position in this coin")
        if len(ctx.positions) >= c.max_open_positions:
            return no(f"{len(ctx.positions)} positions are already open (maximum {c.max_open_positions})")
        if order.notional_usd < c.min_order_usd:
            return no(f"US${order.notional_usd:,.2f} is below the US${c.min_order_usd:g} minimum order")
        coin_pct = order.notional_usd / ctx.equity_usd * 100
        if coin_pct > c.max_position_pct + 0.01:
            return no(f"US${order.notional_usd:,.2f} is {coin_pct:.1f}% of the account "
                      f"(maximum {c.max_position_pct:g}% per coin)")
        total = sum(p.notional_usd for p in ctx.positions) + order.notional_usd
        leverage = total / ctx.equity_usd
        if leverage > c.max_leverage + 1e-9:
            return no(f"positions would total US${total:,.2f} on a US${ctx.equity_usd:,.2f} account "
                      f"({leverage:.2f}x). Borrowing isn't allowed: leverage is capped at {c.max_leverage:g}x")
        if total / ctx.equity_usd * 100 > c.max_total_exposure_pct + 0.01:
            return no(f"all positions would add up to {total / ctx.equity_usd * 100:.0f}% of the account "
                      f"(maximum {c.max_total_exposure_pct:g}%)")
        costs = order.notional_usd * (1 + (c.taker_fee_pct + c.slippage_pct) / 100)
        if order.position_side == "long" and costs > ctx.free_cash_usd:
            return no(f"it needs US${costs:,.2f} but only US${ctx.free_cash_usd:,.2f} of cash is free")
        if order.position_side == "short" and order.notional_usd > ctx.free_cash_usd:
            return no(f"a US${order.notional_usd:,.2f} short needs that much free cash as cover")
        return RiskDecision(True, "passed every risk check")

    def alerts(self, ctx: RiskContext) -> list[Alert]:
        """Account-level alarms to act on now."""
        found = []
        if ctx.state is not BotState.KILLED and ctx.drawdown_pct >= self.cfg.kill_switch_drawdown_pct:
            found.append(Alert.KILL)
        if not ctx.daily_limit_hit and ctx.daily_loss_pct >= self.cfg.daily_loss_limit_pct:
            found.append(Alert.DAILY_LIMIT)
        return found
