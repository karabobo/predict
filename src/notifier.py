"""
notifier.py - durable local notifications with optional webhook/Telegram fanout.

SQLite notification events are the default channel. Telegram is kept only as an
explicit opt-in compatibility path because network delivery has been unstable.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from fetch_markets import DB_PATH

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):
        return False


ENV_PATH = Path(__file__).parent.parent / ".env"
TELEGRAM_TIMEOUT_SECONDS = 10
WEBHOOK_TIMEOUT_SECONDS = 10


def _load_env() -> None:
    load_dotenv(ENV_PATH)


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value or None


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def telegram_configured() -> bool:
    _load_env()
    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    enabled = _env_bool("TELEGRAM_NOTIFICATIONS_ENABLED", False)
    return bool(enabled and token and chat_id)


def send_telegram_message(text: str) -> bool:
    _load_env()
    if not telegram_configured():
        return False

    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    prefix = _env("TELEGRAM_NOTIFY_PREFIX") or "predict"

    payload = {
        "chat_id": chat_id,
        "text": f"[{prefix}]\n{text}",
        "disable_web_page_preview": True,
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        response = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"Telegram notify failed: {exc}")
        return False


def ensure_notification_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT,
            channels TEXT,
            delivered INTEGER DEFAULT 0,
            error_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notification_events_type_time
        ON notification_events(event_type, created_at DESC)
        """
    )
    db.commit()


def log_notification_event(
    *,
    event_type: str,
    title: str,
    message: str,
    payload: dict[str, Any] | None = None,
    channels: list[str] | None = None,
    delivered: bool = False,
    error_text: str | None = None,
    db: sqlite3.Connection | None = None,
) -> int | None:
    close_db = False
    if db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        close_db = True
    try:
        ensure_notification_schema(db)
        cursor = db.execute(
            """
            INSERT INTO notification_events (
                event_type, title, message, payload_json, channels,
                delivered, error_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                title,
                message,
                json.dumps(payload or {}, sort_keys=True),
                ",".join(channels or ["sqlite"]),
                1 if delivered else 0,
                error_text,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
        return int(cursor.lastrowid)
    finally:
        if close_db:
            db.close()


def webhook_configured() -> bool:
    _load_env()
    return bool(_env_bool("WEBHOOK_NOTIFICATIONS_ENABLED", True) and _env("NOTIFICATION_WEBHOOK_URL"))


def send_webhook_message(
    *,
    event_type: str,
    title: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    _load_env()
    url = _env("NOTIFICATION_WEBHOOK_URL")
    if not webhook_configured() or not url:
        return False
    body = {
        "event_type": event_type,
        "title": title,
        "message": message,
        "payload": payload or {},
        "source": _env("NOTIFICATION_SOURCE") or "predict",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = requests.post(url, json=body, timeout=WEBHOOK_TIMEOUT_SECONDS)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"Webhook notify failed: {exc}")
        return False


def send_notification(
    *,
    event_type: str,
    title: str,
    message: str,
    payload: dict[str, Any] | None = None,
    db: sqlite3.Connection | None = None,
) -> bool:
    _load_env()
    channels: list[str] = []
    delivered = False
    errors: list[str] = []

    if _env_bool("NOTIFICATION_SQLITE_ENABLED", True):
        channels.append("sqlite")

    if webhook_configured():
        channels.append("webhook")
        if send_webhook_message(event_type=event_type, title=title, message=message, payload=payload):
            delivered = True
        else:
            errors.append("webhook_failed")

    if telegram_configured():
        channels.append("telegram")
        if send_telegram_message(f"{title}\n{message}"):
            delivered = True
        else:
            errors.append("telegram_failed")

    if "sqlite" in channels:
        log_notification_event(
            event_type=event_type,
            title=title,
            message=message,
            payload=payload,
            channels=channels,
            delivered=delivered,
            error_text=",".join(errors) if errors else None,
            db=db,
        )
        delivered = True

    return delivered


def notify_baseline_trade(
    *,
    market_id: str,
    question: str,
    cycle: int,
    signal: dict[str, Any],
    regime_label: str,
    market_price_yes: float,
    seconds_to_expiry: int | None = None,
) -> bool:
    if not signal.get("should_trade"):
        return False

    estimate = float(signal.get("estimate", 0.5))
    direction = signal.get("direction") or ("UP" if estimate >= 0.5 else "DOWN")
    confidence = str(signal.get("confidence", "low"))
    conviction = int(signal.get("conviction_score", 0))
    reason = str(signal.get("reason", "")).strip()

    message = "\n".join(
        [
            "Baseline trade signal",
            f"cycle: {cycle}",
            f"market_id: {market_id}",
            f"question: {question}",
            f"direction: {direction}",
            f"estimate: {estimate:.2%}",
            f"market_yes: {market_price_yes:.2%}",
            f"seconds_to_expiry: {seconds_to_expiry if seconds_to_expiry is not None else 'n/a'}",
            f"confidence: {confidence}",
            f"conviction: {conviction}",
            f"regime: {regime_label}",
            f"reason: {reason or 'n/a'}",
        ]
    )
    return send_notification(
        event_type="trade_signal",
        title="Baseline trade signal",
        message=message,
        payload={
            "market_id": market_id,
            "cycle": cycle,
            "direction": direction,
            "estimate": estimate,
            "market_price_yes": market_price_yes,
            "confidence": confidence,
            "conviction_score": conviction,
            "regime_label": regime_label,
        },
    )


def notify_deepseek_promotion(
    *,
    run_id: str,
    baseline: str,
    challenger: str,
    results: dict[str, Any],
    report_path: Path,
) -> bool:
    gate = results.get("gate", {})
    if challenger != "deepseek_v3" or not gate.get("passed"):
        return False

    fold_bits = []
    for fold in gate.get("fold_checks", []):
        fold_bits.append(
            f"fold {fold['fold_index']}: "
            f"ROI {fold['roi_delta']:+.2f}pp, "
            f"WR {fold['win_rate_delta']:+.2f}pp, "
            f"trade_ratio {fold['trade_ratio']:.2f}"
        )

    message = "\n".join(
        [
            "DeepSeek promotion PASSED",
            f"run_id: {run_id}",
            f"baseline: {baseline}",
            f"challenger: {challenger}",
            f"aggregate_roi_delta: {gate.get('aggregate_roi_delta', 0.0):+.2f}pp",
            f"aggregate_wr_delta: {gate.get('aggregate_win_rate_delta', 0.0):+.2f}pp",
            f"passing_folds: {gate.get('passing_folds', 0)}/{len(gate.get('fold_checks', []))}",
            f"trade_ratio: {gate.get('trade_ratio', 0.0):.2f}",
            f"drawdown_ratio: {gate.get('drawdown_ratio', 0.0):.2f}",
            "details:",
            *(fold_bits or ["no fold details"]),
            f"report: {report_path}",
        ]
    )
    return send_notification(
        event_type="promotion_passed",
        title="DeepSeek promotion PASSED",
        message=message,
        payload={
            "run_id": run_id,
            "baseline": baseline,
            "challenger": challenger,
            "report_path": str(report_path),
            "gate": gate,
        },
    )
