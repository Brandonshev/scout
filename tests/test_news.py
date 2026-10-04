"""Crypto news headlines: reading feeds, matching coins, and the experiment's news safety check.
Feeds are always faked: nothing is fetched from the internet."""

import httpx
import pytest

from scout import news
from scout.config import NewsFeed, NewsSettings
from scout.news import Headline, judge, parse_feed
from tests.test_experiment import T0, book, lab, pos, spot  # noqa: F401  (lab is a fixture)

pytestmark = pytest.mark.anyio

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>MOON token &amp; team accused of &lt;b&gt;rug pull&lt;/b&gt;</title><link>https://example.com/a</link>
<pubDate>Tue, 29 Sep 2026 03:15:00 GMT</pubDate></item>
<item><title>No date here</title><link>https://example.com/b</link></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Solana hits a record</title><link href="https://example.com/c"/><updated>2026-09-29T04:00:00Z</updated></entry>
</feed>"""


def h(title, t=T0, source="Test"):
    return Headline(source, title, f"https://example.com/{abs(hash(title))}", t)


def test_rss_and_atom_feeds():
    [item] = parse_feed(RSS, "CoinDesk")  # the undated item is skipped
    assert item.title == "MOON token & team accused of rug pull"  # entities and tags cleaned up
    assert (item.source, item.url, item.published_ms) == ("CoinDesk", "https://example.com/a", 1790651700000)
    [entry] = parse_feed(ATOM, "Decrypt")
    assert (entry.title, entry.url, entry.published_ms) == ("Solana hits a record", "https://example.com/c",
                                                            1790654400000)


@pytest.mark.parametrize(("title", "coin", "name", "hit"), [
    ("MOON surges 80% after listing", "@7", "MOON", True),  # spot tokens by name
    ("Traders pile into $MOON", "@7", "MOON", True),
    ("Fly me to the moon", "@7", "MOON", False),  # tickers must be in capitals
    ("Solana network upgrade goes live", "SOL", None, True),  # full names count too
    ("PEPE rallies as memecoins return", "kPEPE", None, True),  # kPEPE = 1,000 PEPE
    ("SEC delays decision", "SEC", None, False),  # everyday capitals don't count as a coin
    ("MOONSHOT raises $5M", "@7", "MOON", False),  # whole words only
])
def test_which_coin_a_headline_is_about(title, coin, name, hit):
    assert h(title).mentions(coin, name) is hit


@pytest.mark.parametrize(("title", "serious"), [
    ("ZORB developers vanish in apparent rug pull", ["rug pull"]),
    ("ZORB hacked: $4M drained from bridge", ["hacked", "drained"]),
    ("Binance to delist ZORB next week", ["delist"]),
    ("ZORB hackathon winners announced", []),  # not a hack
    ("ZORB plunges 40% overnight", []),  # a price move: noted, not acted on
])
def test_serious_words(title, serious):
    assert h(title).serious == serious


def test_big_coins_named_in_hack_stories_are_not_in_danger():
    headlines = [h("Hacker moves $83 million in stolen XRP"), h("ZORB exploited for $3M")]
    xrp, zorb = judge("XRP", None, headlines), judge("ZORB", None, headlines)
    assert xrp.serious and not xrp.danger and "no action" in xrp.why
    assert zorb.danger and zorb.why.startswith("news warning (exploited)")


def test_quiet_and_positive_news():
    verdict = judge("ZORB", None, [h("ZORB lists on Coinbase"), h("Bitcoin steady")])
    assert not verdict.danger and verdict.why == "in the news: “ZORB lists on Coinbase” (Test)"
    assert judge("ZORB", None, []).why == ""


async def test_one_broken_feed_does_not_stop_the_others():
    def handle(request):
        if "broken" in str(request.url):
            return httpx.Response(503)
        assert request.headers["user-agent"].startswith("Scout/")
        return httpx.Response(200, text=RSS)

    cfg = NewsSettings(feeds=[NewsFeed(name="Good", url="https://good.example/rss"),
                              NewsFeed(name="Bad", url="https://broken.example/rss")])
    headlines, errors = await news.fetch_headlines(cfg, transport=httpx.MockTransport(handle))
    assert [x.source for x in headlines] == ["Good"] and list(errors) == ["Bad"]


def test_feeds_must_be_https():
    with pytest.raises(ValueError, match="https"):
        NewsFeed(name="x", url="http://insecure.example/rss")


# ------------------------------------------------------------ in the experiment


def feed_of(*headlines):
    async def fetch():
        return list(headlines), {}
    return fetch


def moon(lab_):
    exp, engine, client, clock = lab_
    client.spot = [spot("@7", "MOON", price=1.0, prev=0.6)]
    client.books["@7"] = book([(0.99, 5000)], [(1.00, 5000)])
    engine.prices.update({"@7": 1.0}, clock())


async def test_a_rug_pull_headline_blocks_the_buy(lab):
    exp, engine, client, clock = lab
    moon(lab)
    exp.news_fetcher = feed_of(h("MOON team accused of rug pull", clock.ms - 3_600_000))
    await exp.refresh_news()
    await exp.scan_high_risk()
    await exp.scan_high_risk()
    assert engine.account.positions() == []
    [row] = engine.conn.execute("SELECT action, name, strategy, price FROM news_decisions").fetchall()
    assert tuple(row) == ("veto", "MOON", "high_risk", 1.0)  # recorded once, not every scan


async def test_old_warnings_expire(lab):
    exp, engine, client, clock = lab
    moon(lab)
    exp.news_fetcher = feed_of(h("MOON team accused of rug pull", clock.ms - 72 * 3_600_000))  # 3 days ago
    await exp.refresh_news()
    await exp.scan_high_risk()
    assert len(engine.account.positions()) == 1


async def test_bad_news_sells_a_held_coin_and_is_checked_later(lab):
    exp, engine, client, clock = lab
    moon(lab)
    exp.news_fetcher = feed_of(h("MOON listed on a big exchange", clock.ms))
    await exp.refresh_news()
    await exp.scan_high_risk()
    [p] = engine.account.positions()
    assert "In the news: “MOON listed on a big exchange”" in engine.notifier.messages[-1][2]
    exp.news_fetcher = feed_of(h("MOON contract exploited, liquidity drained", clock.ms))
    await exp.refresh_news()
    assert engine.account.positions() == []
    reason = engine.conn.execute("SELECT close_reason FROM demo_positions").fetchone()[0]
    assert reason.startswith("[HIGH-RISK] 📰 Selling MOON early: news warning (exploited, drained)")
    category, priority, text = engine.notifier.messages[-1]
    assert priority.name == "NORMAL" and "https://example.com/" in text  # an alert straight away, with the link
    engine.prices.update({"@7": 0.4}, clock())  # it then fell 60%
    from scout.experiment import scorecard_text
    assert "News: blocked 0 buys, sold 1 early; since then 1 of 1 of those coins fell, average -60.0%" in \
        scorecard_text(engine, exp.followed)


async def test_copy_trading_skips_longs_in_trouble_but_not_shorts(lab):
    exp, engine, client, clock = lab
    await exp.refresh_wallets()
    engine.prices.update({"ZORB": 10.0, "BLIP": 10.0}, clock())
    exp.news_fetcher = feed_of(h("ZORB and BLIP hacked", clock.ms))
    await exp.refresh_news()
    await exp.poll_wallets()
    client.positions["0xwallet"] = [pos("ZORB", 1000), pos("BLIP", -1000)]  # a long and a short
    clock.ms += 20_000
    await exp.poll_wallets()
    assert [(p.coin, p.side) for p in engine.account.positions()] == [("BLIP", "short")]  # bad news suits a short


async def test_news_can_be_switched_off(lab):
    exp, engine, client, clock = lab
    exp.settings = exp.settings.model_copy(update={"news": NewsSettings(enabled=False)})
    assert "news" not in [j.name for j in exp.jobs()]
    exp.headlines = [h("MOON rug pull", clock.ms)]
    assert not exp.verdict("@7", "MOON").danger


async def test_headlines_are_stored_once_and_survive_a_restart(lab):
    exp, engine, client, clock = lab
    exp.news_fetcher = feed_of(h("A", clock.ms), h("B", clock.ms))
    await exp.refresh_news()
    await exp.refresh_news()
    assert engine.conn.execute("SELECT COUNT(*) FROM news_headlines").fetchone()[0] == 2
    from scout.experiment import Experiment
    again = Experiment(engine, lambda _: client, echo=lambda _: None)
    assert sorted(x.title for x in again.headlines) == ["A", "B"]
