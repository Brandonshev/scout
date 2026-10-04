"""SQLite storage for everything Scout sees and does.

All timestamps are stored as UTC milliseconds since 1970 (columns ending in
_ms). They are converted to Sydney time only when shown to a person. Money is
stored in USD/USDC, because that's what Hyperliquid uses.

Every row Scout writes about a decision carries the app_version that made it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from scout.version import APP_VERSION

# Bump when the schema changes, so later versions can migrate old databases.
SCHEMA_VERSION = 10

SCHEMA = """
-- Price candles: one row per coin, candle size and start time.
CREATE TABLE IF NOT EXISTS candles (
    coin           TEXT    NOT NULL,
    interval       TEXT    NOT NULL,
    open_time_ms   INTEGER NOT NULL,
    close_time_ms  INTEGER NOT NULL,
    open           REAL    NOT NULL,
    high           REAL    NOT NULL,
    low            REAL    NOT NULL,
    close          REAL    NOT NULL,
    volume         REAL    NOT NULL,
    trades         INTEGER,
    PRIMARY KEY (coin, interval, open_time_ms)
);

-- Point-in-time market stats per coin (price, volume, funding, open interest).
CREATE TABLE IF NOT EXISTS market_snapshots (
    id              INTEGER PRIMARY KEY,
    ts_ms           INTEGER NOT NULL,
    coin            TEXT    NOT NULL,
    mark_price      REAL,
    mid_price       REAL,
    volume_24h_usd  REAL,
    open_interest   REAL,
    funding_rate    REAL,
    UNIQUE (ts_ms, coin)
);

-- The market mood over time, and why. risk_level holds the volatility label.
CREATE TABLE IF NOT EXISTS regime_history (
    id            INTEGER PRIMARY KEY,
    ts_ms         INTEGER NOT NULL,
    regime        TEXT    NOT NULL,
    risk_level    TEXT,
    score         REAL,
    reason        TEXT    NOT NULL,
    details_json  TEXT,
    app_version   TEXT    NOT NULL
);

-- Which coins the scanner looked at, whether each passed its filters, and why.
-- All rows from one scan share the same ts_ms. rank is NULL for excluded coins.
CREATE TABLE IF NOT EXISTS scan_results (
    id              INTEGER PRIMARY KEY,
    ts_ms           INTEGER NOT NULL,
    coin            TEXT    NOT NULL,
    rank            INTEGER,
    volume_24h_usd  REAL,
    passed          INTEGER NOT NULL CHECK (passed IN (0, 1)),
    reason          TEXT    NOT NULL,
    score           REAL,
    details_json    TEXT,
    app_version     TEXT    NOT NULL
);

-- Trade ideas, whether or not they were acted on. candle_ts_ms is the open time of
-- the candle that triggered it, so the same idea is never stored twice.
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    ts_ms         INTEGER NOT NULL,
    coin          TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL,
    action        TEXT    NOT NULL,
    entry_price   REAL,
    stop_price    REAL,
    target_price  REAL,
    regime        TEXT,
    reason        TEXT    NOT NULL,
    acted         INTEGER NOT NULL DEFAULT 0 CHECK (acted IN (0, 1)),
    candle_ts_ms  INTEGER,
    qty           REAL,
    risk_usd      REAL,
    details_json  TEXT,
    app_version   TEXT    NOT NULL
);

-- Fake orders placed in demo/replay mode.
CREATE TABLE IF NOT EXISTS demo_orders (
    id           INTEGER PRIMARY KEY,
    ts_ms        INTEGER NOT NULL,
    coin         TEXT    NOT NULL,
    side         TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type   TEXT    NOT NULL,
    qty          REAL    NOT NULL CHECK (qty > 0),
    price        REAL,
    fill_price   REAL,
    fee_usd      REAL    NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL,
    signal_id    INTEGER REFERENCES signals(id),
    position_id  INTEGER,
    reason       TEXT    NOT NULL,
    app_version  TEXT    NOT NULL
);

-- Fake positions (coins held), open or closed, with reasons for both.
CREATE TABLE IF NOT EXISTS demo_positions (
    id            INTEGER PRIMARY KEY,
    coin          TEXT    NOT NULL,
    side          TEXT    NOT NULL DEFAULT 'long' CHECK (side IN ('long', 'short')),
    status        TEXT    NOT NULL CHECK (status IN ('open', 'closed')),
    opened_ts_ms  INTEGER NOT NULL,
    closed_ts_ms  INTEGER,
    qty           REAL    NOT NULL CHECK (qty > 0),
    entry_price   REAL    NOT NULL,
    stop_price    REAL    NOT NULL,
    target_price  REAL,
    exit_price    REAL,
    fees_usd      REAL    NOT NULL DEFAULT 0,
    funding_usd   REAL    NOT NULL DEFAULT 0,
    last_price    REAL,
    last_price_ms INTEGER,
    strategy      TEXT    NOT NULL DEFAULT 'core',  -- core, copy or high_risk
    source        TEXT,                             -- e.g. the wallet a copied trade follows
    pnl_usd       REAL,
    open_reason   TEXT    NOT NULL,
    close_reason  TEXT,
    app_version   TEXT    NOT NULL
);

-- Account value over time, for charts and drawdown checks.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts_ms                INTEGER PRIMARY KEY,
    equity_usd           REAL NOT NULL,
    cash_usd             REAL NOT NULL,
    positions_value_usd  REAL NOT NULL,
    aud_to_usd_rate      REAL,
    btc_price            REAL,
    mode                 TEXT NOT NULL,
    app_version          TEXT NOT NULL
);

-- How far back each coin's candle history has been checked. A coin listed later than that
-- simply has no older candles, and we don't need to ask again.
CREATE TABLE IF NOT EXISTS candle_coverage (
    coin      TEXT    NOT NULL,
    interval  TEXT    NOT NULL,
    from_ms   INTEGER NOT NULL,
    PRIMARY KEY (coin, interval)
);

-- Messages to your phone: queued here, sent when allowed (quiet hours, rate limits), retried
-- on failure. status: pending, sent, failed, merged (sent as part of a digest), disabled.
CREATE TABLE IF NOT EXISTS notifications (
    id               INTEGER PRIMARY KEY,
    created_ms       INTEGER NOT NULL,
    category         TEXT    NOT NULL,
    priority         INTEGER NOT NULL,
    text             TEXT    NOT NULL,
    status           TEXT    NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'merged', 'disabled')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_ms  INTEGER,
    sent_ms          INTEGER,
    backend          TEXT,
    last_error       TEXT,
    app_version      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications (status, id);

-- Daily exchange rates: how many US dollars one Australian dollar bought (RBA, 4pm Sydney).
CREATE TABLE IF NOT EXISTS fx_rates (
    date         TEXT PRIMARY KEY,  -- YYYY-MM-DD
    usd_per_aud  REAL NOT NULL,
    source       TEXT NOT NULL
);

-- The demo bot's state: RUNNING/PAUSED/KILLED, cash, peak value, today's start value, etc.
CREATE TABLE IF NOT EXISTS bot_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_ms  INTEGER NOT NULL
);

-- Anything notable: startups, wake-from-sleep, risk alerts, errors.
CREATE TABLE IF NOT EXISTS events_log (
    id           INTEGER PRIMARY KEY,
    ts_ms        INTEGER NOT NULL,
    level        TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    message      TEXT    NOT NULL,
    data_json    TEXT,
    app_version  TEXT    NOT NULL
);

-- Crypto news headlines from free RSS feeds (titles and links only, never the articles).
CREATE TABLE IF NOT EXISTS news_headlines (
    url           TEXT,
    source        TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    published_ms  INTEGER NOT NULL,
    seen_ms       INTEGER NOT NULL,
    PRIMARY KEY (source, title)
);

-- Trades the news stopped ("veto") or ended early ("exit"), with the price then, to check later.
CREATE TABLE IF NOT EXISTS news_decisions (
    id        INTEGER PRIMARY KEY,
    ts_ms     INTEGER NOT NULL,
    coin      TEXT    NOT NULL,
    name      TEXT    NOT NULL,
    strategy  TEXT    NOT NULL,
    action    TEXT    NOT NULL,  -- veto | exit
    price     REAL    NOT NULL,
    headline  TEXT    NOT NULL,
    source    TEXT    NOT NULL,
    url       TEXT
);

CREATE INDEX IF NOT EXISTS idx_news_headlines_published ON news_headlines (published_ms);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_coin_ts ON market_snapshots (coin, ts_ms);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts_ms);
CREATE INDEX IF NOT EXISTS idx_demo_positions_status ON demo_positions (status);
CREATE INDEX IF NOT EXISTS idx_events_log_ts ON events_log (ts_ms);
CREATE INDEX IF NOT EXISTS idx_regime_history_ts ON regime_history (ts_ms);
CREATE INDEX IF NOT EXISTS idx_scan_results_ts ON scan_results (ts_ms);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique ON signals (coin, action, candle_ts_ms);
"""

# Columns added after a table was first created: (table, column, definition).
# CREATE TABLE IF NOT EXISTS won't add them to an existing database, so we do.
ADDED_COLUMNS = (
    ("regime_history", "details_json", "TEXT"),  # v0.3.0
    ("scan_results", "score", "REAL"),  # v0.4.0
    ("scan_results", "details_json", "TEXT"),  # v0.4.0
    ("signals", "candle_ts_ms", "INTEGER"),  # v0.5.0
    ("signals", "qty", "REAL"),  # v0.5.0
    ("signals", "risk_usd", "REAL"),  # v0.5.0
    ("signals", "details_json", "TEXT"),  # v0.5.0
    ("demo_positions", "side", "TEXT NOT NULL DEFAULT 'long'"),  # v0.5.0
    ("demo_positions", "funding_usd", "REAL NOT NULL DEFAULT 0"),  # v0.7.0
    ("equity_snapshots", "btc_price", "REAL"),  # v0.9.0
    ("demo_positions", "last_price", "REAL"),  # v0.9.0
    ("demo_positions", "last_price_ms", "INTEGER"),  # v0.9.0
    ("demo_orders", "position_id", "INTEGER"),  # v0.10.0
    ("demo_positions", "strategy", "TEXT NOT NULL DEFAULT 'core'"),  # v0.12.0
    ("demo_positions", "source", "TEXT"),  # v0.12.0
)

TABLES = (
    "candles",
    "market_snapshots",
    "regime_history",
    "scan_results",
    "signals",
    "demo_orders",
    "demo_positions",
    "equity_snapshots",
    "events_log",
    "bot_state",
    "candle_coverage",
    "notifications",
    "fx_rates",
    "news_headlines",
    "news_decisions",
)


def now_ms() -> int:
    """Current UTC time in milliseconds since 1970."""
    return time.time_ns() // 1_000_000


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the dashboard read while the bot writes.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def open_db(db_path: Path) -> sqlite3.Connection:
    """Connect, creating any missing tables first. Existing data is never touched."""
    conn = connect(db_path)
    for table, column, definition in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def init_db(db_path: Path) -> list[str]:
    """Create any missing tables and record that we did. Returns table names."""
    with closing(open_db(db_path)) as conn:
        log_event(conn, "INFO", "db", "database initialised", {"schema_version": SCHEMA_VERSION})
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    return [row["name"] for row in rows]


def log_event(
    conn: sqlite3.Connection,
    level: str,
    category: str,
    message: str,
    data: dict[str, Any] | None = None,
    ts_ms: int | None = None,
) -> None:
    """Record a notable event in events_log. The caller commits. `ts_ms` defaults to now (replays pass
    their simulated time)."""
    conn.execute(
        "INSERT INTO events_log (ts_ms, level, category, message, data_json, app_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now_ms() if ts_ms is None else ts_ms, level, category, message, json.dumps(data) if data else None,
         APP_VERSION),
    )
