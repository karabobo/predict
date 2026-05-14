"""
Settlement tests — provisional parsing must improve timeliness without
contaminating official scorecards.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_market_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            category TEXT,
            end_date TEXT,
            volume REAL,
            price_yes REAL,
            price_no REAL,
            fetched_at TEXT,
            resolved INTEGER DEFAULT 0,
            outcome INTEGER DEFAULT NULL
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
            reasoning TEXT,
            predicted_at TEXT,
            cycle INTEGER,
            conviction_score INTEGER,
            should_trade INTEGER,
            market_price_yes_snapshot REAL
        )
        """
    )
    return db


def test_determine_provisional_outcome_requires_two_agreeing_snapshots():
    from settlement import determine_provisional_outcome

    assert determine_provisional_outcome(
        [{"price_yes": 0.991}, {"price_yes": 0.993}],
        threshold=0.99,
        confirmations=2,
    ) == 1
    assert determine_provisional_outcome(
        [{"price_yes": 0.008}, {"price_yes": 0.009}],
        threshold=0.99,
        confirmations=2,
    ) == 0
    assert determine_provisional_outcome(
        [{"price_yes": 0.992}, {"price_yes": 0.52}],
        threshold=0.99,
        confirmations=2,
    ) is None
    assert determine_provisional_outcome(
        [{"price_yes": 0.992}],
        threshold=0.99,
        confirmations=2,
    ) is None


def test_sync_settlements_creates_provisional_then_official_overrides():
    from fetch_markets import ensure_market_schema
    from settlement import sync_settlements

    db = _make_market_db()
    ensure_market_schema(db)
    db.execute(
        """
        INSERT INTO markets (id, question, category, end_date, volume, price_yes, price_no, fetched_at, resolved, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", "BTC Up or Down", "crypto", "2026-04-03T08:00:00+00:00", 1000, 0.5, 0.5, "2026-04-03T07:59:00+00:00", 0, None),
    )
    db.commit()

    def high_yes_state(_market_id):
        return {"market_id": "m1", "closed": False, "price_yes": 0.995, "price_no": 0.005}

    c1 = sync_settlements(
        db,
        include_provisional=True,
        now_iso="2026-04-03T08:00:30+00:00",
        fetch_state=high_yes_state,
    )
    assert c1["provisional_created"] == 0

    c2 = sync_settlements(
        db,
        include_provisional=True,
        now_iso="2026-04-03T08:01:00+00:00",
        fetch_state=high_yes_state,
    )
    assert c2["provisional_created"] == 1
    row = db.execute(
        "SELECT resolved, outcome, provisional_outcome, provisional_source FROM markets WHERE id = 'm1'"
    ).fetchone()
    assert row["resolved"] == 0
    assert row["outcome"] is None
    assert row["provisional_outcome"] == 1
    assert row["provisional_source"] is not None

    def official_state(_market_id):
        return {"market_id": "m1", "closed": True, "price_yes": 1.0, "price_no": 0.0}

    c3 = sync_settlements(
        db,
        include_provisional=True,
        now_iso="2026-04-03T08:01:30+00:00",
        fetch_state=official_state,
    )
    assert c3["official_resolved"] == 1
    row = db.execute(
        """
        SELECT resolved, outcome, provisional_outcome, provisional_resolved_at, official_resolved_at
        FROM markets WHERE id = 'm1'
        """
    ).fetchone()
    assert row["resolved"] == 1
    assert row["outcome"] == 1
    assert row["provisional_outcome"] is None
    assert row["provisional_resolved_at"] is None
    assert row["official_resolved_at"] is not None


def test_closed_near_terminal_price_counts_as_official_resolution():
    from fetch_markets import ensure_market_schema
    from settlement import sync_settlements

    db = _make_market_db()
    ensure_market_schema(db)
    db.execute(
        """
        INSERT INTO markets (id, question, category, end_date, volume, price_yes, price_no, fetched_at, resolved, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m_fast", "BTC Up or Down", "crypto", "2026-04-03T08:00:00+00:00", 1000, 0.5, 0.5, "2026-04-03T07:59:00+00:00", 0, None),
    )
    db.commit()

    def closed_high_yes(_market_id):
        return {"market_id": "m_fast", "closed": True, "price_yes": 0.995, "price_no": 0.005}

    counts = sync_settlements(
        db,
        include_provisional=True,
        now_iso="2026-04-03T08:00:30+00:00",
        fetch_state=closed_high_yes,
    )

    assert counts["official_resolved"] == 1
    row = db.execute("SELECT resolved, outcome, official_resolved_at FROM markets WHERE id = 'm_fast'").fetchone()
    assert row["resolved"] == 1
    assert row["outcome"] == 1
    assert row["official_resolved_at"] is not None


def test_provisional_markets_do_not_enter_official_signal_metrics():
    from fetch_markets import ensure_market_schema
    from score import calculate_signal_metrics

    db = _make_market_db()
    ensure_market_schema(db)
    db.execute(
        """
        INSERT INTO markets (
            id, question, category, end_date, volume, price_yes, price_no, fetched_at,
            resolved, outcome, provisional_outcome, provisional_resolved_at, provisional_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "m2",
            "BTC Up or Down",
            "crypto",
            "2026-04-03T08:00:00+00:00",
            1000,
            0.5,
            0.5,
            "2026-04-03T07:59:00+00:00",
            0,
            None,
            1,
            "2026-04-03T08:01:00+00:00",
        "gamma_outcome_prices:0.99x2",
        ),
    )
    db.execute(
        """
        INSERT INTO predictions (
            market_id, agent, estimate, confidence, reasoning, predicted_at, cycle,
            conviction_score, should_trade, market_price_yes_snapshot
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m2", "contrarian_rule", 0.62, "medium", "test", "2026-04-03T07:58:00+00:00", 1, 3, 1, 0.48),
    )
    db.commit()

    assert calculate_signal_metrics(db) == {}
