"""High-risk small coins: a sudden burst of volume and price, bought small, sold at a profit target or
after a time limit. Most of these lose; this measures whether the few that don't make up for it.

Fills are simulated by walking the REAL order book (buying takes the cheapest sell orders, then the
next ones...), so a thin coin costs what it really would. If there aren't enough buyers when we sell,
the rest is assumed to sell at half the last price: in a rug pull, the buyers vanish.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scout.config import HighRiskSettings
from scout.data import L2Book, MarketCoin, SpotCoin, format_price


@dataclass(frozen=True)
class Candidate:
    coin: str  # the trading name: a perp ("PEPE") or a spot pair ("@107")
    name: str  # what people call it
    kind: str  # "spot" or "perp"
    price: float
    change_pct: float
    volume_usd: float
    volume_ratio: float | None  # 24h volume vs its usual; None if there's no history (new or quiet coin)
    spread_pct: float = 0.0
    exit_depth_usd: float = 0.0

    @property
    def why(self) -> str:
        surge = f"{self.volume_ratio:.1f}x its usual volume" if self.volume_ratio else "no volume history (new coin)"
        return (f"{self.name} ({self.kind}) is up {self.change_pct:+.0f}% in 24h on US${self.volume_usd:,.0f} "
                f"traded, {surge}; US${self.exit_depth_usd:,.0f} of buyers within 5%")


def prefilter(spot: Sequence[SpotCoin], perps: Sequence[MarketCoin], cfg: HighRiskSettings,
              skip: set[str], core_min_volume: float) -> list[Candidate]:
    """Cheap first pass on the market tables: small, active and rising fast."""
    found = []
    if cfg.include_spot:
        for c in spot:
            if (c.pair not in skip and cfg.min_volume_usd <= c.volume_24h_usd <= cfg.max_volume_usd
                    and c.change_24h_pct >= cfg.min_change_pct):
                found.append(Candidate(c.pair, c.name, "spot", c.mark_price, c.change_24h_pct, c.volume_24h_usd, None))
    if cfg.include_perps:
        for c in perps:
            if (not c.is_delisted and c.coin not in skip and ":" not in c.coin
                    and c.volume_24h_usd < min(cfg.max_volume_usd, core_min_volume)
                    and c.volume_24h_usd >= cfg.min_volume_usd and c.change_24h_pct >= cfg.min_change_pct):
                found.append(Candidate(c.coin, c.coin, "perp", c.mark_price, c.change_24h_pct, c.volume_24h_usd, None))
    return sorted(found, key=lambda c: -c.change_pct)


def usual_volume_ratio(volume_24h: float, daily_volumes: Sequence[float]) -> float | None:
    """24h volume vs the average of earlier days (None if under 3 days of history)."""
    if len(daily_volumes) < 3:
        return None
    usual = sum(daily_volumes) / len(daily_volumes)
    return volume_24h / usual if usual > 0 else None


def book_depth(book: L2Book, band_pct: float = 5.0) -> tuple[float, float]:
    """(spread %, US$ of buy orders within band_pct below the price)."""
    if not book.bids or not book.asks:
        return 100.0, 0.0
    bid, ask = book.bids[0].price, book.asks[0].price
    mid = (bid + ask) / 2
    floor = mid * (1 - band_pct / 100)
    depth = sum(level.price * level.size for level in book.bids if level.price >= floor)
    return (ask - bid) / mid * 100, depth


def check(candidate: Candidate, book: L2Book, daily_volumes: Sequence[float], cfg: HighRiskSettings) -> tuple[bool, Candidate, str]:
    """Final checks with the order book and history. Returns (buy?, candidate with details, why not)."""
    spread, depth = book_depth(book)
    ratio = usual_volume_ratio(candidate.volume_usd, daily_volumes)
    detailed = Candidate(candidate.coin, candidate.name, candidate.kind, candidate.price, candidate.change_pct,
                         candidate.volume_usd, ratio, spread, depth)
    if spread > cfg.max_spread_pct:
        return False, detailed, f"spread {spread:.1f}% (max {cfg.max_spread_pct:g}%)"
    if depth < cfg.min_exit_depth_usd:
        return False, detailed, f"only US${depth:,.0f} of buyers within 5%: we couldn't get out"
    if ratio is not None and ratio < cfg.volume_surge:
        return False, detailed, f"volume only {ratio:.1f}x usual (need {cfg.volume_surge:g}x)"
    return True, detailed, ""


def fill_buy(book: L2Book, spend_usd: float) -> tuple[float, float] | None:
    """Spend `spend_usd` buying from the cheapest sell orders up. Returns (coins, average price),
    or None if the book is too thin to fill it."""
    left, qty = spend_usd, 0.0
    for level in book.asks:
        take = min(left, level.price * level.size)
        qty += take / level.price
        left -= take
        if left <= 1e-9:
            return qty, spend_usd / qty
    return None


def fill_sell(book: L2Book, qty: float) -> tuple[float, bool]:
    """Sell `qty` into the highest buy orders down. Returns (average price, enough buyers?).
    Anything the book can't absorb is assumed sold at half the last price."""
    left, proceeds, last = qty, 0.0, None
    for level in book.bids:
        take = min(left, level.size)
        proceeds += take * level.price
        left -= take
        last = level.price
        if left <= 1e-12:
            return proceeds / qty, True
    if last is None:
        return 0.0, False
    proceeds += left * last * 0.5
    return proceeds / qty, False


def exit_reason(entry: float, price: float, held_hours: float, cfg: HighRiskSettings) -> str | None:
    """Why a high-risk position should be sold now, or None to keep holding."""
    if price >= entry * (1 + cfg.take_profit_pct / 100):
        return f"hit the +{cfg.take_profit_pct:g}% profit target (${format_price(price)})"
    if held_hours >= cfg.max_hold_hours:
        return f"held {cfg.max_hold_hours:g} hours (the time limit), price ${format_price(price)}"
    return None


def names(spot: Sequence[SpotCoin]) -> Mapping[str, str]:
    """Spot pair -> token name, e.g. "@107" -> "HYPE"."""
    return {c.pair: c.name for c in spot}
