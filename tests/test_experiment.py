"""The experiment: copy trading and high-risk coins, with a fake exchange (nothing real is touched)."""

import pytest

from scout import copytrade, highrisk
from scout.config import CopySettings, HighRiskSettings
from scout.data import L2Book, MarketCoin, SpotCoin, WalletPosition
from scout.db import open_db
from scout.demo import DemoEngine
from scout.experiment import Experiment, experiment_settings, scorecard_text
from scout.notify import Priority
from scout.risk import BotState, Order, RiskContext, RiskManager
from scout.service import build_plist, heartbeat_path

pytestmark = pytest.mark.anyio
T0 = 1_780_000_000_000
DAY = 86_400_000
COPY = CopySettings()
RISKY = HighRiskSettings()


def book(bids, asks, coin="X"):
    return L2Book.model_validate({"coin": coin, "time": 0, "levels": [
        [{"px": str(p), "sz": str(s), "n": 1} for p, s in bids],
        [{"px": str(p), "sz": str(s), "n": 1} for p, s in asks]]})


def fill(coin, closed_pnl, t, fee=0.0):
    return {"coin": coin, "closedPnl": str(closed_pnl), "time": t, "fee": str(fee)}


# ------------------------------------------------------------ picking wallets


def leaderboard_row(wallet, account, month_pnl, all_pnl=1.0, week_vlm=1.0):
    return {"ethAddress": wallet, "accountValue": str(account), "windowPerformances": [
        ["day", {"pnl": "0", "roi": "0", "vlm": "0"}], ["week", {"pnl": "0", "roi": "0", "vlm": str(week_vlm)}],
        ["month", {"pnl": str(month_pnl), "roi": "0", "vlm": "1"}], ["allTime", {"pnl": str(all_pnl), "roi": "0", "vlm": "1"}]]}


def test_leaderboard_is_only_a_first_filter():
    rows = [leaderboard_row("0xbig", 1e6, 50_000), leaderboard_row("0xsmall", 5_000, 4_000),
            leaderboard_row("0xloser", 1e6, -10), leaderboard_row("0xidle", 1e6, 9e4, week_vlm=0),
            leaderboard_row("0xbest", 2e5, 40_000)]
    assert copytrade.leaderboard_candidates(rows, COPY) == [("0xbest", 2e5), ("0xbig", 1e6)]


def steady_trader(n=30, win=300.0, loss=-100.0):
    """A trader who wins 2 of every 3 trades, spread over the 30 days."""
    return [fill("ETH", win if i % 3 else loss, T0 - (i + 1) * DAY, fee=5) for i in range(n)]


def test_a_consistent_trader_is_followed():
    s = copytrade.score_wallet("0xgood", 100_000, steady_trader(27), T0, COPY)
    assert s.excluded == ""
    assert s.pnl_usd == pytest.approx(18 * 300 - 9 * 100 - 27 * 5)
    assert s.profit_factor == pytest.approx(5400 / 900)
    assert s.good_windows == 3 and s.score > 0


@pytest.mark.parametrize(("fills", "why"), [
    ([fill("ETH", 1, T0 - DAY)] * 2500, "market-making bot"),
    (steady_trader(5), "only 5 closed trades"),
    (steady_trader(30, win=10, loss=-100), "lost money"),
    ([fill("hyna:FARTCOIN", 500, T0 - (i + 1) * DAY) for i in range(30)], "only 0 closed trades"),
    ([fill("ETH", 5000, T0 - DAY)] * 12 + [fill("ETH", -100, T0 - 25 * DAY)] * 12, "inconsistent"),
])
def test_wallets_that_are_not_followed(fills, why):
    assert why in copytrade.score_wallet("0x", 100_000, fills, T0, COPY).excluded


def test_pick_the_best():
    a = copytrade.score_wallet("0xa", 100_000, steady_trader(30), T0, COPY)
    b = copytrade.score_wallet("0xb", 1_000_000, steady_trader(30), T0, COPY)  # same profit, bigger account
    assert [s.wallet for s in copytrade.pick_wallets([b, a], 1)] == ["0xa"]
    assert copytrade.wallets_from_json(copytrade.wallets_to_json([a])) == [a]


# ------------------------------------------------------------ following


def pos(coin, size, value=50_000.0):
    return WalletPosition(coin, size, 100.0, value)


def test_first_look_copies_nothing():
    assert copytrade.diff_positions("w", None, [pos("ETH", 1)], 1e6, COPY) == []


def test_new_positions_and_closes():
    before = copytrade.snapshot([pos("ETH", 1), pos("SOL", -5)])
    changes = copytrade.diff_positions("w", before, [pos("SOL", 5), pos("BTC", 1)], 1e6, COPY)
    assert {(c.coin, c.action, c.side) for c in changes} == {
        ("ETH", "close", "long"),  # closed
        ("SOL", "close", "short"), ("SOL", "open", "long"),  # flipped
        ("BTC", "open", "long"),  # new
    }


def test_small_dex_and_short_positions_can_be_ignored():
    before = copytrade.snapshot([])
    current = [pos("ETH", 1, value=100.0), pos("xyz:TSLA", 1), pos("SOL", -1)]
    assert copytrade.diff_positions("w", before, current, 1e6, COPY) == [
        copytrade.Change("w", "SOL", "open", "short", 50_000.0, 100.0)]
    no_shorts = CopySettings(allow_shorts=False)
    assert copytrade.diff_positions("w", before, current, 1e6, no_shorts) == []


# ------------------------------------------------------------ high-risk scanner


def spot(pair, name, price=1.0, prev=0.8, vol=100_000.0):
    return SpotCoin(pair, name, price, prev, vol, 2)


def perp(coin, change_to=1.3, vol=500_000.0):
    return MarketCoin(coin, False, 5, change_to, change_to, 1.0, vol, 1000.0, 0.0001, 1)


def test_prefilter_finds_small_fast_risers():
    found = highrisk.prefilter(
        [spot("@1", "MOON", prev=0.5), spot("@2", "FLAT", prev=0.99), spot("@3", "HUGE", vol=9e6), spot("@4", "HELD")],
        [perp("PEPE2", 1.4), perp("BIG", 1.4, vol=5e7), perp("dex:X", 1.9)],
        RISKY, skip={"@4"}, core_min_volume=20_000_000)
    assert [c.name for c in found] == ["MOON", "PEPE2"]  # +100%, +40%


def test_final_checks_use_the_order_book_and_history():
    candidate = highrisk.Candidate("@1", "MOON", "spot", 1.0, 40.0, 100_000.0, None)
    deep = book([(0.99, 2000), (0.97, 2000)], [(1.01, 5000)])
    assert highrisk.check(candidate, deep, [], RISKY)[0]  # no history: a new coin, allowed
    assert "0.5x usual" in highrisk.check(candidate, deep, [200_000] * 5, RISKY)[2]
    assert "we couldn't get out" in highrisk.check(candidate, book([(0.99, 10)], [(1.01, 10)]), [], RISKY)[2]
    assert "spread" in highrisk.check(candidate, book([(0.80, 5000)], [(1.20, 5000)]), [], RISKY)[2]


def test_order_book_fills():
    b = book([(0.99, 100), (0.90, 100)], [(1.00, 50), (1.10, 100)])
    qty, avg = highrisk.fill_buy(b, 105.0)  # US$50 at 1.00, then US$55 at 1.10
    assert qty == pytest.approx(50 + 50) and avg == pytest.approx(1.05)
    assert highrisk.fill_buy(b, 10_000) is None  # not enough sellers
    assert highrisk.fill_sell(b, 150) == (pytest.approx((100 * 0.99 + 50 * 0.90) / 150), True)
    price, enough = highrisk.fill_sell(b, 400)  # 200 coins can't be sold: half the last price
    assert not enough and price == pytest.approx((99 + 90 + 200 * 0.45) / 400)


def test_exit_rules():
    assert highrisk.exit_reason(1.0, 1.5, 1, RISKY).startswith("hit the +50% profit target")
    assert highrisk.exit_reason(1.0, 0.2, 48, RISKY).startswith("held 48 hours")
    assert highrisk.exit_reason(1.0, 0.2, 10, RISKY) is None  # no stop loss: it rides


# ------------------------------------------------------------ no stop losses, but still limits


def test_the_experiment_allows_trades_without_stops_but_keeps_limits(settings):
    xs = experiment_settings(settings)
    rm = RiskManager(xs.risk)
    ctx = RiskContext(BotState.RUNNING, 700, 700, 700, 700, False, (), 1.0, True)
    assert rm.check(Order("ETH", "buy", 0.03, 2000, False, "t"), ctx).approved  # 60 = 8.6%: fine, no stop
    too_big = rm.check(Order("ETH", "buy", 0.05, 2000, False, "t"), ctx)
    assert not too_big.approved and "maximum 10% per coin" in too_big.reason
    core = RiskManager(settings.risk)
    assert "no stop loss" in core.check(Order("ETH", "buy", 0.03, 2000, False, "t"), ctx).reason  # main demo


# ------------------------------------------------------------ the experiment, end to end


class FakeClient:
    def __init__(self):
        self.positions: dict[str, list[WalletPosition]] = {}
        self.books: dict[str, L2Book] = {}
        self.spot: list[SpotCoin] = []
        self.perps: list[MarketCoin] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def positions_of(self, wallet):
        return self.positions.get(wallet, [])

    async def fills_of(self, wallet, start_ms):
        return steady_trader(30)

    async def market(self):
        return self.perps

    async def spot_market(self):
        return self.spot

    async def l2_book(self, coin, sig_figs=None):
        return self.books[coin]

    async def candles(self, coin, interval, start, end):
        return []


class Clock:
    def __init__(self):
        self.ms = T0

    def __call__(self):
        return self.ms


class Outbox:
    enabled = True

    def __init__(self):
        self.messages = []

    def notify(self, text, category, priority=None):
        self.messages.append((category, priority, text))


@pytest.fixture
def lab(settings, tmp_path):
    xs = experiment_settings(settings.model_copy(update={
        "experiment": settings.experiment.model_copy(update={"db_path": tmp_path / "experiment.db"})}))
    clock = Clock()
    conn = open_db(xs.app.db_path)
    engine = DemoEngine(xs, conn, clock=clock)
    engine.notifier = Outbox()
    client = FakeClient()

    async def leaderboard():
        return [leaderboard_row("0xwallet", 1e6, 50_000)]

    exp = Experiment(engine, lambda _: client, leaderboard, echo=lambda _: None)
    yield exp, engine, client, clock
    conn.close()


async def test_copy_trading_follows_opens_and_closes(lab):
    exp, engine, client, clock = lab
    await exp.refresh_wallets()
    assert [s.wallet for s in exp.followed] == ["0xwallet"]
    engine.prices.update({"ETH": 2000.0, "SOL": 100.0}, clock())
    client.positions["0xwallet"] = [pos("ETH", 50)]  # already open when we start watching
    await exp.poll_wallets()
    assert engine.account.positions() == []  # too late to copy that one
    client.positions["0xwallet"] = [pos("ETH", 50), pos("SOL", 400)]  # a new position
    clock.ms += 20_000
    await exp.poll_wallets()
    [copied] = engine.account.positions()
    assert (copied.coin, copied.strategy, copied.source) == ("SOL", "copy", "0xwallet")
    start = engine.settings.demo.starting_balance_usdc
    assert copied.qty * copied.entry_price == pytest.approx(start * 0.07, rel=0.02)  # 7% of the account
    assert not copied.has_stop
    assert engine.notifier.messages[-1][1] is Priority.BATCH and "[COPY]" in engine.notifier.messages[-1][2]
    client.positions["0xwallet"] = [pos("ETH", 50)]  # the wallet sold SOL
    clock.ms += 20_000
    await exp.poll_wallets()
    assert engine.account.positions() == []
    closed = engine.conn.execute("SELECT close_reason FROM demo_positions").fetchone()[0]
    assert "the wallet we copied" in closed


async def test_no_late_copies_after_a_gap(lab):
    exp, engine, client, clock = lab
    await exp.refresh_wallets()
    engine.prices.update({"SOL": 100.0}, clock())
    await exp.poll_wallets()
    client.positions["0xwallet"] = [pos("SOL", 400)]
    clock.ms += 3_600_000  # the Mac slept for an hour
    engine.prices.update({"SOL": 100.0}, clock())
    await exp.poll_wallets()
    assert engine.account.positions() == []


async def test_copy_budget_is_respected(lab):
    exp, engine, client, clock = lab
    await exp.refresh_wallets()
    coins = [f"C{i}" for i in range(15)]
    engine.prices.update(dict.fromkeys(coins, 10.0), clock())
    await exp.poll_wallets()
    client.positions["0xwallet"] = [pos(c, 1000) for c in coins]
    clock.ms += 20_000
    await exp.poll_wallets()
    copy_value = sum(p.qty * p.entry_price for p in engine.account.positions())
    assert copy_value <= engine.settings.demo.starting_balance_usdc * 0.70 + 1  # never more than 70% in copies


async def test_high_risk_buys_from_the_book_and_sells_at_the_target(lab):
    exp, engine, client, clock = lab
    client.spot = [spot("@7", "MOON", price=1.0, prev=0.6)]
    client.books["@7"] = book([(0.99, 5000), (0.98, 5000)], [(1.00, 20), (1.02, 5000)])
    engine.prices.update({"@7": 1.0}, clock())
    await exp.scan_high_risk()
    [p] = engine.account.positions()
    assert (p.strategy, p.source) == ("high_risk", "MOON")
    assert 1.0 < p.entry_price < 1.02  # walked past the first thin level
    assert p.qty * p.entry_price == pytest.approx(engine.settings.demo.starting_balance_usdc * 0.06, rel=0.05)
    engine.prices.update({"@7": 1.6}, clock())
    client.books["@7"] = book([(1.58, 5000)], [(1.62, 5000)])
    await exp.high_risk_exits()
    row = engine.conn.execute("SELECT exit_price, pnl_usd, close_reason FROM demo_positions").fetchone()
    assert row["exit_price"] == pytest.approx(1.58, rel=0.002) and row["pnl_usd"] > 0
    assert "profit target" in row["close_reason"]
    assert "High-risk coins: +A$" in scorecard_text(engine, exp.followed)


async def test_high_risk_wont_rebuy_the_same_coin_the_same_day(lab):
    exp, engine, client, clock = lab
    client.spot = [spot("@7", "MOON", price=1.0, prev=0.6)]
    client.books["@7"] = book([(0.99, 5000)], [(1.00, 5000)])
    engine.prices.update({"@7": 1.0}, clock())
    await exp.scan_high_risk()
    [p] = engine.account.positions()
    await engine.close(p, "test")
    await exp.scan_high_risk()
    assert engine.account.positions() == []


def test_experiment_has_its_own_heartbeat_and_service(settings, tmp_path):
    xs = experiment_settings(settings)
    assert heartbeat_path(xs).name == "heartbeat-experiment.json" != heartbeat_path(settings).name
    plist = build_plist(settings, tmp_path / "scout", tmp_path, tmp_path / "c", tmp_path / "e",
                        command=("experiment", "run"), label="au.scout.demo.experiment")
    assert plist["Label"] == "au.scout.demo.experiment"
    assert plist["ProgramArguments"][1:3] == ["experiment", "run"]
    assert plist["StandardErrorPath"].endswith("experiment.err.log")


def test_experiment_settings(settings):
    xs = experiment_settings(settings)
    assert xs.app.db_path.name == "experiment.db"
    assert xs.risk.require_stop is False and xs.risk.max_position_pct == 10
    assert xs.risk.max_leverage == 1.0  # still no borrowing
    assert settings.risk.require_stop is True  # the main demo is unchanged


def test_hidden_open_losses_rule_a_wallet_out():
    """Sells every winner, never closes a loser: 100% win rate on closed trades, but a big open loss."""
    winners_only = [fill("ETH", 200, T0 - (i + 1) * DAY) for i in range(30)]
    flattering = copytrade.score_wallet("0xhider", 100_000, winners_only, T0, COPY)
    assert flattering.excluded == "" and flattering.win_rate_pct == 100
    honest = copytrade.score_wallet("0xhider", 100_000, winners_only, T0, COPY, open_pnl_usd=-25_000)
    assert "open losses of 25%" in honest.excluded
    smaller = copytrade.score_wallet("0xhider", 100_000, winners_only, T0, COPY, open_pnl_usd=-5_000)
    assert smaller.excluded == "" and smaller.return_pct == pytest.approx(1.0)  # (6,000 - 5,000) / 100,000


def test_accounts_are_named_in_alerts_and_the_scorecard(lab):
    from scout.notify import Notifier
    exp, engine, client, clock = lab
    header = Notifier(engine.conn, engine.settings, [], label=engine.settings.experiment.name).header()
    assert header.endswith("· 70/30:2 · DEMO")
    card = scorecard_text(engine, exp.followed)
    assert card.startswith("🧪 70/30:2 scorecard")
