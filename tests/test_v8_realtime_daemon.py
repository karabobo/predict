import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from realtime_wss import BtcTickerStore, LiveBookStore
from v8_realtime_daemon import DaemonConfig, V8RealtimeDaemon


class _FakeClient:
    starts = []
    stops = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeClient.starts.append(kwargs)

    def start(self):
        self.started = True

    def stop(self):
        _FakeClient.stops += 1


def _db():
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
            outcome INTEGER DEFAULT NULL,
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
            edge REAL,
            confidence TEXT,
            reasoning TEXT,
            predicted_at TEXT,
            cycle INTEGER
        )
        """
    )
    return db


def test_daemon_starts_wss_clients_for_current_market(monkeypatch):
    _FakeClient.starts = []
    _FakeClient.stops = 0
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

    monkeypatch.setattr("v8_realtime_daemon.fetch_btc_candles", lambda limit=20: {"candles": []})
    monkeypatch.setattr("v8_realtime_daemon.fetch_active_markets", lambda: [])
    daemon = V8RealtimeDaemon(
        db=db,
        config=DaemonConfig(once=True, use_wss=True),
        polymarket_client_factory=_FakeClient,
        coinbase_client_factory=_FakeClient,
    )

    result = daemon.step(now=now)

    assert result["status"] == "no_btc_context"
    assert _FakeClient.starts[0]["on_message"]
    assert _FakeClient.starts[0]["asset_ids"] == ["yes-token", "no-token"]


def test_daemon_step_can_run_without_wss_and_use_existing_book(monkeypatch, tmp_path):
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
    books = LiveBookStore()
    books.apply_message(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.50", "size": "200"}],
        }
    )
    ticker = BtcTickerStore()
    ticker.apply_coinbase_message(
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "101.0",
            "time": now.isoformat(),
        }
    )
    monkeypatch.setattr("v8_realtime_daemon.fetch_active_markets", lambda: [])
    monkeypatch.setattr(
        "v8_realtime_daemon.fetch_btc_candles",
        lambda limit=20: {
            "candles": [
                {"open_time": (now - timedelta(minutes=5)).isoformat(), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
                {"open_time": (now - timedelta(seconds=5)).isoformat(), "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.7, "volume": 2.0},
            ]
        },
    )
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
    monkeypatch.setenv("POLYMARKET_PAPER_TRADING", "1")
    daemon = V8RealtimeDaemon(
        db=db,
        config=DaemonConfig(once=True, use_wss=False, prior_artifact=tmp_path / "missing_prior.pkl"),
        book_store=books,
        ticker_store=ticker,
    )

    result = daemon.step(now=now)

    assert result["status"] == "paper_filled"
    assert result["ticker_source"] == "coinbase_wss"
    assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 1
