#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.foundation_shadow import DEFAULT_ARTIFACT_PATH, DEFAULT_METADATA_PATH
from src.strategies.regime import compute_regime_from_candles
from src.v3.backtest_rules import build_contexts, load_candles
from src.v3.probability_foundation import ProbabilityFoundationService
from src.v3.rule_variants import available_rules


DEFAULT_BACKTEST_DB = ROOT / "data" / "polymarket_backtest.db"
DEFAULT_BTC_CANDLES = Path("/root/Data/btc_5m.parquet")


@dataclass(frozen=True)
class TrainingContext:
    market: dict[str, Any]
    formatted_candles: list[dict[str, Any]]
    production_regime: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train foundation shadow model from historical BTC 5m markets")
    parser.add_argument("--db", type=Path, default=DEFAULT_BACKTEST_DB)
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_BTC_CANDLES)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT_PATH)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--limit", type=int, default=0, help="Optional most-recent context limit; 0 uses all")
    parser.add_argument("--from-date", type=str, default=None)
    parser.add_argument("--to-date", type=str, default=None)
    return parser.parse_args()


def load_markets(db_path: Path, from_date: str | None, to_date: str | None) -> list[sqlite3.Row]:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    clauses = ["outcome IS NOT NULL", "window_start_ts IS NOT NULL"]
    params: list[Any] = []
    if from_date:
        clauses.append("end_date >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("end_date <= ?")
        params.append(to_date)
    try:
        return db.execute(
            f"""
            SELECT market_id, question, end_date, outcome, final_price_yes, window_start_ts
            FROM historical_markets
            WHERE {' AND '.join(clauses)}
            ORDER BY end_date ASC, market_id ASC
            """,
            params,
        ).fetchall()
    finally:
        db.close()


def build_training_contexts(
    *,
    db_path: Path,
    btc_candles_path: Path,
    from_date: str | None,
    to_date: str | None,
    limit: int,
    lookback: int = 20,
) -> list[TrainingContext]:
    candles = load_candles(btc_candles_path)
    contexts_by_start, _ = build_contexts(candles, lookback)
    rows = load_markets(db_path, from_date, to_date)
    contexts: list[TrainingContext] = []
    for row in rows:
        context_pack = contexts_by_start.get(int(row["window_start_ts"]))
        if context_pack is None:
            continue
        formatted = context_pack["context_candles"]
        contexts.append(
            TrainingContext(
                market={
                    "market_id": row["market_id"],
                    "question": row["question"],
                    "end_date": row["end_date"],
                    "outcome": int(row["outcome"]),
                    "implied_price_yes": float(row["final_price_yes"] or 0.5),
                    "timestamp": int(row["window_start_ts"]),
                },
                formatted_candles=formatted,
                production_regime=compute_regime_from_candles(formatted),
            )
        )
    if limit > 0:
        return contexts[-limit:]
    return contexts


def main() -> None:
    args = parse_args()
    contexts = build_training_contexts(
        db_path=args.db,
        btc_candles_path=args.btc_candles,
        from_date=args.from_date,
        to_date=args.to_date,
        limit=args.limit,
    )
    rules = available_rules()
    candidate_rule = rules["baseline_router_v2_candidate_filter"]
    service = ProbabilityFoundationService()
    summary = service.fit(contexts, signal_provider=candidate_rule)
    metadata = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "label_source": "market",
        "contexts": len(contexts),
        "db": str(args.db),
        "btc_candles": str(args.btc_candles),
        "candidate_filter": "baseline_router_v2_candidate_filter",
        "summary": {
            "train_samples": summary.train_samples,
            "calibration_samples": summary.calibration_samples,
            "primary_model_name": summary.primary_model_name,
            "secondary_model_name": summary.secondary_model_name,
            "calibrated": summary.calibrated,
            "diagnostics": dict(summary.diagnostics),
        },
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    with args.artifact.open("wb") as handle:
        pickle.dump({"service": service, "metadata": metadata}, handle)
    args.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    print(
        "trained foundation shadow "
        f"contexts={len(contexts)} train={summary.train_samples} cal={summary.calibration_samples} "
        f"primary={summary.primary_model_name} secondary={summary.secondary_model_name} "
        f"artifact={args.artifact}"
    )


if __name__ == "__main__":
    main()
