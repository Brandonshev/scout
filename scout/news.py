"""Free crypto news headlines (RSS feeds), used by the experiment as a safety check.

What it does with them:
- a SERIOUS headline about a smaller coin (hack, exploit, rug pull, scam, delisting, insolvency...) blocks
  buying it, and sells a long we already hold. That's the "get out before the rug pull" part;
- big established coins (BTC, ETH, XRP...) are exempt: they can't be rug-pulled, and they appear in hack
  stories every day as the stolen money ("hacker moves $83M in stolen XRP"), which says nothing about XRP;
- other headlines about a coin (price moves, listings, partnerships) are only quoted in the trade's reason.
  They don't decide anything: by the time a "price surges" headline is written, the move has usually happened.

Only headlines (title, source, link, time) are read and stored, never the articles themselves.

Matching is by keywords, so it's crude: it can miss a story or match the wrong one. Every news decision is
recorded, and the scorecard checks later whether the coins it blocked or sold actually fell.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from scout.config import NewsFeed, NewsSettings
from scout.version import APP_VERSION

log = logging.getLogger(__name__)
HOUR_MS = 3_600_000
MAX_FEED_BYTES = 3_000_000

# Headline words that mean real trouble for a coin (checked as whole words, any case).
SERIOUS = (
    "hack", "hacked", "hacker", "hackers", "exploit", "exploited", "exploiter", "drained",
    "rug pull", "rug pulls", "rugpull", "rug-pull", "rugged", "scam", "scammers", "fraud", "fraudulent",
    "ponzi", "exit scam", "stolen", "theft", "attacker", "delist", "delists", "delisted", "delisting",
    "insolvent", "insolvency", "bankrupt", "bankruptcy", "halts withdrawals", "suspends withdrawals",
    "pauses withdrawals", "freezes withdrawals", "depeg", "depegs", "depegged", "sues", "sued", "charged",
    "indicted", "arrested", "vulnerability", "backdoor", "compromised",
)
# Price-move words: noted, never acted on (they describe what already happened).
MOVES = ("crash", "crashes", "plunge", "plunges", "tumbles", "sinks", "dumps", "slides", "surges", "soars",
         "rallies", "jumps", "skyrockets", "pumps", "record high", "all-time high")

# Full names for tickers, so "Solana" matches SOL.
ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("Bitcoin",), "ETH": ("Ethereum", "Ether"), "SOL": ("Solana",), "XRP": ("Ripple",),
    "DOGE": ("Dogecoin",), "ADA": ("Cardano",), "AVAX": ("Avalanche",), "LINK": ("Chainlink",),
    "DOT": ("Polkadot",), "LTC": ("Litecoin",), "BNB": ("BNB Chain", "Binance Coin"), "TRX": ("Tron",),
    "SUI": ("Sui",), "APT": ("Aptos",), "ARB": ("Arbitrum",), "OP": ("Optimism",), "NEAR": ("Near Protocol",),
    "HYPE": ("Hyperliquid",), "TON": ("Toncoin",), "ATOM": ("Cosmos",), "UNI": ("Uniswap",), "AAVE": ("Aave",),
    "PEPE": ("Pepe",), "SHIB": ("Shiba Inu",), "WIF": ("dogwifhat",), "BONK": ("Bonk",), "XLM": ("Stellar",),
    "HBAR": ("Hedera",), "FIL": ("Filecoin",), "INJ": ("Injective",), "TIA": ("Celestia",), "SEI": ("Sei",),
    "ENA": ("Ethena",), "PENDLE": ("Pendle",), "LDO": ("Lido",), "MKR": ("Maker",), "CRV": ("Curve",),
    "JUP": ("Jupiter",), "PYTH": ("Pyth",), "ONDO": ("Ondo",), "TAO": ("Bittensor",), "RENDER": ("Render",),
    "FET": ("Fetch.ai",), "WLD": ("Worldcoin", "World Network"), "STX": ("Stacks",), "KAS": ("Kaspa",),
    "BCH": ("Bitcoin Cash",), "ETC": ("Ethereum Classic",), "POL": ("Polygon",), "IMX": ("Immutable",),
    "TRUMP": ("Official Trump", "$TRUMP"), "BERA": ("Berachain",), "VIRTUAL": ("Virtuals",),
}
# Big, long-established coins: news never blocks or sells them (see the top of this file).
ESTABLISHED = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BNB", "TRX", "TON",
               "ATOM", "XLM", "BCH", "ETC", "HBAR", "SUI", "UNI", "NEAR", "FIL", "APT", "ARB", "OP", "POL"}
# Tickers that are also everyday capitals in headlines ("SEC", "AI", "US"...): matched by full name only.
AMBIGUOUS = {"SEC", "ETF", "ETFS", "CEO", "US", "USA", "UK", "EU", "AI", "IPO", "DOGE", "GDP", "CPI", "FED", "FBI",
             "DOJ", "IRS", "API", "NFT", "DAO", "TVL", "ATH", "NEW", "BIG", "ONE", "ME", "IO", "ID", "HOT", "GAS",
             "MOVE", "NOT", "SUPER", "TOKEN", "BLAST", "BANANA", "OM", "S", "W", "G", "T"}


@dataclass(frozen=True)
class Headline:
    source: str
    title: str
    url: str
    published_ms: int

    @property
    def serious(self) -> list[str]:
        return _words_in(self.title, SERIOUS)

    @property
    def move(self) -> list[str]:
        return _words_in(self.title, MOVES)

    def mentions(self, coin: str, name: str | None = None) -> bool:
        return any(_mentioned(self.title, term, case) for term, case in search_terms(coin, name))

    @property
    def quote(self) -> str:
        return f"“{self.title}” ({self.source})"


def _words_in(text: str, words: Iterable[str]) -> list[str]:
    return [w for w in words if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text, re.IGNORECASE)]


def _mentioned(text: str, term: str, case_sensitive: bool) -> bool:
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.search(rf"(?<![\w$]){re.escape(term)}(?!\w)", text, flags) is not None or \
        re.search(rf"\${re.escape(term)}(?!\w)", text, flags) is not None


def base_ticker(coin: str) -> str:
    """"kPEPE" (Hyperliquid's 1,000-PEPE contract) -> "PEPE"."""
    return coin[1:] if len(coin) > 2 and coin[0] == "k" and coin[1:].isupper() else coin


def search_terms(coin: str, name: str | None = None) -> list[tuple[str, bool]]:
    """(term, case-sensitive?) to look for. Tickers must be in capitals ("SOL", not "sol"); names any case."""
    terms: list[tuple[str, bool]] = []
    for ticker in dict.fromkeys(base_ticker(t) for t in (coin, name) if t and not t.startswith("@")):
        if len(ticker) >= 3 and ticker.upper() not in AMBIGUOUS:
            terms.append((ticker.upper(), True))
        terms.extend((alias, False) for alias in ALIASES.get(ticker.upper(), ()))
    return terms


# ------------------------------------------------------------ reading feeds


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", "".join(element.itertext()))).strip()


def _when(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    try:
        moment = parsedate_to_datetime(text)  # RSS: "Tue, 29 Sep 2026 03:15:00 GMT"
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(text)  # Atom: "2026-09-29T03:15:00Z"
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def parse_feed(xml_text: str | bytes, source: str) -> list[Headline]:
    """Headlines from an RSS 2.0 or Atom feed. Items without a title or date are skipped."""
    root = ElementTree.fromstring(xml_text)
    found = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child for child in item}
        title = " ".join(_text(fields.get("title")).split())
        link = fields.get("link")
        url = (link.get("href") or _text(link)) if link is not None else ""
        when = None
        for key in ("pubDate", "published", "updated", "date"):
            if key in fields and (when := _when(_text(fields[key]))) is not None:
                break
        if title and when is not None:
            found.append(Headline(source, title, url.strip(), when))
    return found


async def fetch_feed(http: httpx.AsyncClient, feed: NewsFeed) -> list[Headline]:
    response = await http.get(feed.url)
    response.raise_for_status()
    if len(response.content) > MAX_FEED_BYTES:
        raise ValueError(f"feed too large ({len(response.content):,} bytes)")
    return parse_feed(response.content, feed.name)


async def fetch_headlines(cfg: NewsSettings, transport: httpx.AsyncBaseTransport | None = None
                          ) -> tuple[list[Headline], dict[str, str]]:
    """All feeds at once. Returns (headlines, {feed name: error}) — one broken feed doesn't stop the rest."""
    headers = {"User-Agent": f"Scout/{APP_VERSION} (personal crypto news reader)"}
    async with httpx.AsyncClient(timeout=cfg.timeout_seconds, headers=headers, follow_redirects=True,
                                 transport=transport) as http:
        results = await asyncio.gather(*(fetch_feed(http, f) for f in cfg.feeds), return_exceptions=True)
    headlines, errors = [], {}
    for feed, result in zip(cfg.feeds, results, strict=True):
        if isinstance(result, BaseException):
            errors[feed.name] = f"{type(result).__name__}: {result}"
            log.warning("news feed %s failed: %s", feed.name, result)
        else:
            headlines.extend(result)
    return headlines, errors


# ------------------------------------------------------------ storing and judging


def save(conn: sqlite3.Connection, headlines: Iterable[Headline], seen_ms: int) -> int:
    """Store new headlines (the caller commits). Returns how many were new."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO news_headlines (source, title, url, published_ms, seen_ms) VALUES (?, ?, ?, ?, ?)",
        [(h.source, h.title, h.url, h.published_ms, seen_ms) for h in headlines])
    return conn.total_changes - before


def recent(conn: sqlite3.Connection, since_ms: int) -> list[Headline]:
    rows = conn.execute("SELECT source, title, url, published_ms FROM news_headlines WHERE published_ms >= ? "
                        "ORDER BY published_ms DESC", (since_ms,))
    return [Headline(r["source"], r["title"], r["url"], r["published_ms"]) for r in rows]


@dataclass(frozen=True)
class Verdict:
    coin: str
    serious: list[Headline]  # trouble: block buying, sell longs (unless the coin is established)
    other: list[Headline]  # mentioned, nothing alarming
    established: bool = False

    @property
    def danger(self) -> bool:
        return bool(self.serious) and not self.established

    @property
    def why(self) -> str:
        if self.serious and self.established:
            return f"in the news (a big coin, so no action): {self.serious[0].quote}"
        if self.serious:
            h = self.serious[0]
            return f"news warning ({', '.join(h.serious)}): {h.quote}"
        if self.other:
            return f"in the news: {self.other[0].quote}" + (f" +{len(self.other) - 1} more" if len(self.other) > 1 else "")
        return ""


def judge(coin: str, name: str | None, headlines: Sequence[Headline]) -> Verdict:
    """What the headlines say about one coin (newest first)."""
    about = sorted((h for h in headlines if h.mentions(coin, name)), key=lambda h: -h.published_ms)
    return Verdict(coin, [h for h in about if h.serious], [h for h in about if not h.serious],
                   base_ticker(coin).upper() in ESTABLISHED)


def record_decision(conn: sqlite3.Connection, ts_ms: int, coin: str, name: str, strategy: str, action: str,
                    price: float, headline: Headline) -> None:
    """Remember a news-based decision so we can later check whether it was right (the caller commits)."""
    conn.execute(
        "INSERT INTO news_decisions (ts_ms, coin, name, strategy, action, price, headline, source, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts_ms, coin, name, strategy, action, price, headline.title, headline.source, headline.url))


def decided_recently(conn: sqlite3.Connection, coin: str, action: str, since_ms: int) -> bool:
    return conn.execute("SELECT 1 FROM news_decisions WHERE coin = ? AND action = ? AND ts_ms >= ?",
                        (coin, action, since_ms)).fetchone() is not None


def hindsight(conn: sqlite3.Connection, prices: Mapping[str, float]) -> dict:
    """How the coins moved since news made us skip or sell them. A fall means the news was right."""
    rows = conn.execute("SELECT action, coin, price FROM news_decisions").fetchall()
    moves = [(prices[r["coin"]] / r["price"] - 1) * 100 for r in rows if r["coin"] in prices and r["price"] > 0]
    return {"vetoes": sum(r["action"] == "veto" for r in rows), "exits": sum(r["action"] == "exit" for r in rows),
            "checked": len(moves), "avg_move_pct": sum(moves) / len(moves) if moves else None,
            "fell": sum(m < 0 for m in moves)}
