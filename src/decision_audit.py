"""
decision_audit.py - explain realtime paper decisions at entry windows.

Each 5s/15s evaluation writes stage rows so the UI can explain why a market did
or did not produce a paper order.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def ensure_decision_audit_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            cycle INTEGER,
            rule_profile TEXT,
            entry_offset_seconds INTEGER,
            stage TEXT NOT NULL,
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
            created_at TEXT NOT NULL,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_audit_market_time
        ON decision_audit(market_id, created_at DESC)
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_audit_stage_time
        ON decision_audit(stage, created_at DESC)
        """
    )
    db.commit()


def log_decision_audit(
    db: sqlite3.Connection,
    *,
    market_id: str,
    cycle: int,
    rule_profile: str,
    entry_offset_seconds: int,
    stage: str,
    rule_name: str | None = None,
    signal: dict[str, Any] | None = None,
    reason: str | None = None,
    prior_prob: float | None = None,
    prior_direction: str | None = None,
    prior_edge: float | None = None,
    passed: bool | None = None,
    book_ready: bool | None = None,
    best_bid: float | None = None,
    best_ask: float | None = None,
    spread: float | None = None,
    status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    ensure_decision_audit_schema(db)
    signal = signal or {}
    row_reason = reason if reason is not None else _str_or_none(signal.get("reason"))
    cursor = db.execute(
        """
        INSERT INTO decision_audit (
            market_id, cycle, rule_profile, entry_offset_seconds, stage, rule_name,
            should_trade, direction, estimate, confidence, conviction_score, reason,
            prior_prob, prior_direction, prior_edge, passed,
            book_ready, best_bid, best_ask, spread, status, payload_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_id,
            int(cycle),
            rule_profile,
            int(entry_offset_seconds),
            stage,
            rule_name or _str_or_none(signal.get("strategy_name")),
            _bool_int(signal.get("should_trade")),
            _str_or_none(signal.get("direction")),
            _float_or_none(signal.get("estimate")),
            _str_or_none(signal.get("confidence")),
            _int_or_none(signal.get("conviction_score")),
            row_reason,
            prior_prob,
            prior_direction,
            prior_edge,
            _bool_int(passed),
            _bool_int(book_ready),
            best_bid,
            best_ask,
            spread,
            status,
            json.dumps(payload or {}, sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()
    return int(cursor.lastrowid)


def log_rule_evaluations(
    db: sqlite3.Connection,
    *,
    market_id: str,
    cycle: int,
    rule_profile: str,
    entry_offset_seconds: int,
    evaluations: Any,
) -> None:
    for evaluation in evaluations:
        signal = getattr(evaluation, "signal", {}) or {}
        error = getattr(evaluation, "error", None)
        log_decision_audit(
            db,
            market_id=market_id,
            cycle=cycle,
            rule_profile=rule_profile,
            entry_offset_seconds=entry_offset_seconds,
            stage="rule",
            rule_name=getattr(evaluation, "rule_name", None),
            signal=signal,
            reason=error or signal.get("reason"),
            status="error" if error else "trade" if signal.get("should_trade") else "no_trade",
        )


def _bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
