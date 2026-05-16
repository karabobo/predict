"""
paper_trading.py - deterministic paper order ledger for promoted profiles.

This module mirrors the live-order planning rules without touching the CLOB SDK.
It records simulated fills against the market prices stored with each market row,
so paper execution can be enabled before live trading credentials exist.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_trading import LiveTradingConfig, build_trade_plan
from v3.l2_replay import BookState, SimulatedFill

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PaperTradingConfig:
    enabled: bool
    min_edge: float
    min_seconds_to_expiry: int
    medium_bet_usd: float
    high_bet_usd: float


def load_paper_trading_config() -> PaperTradingConfig:
    return PaperTradingConfig(
        enabled=_env_bool("POLYMARKET_PAPER_TRADING", False),
        min_edge=float(os.getenv("POLYMARKET_PAPER_MIN_EDGE", "0.01")),
        min_seconds_to_expiry=int(os.getenv("POLYMARKET_PAPER_MIN_SECONDS_TO_EXPIRY", "45")),
        medium_bet_usd=float(os.getenv("POLYMARKET_PAPER_MEDIUM_BET_USD", "75")),
        high_bet_usd=float(os.getenv("POLYMARKET_PAPER_HIGH_BET_USD", "150")),
    )


def ensure_paper_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER,
            market_id TEXT NOT NULL,
            token_id TEXT,
            direction TEXT,
            side TEXT,
            confidence TEXT,
            conviction_score INTEGER,
            bet_amount_usd REAL,
            predicted_prob REAL,
            market_price REAL,
            expected_edge REAL,
            shares REAL,
            status TEXT,
            error_text TEXT,
            created_at TEXT,
            FOREIGN KEY (prediction_id) REFERENCES predictions(id),
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_prediction_once
        ON paper_orders(prediction_id)
        """
    )
    for column in (
        "rule_profile TEXT",
        "rule_name TEXT",
        "entry_offset_seconds INTEGER",
        "prior_prob REAL",
        "prior_edge REAL",
        "book_hash TEXT",
        "best_bid REAL",
        "best_ask REAL",
        "spread REAL",
        "fill_source TEXT",
        "settlement_outcome INTEGER",
        "settlement_source TEXT",
        "settled_at TEXT",
        "won INTEGER",
        "pnl_usd REAL",
        "roi REAL",
    ):
        _ensure_column(db, "paper_orders", column)
    db.commit()


def _as_live_config(config: PaperTradingConfig) -> LiveTradingConfig:
    return LiveTradingConfig(
        enabled=True,
        dry_run=True,
        host="paper",
        chain_id=137,
        signature_type=0,
        private_key=None,
        funder=None,
        api_key=None,
        api_secret=None,
        api_passphrase=None,
        order_type="FAK",
        min_edge=config.min_edge,
        min_seconds_to_expiry=config.min_seconds_to_expiry,
        medium_bet_usd=config.medium_bet_usd,
        high_bet_usd=config.high_bet_usd,
    )


def _pending_paper_predictions(db: sqlite3.Connection) -> list[dict[str, Any]]:
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        SELECT
            p.id AS prediction_id,
            p.market_id,
            p.agent,
            p.estimate,
            p.confidence,
            p.predicted_at,
            p.cycle,
            p.conviction_score,
            p.should_trade,
            p.model_version,
            p.prediction_source,
            m.question,
            m.end_date,
            m.price_yes,
            m.price_no,
            m.condition_id,
            m.token_yes,
            m.token_no
        FROM predictions p
        JOIN markets m ON m.id = p.market_id
        WHERE m.resolved = 0
          AND m.end_date > ?
          AND COALESCE(p.should_trade, 1) = 1
          AND COALESCE(p.conviction_score, 0) >= 3
          AND NOT EXISTS (
              SELECT 1
              FROM paper_orders po
              WHERE po.prediction_id = p.id
          )
        ORDER BY m.end_date ASC, p.predicted_at ASC
        """,
        (now_iso,),
    )
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _log_paper_order(
    db: sqlite3.Connection,
    row: dict[str, Any],
    plan: dict[str, Any] | None,
    *,
    status: str,
    error_text: str | None = None,
) -> None:
    shares = None
    if plan and plan["market_price"] > 0:
        shares = float(plan["bet_amount_usd"]) / float(plan["market_price"])
    db.execute(
        """
        INSERT OR IGNORE INTO paper_orders (
            prediction_id, market_id, token_id, direction, side, confidence,
            conviction_score, bet_amount_usd, predicted_prob, market_price,
            expected_edge, shares, status, error_text, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["prediction_id"],
            row["market_id"],
            plan.get("token_id") if plan else None,
            plan.get("direction") if plan else None,
            plan.get("side") if plan else None,
            row.get("confidence"),
            row.get("conviction_score"),
            plan.get("bet_amount_usd") if plan else None,
            plan.get("predicted_prob") if plan else None,
            plan.get("market_price") if plan else None,
            plan.get("expected_edge") if plan else None,
            shares,
            status,
            error_text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def _ensure_column(db: sqlite3.Connection, table: str, column_definition: str) -> None:
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")
        db.commit()
    except sqlite3.OperationalError:
        pass


def execute_book_paper_order(
    db: sqlite3.Connection,
    *,
    market_id: str,
    question: str,
    token_yes: str | None,
    token_no: str | None,
    signal: dict[str, Any],
    yes_book: BookState | None,
    no_book: BookState | None = None,
    config: PaperTradingConfig | None = None,
    prediction_id: int | None = None,
    rule_profile: str = "v8_integrated_candidate",
    entry_offset_seconds: int | None = None,
    prior_prob: float | None = None,
    prior_edge: float | None = None,
) -> dict[str, Any]:
    ensure_paper_schema(db)
    config = config or load_paper_trading_config()
    if not config.enabled:
        return {"market_id": market_id, "status": "paper_disabled"}
    if _has_existing_primary_order(db, market_id=market_id, rule_profile=rule_profile):
        return {"market_id": market_id, "status": "duplicate_market_profile"}

    direction = str(signal.get("direction") or "").upper()
    if direction not in {"UP", "DOWN"}:
        _log_book_paper_order(
            db,
            market_id=market_id,
            prediction_id=prediction_id,
            signal=signal,
            status="skipped_missing_direction",
            error_text="missing_direction",
            rule_profile=rule_profile,
            entry_offset_seconds=entry_offset_seconds,
            prior_prob=prior_prob,
            prior_edge=prior_edge,
        )
        return {"market_id": market_id, "status": "skipped_missing_direction"}

    amount_usd = _bet_amount_from_signal(signal, config)
    if amount_usd <= 0:
        _log_book_paper_order(
            db,
            market_id=market_id,
            prediction_id=prediction_id,
            signal=signal,
            status="skipped_no_bet_size",
            error_text="no_bet_size",
            rule_profile=rule_profile,
            entry_offset_seconds=entry_offset_seconds,
            prior_prob=prior_prob,
            prior_edge=prior_edge,
        )
        return {"market_id": market_id, "status": "skipped_no_bet_size"}

    book = no_book if direction == "DOWN" and no_book is not None else yes_book
    if book is None:
        _log_book_paper_order(
            db,
            market_id=market_id,
            prediction_id=prediction_id,
            signal=signal,
            status="skipped_book_not_ready",
            error_text="book_not_ready",
            rule_profile=rule_profile,
            entry_offset_seconds=entry_offset_seconds,
            prior_prob=prior_prob,
            prior_edge=prior_edge,
        )
        return {"market_id": market_id, "status": "skipped_book_not_ready"}

    if direction == "DOWN" and no_book is not None:
        fill = book.simulate_market_buy(amount_usd)
    else:
        fill = book.simulate_market_buy_outcome("YES" if direction == "UP" else "NO", amount_usd)
    if fill.average_price is None or fill.filled_ratio <= 0:
        status = "skipped_no_l2_liquidity"
        _log_book_paper_order(
            db,
            market_id=market_id,
            prediction_id=prediction_id,
            signal=signal,
            status=status,
            error_text="no_l2_liquidity",
            fill=fill,
            book=book,
            rule_profile=rule_profile,
            entry_offset_seconds=entry_offset_seconds,
            prior_prob=prior_prob,
            prior_edge=prior_edge,
        )
        return {"market_id": market_id, "status": status}

    predicted_prob = _predicted_prob(signal, direction)
    expected_edge = predicted_prob - float(fill.average_price)
    if expected_edge < config.min_edge:
        status = "skipped_edge_below_threshold"
        _log_book_paper_order(
            db,
            market_id=market_id,
            prediction_id=prediction_id,
            signal=signal,
            status=status,
            error_text="edge_below_threshold",
            fill=fill,
            book=book,
            rule_profile=rule_profile,
            entry_offset_seconds=entry_offset_seconds,
            prior_prob=prior_prob,
            prior_edge=prior_edge,
        )
        return {"market_id": market_id, "status": status, "expected_edge": expected_edge}

    _log_book_paper_order(
        db,
        market_id=market_id,
        prediction_id=prediction_id,
        signal=signal,
        status="paper_filled",
        fill=fill,
        book=book,
        rule_profile=rule_profile,
        entry_offset_seconds=entry_offset_seconds,
        prior_prob=prior_prob,
        prior_edge=prior_edge,
        token_id=token_yes if direction == "UP" else token_no,
    )
    return {
        "market_id": market_id,
        "status": "paper_filled",
        "direction": direction,
        "expected_edge": expected_edge,
        "average_price": fill.average_price,
        "shares": fill.shares,
    }


def _has_existing_primary_order(db: sqlite3.Connection, *, market_id: str, rule_profile: str) -> bool:
    row = db.execute(
        """
        SELECT 1
        FROM paper_orders
        WHERE market_id = ?
          AND COALESCE(rule_profile, '') = ?
          AND status = 'paper_filled'
        LIMIT 1
        """,
        (market_id, rule_profile),
    ).fetchone()
    return row is not None


def _bet_amount_from_signal(signal: dict[str, Any], config: PaperTradingConfig) -> float:
    confidence = str(signal.get("confidence") or "").lower()
    conviction = int(signal.get("conviction_score") or 0)
    if confidence == "high" or conviction >= 4:
        return config.high_bet_usd
    if confidence == "medium" or conviction >= 3:
        return config.medium_bet_usd
    return 0.0


def _predicted_prob(signal: dict[str, Any], direction: str) -> float:
    estimate = max(0.0, min(1.0, float(signal.get("estimate", 0.5))))
    return estimate if direction == "UP" else 1.0 - estimate


def _log_book_paper_order(
    db: sqlite3.Connection,
    *,
    market_id: str,
    prediction_id: int | None,
    signal: dict[str, Any],
    status: str,
    rule_profile: str,
    entry_offset_seconds: int | None,
    prior_prob: float | None,
    prior_edge: float | None,
    error_text: str | None = None,
    fill: SimulatedFill | None = None,
    book: BookState | None = None,
    token_id: str | None = None,
) -> None:
    direction = str(signal.get("direction") or "").upper() or None
    metrics = book.metrics() if book is not None else None
    average_price = fill.average_price if fill else None
    predicted_prob = _predicted_prob(signal, direction or "UP") if direction else None
    expected_edge = predicted_prob - average_price if predicted_prob is not None and average_price is not None else None
    db.execute(
        """
        INSERT OR IGNORE INTO paper_orders (
            prediction_id, market_id, token_id, direction, side, confidence,
            conviction_score, bet_amount_usd, predicted_prob, market_price,
            expected_edge, shares, status, error_text, created_at,
            rule_profile, rule_name, entry_offset_seconds, prior_prob, prior_edge,
            book_hash, best_bid, best_ask, spread, fill_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            market_id,
            token_id,
            direction,
            "BUY" if direction else None,
            signal.get("confidence"),
            int(signal.get("conviction_score", 0) or 0),
            fill.requested_usdc if fill else None,
            predicted_prob,
            average_price,
            expected_edge,
            fill.shares if fill else None,
            status,
            error_text,
            datetime.now(timezone.utc).isoformat(),
            rule_profile,
            signal.get("strategy_name"),
            entry_offset_seconds,
            prior_prob,
            prior_edge,
            metrics.book_hash if metrics else None,
            metrics.best_bid if metrics else None,
            metrics.best_ask if metrics else None,
            metrics.spread if metrics else None,
            "wss_book" if book is not None else None,
        ),
    )
    db.commit()


def execute_paper_orders(
    db: sqlite3.Connection | None = None,
    config: PaperTradingConfig | None = None,
) -> list[dict[str, Any]]:
    config = config or load_paper_trading_config()
    if not config.enabled:
        print("  Paper trading disabled")
        return []

    close_db = False
    if db is None:
        db = sqlite3.connect(DB_PATH)
        close_db = True

    try:
        ensure_paper_schema(db)
        pending = _pending_paper_predictions(db)
        if not pending:
            print("  No pending paper orders")
            return []

        live_config = _as_live_config(config)
        results: list[dict[str, Any]] = []
        for row in pending:
            plan, reason = build_trade_plan(row, live_config)
            if reason:
                _log_paper_order(db, row, None, status=f"skipped_{reason}", error_text=reason)
                results.append({"market_id": row["market_id"], "status": f"skipped_{reason}"})
                continue
            _log_paper_order(db, row, plan, status="paper_filled")
            results.append(
                {
                    "market_id": row["market_id"],
                    "status": "paper_filled",
                    "direction": plan["direction"],
                    "expected_edge": plan["expected_edge"],
                }
            )
        return results
    finally:
        if close_db:
            db.close()


def settle_paper_orders(
    db: sqlite3.Connection | None = None,
    *,
    include_provisional: bool = True,
) -> dict[str, int | float]:
    close_db = False
    if db is None:
        db = sqlite3.connect(DB_PATH)
        close_db = True

    try:
        ensure_paper_schema(db)
        cursor = db.execute(
            """
            SELECT
                po.id,
                po.direction,
                po.bet_amount_usd,
                po.market_price,
                po.shares,
                m.outcome,
                m.provisional_outcome,
                m.official_resolved_at,
                m.provisional_resolved_at
            FROM paper_orders po
            JOIN markets m ON m.id = po.market_id
            WHERE po.status = 'paper_filled'
              AND po.settled_at IS NULL
              AND (
                  m.resolved = 1
                  OR (? = 1 AND m.provisional_outcome IS NOT NULL)
              )
            ORDER BY po.id ASC
            """,
            (1 if include_provisional else 0,),
        )
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        settled = 0
        wins = 0
        pnl_total = 0.0
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in rows:
            official_outcome = row.get("outcome")
            provisional_outcome = row.get("provisional_outcome")
            if official_outcome is not None:
                outcome = int(official_outcome)
                source = "official"
                settled_at = row["official_resolved_at"] or now_iso
            elif include_provisional and provisional_outcome is not None:
                outcome = int(provisional_outcome)
                source = "provisional"
                settled_at = row["provisional_resolved_at"] or now_iso
            else:
                continue

            direction = str(row["direction"] or "").upper()
            won = (direction == "UP" and outcome == 1) or (direction == "DOWN" and outcome == 0)
            bet_amount = float(row["bet_amount_usd"] or 0.0)
            entry_price = float(row["market_price"] or 0.0)
            shares = float(row["shares"] or 0.0)
            if won:
                pnl = shares * max(1.0 - entry_price, 0.0)
            else:
                pnl = -bet_amount
            roi = pnl / bet_amount if bet_amount > 0 else None

            db.execute(
                """
                UPDATE paper_orders
                SET settlement_outcome = ?,
                    settlement_source = ?,
                    settled_at = ?,
                    won = ?,
                    pnl_usd = ?,
                    roi = ?
                WHERE id = ?
                """,
                (outcome, source, settled_at, 1 if won else 0, pnl, roi, row["id"]),
            )
            settled += 1
            wins += 1 if won else 0
            pnl_total += pnl

        db.commit()
        return {"settled": settled, "wins": wins, "pnl_usd": pnl_total}
    finally:
        if close_db:
            db.close()


def paper_performance_summary(db: sqlite3.Connection) -> dict[str, Any]:
    ensure_paper_schema(db)
    cursor = db.execute(
        """
        SELECT
            COUNT(*) AS settled,
            SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(COALESCE(bet_amount_usd, 0.0)) AS stake,
            SUM(COALESCE(pnl_usd, 0.0)) AS pnl
        FROM paper_orders
        WHERE settled_at IS NOT NULL
        """
    )
    raw = cursor.fetchone()
    columns = [col[0] for col in cursor.description]
    row = dict(zip(columns, raw)) if raw else {}
    settled = int(row.get("settled") or 0)
    wins = int(row.get("wins") or 0)
    stake = float(row.get("stake") or 0.0)
    pnl = float(row.get("pnl") or 0.0)
    return {
        "settled": settled,
        "wins": wins,
        "win_rate": wins / settled if settled else 0.0,
        "stake_usd": stake,
        "pnl_usd": pnl,
        "roi": pnl / stake if stake else 0.0,
    }

if __name__ == "__main__":
    execute_paper_orders()
