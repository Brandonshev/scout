# Scout status (final)

> **🛑 Project ended on Mon 5 Oct 2026, 2am Sydney time.** All three accounts and this hourly page have been stopped at Brandon's request. These are the final results; the page will not update again. Open positions were left as they were (not sold).

Updated **Mon 05 Oct 02:08 Sydney time**, refreshed every hour. Scout v0.16.0, code version `fe757ab`.

All three accounts trade **fake money** (each started with A$1,000) on real live Hyperliquid prices. Nothing here is financial advice. The code is on the `main` branch of this repository.

## Breakout:1
*Buys coins breaking out of their recent range when the market mood is healthy; stop loss on every trade; long only.*

- **Balance:** A$966.73 (-3.33% since Tue 29 Sep 01:40) · holding BTC instead: +2.42%
- **State:** RUNNING · loop heartbeat 22s ago, v0.15.0
- **Closed trades:** 5 (0 won), total −A$31.23

| Coin | Side | Entry | Now | Profit/loss | Opened | Why |
|---|---|---|---|---|---|---|
| AAVE | long | $177.04 | $178.81 | +A$1.31 (+0.9%) | Fri 02 Oct 14:01 | Buying 0.57 AAVE (≈US$100.64) at $176.56: market mood is positive (RISK_ON), AAVE broke above its 3.3-day high of $176.23 (last 4h close $176.74) on 2.4x normal volume, RSI 71. Stop at $164.87 (6.6% below). Risking A$9.… |
| WLD | long | $0.6061 | $0.5856 | −A$3.35 (-3.5%) | Sat 03 Oct 22:02 | Buying 112.1 WLD (≈US$67.77) at $0.6046: market mood is positive (RISK_ON), WLD broke above its 3.3-day high of $0.5884 (last 4h close $0.6037) on 1.6x normal volume, RSI 66. Stop at $0.5449 (9.9% below). Risking A$9.72… |

Last 10 closed trades:

| Closed | Coin | Side | Result | Why it closed |
|---|---|---|---|---|
| Sat 03 Oct 18:02 | MON | long | −A$4.22 | Selling MON: the trend broke: a 4h candle closed at $0.03113, below its 20-candle average $0.03168. Entered at $0.03236, now $0.03121 (-3.6%). |
| Sat 03 Oct 05:06 | BTC | long | −A$6.10 | Selling BTC: the stop loss at $84,271.46 was hit (price $84,210.50). |
| Fri 02 Oct 02:01 | CRV | long | −A$5.75 | Selling CRV: the trend broke: a 4h candle closed at $0.3797, below its 20-candle average $0.3824. Entered at $0.3975, now $0.3801 (-4.4%). |
| Thu 01 Oct 07:20 | AVAX | long | −A$5.27 | Selling AVAX: the trend broke: a 4h candle closed at $10.90, below its 20-candle average $10.95. Entered at $11.39, now $10.94 (-3.9%). |
| Wed 30 Sep 12:17 | LINK | long | −A$9.89 | Selling LINK: the stop loss at $14.38 was hit (price $14.38). |

Warnings in the last 24h:

- Mon 05 Oct 01:17 WARNING: No live prices for 122 seconds.
- Sun 04 Oct 22:44 WARNING: Scout was not running for 14 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 22:30 WARNING: Scout was not running for 100 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 20:49 WARNING: Scout was not running for 88 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 15:32 WARNING: Prices are stale, so the hourly check is postponed.

## 70/30:2
*70% copies new positions of top Hyperliquid wallets; 30% buys small coins with sudden price and volume jumps; news headlines as a safety check.*

- **Balance:** A$1,004.16 (+0.42% since Tue 29 Sep 15:53) · holding BTC instead: +1.78%
- **State:** RUNNING · loop heartbeat 24s ago, v0.15.0
- **Closed trades:** 12 (3 won), total +A$8.44

| Coin | Side | Entry | Now | Profit/loss | Opened | Why |
|---|---|---|---|---|---|---|
| KAS | long | $0.04423 | $0.04255 | −A$2.73 (-4.0%) | Wed 30 Sep 15:07 | [COPY] 🟢 BOUGHT 1090 KAS at $0.04421 (A$68.84), copying 0xda51…d3e2 (30d +59.6% on US$108,055, 535 trades, 85% won) |
| NEAR | long | $4.88 | $4.84 | −A$0.63 (-0.9%) | Fri 02 Oct 01:02 | [COPY] 🟢 BOUGHT 9.7 NEAR at $4.88 (A$67.63), copying 0x19b1…c8f3 (30d +31.5% on US$150,041, 247 trades, 97% won). In the news (a big coin, so no action): “NEAR Intents hit by $3.8 million exploit as crypto's rough year … |
| ZEC | long | $1,351.43 | $1,325.65 | −A$1.19 (-2.1%) | Fri 02 Oct 04:11 | [COPY] 🟢 BOUGHT 0.03 ZEC at $1,350.75 (A$57.89), copying 0x8bf9…67fb (30d +55.6% on US$129,026, 394 trades, 27% won) |
| ONDO | long | $0.4955 | $0.4951 | −A$0.14 (-0.2%) | Fri 02 Oct 04:59 | [COPY] 🟢 BOUGHT 96 ONDO at $0.4953 (A$67.92), copying 0x19b1…c8f3 (30d +31.5% on US$150,041, 247 trades, 97% won). In the news: “Kakaopay partners with Dinari, Ondo to explore tokenized Korean stocks” (Cointelegraph) |
| ETH | long | $2,700.50 | $2,696.25 | −A$0.19 (-0.3%) | Fri 02 Oct 08:10 | [COPY] 🟢 BOUGHT 0.0176 ETH at $2,699.15 (A$67.86), copying 0x8bf9…67fb (30d +55.6% on US$129,026, 394 trades, 27% won). In the news: “Ethereum staking reward burn proposal EIP-8363 pulled from Hegota upgrade” (The Block… |
| BTC | long | $85,991.47 | $85,236.50 | −A$0.67 (-1.0%) | Sat 03 Oct 00:50 | [COPY] 🟢 BOUGHT 0.00056 BTC at $85,948.50 (A$68.76), copying 0x7927…271b (30d +66.7% on US$221,924, 829 trades, 75% won). In the news: “Bitcoin nears highest level since January as $85,000 sell wall clears, US jobs data… |
| BCH | long | $311.96 | $318.05 | +A$1.27 (+1.8%) | Sat 03 Oct 01:05 | [COPY] 🟢 BOUGHT 0.155 BCH at $311.81 (A$69.04), copying 0x1bcf…00cc (30d +36.5% on US$124,078, 844 trades, 57% won) |

Last 10 closed trades:

| Closed | Coin | Side | Result | Why it closed |
|---|---|---|---|---|
| Sun 04 Oct 23:23 | GALA | long | −A$1.64 | [HIGH-RISK] Selling GALA: held 48 hours (the time limit), price $0.002538. |
| Sun 04 Oct 15:54 | SUPER | long | +A$4.50 | [HIGH-RISK] Selling SUPER: held 48 hours (the time limit), price $0.2545. |
| Sun 04 Oct 04:27 | MOVE | long | −A$4.02 | [HIGH-RISK] Selling MOVE: held 48 hours (the time limit), price $0.01006. |
| Sat 03 Oct 16:14 | @243 | long | −A$4.79 | [HIGH-RISK] Selling UMON: held 48 hours (the time limit), price $0.03156. |
| Sat 03 Oct 13:21 | SAND | long | +A$33.60 | [HIGH-RISK] Selling SAND: hit the +50% profit target ($0.07993). |
| Sat 03 Oct 04:59 | LINK | long | −A$3.80 | [COPY] Selling LINK: the wallet we copied (0x3eb8…e9d9) closed its position. |
| Sat 03 Oct 00:49 | BTC | short | +A$0.13 | [COPY] Closing the short in BTC: the wallet we copied (0x7927…271b) closed its position. |
| Fri 02 Oct 14:18 | BERA | long | −A$1.48 | [HIGH-RISK] Selling BERA: held 48 hours (the time limit), price $0.2412. |
| Fri 02 Oct 01:00 | INIT | long | −A$6.13 | [HIGH-RISK] Selling INIT: held 48 hours (the time limit), price $0.09964. |
| Thu 01 Oct 22:29 | @227 | long | −A$3.46 | [HIGH-RISK] Selling AAVE0: held 48 hours (the time limit), price $164.12. |

Warnings in the last 24h:

- Mon 05 Oct 01:17 WARNING: No live prices for 121 seconds.
- Mon 05 Oct 01:17 ERROR: wallet check failed (clearinghouseState failed after 6 attempts (network error: ConnectTimeout(''))); trying again next round.
- Sun 04 Oct 22:44 WARNING: Scout was not running for 13 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 22:30 WARNING: Scout was not running for 7 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 22:22 WARNING: Scout was not running for 60 minutes (Mac asleep?). Waiting for fresh prices before doing anything.

## SMART:3
*50% Bitcoin while above its 50-day average; 25% long the 6 best-ranked coins and 25% short the 6 worst, rebalanced daily.*

- **Balance:** A$1,004.12 (+0.41% since Fri 02 Oct 04:19) · holding BTC instead: +0.53%
- **State:** RUNNING · loop heartbeat 49s ago, v0.15.0
- **Closed trades:** 0 (0 won), total +A$0.00

| Coin | Side | Entry | Now | Profit/loss | Opened | Why |
|---|---|---|---|---|---|---|
| CASHCAT | short | $0.1693 | $0.1610 | +A$1.46 (+4.9%) | Fri 02 Oct 04:20 | [SMART] 🟣 SHORTED 122 CASHCAT at $0.1694 (A$29.52): #1 worst of 40 ranked coins: 1-month trend -40%; 25% below its 20-day high; typical daily move 9.8%. Safety stop $0.3357. |
| XPL | short | $0.09725 | $0.09629 | +A$0.43 (+1.0%) | Fri 02 Oct 04:20 | [SMART] 🟣 SHORTED 306 XPL at $0.09730 (A$42.53): #2 worst of 40 ranked coins: 17% below its 20-day high; biggest day lately +23%; 2-month trend +27%. Safety stop $0.1624. |
| FARTCOIN | short | $0.1761 | $0.1774 | −A$0.26 (-0.6%) | Fri 02 Oct 04:20 | [SMART] 🟣 SHORTED 165.8 FARTCOIN at $0.1762 (A$41.73): #3 worst of 40 ranked coins: moves 2.0x as much as Bitcoin; 14% below its 20-day high; 2-month trend +32%. Safety stop $0.2790. |
| ONDO | short | $0.4966 | $0.4951 | +A$0.12 (+0.3%) | Fri 02 Oct 04:20 | [SMART] 🟣 SHORTED 53 ONDO at $0.4969 (A$37.62): #4 worst of 40 ranked coins: biggest day lately +27%; typical daily move 7.6%; 15% below its 20-day high. Safety stop $0.7664. |
| BCH | short | $307.01 | $318.05 | −A$1.57 (-3.6%) | Fri 02 Oct 04:20 | [SMART] 🟣 SHORTED 0.1 BCH at $307.17 (A$43.88): #5 worst of 40 ranked coins: biggest day lately +29%; 2-month trend +43%; moves 1.5x as much as Bitcoin. Safety stop $437.59. |
| kPEPE | short | $0.004443 | $0.004292 | +A$1.63 (+3.3%) | Fri 02 Oct 04:20 | [SMART] 🟣 SHORTED 7675 kPEPE at $0.004445 (A$48.74): #6 worst of 40 ranked coins: moves 2.1x as much as Bitcoin; 14% below its 20-day high; biggest day lately +19%. Safety stop $0.006596. |
| BTC | long | $84,822.89 | $85,236.50 | +A$2.03 (+0.4%) | Fri 02 Oct 04:20 | [SMART] 🟢 BOUGHT 0.00404 BTC at $84,780.50 (A$489.30): Bitcoin closed at $83,607, above its 50-day average of $77,281: its trend is up. Safety stop $72,700.50. |
| BNB | long | $770.61 | $787.89 | +A$1.50 (+2.2%) | Fri 02 Oct 04:20 | [SMART] 🟢 BOUGHT 0.063 BNB at $770.23 (A$69.32): #1 best of 40 ranked coins: typical daily move 1.7%; biggest day lately +4%; moves 0.6x as much as Bitcoin. Safety stop $647.82. |
| ETH | long | $2,701.60 | $2,696.25 | −A$0.15 (-0.3%) | Fri 02 Oct 04:20 | [SMART] 🟢 BOUGHT 0.0128 ETH at $2,700.25 (A$49.38): #2 best of 40 ranked coins: typical daily move 2.4%; biggest day lately +7%; 3% below its 20-day high. Safety stop $2,226.93. |
| ZRO | long | $1.82 | $1.99 | +A$1.90 (+8.9%) | Fri 02 Oct 04:20 | [SMART] 🟢 BOUGHT 8.2 ZRO at $1.82 (A$21.31): #3 best of 40 ranked coins: moves 0.7x as much as Bitcoin; at its 20-day high; 2-month trend +126%. Safety stop $0.8989. |
| XMR | long | $548.41 | $548.62 | −A$0.11 (-0.3%) | Fri 02 Oct 04:20 | [SMART] 🟢 BOUGHT 0.045 XMR at $548.13 (A$35.24): #4 best of 40 ranked coins: moves 0.5x as much as Bitcoin; typical daily move 3.4%; biggest day lately +9%. Safety stop $348.38. |
| ASTER | long | $0.7414 | $0.7094 | −A$1.78 (-4.4%) | Fri 02 Oct 04:20 | [SMART] 🟢 BOUGHT 38 ASTER at $0.7410 (A$40.22): #5 best of 40 ranked coins: biggest day lately +6%; moves 0.7x as much as Bitcoin; typical daily move 3.0%. Safety stop $0.5271. |
| CRV | long | $0.3836 | $0.3691 | −A$1.08 (-3.9%) | Fri 02 Oct 04:20 | [SMART] 🟢 BOUGHT 50.9 CRV at $0.3834 (A$27.88): #6 best of 40 ranked coins: biggest day lately +7%; at its 20-day high; typical daily move 4.3%. Safety stop $0.2218. |

Warnings in the last 24h:

- Mon 05 Oct 01:17 WARNING: No live prices for 123 seconds.
- Sun 04 Oct 22:44 WARNING: Scout was not running for 13 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 22:30 WARNING: Scout was not running for 6 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 22:22 WARNING: Scout was not running for 60 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
- Sun 04 Oct 21:21 WARNING: Scout was not running for 31 minutes (Mac asleep?). Waiting for fresh prices before doing anything.
