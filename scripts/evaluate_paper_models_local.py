#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.strategies.regime import compute_regime_from_candles
from src.v3.arena import build_blocked_folds
from src.v3.backtest_rules import build_contexts, load_candles
from src.v3.probability_foundation import Paper5MModelService


DEFAULT_DB = ROOT / "data" / "polymarket_backtest.db"
DEFAULT_CANDLES = Path("/root/Data/btc_5m.parquet")


@dataclass(frozen=True)
class LocalContext:
    market: dict[str, Any]
    formatted_candles: list[dict[str, Any]]
    production_regime: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate paper-style 5m models on local BTC/market history")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Historical market SQLite DB")
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES, help="Local BTC 5m parquet/csv")
    parser.add_argument("--warm-up", type=int, default=500, help="Warm-up contexts reserved for training")
    parser.add_argument("--folds", type=int, default=4, help="Blocked time-series folds")
    parser.add_argument("--from-date", type=str, default=None, help="Optional inclusive market end_date lower bound")
    parser.add_argument("--to-date", type=str, default=None, help="Optional inclusive market end_date upper bound")
    parser.add_argument(
        "--label-source",
        choices=("market", "btc_next"),
        default="market",
        help="Use Polymarket market outcomes or direct next-BTC-candle direction labels",
    )
    parser.add_argument(
        "--contenders",
        nargs="+",
        default=["paper_xgb_5m", "paper_logreg_5m"],
        help="Contender names to evaluate",
    )
    parser.add_argument(
        "--split-by-vol",
        action="store_true",
        help="Train/evaluate separate models for LOW/MEDIUM/HIGH volatility regimes",
    )
    return parser.parse_args()


def load_markets(db_path: Path, from_date: str | None, to_date: str | None) -> list[sqlite3.Row]:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    clauses = ["1=1"]
    params: list[Any] = []
    if from_date:
        clauses.append("end_date >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("end_date <= ?")
        params.append(to_date)
    query = f"""
        SELECT market_id, question, end_date, outcome, final_price_yes, window_start_ts
        FROM historical_markets
        WHERE {' AND '.join(clauses)}
        ORDER BY end_date ASC, market_id ASC
    """
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def build_local_contexts(
    *,
    db_path: Path,
    btc_candles_path: Path,
    from_date: str | None,
    to_date: str | None,
    lookback: int = 20,
) -> list[LocalContext]:
    candles = load_candles(btc_candles_path)
    contexts_by_start, _ = build_contexts(candles, lookback)
    rows = load_markets(db_path, from_date, to_date)
    contexts: list[LocalContext] = []
    for idx, row in enumerate(rows):
        if row["window_start_ts"] is None or row["outcome"] is None:
            continue
        context_pack = contexts_by_start.get(int(row["window_start_ts"]))
        if context_pack is None:
            continue
        formatted = context_pack["context_candles"]
        contexts.append(
            LocalContext(
                market={
                    "index": idx,
                    "timestamp": int(row["window_start_ts"]),
                    "outcome": int(row["outcome"]),
                    "implied_price_yes": float(row["final_price_yes"] or 0.5),
                },
                formatted_candles=formatted,
                production_regime=compute_regime_from_candles(formatted),
            )
        )
    return contexts


def build_btc_next_contexts(
    *,
    btc_candles_path: Path,
    from_date: str | None,
    to_date: str | None,
    lookback: int = 20,
) -> list[LocalContext]:
    candles = load_candles(btc_candles_path)
    from_ts = int(pd_timestamp(from_date).timestamp()) if from_date else None
    to_ts = int(pd_timestamp(to_date).timestamp()) if to_date else None
    contexts: list[LocalContext] = []
    for idx in range(lookback, len(candles) - 1):
        target = candles[idx]
        next_candle = candles[idx + 1]
        if from_ts is not None and target.ts < from_ts:
            continue
        if to_ts is not None and target.ts > to_ts:
            continue
        formatted = [
            {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles[idx - lookback:idx]
        ]
        outcome = 1 if next_candle.close >= next_candle.open else 0
        contexts.append(
            LocalContext(
                market={
                    "index": idx,
                    "timestamp": target.ts,
                    "outcome": outcome,
                    "implied_price_yes": 0.5,
                },
                formatted_candles=formatted,
                production_regime=compute_regime_from_candles(formatted),
            )
        )
    return contexts


def pd_timestamp(value: str):
    import pandas as pd

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


def summarize_predictions(probs: list[float], outcomes: list[int]) -> dict[str, float | int]:
    n = len(outcomes)
    correct = 0
    tp = fp = tn = fn = 0
    brier_total = 0.0
    for prob_up, outcome in zip(probs, outcomes):
        pred_up = prob_up >= 0.5
        actual_up = outcome == 1
        correct += 1 if pred_up == actual_up else 0
        brier_total += (prob_up - outcome) ** 2
        if pred_up and actual_up:
            tp += 1
        elif pred_up and not actual_up:
            fp += 1
        elif (not pred_up) and actual_up:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        "samples": n,
        "accuracy": correct / n if n else 0.0,
        "avg_brier": brier_total / n if n else 0.0,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def evaluate_model(name: str, contexts: list[LocalContext], warm_up: int, folds: int) -> dict[str, Any]:
    fold_ranges = build_blocked_folds(contexts, warm_up=warm_up, folds=folds)
    if name in {"paper_xgb_5m", "paper_xgb_5m_raw"}:
        service_kind = "xgboost"
    else:
        service_kind = "logreg"
    feature_set = "raw" if name.endswith("_raw") else "derived"
    use_calibration = not name.endswith("_raw")
    fold_rows: list[dict[str, Any]] = []
    all_probs: list[float] = []
    all_outcomes: list[int] = []

    for fold_index, (train_contexts, eval_contexts) in enumerate(fold_ranges):
        service = Paper5MModelService(
            model_kind=service_kind,
            feature_set=feature_set,
            use_calibration=use_calibration,
        )
        summary = service.fit(train_contexts)
        probs: list[float] = []
        outcomes: list[int] = []
        for context in eval_contexts:
            prediction = service.predict(context)
            probs.append(float(prediction.prob_up))
            outcomes.append(int(context.market["outcome"]))
        metrics = summarize_predictions(probs, outcomes)
        metrics.update(
            {
                "fold": fold_index,
                "train_samples": summary.train_samples,
                "calibration_samples": summary.calibration_samples,
            }
        )
        fold_rows.append(metrics)
        all_probs.extend(probs)
        all_outcomes.extend(outcomes)

    aggregate = summarize_predictions(all_probs, all_outcomes)
    aggregate["folds"] = fold_rows
    return aggregate


def evaluate_model_by_vol(name: str, contexts: list[LocalContext], warm_up: int, folds: int) -> dict[str, Any]:
    buckets = {
        "LOW_VOL": [ctx for ctx in contexts if ctx.production_regime["label"].startswith("LOW_VOL")],
        "MEDIUM_VOL": [ctx for ctx in contexts if ctx.production_regime["label"].startswith("MEDIUM_VOL")],
        "HIGH_VOL": [ctx for ctx in contexts if ctx.production_regime["label"].startswith("HIGH_VOL")],
    }
    overall_probs: list[float] = []
    overall_outcomes: list[int] = []
    reports: dict[str, Any] = {}

    for bucket_name, bucket_contexts in buckets.items():
        if len(bucket_contexts) <= warm_up:
            reports[bucket_name] = {"samples": len(bucket_contexts), "skipped": True}
            continue
        report = evaluate_model(name, bucket_contexts, warm_up, folds)
        reports[bucket_name] = report
        for fold in build_blocked_folds(bucket_contexts, warm_up=warm_up, folds=folds):
            pass
        # Recompute aggregate stream from the bucket report is not possible from summary alone,
        # so rerun prediction collection here to preserve exact global metrics.
        if name in {"paper_xgb_5m", "paper_xgb_5m_raw"}:
            service_kind = "xgboost"
        else:
            service_kind = "logreg"
        feature_set = "raw" if name.endswith("_raw") else "derived"
        use_calibration = not name.endswith("_raw")
        for train_contexts, eval_contexts in build_blocked_folds(bucket_contexts, warm_up=warm_up, folds=folds):
            service = Paper5MModelService(
                model_kind=service_kind,
                feature_set=feature_set,
                use_calibration=use_calibration,
            )
            service.fit(train_contexts)
            for context in eval_contexts:
                prediction = service.predict(context)
                overall_probs.append(float(prediction.prob_up))
                overall_outcomes.append(int(context.market["outcome"]))

    aggregate = summarize_predictions(overall_probs, overall_outcomes)
    aggregate["vol_buckets"] = reports
    return aggregate


def print_report(name: str, report: dict[str, Any]) -> None:
    print(f"\n=== {name} ===")
    print(
        f"samples={report['samples']} accuracy={report['accuracy']:.4f} "
        f"brier={report['avg_brier']:.4f} precision={report['precision']:.4f} "
        f"recall={report['recall']:.4f} f1={report['f1']:.4f}"
    )
    print(
        f"confusion tp={report['tp']} fp={report['fp']} tn={report['tn']} fn={report['fn']}"
    )
    for fold in report["folds"]:
        print(
            f"fold={fold['fold']} samples={fold['samples']} acc={fold['accuracy']:.4f} "
            f"brier={fold['avg_brier']:.4f} precision={fold['precision']:.4f} "
            f"recall={fold['recall']:.4f} f1={fold['f1']:.4f} "
            f"train={fold['train_samples']} cal={fold['calibration_samples']}"
        )


def print_vol_report(name: str, report: dict[str, Any]) -> None:
    print(f"\n=== {name} | split_by_vol ===")
    print(
        f"samples={report['samples']} accuracy={report['accuracy']:.4f} "
        f"brier={report['avg_brier']:.4f} precision={report['precision']:.4f} "
        f"recall={report['recall']:.4f} f1={report['f1']:.4f}"
    )
    print(
        f"confusion tp={report['tp']} fp={report['fp']} tn={report['tn']} fn={report['fn']}"
    )
    for bucket_name, bucket in report["vol_buckets"].items():
        if bucket.get("skipped"):
            print(f"{bucket_name}: skipped samples={bucket['samples']}")
            continue
        print(
            f"{bucket_name}: samples={bucket['samples']} accuracy={bucket['accuracy']:.4f} "
            f"brier={bucket['avg_brier']:.4f} precision={bucket['precision']:.4f} "
            f"recall={bucket['recall']:.4f} f1={bucket['f1']:.4f}"
        )


def main() -> None:
    args = parse_args()
    if args.label_source == "btc_next":
        contexts = build_btc_next_contexts(
            btc_candles_path=args.btc_candles,
            from_date=args.from_date,
            to_date=args.to_date,
        )
    else:
        contexts = build_local_contexts(
            db_path=args.db,
            btc_candles_path=args.btc_candles,
            from_date=args.from_date,
            to_date=args.to_date,
        )
    print(
        f"contexts={len(contexts)} db={args.db} btc_candles={args.btc_candles} "
        f"warm_up={args.warm_up} folds={args.folds} label_source={args.label_source} "
        f"split_by_vol={args.split_by_vol}"
    )
    for contender in args.contenders:
        if args.split_by_vol:
            print_vol_report(contender, evaluate_model_by_vol(contender, contexts, args.warm_up, args.folds))
        else:
            print_report(contender, evaluate_model(contender, contexts, args.warm_up, args.folds))


if __name__ == "__main__":
    main()
