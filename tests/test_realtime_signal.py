import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from realtime_signal import (
    apply_realtime_price_to_candles,
    build_signal_key,
    ensure_realtime_schema,
    live_direction,
    reference_price_for_market,
    select_realtime_market,
)


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
            token_yes TEXT,
            resolved INTEGER DEFAULT 0
        )
        """
    )
    return db


def test_select_realtime_market_prefers_active_window():
    db = _db()
    now = datetime(2026, 5, 6, 12, 2, tzinfo=timezone.utc)
    active_end = now + timedelta(minutes=3)
    warmup_end = now + timedelta(minutes=8)
    db.execute(
        "INSERT INTO markets (id, question, price_yes, price_no, end_date, resolved) VALUES (?, ?, ?, ?, ?, 0)",
        ("active", "Bitcoin Up or Down", 0.5, 0.5, active_end.isoformat()),
    )
    db.execute(
        "INSERT INTO markets (id, question, price_yes, price_no, end_date, resolved) VALUES (?, ?, ?, ?, ?, 0)",
        ("warmup", "Bitcoin Up or Down", 0.5, 0.5, warmup_end.isoformat()),
    )

    market = select_realtime_market(db, now=now, lookahead_seconds=600)

    assert market["id"] == "active"
    assert market["status"] == "active"


def test_live_direction_handles_near_tie():
    assert live_direction(reference_price=100.0, current_price=101.0)[0] == "UP"
    assert live_direction(reference_price=100.0, current_price=99.0)[0] == "DOWN"
    assert live_direction(reference_price=100.0, current_price=100.001, near_tie_pct=0.001)[0] == "NEAR_TIE"


def test_apply_realtime_price_patches_last_candle_only():
    candles = [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
        {"open": 100.5, "high": 101.0, "low": 100.0, "close": 100.7, "volume": 2.0},
    ]

    patched = apply_realtime_price_to_candles(candles, 102.0)

    assert candles[-1]["close"] == 100.7
    assert patched[-1]["close"] == 102.0
    assert patched[-1]["high"] == 102.0
    assert patched[-1]["direction"] == "UP"


def test_reference_price_matches_market_start_candle():
    start = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    market = {"start_date": start}
    candles = [
        {"open_time": (start - timedelta(minutes=5)).isoformat(), "open": 99.0},
        {"open_time": start.isoformat(), "open": 100.0},
    ]

    price, source = reference_price_for_market(market, candles)

    assert price == 100.0
    assert source == "matched_candle_open"


def test_signal_key_changes_on_reason_or_direction():
    up = {"should_trade": True, "direction": "UP", "confidence": "medium", "conviction_score": 3, "reason": "a"}
    up2 = {"should_trade": True, "direction": "UP", "confidence": "medium", "conviction_score": 3, "reason": "b"}
    down = {"should_trade": True, "direction": "DOWN", "confidence": "medium", "conviction_score": 3, "reason": "a"}

    assert build_signal_key(up) != build_signal_key(up2)
    assert build_signal_key(up) != build_signal_key(down)


def test_realtime_schema_creates_state_and_events_tables():
    db = sqlite3.connect(":memory:")
    ensure_realtime_schema(db)

    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }

    assert "realtime_signal_state" in tables
    assert "realtime_signal_events" in tables
    assert "realtime_shadow_rule_events" in tables
    shadow_columns = {
        row[1]
        for row in db.execute("PRAGMA table_info(realtime_shadow_rule_events)").fetchall()
    }
    assert {
        "reference_price",
        "current_price",
        "live_direction",
        "distance_from_reference_pct",
        "price_source",
    }.issubset(shadow_columns)
