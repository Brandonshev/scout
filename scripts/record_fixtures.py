"""Record real Hyperliquid responses into tests/fixtures so tests run offline.

Run occasionally to refresh:  uv run python scripts/record_fixtures.py
Responses are trimmed to keep the fixtures small.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

API_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hyperliquid"
KEEP_ASSETS = 25


def save(name: str, data: object) -> None:
    path = OUT / name
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"saved {path.relative_to(OUT.parents[2])}")


def newest_listing(meta: dict, ctxs: list) -> int:
    """Index of the most recently listed coin that is trading (new coins are added at the end)."""
    return next(
        i for i in range(len(meta["universe"]) - 1, -1, -1)
        if not meta["universe"][i].get("isDelisted") and float(ctxs[i]["dayNtlVlm"]) > 0
    )


def trim_meta_and_ctxs(meta: dict, ctxs: list) -> list:
    # Keep the first assets, one delisted asset and the newest listing, so tests cover those cases.
    keep = list(range(KEEP_ASSETS))
    delisted = next((i for i, a in enumerate(meta["universe"]) if a.get("isDelisted")), None)
    for extra in (delisted, newest_listing(meta, ctxs)):
        if extra is not None and extra not in keep:
            keep.append(extra)
    universe = [meta["universe"][i] for i in keep]
    return [{"universe": universe}, [ctxs[i] for i in keep]]


def trim_mids(mids: dict, names: set[str]) -> dict:
    spot = [k for k in mids if k.startswith("@") or "/" in k][:3]
    return {k: v for k, v in mids.items() if k in names or k in spot}


async def record_ws_message() -> dict:
    async with connect(WS_URL) as ws:
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        while True:
            message = json.loads(await ws.recv())
            if message.get("channel") == "allMids":
                return message


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=15) as http:
        meta, ctxs = http.post(API_URL, json={"type": "metaAndAssetCtxs"}).json()
        trimmed = trim_meta_and_ctxs(meta, ctxs)
        save("meta_and_asset_ctxs.json", trimmed)
        names = {a["name"] for a in trimmed[0]["universe"]}

        save("all_mids.json", trim_mids(http.post(API_URL, json={"type": "allMids"}).json(), names))

        now = int(time.time() * 1000)
        body = {
            "type": "candleSnapshot",
            "req": {"coin": "BTC", "interval": "4h", "startTime": now - 30 * 86_400_000, "endTime": now},
        }
        save("candles_btc_4h.json", http.post(API_URL, json=body).json())

        # Daily candles and order books for the scanner tests
        newest = meta["universe"][newest_listing(meta, ctxs)]["name"]
        active = [
            (float(c["dayNtlVlm"]), a["name"]) for a, c in zip(trimmed[0]["universe"], trimmed[1])
            if not a.get("isDelisted") and float(c["dayNtlVlm"]) > 0
        ]
        thinnest = min(active)[1]
        for coin in ("BTC", "ETH", "SOL", newest, thinnest):
            body = {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1d", "startTime": now - 120 * 86_400_000, "endTime": now},
            }
            save(f"candles_1d_{coin}.json", http.post(API_URL, json=body).json())
        for coin in ("BTC", "SOL", thinnest):
            save(f"l2_book_{coin}.json", http.post(API_URL, json={"type": "l2Book", "coin": coin}).json())
            body = {"type": "l2Book", "coin": coin, "nSigFigs": 3}
            save(f"l2_book_{coin}_sig3.json", http.post(API_URL, json=body).json())
        save("scanner_fixture_coins.json", {"newest_listing": newest, "thinnest": thinnest})

    message = asyncio.run(record_ws_message())
    message["data"]["mids"] = trim_mids(message["data"]["mids"], names)
    save("ws_all_mids.json", message)


if __name__ == "__main__":
    main()
