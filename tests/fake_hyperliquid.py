"""An offline stand-in for Hyperliquid's /info endpoint, built on recorded fixtures."""

from __future__ import annotations

import json

import httpx

from scout.data import INTERVAL_MS, MAX_CANDLES_AVAILABLE
from tests.conftest import FIXTURES, load_fixture


def synthetic_candle(coin: str, interval: str, open_ms: int) -> dict:
    price = 100.0 + (open_ms // INTERVAL_MS[interval]) % 50
    return {
        "t": open_ms,
        "T": open_ms + INTERVAL_MS[interval] - 1,
        "s": coin,
        "i": interval,
        "o": str(price),
        "c": str(price + 1),
        "h": str(price + 2),
        "l": str(price - 1),
        "v": "12.5",
        "n": 42,
    }


class FakeHyperliquid:
    """Answers info requests like the real API.

    Candles are generated for any range, but (like the real API) only the latest
    5000 exist, and each response is capped at `page_size`.
    """

    def __init__(self, now_ms: int, page_size: int = 5000) -> None:
        self.now_ms = now_ms
        self.page_size = page_size
        self.requests: list[dict] = []
        self.failures: list[int] = []  # status codes to return before succeeding
        self.listed_from: dict[str, int] = {}  # coin -> first candle time (listed later than the 5000 limit)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.failures:
            return httpx.Response(self.failures.pop(0), text="try later")
        match body["type"]:
            case "metaAndAssetCtxs":
                return httpx.Response(200, json=load_fixture("meta_and_asset_ctxs.json"))
            case "allMids":
                return httpx.Response(200, json=load_fixture("all_mids.json"))
            case "candleSnapshot":
                return httpx.Response(200, json=self.candles(body["req"]))
            case "l2Book":
                return httpx.Response(200, json=self.book(body["coin"], body.get("nSigFigs")))
        return httpx.Response(422, text="unknown request type")

    def book(self, coin: str, sig_figs: int | None) -> dict:
        """The recorded thin book for the thin coin, BTC's deep book for everything else."""
        name = coin if (FIXTURES / f"l2_book_{coin}.json").exists() else "BTC"
        suffix = "_sig3" if sig_figs == 3 else ""
        return load_fixture(f"l2_book_{name}{suffix}.json") | {"coin": coin}

    def candles(self, req: dict) -> list[dict]:
        step = INTERVAL_MS[req["interval"]]
        newest = self.now_ms // step * step
        oldest = newest - (MAX_CANDLES_AVAILABLE - 1) * step
        first = max(oldest, -(-req["startTime"] // step) * step, self.listed_from.get(req["coin"], 0))
        out = []
        t = first
        while t <= min(req["endTime"], newest) and len(out) < self.page_size:
            out.append(synthetic_candle(req["coin"], req["interval"], t))
            t += step
        return out
