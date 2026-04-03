"""
predict.py — Production prediction orchestration.

Production uses one deterministic baseline strategy:
streak momentum + confirmation + regime filter.

AI remains available for offline research, not for the per-market live path.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from notifier import notify_baseline_trade
from strategies.momentum import contrarian_signal as _baseline_signal
from strategies.regime import compute_regime_from_candles as _compute_regime_from_candles

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
ACTIVE_AGENT = "contrarian_rule"


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


def store_prediction(
    db: sqlite3.Connection,
    market_id: str,
    agent: str,
    signal: dict[str, Any],
    regime_label: str,
    cycle: int,
    market_price_yes_snapshot: float | None = None,
    seconds_to_expiry: int | None = None,
) -> None:
    predicted_at = datetime.now(timezone.utc).isoformat()
    estimate = float(signal.get("estimate", 0.5))
    confidence = str(signal.get("confidence", "low"))
    conviction_score = int(signal.get("conviction_score", 0))
    reasoning = str(signal.get("reason", ""))
    should_trade = 1 if signal.get("should_trade", False) else 0

    db.execute(
        """
        INSERT INTO predictions
        (market_id, agent, estimate, edge, confidence, reasoning, predicted_at, cycle,
         conviction_score, regime, should_trade, market_price_yes_snapshot, seconds_to_expiry)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    db.commit()


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
    print(f"--- Cycle {cycle}: Baseline Momentum ---")
    db = sqlite3.connect(DB_PATH)
    ensure_db_schema(db)

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
        SELECT id, question, price_yes, end_date
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
    final_signal = _apply_regime_filter(raw_signal, regime)

    for market_id, question, price_yes, end_date in markets:
        seconds_to_expiry = max(int((_parse_timestamp(end_date) - datetime.now(timezone.utc)).total_seconds()), 0)
        store_prediction(
            db,
            market_id,
            ACTIVE_AGENT,
            final_signal,
            regime["label"],
            cycle,
            market_price_yes_snapshot=float(price_yes),
            seconds_to_expiry=seconds_to_expiry,
        )
        estimate = final_signal["estimate"]
        direction = final_signal.get("direction") or "SKIP"
        print(
            f"  [{ACTIVE_AGENT}] {direction} ({estimate:.0%}) "
            f"| market {price_yes:.0%} | tte {seconds_to_expiry}s | {question[:60]}"
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
