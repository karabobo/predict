import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from signal_arena_app import build_view_model, app


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            price_yes REAL,
            price_no REAL,
            end_date TEXT,
            resolved INTEGER DEFAULT 0,
            outcome INTEGER,
            provisional_outcome INTEGER,
            token_yes TEXT,
            token_no TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE decision_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            cycle INTEGER,
            rule_profile TEXT,
            entry_offset_seconds INTEGER,
            stage TEXT,
            rule_name TEXT,
            should_trade INTEGER,
            direction TEXT,
            estimate REAL,
            confidence TEXT,
            conviction_score INTEGER,
            reason TEXT,
            prior_prob REAL,
            prior_direction TEXT,
            prior_edge REAL,
            passed INTEGER,
            book_ready INTEGER,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            status TEXT,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            rule_profile TEXT,
            rule_name TEXT,
            direction TEXT,
            status TEXT,
            entry_offset_seconds INTEGER,
            market_price REAL,
            expected_edge REAL,
            fill_source TEXT,
            settlement_source TEXT,
            won INTEGER,
            pnl_usd REAL,
            created_at TEXT,
            settled_at TEXT,
            bet_amount_usd REAL
        )
        """
    )
    return db


def test_build_view_model_reads_audit_and_paper_state():
    db = _db()
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO markets VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)",
        ("m1", "BTC up?", 0.51, 0.49, (now + timedelta(minutes=3)).isoformat(), "yes", "no"),
    )
    db.execute(
        """
        INSERT INTO decision_audit (
            market_id, cycle, rule_profile, entry_offset_seconds, stage, rule_name,
            should_trade, direction, estimate, status, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", 1, "v8_broad_paper_candidate", 15, "rule", "baseline_current", 0, None, 0.5, "no_trade", "skip", now.isoformat()),
    )
    db.execute(
        """
        INSERT INTO decision_audit (
            market_id, cycle, rule_profile, entry_offset_seconds, stage, rule_name,
            should_trade, direction, estimate, status, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", 1, "v8_broad_paper_candidate", 15, "final", "alpha_router_no_trade", 0, None, 0.5, "no_trade_signal", "none", now.isoformat()),
    )
    db.execute(
        """
        INSERT INTO paper_orders (
            market_id, rule_profile, rule_name, direction, status, entry_offset_seconds,
            market_price, expected_edge, fill_source, pnl_usd, created_at, bet_amount_usd
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", "v8_broad_paper_candidate", "baseline_current", "UP", "paper_filled", 15, 0.5, 0.05, "wss_book", 0.0, now.isoformat(), 75.0),
    )
    db.commit()

    model = build_view_model(db)

    assert model["current_market"]["id"] == "m1"
    assert model["latest_window"]["entry_offset_seconds"] == 15
    assert model["final_decision"]["status"] == "no_trade_signal"
    assert model["rule_arena"][0]["rule_name"] == "baseline_current"
    assert model["paper_summary"]["orders"] == 1


def test_signal_arena_route_renders(monkeypatch):
    db = _db()
    monkeypatch.setattr("signal_arena_app.connect", lambda _db_path=None: db)

    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "5分钟市场模型与规则信号台" in body
    assert "Decision Audit" in body
