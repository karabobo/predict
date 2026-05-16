import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notifier import (
    ensure_notification_schema,
    notify_baseline_trade,
    send_notification,
    telegram_configured,
)


def test_send_notification_records_sqlite_event(monkeypatch):
    db = sqlite3.connect(":memory:")
    ensure_notification_schema(db)
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS_ENABLED", "0")

    delivered = send_notification(
        event_type="test_event",
        title="Test",
        message="payload",
        payload={"market_id": "m1"},
        db=db,
    )

    assert delivered is True
    row = db.execute(
        "SELECT event_type, title, message, channels, delivered, payload_json FROM notification_events"
    ).fetchone()
    assert row[0] == "test_event"
    assert row[1] == "Test"
    assert row[2] == "payload"
    assert row[3] == "sqlite"
    assert row[4] == 0
    assert '"market_id": "m1"' in row[5]


def test_telegram_is_opt_in(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS_ENABLED", "0")

    assert telegram_configured() is False


def test_baseline_trade_uses_durable_notification(monkeypatch, tmp_path):
    db_path = tmp_path / "notify.db"
    db = sqlite3.connect(db_path)
    ensure_notification_schema(db)
    db.close()
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS_ENABLED", "0")
    monkeypatch.setattr("notifier.DB_PATH", db_path)

    sent = notify_baseline_trade(
        market_id="m1",
        question="BTC up?",
        cycle=1,
        signal={
            "should_trade": True,
            "estimate": 0.62,
            "direction": "UP",
            "confidence": "medium",
            "conviction_score": 3,
            "reason": "test",
        },
        regime_label="test_regime",
        market_price_yes=0.50,
        seconds_to_expiry=295,
    )

    assert sent is True
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT event_type, title, message FROM notification_events").fetchone()
    finally:
        db.close()
    assert row[0] == "trade_signal"
    assert row[1] == "Baseline trade signal"
    assert "direction: UP" in row[2]
