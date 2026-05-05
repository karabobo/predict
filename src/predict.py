"""
predict.py — Production prediction orchestration.

Production uses one deterministic baseline strategy:
streak momentum + confirmation + regime filter.

AI remains available for offline research, not for the per-market live path.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundation_shadow import (
    ensure_shadow_schema,
    fetch_order_book_snapshot,
    foundation_shadow_prediction,
    store_order_book_snapshot,
    store_shadow_prediction,
)
from notifier import notify_baseline_trade
from strategies.momentum import contrarian_signal as _baseline_signal
from strategies.regime import compute_regime_from_candles as _compute_regime_from_candles

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
ACTIVE_AGENT = "contrarian_rule"
DEFAULT_ALPHA_RULES = (
    "baseline_router_v2,"
    "baseline_router_v1_plus_sparse_combo,"
    "baseline_current"
)


def _parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_regime_from_candles(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Backwards-compatible export for tests and older callers."""
    return _compute_regime_from_candles(candles)


def contrarian_signal(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Backwards-compatible export for the current baseline signal."""
    return _baseline_signal(candles)


def ensure_db_schema(db: sqlite3.Connection) -> None:
    for column in [
        "regime TEXT",
        "conviction_score TEXT",
        "model_version TEXT",
        "should_trade INTEGER DEFAULT 1",
        "market_price_yes_snapshot REAL",
        "seconds_to_expiry INTEGER",
    ]:
        try:
            db.execute(f"ALTER TABLE predictions ADD COLUMN {column}")
            db.commit()
        except sqlite3.OperationalError:
            pass


def _configured_alpha_rules() -> list[str]:
    raw = os.getenv("PREDICT_ALPHA_RULES", DEFAULT_ALPHA_RULES)
    rules = [part.strip() for part in raw.split(",") if part.strip()]
    return rules or ["baseline_current"]


def _normalize_signal(signal: dict[str, Any], strategy_name: str) -> dict[str, Any]:
    normalized = dict(signal)
    normalized["estimate"] = min(max(float(normalized.get("estimate", 0.5)), 0.0), 1.0)
    normalized["should_trade"] = bool(normalized.get("should_trade", False))
    normalized["conviction_score"] = int(normalized.get("conviction_score", 0) or 0)
    normalized["confidence"] = str(normalized.get("confidence", "low") or "low")
    normalized["reason"] = str(normalized.get("reason", "") or strategy_name)
    normalized["strategy_name"] = strategy_name
    meta = normalized.get("meta")
    normalized["meta"] = dict(meta) if isinstance(meta, dict) else {}
    normalized["meta"]["strategy_name"] = strategy_name
    return normalized


def alpha_router_signal(
    candles: list[dict[str, Any]],
    regime: dict[str, Any],
    *,
    rule_names: list[str] | None = None,
) -> dict[str, Any]:
    """Run production-approved deterministic alpha rules in priority order.

    This intentionally excludes ML foundation and order-book rules: they need
    separate model persistence / live microstructure plumbing before production.
    """
    from v3.rule_variants import available_rules

    rules = available_rules()
    configured = rule_names or _configured_alpha_rules()
    errors: list[str] = []

    for name in configured:
        rule = rules.get(name)
        if rule is None:
            errors.append(f"{name}:missing")
            continue
        try:
            signal = _normalize_signal(rule(candles, regime), name)
        except Exception as exc:
            errors.append(f"{name}:{exc}")
            continue
        if signal.get("should_trade"):
            if errors:
                signal["reason"] = f"{signal['reason']} | router_warnings={';'.join(errors)}"
            return signal

    fallback = _normalize_signal(contrarian_signal(candles), "baseline_current_fallback")
    fallback["should_trade"] = False
    fallback["estimate"] = 0.5
    fallback["direction"] = None
    reason_parts = ["alpha_router_no_trade"]
    if errors:
        reason_parts.append(f"router_warnings={';'.join(errors)}")
    fallback["reason"] = " | ".join(reason_parts)
    fallback["conviction_score"] = 0
    fallback["confidence"] = "low"
    fallback["strategy_name"] = "alpha_router_no_trade"
    fallback["meta"]["strategy_name"] = "alpha_router_no_trade"
    return fallback


def store_prediction(
    db: sqlite3.Connection,
    market_id: str,
    agent: str,
    signal: dict[str, Any],
    regime_label: str,
    cycle: int,
    market_price_yes_snapshot: float | None = None,
    seconds_to_expiry: int | None = None,
) -> int:
    predicted_at = datetime.now(timezone.utc).isoformat()
    estimate = float(signal.get("estimate", 0.5))
    confidence = str(signal.get("confidence", "low"))
    conviction_score = int(signal.get("conviction_score", 0))
    reasoning = str(signal.get("reason", ""))
    should_trade = 1 if signal.get("should_trade", False) else 0
    model_version = str(signal.get("strategy_name", "baseline_current"))

    db.execute(
        """
        INSERT INTO predictions
        (market_id, agent, estimate, edge, confidence, reasoning, predicted_at, cycle,
         conviction_score, regime, should_trade, market_price_yes_snapshot, seconds_to_expiry,
         model_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_id,
            agent,
            estimate,
            abs(estimate - 0.5),
            confidence,
            reasoning,
            predicted_at,
            cycle,
            conviction_score,
            regime_label,
            should_trade,
            market_price_yes_snapshot,
            seconds_to_expiry,
            model_version,
        ),
    )
    db.commit()
    return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])


def _apply_regime_filter(signal: dict[str, Any], regime: dict[str, Any]) -> dict[str, Any]:
    """Skip toxic mean-reverting regimes while preserving the baseline signal structure."""
    if not regime.get("is_mean_reverting"):
        signal["regime_label"] = regime["label"]
        return signal

    filtered = dict(signal)
    filtered["estimate"] = 0.5
    filtered["should_trade"] = False
    filtered["direction"] = None
    filtered["confidence"] = "low"
    filtered["conviction_score"] = 0
    filtered["reason"] = f"{signal.get('reason', 'signal')} | regime_filter"
    filtered["regime_label"] = regime["label"]
    return filtered


def run_predictions(cycle: int = 1, market_limit: int = 5, btc_data: dict[str, Any] | None = None) -> None:
    print(f"--- Cycle {cycle}: Alpha Router ---")
    db = sqlite3.connect(DB_PATH)
    ensure_db_schema(db)
    ensure_shadow_schema(db)

    if btc_data is None:
        from btc_data import fetch_btc_candles

        market_data = fetch_btc_candles(limit=20)
    else:
        market_data = btc_data

    if not market_data or not market_data.get("candles"):
        print("Error: No BTC data")
        db.close()
        return

    regime = compute_regime_from_candles(market_data["candles"])
    print(f"Regime: {regime['label']}")

    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        SELECT id, question, price_yes, end_date, token_yes
        FROM markets
        WHERE resolved = 0 AND end_date > ?
        ORDER BY end_date ASC
        LIMIT ?
        """,
        (now_iso, market_limit),
    )
    markets = cursor.fetchall()

    if not markets:
        print("No active markets.")
        db.close()
        return

    raw_signal = contrarian_signal(market_data["candles"])
    alpha_signal = alpha_router_signal(market_data["candles"], regime)
    foundation_candidate_signal = alpha_router_signal(
        market_data["candles"],
        regime,
        rule_names=["baseline_router_v2_candidate_filter"],
    )
    final_signal = alpha_signal if alpha_signal.get("should_trade") else _apply_regime_filter(raw_signal, regime)
    if not final_signal.get("should_trade") and alpha_signal.get("reason"):
        final_signal["reason"] = f"{final_signal.get('reason', 'signal')} | {alpha_signal['reason']}"
        final_signal["strategy_name"] = alpha_signal.get("strategy_name", "alpha_router_no_trade")
    print(f"Strategy: {final_signal.get('strategy_name', 'baseline_current')}")

    for market_id, question, price_yes, end_date, token_yes in markets:
        seconds_to_expiry = max(int((_parse_timestamp(end_date) - datetime.now(timezone.utc)).total_seconds()), 0)
        prediction_id = store_prediction(
            db,
            market_id,
            ACTIVE_AGENT,
            final_signal,
            regime["label"],
            cycle,
            market_price_yes_snapshot=float(price_yes),
            seconds_to_expiry=seconds_to_expiry,
        )
        book = fetch_order_book_snapshot(token_yes)
        store_order_book_snapshot(db, market_id=market_id, token_id=token_yes, book=book)
        shadow = foundation_shadow_prediction(
            candles=market_data["candles"],
            regime=regime,
            candidate_signal=foundation_candidate_signal,
            final_signal=final_signal,
        )
        if book:
            shadow.setdefault("diagnostics", {})["book"] = {
                "midpoint": book.get("midpoint"),
                "spread_pct": book.get("spread_pct"),
                "depth_imbalance": book.get("depth_imbalance"),
                "bid_depth_5pct": book.get("bid_depth_5pct"),
                "ask_depth_5pct": book.get("ask_depth_5pct"),
            }
        store_shadow_prediction(db, prediction_id=prediction_id, market_id=market_id, shadow=shadow)
        estimate = final_signal["estimate"]
        direction = final_signal.get("direction") or "SKIP"
        shadow_prob = shadow.get("prob_up")
        shadow_text = f" | shadow {float(shadow_prob):.0%}" if shadow_prob is not None else f" | shadow {shadow.get('status')}"
        print(
            f"  [{ACTIVE_AGENT}] {direction} ({estimate:.0%}) "
            f"| market {price_yes:.0%} | tte {seconds_to_expiry}s{shadow_text} | {question[:60]}"
        )
        if final_signal.get("should_trade"):
            notify_baseline_trade(
                market_id=market_id,
                question=question,
                cycle=cycle,
                signal=final_signal,
                regime_label=regime["label"],
                market_price_yes=float(price_yes),
                seconds_to_expiry=seconds_to_expiry,
            )

    db.close()
    print(f"\nBaseline predictions stored in {DB_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument("--markets", type=int, default=5)
    args = parser.parse_args()
    run_predictions(cycle=args.cycle, market_limit=args.markets)
