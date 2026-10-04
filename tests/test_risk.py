import pytest

from scout.config import RiskSettings
from scout.risk import size_position

NO_COSTS = RiskSettings(taker_fee_pct=0, slippage_pct=0)


def test_risk_one_percent_based_on_stop_distance():
    size = size_position(1000, 1000, entry=100, stop=95, cfg=NO_COSTS)
    assert size.ok
    assert size.qty == pytest.approx(2)  # losing $5 per coin × 2 coins = $10 = 1% of $1,000
    assert size.risk_usd == pytest.approx(10)
    assert size.capped_by is None


def test_fees_and_slippage_are_part_of_the_risk():
    with_costs = size_position(1000, 1000, entry=100, stop=95, cfg=RiskSettings())
    assert with_costs.qty < 2
    assert with_costs.risk_usd == pytest.approx(10, abs=0.01)


def test_wild_volatility_halves_the_size():
    full = size_position(1000, 1000, entry=100, stop=95, cfg=NO_COSTS)
    half = size_position(1000, 1000, entry=100, stop=95, cfg=NO_COSTS, size_multiplier=0.5)
    assert half.qty == pytest.approx(full.qty / 2)
    assert half.risk_usd == pytest.approx(5)


def test_tight_stop_is_capped_at_max_position_size():
    size = size_position(1000, 1000, entry=100, stop=99.5, cfg=NO_COSTS)  # uncapped would be $2,000
    assert size.notional_usd == pytest.approx(250)
    assert "25%" in size.capped_by
    assert size.risk_usd < size.target_risk_usd


def test_never_spends_more_than_the_cash():
    size = size_position(1000, 100, entry=100, stop=95, cfg=NO_COSTS)
    assert size.notional_usd <= 100
    assert size.capped_by == "the cash available"


def test_too_small_for_the_minimum_order():
    size = size_position(100, 100, entry=100, stop=50, cfg=NO_COSTS)  # risk $1 -> $2 position
    assert not size.ok
    assert "minimum order" in size.problem


def test_rounds_down_to_the_exchange_step():
    size = size_position(1000, 1000, entry=3, stop=2.9, cfg=NO_COSTS, sz_decimals=0)
    assert size.qty == int(size.qty)
    assert size.notional_usd <= 250


def test_shorts_are_sized_the_same_way():
    size = size_position(1000, 1000, entry=100, stop=105, cfg=NO_COSTS)
    assert size.qty == pytest.approx(2)


def test_invalid_prices():
    assert not size_position(1000, 1000, entry=100, stop=100, cfg=NO_COSTS).ok


# ------------------------------------------------------------ risk manager

from dataclasses import replace  # noqa: E402

from scout.risk import Alert, BotState, HeldPosition, Order, RiskContext, RiskManager  # noqa: E402

RM = RiskManager(RiskSettings())  # 25% per coin, 3 positions, 75% total, 1x, 3% daily, 15% kill
CTX = RiskContext(
    state=BotState.RUNNING, equity_usd=1000.0, free_cash_usd=1000.0, peak_equity_usd=1000.0,
    day_start_equity_usd=1000.0, daily_limit_hit=False, positions=(), price_age_seconds=2.0, allow_shorts=False,
)
BUY = Order("ETH", "buy", qty=1.0, price=200.0, reduce_only=False, reason="test", stop_price=190.0)
CLOSE = Order("ETH", "sell", qty=1.0, price=200.0, reduce_only=True, reason="test")


def rejected(order=BUY, **changes) -> str:
    decision = RM.check(order, replace(CTX, **changes))
    assert not decision.approved
    assert decision.reason.startswith(f"Not opening {order.coin}: ")
    return decision.reason


def test_a_normal_order_passes():
    assert RM.check(BUY, CTX).approved


@pytest.mark.parametrize(
    "changes",
    [
        {"state": BotState.KILLED},
        {"state": BotState.PAUSED},
        {"price_age_seconds": 999},
        {"price_age_seconds": None},
        {"daily_limit_hit": True},
        {"equity_usd": 500.0},  # 50% below peak
    ],
)
def test_closing_is_always_allowed(changes):
    assert RM.check(CLOSE, replace(CTX, **changes)).approved


def test_kill_switch_blocks_new_trades():
    assert "kill switch is on" in rejected(state=BotState.KILLED)
    assert "reset-kill" in rejected(state=BotState.KILLED)


def test_pause_blocks_new_trades():
    assert "paused" in rejected(state=BotState.PAUSED)


def test_stale_prices_block_new_trades():
    assert "61 seconds old" in rejected(price_age_seconds=61)
    assert "no live price" in rejected(price_age_seconds=None)
    assert RM.check(BUY, replace(CTX, price_age_seconds=59)).approved


def test_daily_loss_limit_blocks_new_trades():
    assert "today's loss limit was hit" in rejected(daily_limit_hit=True)
    assert "down 3.0% since midnight Sydney" in rejected(equity_usd=970.0)  # reached even before the flag is set
    assert RM.check(BUY, replace(CTX, equity_usd=975.0, free_cash_usd=975.0)).approved


def test_drawdown_at_kill_level_blocks_new_trades():
    reason = rejected(equity_usd=1000.0, peak_equity_usd=1200.0, day_start_equity_usd=1000.0)
    assert "below its peak" in reason and "kill switch level 15%" in reason


def test_max_open_positions():
    held = tuple(HeldPosition(c, "long", 50.0) for c in ("A", "B", "C"))
    assert "3 positions are already open (maximum 3)" in rejected(positions=held)


def test_one_position_per_coin():
    assert "already a position in this coin" in rejected(positions=(HeldPosition("ETH", "long", 50.0),))


def test_max_percent_of_account_per_coin():
    big = replace(BUY, qty=1.3)  # US$260 = 26% of US$1,000
    assert "26.0% of the account (maximum 25% per coin)" in rejected(big)


def test_max_total_exposure():
    # Two positions that have grown to 30% each (60%), plus a new 20% one = 80% > 75%
    held = (HeldPosition("A", "long", 300.0), HeldPosition("B", "long", 300.0))
    assert "add up to 80% of the account (maximum 75%)" in rejected(positions=held)
    # Exactly at the limit is fine: 50% held + 25% new = 75%
    held = (HeldPosition("A", "long", 250.0), HeldPosition("B", "long", 250.0))
    assert RM.check(replace(BUY, qty=1.25), replace(CTX, positions=held)).approved


def test_leverage_is_hard_capped_at_1x():
    rm = RiskManager(RiskSettings(max_total_exposure_pct=100))
    held = (HeldPosition("A", "long", 250.0), HeldPosition("B", "long", 250.0), )
    ctx = replace(CTX, positions=held, equity_usd=600.0, peak_equity_usd=600.0, day_start_equity_usd=600.0)
    decision = rm.check(replace(BUY, qty=0.75), ctx)  # 500 + 150 = 650 on a 600 account = 1.08x
    assert not decision.approved
    assert "leverage is capped at 1x" in decision.reason


def test_not_enough_cash():
    assert "only US$100.00 of cash is free" in rejected(free_cash_usd=100.0)


def test_shorts_blocked_when_switched_off():
    short = Order("ETH", "sell", 1.0, 200.0, reduce_only=False, reason="test", stop_price=210.0)
    assert "short selling is switched off" in rejected(short)
    assert RM.check(short, replace(CTX, allow_shorts=True)).approved


def test_every_new_position_needs_a_sensible_stop():
    assert "has no stop loss" in rejected(replace(BUY, stop_price=None))
    assert "wrong side of the price" in rejected(replace(BUY, stop_price=205.0))


def test_minimum_order_size():
    assert "below the US$10 minimum order" in rejected(replace(BUY, qty=0.04))


def test_alerts():
    assert RM.alerts(CTX) == []
    assert RM.alerts(replace(CTX, equity_usd=850.0, day_start_equity_usd=850.0)) == [Alert.KILL]
    assert RM.alerts(replace(CTX, equity_usd=969.0)) == [Alert.DAILY_LIMIT]
    assert RM.alerts(replace(CTX, equity_usd=969.0, daily_limit_hit=True)) == []  # already latched
    assert RM.alerts(replace(CTX, equity_usd=800.0, state=BotState.KILLED, day_start_equity_usd=800.0)) == []
