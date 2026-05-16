import os
import sqlite3
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading import PaperTradingConfig, ensure_paper_schema
from realtime_paper_runtime import execute_v8_book_paper_order, market_entry_offset_seconds, selected_entry_slot
from realtime_wss import LiveBookStore


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            agent TEXT,
            estimate REAL,
            edge REAL,
            confidence TEXT,
            reasoning TEXT,
            predicted_at TEXT,
            cycle INTEGER
        )
        """
    )
    db.execute(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            price_yes REAL,
            price_no REAL,
            end_date TEXT,
            token_yes TEXT,
            token_no TEXT,
            resolved INTEGER DEFAULT 0
        )
        """
    )
    ensure_paper_schema(db)
    return db


def _config():
    return PaperTradingConfig(
        enabled=True,
        min_edge=0.01,
        min_seconds_to_expiry=45,
        medium_bet_usd=75.0,
        high_bet_usd=150.0,
    )


def test_entry_slot_allows_continuous_active_market_offsets():
    assert selected_entry_slot(5) == 5
    assert selected_entry_slot(6) == 6
    assert selected_entry_slot(15) == 15
    assert selected_entry_slot(30) == 30


def test_execute_v8_book_paper_order_uses_live_book_store():
    db = _db()
    now = datetime(2026, 5, 15, 12, 0, 5, tzinfo=timezone.utc)
    market = {
        "id": "m1",
        "question": "BTC up?",
        "start_date": now - timedelta(seconds=5),
        "token_yes": "yes-token",
        "token_no": "no-token",
    }
    assert market_entry_offset_seconds(market, now=now) == 5
    store = LiveBookStore()
    store.apply_message(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.50", "size": "200"}],
        }
    )
    signal = {
        "direction": "UP",
        "estimate": 0.62,
        "confidence": "medium",
        "conviction_score": 3,
        "strategy_name": "router_overlay_ensemble",
    }

    result = execute_v8_book_paper_order(
        db,
        market=market,
        signal=signal,
        book_store=store,
        config=_config(),
        now=now,
        prior_prob=0.62,
        prior_edge=0.12,
    )

    assert result["status"] == "paper_filled"
    assert result["entry_offset_seconds"] == 5
    row = db.execute("SELECT entry_offset_seconds, fill_source FROM paper_orders").fetchone()
    assert tuple(row) == (5, "wss_book")


def test_execute_v8_book_paper_order_skips_too_close_to_expiry():
    db = _db()
    now = datetime(2026, 5, 15, 12, 4, 30, tzinfo=timezone.utc)
    market = {
        "id": "m1",
        "question": "BTC up?",
        "start_date": now - timedelta(minutes=4, seconds=30),
        "end_date": now + timedelta(seconds=30),
        "token_yes": "yes-token",
        "token_no": "no-token",
    }
    signal = {
        "direction": "UP",
        "estimate": 0.62,
        "confidence": "medium",
        "conviction_score": 3,
        "strategy_name": "router_overlay_ensemble",
    }

    result = execute_v8_book_paper_order(
        db,
        market=market,
        signal=signal,
        book_store=LiveBookStore(),
        config=_config(),
        now=now,
    )

    assert result["status"] == "skipped_too_close_to_expiry"
    assert result["entry_offset_seconds"] == 270


def test_run_v8_realtime_paper_once_records_book_based_order(monkeypatch):
    from realtime_paper_runtime import run_v8_realtime_paper_once

    db = _db()
    now = datetime(2026, 5, 15, 12, 0, 5, tzinfo=timezone.utc)
    db.execute(
        """
        INSERT INTO markets (
            id, question, price_yes, price_no, end_date, token_yes, token_no, resolved
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        ("m1", "BTC up?", 0.50, 0.50, (now + timedelta(minutes=4, seconds=55)).isoformat(), "yes-token", "no-token"),
    )
    db.commit()
    store = LiveBookStore()
    store.apply_message(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.50", "size": "200"}],
        }
    )
    btc_context = {
        "candles": [
            {"open_time": (now - timedelta(minutes=5)).isoformat(), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
            {"open_time": (now - timedelta(seconds=5)).isoformat(), "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.7, "volume": 2.0},
        ]
    }

    monkeypatch.setattr(
        "realtime_paper_runtime.alpha_router_signal",
        lambda *_args, **_kwargs: {
            "direction": "UP",
            "estimate": 0.62,
            "confidence": "medium",
            "conviction_score": 3,
            "should_trade": True,
            "reason": "test",
            "strategy_name": "router_overlay_ensemble",
        },
    )

    result = run_v8_realtime_paper_once(
        db,
        book_store=store,
        btc_ticker={"price": 101.0, "source": "test"},
        btc_context=btc_context,
        now=now,
        config=_config(),
        refresh_markets=False,
        cycle=1,
    )

    assert result["status"] == "paper_filled"
    assert result["prediction_id"] == 1
    assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 1


def test_run_v8_realtime_paper_once_uses_configured_rule_profile(monkeypatch):
    from realtime_paper_runtime import run_v8_realtime_paper_once

    db = _db()
    now = datetime(2026, 5, 15, 12, 0, 5, tzinfo=timezone.utc)
    db.execute(
        """
        INSERT INTO markets (
            id, question, price_yes, price_no, end_date, token_yes, token_no, resolved
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        ("m1", "BTC up?", 0.50, 0.50, (now + timedelta(minutes=4, seconds=55)).isoformat(), "yes-token", "no-token"),
    )
    db.commit()
    seen = {}

    monkeypatch.setenv("PREDICT_RULE_PROFILE", "v8_broad_paper_candidate")
    def fake_alpha_router_signal(*_args, **kwargs):
        seen["rule_names"] = kwargs["rule_names"]
        return {"should_trade": False}

    monkeypatch.setattr("realtime_paper_runtime.alpha_router_signal", fake_alpha_router_signal)

    result = run_v8_realtime_paper_once(
        db,
        book_store=LiveBookStore(),
        btc_ticker={"price": 101.0, "source": "test"},
        btc_context={
            "candles": [
                {"open_time": (now - timedelta(minutes=5)).isoformat(), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
                {"open_time": (now - timedelta(seconds=5)).isoformat(), "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.7, "volume": 2.0},
            ]
        },
        now=now,
        config=_config(),
        refresh_markets=False,
        cycle=1,
    )

    assert result["status"] == "no_trade_signal"
    assert result["rule_profile"] == "v8_broad_paper_candidate"
    assert "baseline_current" in seen["rule_names"]
    rows = db.execute(
        "SELECT stage, rule_profile, status FROM decision_audit ORDER BY id ASC"
    ).fetchall()
    assert any(tuple(row) == ("rule", "v8_broad_paper_candidate", "no_trade") for row in rows)
    assert tuple(rows[-1]) == ("final", "v8_broad_paper_candidate", "no_trade_signal")


class _FakePrior:
    def __init__(self, prob_up):
        self.prob_up = prob_up

    def predict(self, _context):
        return SimpleNamespace(prob_up=self.prob_up)


def test_broad_profile_uses_prior_scout_when_rules_do_not_trade(monkeypatch):
    from realtime_paper_runtime import run_v8_realtime_paper_once

    db = _db()
    now = datetime(2026, 5, 15, 12, 0, 5, tzinfo=timezone.utc)
    db.execute(
        """
        INSERT INTO markets (
            id, question, price_yes, price_no, end_date, token_yes, token_no, resolved
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        ("m1", "BTC up?", 0.50, 0.50, (now + timedelta(minutes=4, seconds=55)).isoformat(), "yes-token", "no-token"),
    )
    db.commit()
    store = LiveBookStore()
    store.apply_message(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.50", "size": "200"}],
        }
    )
    monkeypatch.setenv("PREDICT_RULE_PROFILE", "v8_broad_paper_candidate")
    monkeypatch.setenv("PREDICT_PRIOR_SCOUT_ENABLED", "1")
    monkeypatch.setattr(
        "realtime_paper_runtime.alpha_router_signal",
        lambda *_args, **_kwargs: {"should_trade": False},
    )

    result = run_v8_realtime_paper_once(
        db,
        book_store=store,
        btc_ticker={"price": 101.0, "source": "test"},
        btc_context={
            "candles": [
                {"open_time": (now - timedelta(minutes=5)).isoformat(), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
                {"open_time": (now - timedelta(seconds=5)).isoformat(), "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.7, "volume": 2.0},
            ]
        },
        prior_model=_FakePrior(0.56),
        now=now,
        config=_config(),
        refresh_markets=False,
        cycle=1,
    )

    assert result["status"] == "paper_filled"
    row = db.execute("SELECT rule_name, prior_prob, prior_edge FROM paper_orders").fetchone()
    assert tuple(row) == ("prior_probability_scout", 0.56, 0.06000000000000005)
    stages = db.execute(
        "SELECT stage, rule_name, status FROM decision_audit ORDER BY id ASC"
    ).fetchall()
    assert ("prior", "prior_probability_scout", "prior_passed") in [tuple(row) for row in stages]
    assert ("book", "prior_probability_scout", "paper_filled") in [tuple(row) for row in stages]
    assert tuple(stages[-1]) == ("final", "prior_probability_scout", "paper_filled")
