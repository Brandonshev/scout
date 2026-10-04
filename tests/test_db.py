import sqlite3
from contextlib import closing

import pytest

from scout import APP_VERSION
from scout.db import SCHEMA_VERSION, TABLES, connect, init_db, log_event


def test_init_db_creates_all_tables(tmp_path):
    tables = init_db(tmp_path / "data" / "scout.db")
    assert set(TABLES) <= set(tables)


def test_init_db_is_safe_to_rerun_and_keeps_data(tmp_path):
    db_path = tmp_path / "scout.db"
    init_db(db_path)
    init_db(db_path)
    with closing(connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # one "database initialised" event per run
        assert conn.execute("SELECT COUNT(*) FROM events_log").fetchone()[0] == 2


def test_events_are_stamped_with_version(tmp_path):
    db_path = tmp_path / "scout.db"
    init_db(db_path)
    with closing(connect(db_path)) as conn:
        log_event(conn, "WARNING", "risk", "daily loss limit reached", {"loss_pct": 3.1})
        conn.commit()
        row = conn.execute("SELECT * FROM events_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["app_version"] == APP_VERSION
    assert row["category"] == "risk"
    assert '"loss_pct": 3.1' in row["data_json"]


def test_demo_orders_reject_bad_side(tmp_path):
    db_path = tmp_path / "scout.db"
    init_db(db_path)
    with closing(connect(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO demo_orders (ts_ms, coin, side, order_type, qty, status, reason, app_version) "
            "VALUES (0, 'BTC', 'short', 'market', 1, 'filled', 'test', ?)",
            (APP_VERSION,),
        )
