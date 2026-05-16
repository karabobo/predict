import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading import ensure_paper_schema
from tui import fetch_monitor_rows, render_monitor


def test_render_monitor_includes_latest_prediction_and_paper_order():
    db = sqlite3.connect(":memory:")
    db.execute(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            end_date TEXT,
            price_yes REAL,
            price_no REAL,
            resolved INTEGER DEFAULT 0
        )
        """
    )
    db.execute(
        """
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            model_version TEXT,
            estimate REAL,
            confidence TEXT,
            conviction_score INTEGER,
            should_trade INTEGER
        )
        """
    )
    ensure_paper_schema(db)
    now = datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc)
    db.execute(
        "INSERT INTO markets VALUES (?, ?, ?, ?, ?, 0)",
        ("m1", "BTC up?", (now + timedelta(minutes=5)).isoformat(), 0.55, 0.45),
    )
    db.execute(
        """
        INSERT INTO predictions (
            market_id, model_version, estimate, confidence, conviction_score, should_trade
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("m1", "router_overlay_ensemble", 0.62, "medium", 3, 1),
    )
    db.execute(
        """
        INSERT INTO paper_orders (
            prediction_id, market_id, direction, status, expected_edge,
            entry_offset_seconds, best_bid, best_ask, spread, fill_source,
            settlement_source, won, pnl_usd
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1, "m1", "UP", "paper_filled", 0.07, 5, 0.49, 0.50, 0.01, "wss_book", "official", 1, 75.0),
    )
    db.commit()

    rows = fetch_monitor_rows(db)
    rendered = render_monitor(rows, now=now)

    assert "m1" in rendered
    assert "router_overlay_ensemble" in rendered
    assert "paper_filled" in rendered
    assert "@5s" in rendered
    assert "edge=+0.070" in rendered
    assert "pnl=+75.00" in rendered
    assert "wss_book" in rendered
