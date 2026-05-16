import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading import (
    PaperTradingConfig,
    ensure_paper_schema,
    execute_book_paper_order,
    execute_paper_orders,
    paper_performance_summary,
    settle_paper_orders,
)
from v3.l2_replay import BookState


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            end_date TEXT,
            price_yes REAL,
            price_no REAL,
            resolved INTEGER DEFAULT 0,
            outcome INTEGER DEFAULT NULL,
            provisional_outcome INTEGER DEFAULT NULL,
            provisional_resolved_at TEXT,
            official_resolved_at TEXT,
            condition_id TEXT,
            token_yes TEXT,
            token_no TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            agent TEXT,
            estimate REAL,
            confidence TEXT,
            predicted_at TEXT,
            cycle INTEGER,
            conviction_score INTEGER,
            should_trade INTEGER,
            model_version TEXT,
            prediction_source TEXT
        )
        """
    )
    ensure_paper_schema(db)
    return db


def _config(**overrides) -> PaperTradingConfig:
    values = {
        "enabled": True,
        "min_edge": 0.01,
        "min_seconds_to_expiry": 45,
        "medium_bet_usd": 75.0,
        "high_bet_usd": 150.0,
    }
    values.update(overrides)
    return PaperTradingConfig(**values)


def test_execute_paper_orders_records_simulated_fill():
    db = _db()
    now = datetime.now(timezone.utc)
    db.execute(
        """
        INSERT INTO markets (
            id, question, end_date, price_yes, price_no, resolved, token_yes, token_no
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        ("m1", "BTC up?", (now + timedelta(minutes=5)).isoformat(), 0.55, 0.45, "yes-token", "no-token"),
    )
    db.execute(
        """
        INSERT INTO predictions (
            market_id, agent, estimate, confidence, predicted_at, cycle,
            conviction_score, should_trade, model_version, prediction_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", "contrarian_rule", 0.62, "medium", now.isoformat(), 1, 3, 1, "router_overlay_ensemble", "paper_test"),
    )
    db.commit()

    results = execute_paper_orders(db, _config())

    assert results == [
        {
            "market_id": "m1",
            "status": "paper_filled",
            "direction": "UP",
            "expected_edge": 0.06999999999999995,
        }
    ]
    row = db.execute("SELECT status, direction, bet_amount_usd, market_price, shares FROM paper_orders").fetchone()
    assert row[0] == "paper_filled"
    assert row[1] == "UP"
    assert row[2] == 75.0
    assert row[3] == 0.55
    assert abs(row[4] - (75.0 / 0.55)) < 1e-9


def test_execute_paper_orders_is_idempotent_per_prediction():
    db = _db()
    now = datetime.now(timezone.utc)
    db.execute(
        """
        INSERT INTO markets (
            id, question, end_date, price_yes, price_no, resolved, token_yes, token_no
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        ("m1", "BTC up?", (now + timedelta(minutes=5)).isoformat(), 0.55, 0.45, "yes-token", "no-token"),
    )
    db.execute(
        """
        INSERT INTO predictions (
            market_id, agent, estimate, confidence, predicted_at, cycle,
            conviction_score, should_trade, model_version, prediction_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", "contrarian_rule", 0.62, "medium", now.isoformat(), 1, 3, 1, "router_overlay_ensemble", "paper_test"),
    )
    db.commit()

    execute_paper_orders(db, _config())
    execute_paper_orders(db, _config())

    assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 1


def test_execute_paper_orders_skips_when_edge_is_too_low():
    db = _db()
    now = datetime.now(timezone.utc)
    db.execute(
        """
        INSERT INTO markets (
            id, question, end_date, price_yes, price_no, resolved, token_yes, token_no
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        ("m1", "BTC up?", (now + timedelta(minutes=5)).isoformat(), 0.60, 0.40, "yes-token", "no-token"),
    )
    db.execute(
        """
        INSERT INTO predictions (
            market_id, agent, estimate, confidence, predicted_at, cycle,
            conviction_score, should_trade, model_version, prediction_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", "contrarian_rule", 0.62, "medium", now.isoformat(), 1, 3, 1, "router_overlay_ensemble", "paper_test"),
    )
    db.commit()

    results = execute_paper_orders(db, _config(min_edge=0.05))

    assert results == [{"market_id": "m1", "status": "skipped_edge_below_threshold"}]


def test_execute_book_paper_order_uses_wss_book_fill_and_extended_columns():
    db = _db()
    signal = {
        "direction": "UP",
        "estimate": 0.62,
        "confidence": "medium",
        "conviction_score": 3,
        "strategy_name": "router_overlay_ensemble",
    }
    yes_book = BookState.from_snapshot(
        bid_prices=[0.48],
        bid_sizes=[100],
        ask_prices=[0.50],
        ask_sizes=[300],
    )

    result = execute_book_paper_order(
        db,
        market_id="m1",
        question="BTC up?",
        token_yes="yes-token",
        token_no="no-token",
        signal=signal,
        yes_book=yes_book,
        config=_config(min_edge=0.01),
        rule_profile="v8_integrated_candidate",
        entry_offset_seconds=5,
        prior_prob=0.62,
        prior_edge=0.12,
    )

    assert result["status"] == "paper_filled"
    assert result["average_price"] == 0.5
    row = db.execute(
        """
        SELECT status, market_price, expected_edge, rule_profile, rule_name,
               entry_offset_seconds, prior_prob, prior_edge, best_ask, fill_source
        FROM paper_orders
        """
    ).fetchone()
    assert row == (
        "paper_filled",
        0.5,
        0.12,
        "v8_integrated_candidate",
        "router_overlay_ensemble",
        5,
        0.62,
        0.12,
        0.5,
        "wss_book",
    )


def test_execute_book_paper_order_is_idempotent_per_market_profile():
    db = _db()
    signal = {
        "direction": "UP",
        "estimate": 0.62,
        "confidence": "medium",
        "conviction_score": 3,
        "strategy_name": "router_overlay_ensemble",
    }
    yes_book = BookState.from_snapshot(
        bid_prices=[0.48],
        bid_sizes=[100],
        ask_prices=[0.50],
        ask_sizes=[300],
    )

    execute_book_paper_order(
        db,
        market_id="m1",
        question="BTC up?",
        token_yes="yes-token",
        token_no="no-token",
        signal=signal,
        yes_book=yes_book,
        config=_config(),
        rule_profile="v8_integrated_candidate",
        entry_offset_seconds=5,
    )
    result = execute_book_paper_order(
        db,
        market_id="m1",
        question="BTC up?",
        token_yes="yes-token",
        token_no="no-token",
        signal=signal,
        yes_book=yes_book,
        config=_config(),
        rule_profile="v8_integrated_candidate",
        entry_offset_seconds=15,
    )

    assert result["status"] == "duplicate_market_profile"
    assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 1


def test_settle_paper_orders_scores_official_pnl():
    db = _db()
    db.execute(
        """
        INSERT INTO markets (
            id, question, end_date, price_yes, price_no, resolved, outcome,
            official_resolved_at, token_yes, token_no
        )
        VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
        """,
        ("m1", "BTC up?", "2026-03-01T00:05:00+00:00", 1.0, 0.0, "2026-03-01T00:05:30+00:00", "yes", "no"),
    )
    ensure_paper_schema(db)
    db.execute(
        """
        INSERT INTO paper_orders (
            market_id, direction, bet_amount_usd, market_price, shares, status
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("m1", "UP", 75.0, 0.50, 150.0, "paper_filled"),
    )
    db.commit()

    counts = settle_paper_orders(db, include_provisional=False)

    assert counts == {"settled": 1, "wins": 1, "pnl_usd": 75.0}
    row = db.execute(
        "SELECT settlement_outcome, settlement_source, won, pnl_usd, roi FROM paper_orders"
    ).fetchone()
    assert row == (1, "official", 1, 75.0, 1.0)
    summary = paper_performance_summary(db)
    assert summary["settled"] == 1
    assert summary["win_rate"] == 1.0
    assert summary["pnl_usd"] == 75.0


def test_settle_paper_orders_can_use_provisional_outcome():
    db = _db()
    db.execute(
        """
        INSERT INTO markets (
            id, question, end_date, price_yes, price_no, resolved, outcome,
            provisional_outcome, provisional_resolved_at, token_yes, token_no
        )
        VALUES (?, ?, ?, ?, ?, 0, NULL, 0, ?, ?, ?)
        """,
        ("m1", "BTC up?", "2026-03-01T00:05:00+00:00", 0.0, 1.0, "2026-03-01T00:05:20+00:00", "yes", "no"),
    )
    ensure_paper_schema(db)
    db.execute(
        """
        INSERT INTO paper_orders (
            market_id, direction, bet_amount_usd, market_price, shares, status
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("m1", "UP", 75.0, 0.50, 150.0, "paper_filled"),
    )
    db.commit()

    counts = settle_paper_orders(db, include_provisional=True)

    assert counts == {"settled": 1, "wins": 0, "pnl_usd": -75.0}
    row = db.execute("SELECT settlement_source, won, pnl_usd, roi FROM paper_orders").fetchone()
    assert row == ("provisional", 0, -75.0, -1.0)
