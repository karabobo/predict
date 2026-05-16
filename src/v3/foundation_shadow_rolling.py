from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.strategies.regime import compute_regime_from_candles
from src.v3.arena import build_blocked_folds
from src.v3.backtest_rules import build_contexts, choose_entry_price, load_candles
from src.v3.probability_foundation import ProbabilityFoundationService
from src.v3.rule_variants import available_rules


DEFAULT_DB = ROOT / "data" / "polymarket_backtest.db"
DEFAULT_CANDLES = Path("/home/ubuntu/migration/Data/btc_5m.parquet")


@dataclass(frozen=True)
class RollingContext:
    market: dict[str, Any]
    formatted_candles: list[dict[str, Any]]
    production_regime: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rolling out-of-sample evaluation for the foundation probability model.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--warm-up", type=int, default=500)
    parser.add_argument("--folds", type=int, default=6)
    parser.add_argument("--candidate-rule", default="none", help="Use 'none' for standalone broad probability model.")
    parser.add_argument("--min-prob-edges", default="0,0.01,0.02,0.03,0.05")
    parser.add_argument("--entry-price-source", default="neutral_50", choices=["neutral_50", "recent_up_share", "market_final_yes", "model_edge_5", "model_edge_8"])
    parser.add_argument("--bet-size", type=float, default=75.0)
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    return parser.parse_args()


def load_markets(db_path: Path, from_date: str | None, to_date: str | None) -> list[sqlite3.Row]:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    clauses = ["outcome IS NOT NULL", "window_start_ts IS NOT NULL"]
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


def build_rolling_contexts(
    *,
    db_path: Path,
    btc_candles_path: Path,
    from_date: str | None,
    to_date: str | None,
    lookback: int,
) -> tuple[list[RollingContext], dict[str, int]]:
    candles = load_candles(btc_candles_path)
    contexts_by_start, build_stats = build_contexts(candles, lookback)
    rows = load_markets(db_path, from_date, to_date)
    contexts: list[RollingContext] = []
    skipped_missing_context = 0
    for idx, row in enumerate(rows):
        context_pack = contexts_by_start.get(int(row["window_start_ts"]))
        if context_pack is None:
            skipped_missing_context += 1
            continue
        formatted = context_pack["context_candles"]
        contexts.append(
            RollingContext(
                market={
                    "index": idx,
                    "market_id": str(row["market_id"]),
                    "question": str(row["question"]),
                    "end_date": row["end_date"],
                    "timestamp": int(row["window_start_ts"]),
                    "outcome": int(row["outcome"]),
                    "final_price_yes": float(row["final_price_yes"] or 0.5),
                },
                formatted_candles=formatted,
                production_regime=compute_regime_from_candles(formatted),
            )
        )
    return contexts, {
        "market_rows": len(rows),
        "contexts_built": int(build_stats["contexts"]),
        "skipped_missing_context": skipped_missing_context,
    }


def parse_edges(raw: str) -> list[float]:
    edges = sorted({float(part.strip()) for part in raw.split(",") if part.strip()})
    if not edges:
        raise ValueError("--min-prob-edges must contain at least one number")
    return edges


def run_rolling(args: argparse.Namespace) -> dict[str, Any]:
    contexts, data_stats = build_rolling_contexts(
        db_path=args.db,
        btc_candles_path=args.btc_candles,
        from_date=args.from_date,
        to_date=args.to_date,
        lookback=args.lookback,
    )
    rules = available_rules()
    if args.candidate_rule == "none":
        signal_provider = None
    else:
        if args.candidate_rule not in rules:
            raise ValueError(f"Unknown candidate rule: {args.candidate_rule}")
        signal_provider = rules[args.candidate_rule]

    fold_ranges = build_blocked_folds(contexts, warm_up=args.warm_up, folds=args.folds)
    predictions: list[dict[str, Any]] = []
    fold_summaries: list[dict[str, Any]] = []

    for fold_index, (train_contexts, eval_contexts) in enumerate(fold_ranges):
        service = ProbabilityFoundationService()
        summary = service.fit(train_contexts, signal_provider=signal_provider)
        fold_correct = 0
        fold_brier = 0.0
        for context in eval_contexts:
            candidate_signal = signal_provider(context.formatted_candles, context.production_regime) if signal_provider else None
            prediction = service.predict(context, candidate_signal=candidate_signal)
            prob_up = float(prediction.prob_up)
            outcome = int(context.market["outcome"])
            direction = "UP" if prob_up > 0.5 else "DOWN" if prob_up < 0.5 else "SKIP"
            correct = (direction == "UP" and outcome == 1) or (direction == "DOWN" and outcome == 0)
            fold_correct += 1 if correct else 0
            fold_brier += (prob_up - outcome) ** 2
            predictions.append(
                {
                    "fold": fold_index,
                    "prob_up": prob_up,
                    "prob_edge": abs(prob_up - 0.5),
                    "direction": direction,
                    "outcome": outcome,
                    "correct": correct,
                    "regime": context.production_regime["label"],
                    "final_price_yes": context.market["final_price_yes"],
                    "formatted_candles": context.formatted_candles,
                }
            )
        fold_summaries.append(
            {
                "fold": fold_index,
                "train_contexts": len(train_contexts),
                "eval_contexts": len(eval_contexts),
                "train_samples": summary.train_samples,
                "calibration_samples": summary.calibration_samples,
                "accuracy": round(fold_correct / len(eval_contexts) * 100.0, 2) if eval_contexts else 0.0,
                "avg_brier": round(fold_brier / len(eval_contexts), 6) if eval_contexts else 0.0,
                "primary_model_name": summary.primary_model_name,
                "secondary_model_name": summary.secondary_model_name,
                "calibrated": summary.calibrated,
                "diagnostics": dict(summary.diagnostics),
            }
        )

    thresholds = []
    for min_edge in parse_edges(args.min_prob_edges):
        thresholds.append(summarize_threshold(predictions, min_edge=min_edge, args=args))

    return {
        "db": str(args.db),
        "btc_candles": str(args.btc_candles),
        "candidate_rule": args.candidate_rule,
        "entry_price_source": args.entry_price_source,
        "bet_size": args.bet_size,
        "warm_up": args.warm_up,
        "folds": len(fold_ranges),
        "lookback": args.lookback,
        "markets": data_stats["market_rows"],
        "eligible_markets": len(contexts),
        "eval_predictions": len(predictions),
        **data_stats,
        "fold_summaries": fold_summaries,
        "thresholds": thresholds,
    }


def summarize_threshold(predictions: list[dict[str, Any]], *, min_edge: float, args: argparse.Namespace) -> dict[str, Any]:
    selected = [row for row in predictions if row["direction"] != "SKIP" and row["prob_edge"] >= min_edge]
    wins = sum(1 for row in selected if row["correct"])
    pnl = 0.0
    wagered = 0.0
    regime_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "wagered": 0.0})
    for row in selected:
        if args.entry_price_source == "neutral_50":
            entry_price_yes = 0.5
        else:
            pseudo_market = {"final_price_yes": row["final_price_yes"]}
            entry_price_yes = choose_entry_price(pseudo_market, row["formatted_candles"], args.entry_price_source, row["prob_up"])
        entry_price = entry_price_yes if row["direction"] == "UP" else min(max(1.0 - entry_price_yes, 0.01), 0.99)
        trade_pnl = args.bet_size * (1.0 / entry_price - 1.0) if row["correct"] else -args.bet_size
        pnl += trade_pnl
        wagered += args.bet_size
        bucket = regime_stats[row["regime"]]
        bucket["trades"] += 1
        bucket["wins"] += 1 if row["correct"] else 0
        bucket["pnl"] += trade_pnl
        bucket["wagered"] += args.bet_size

    return {
        "min_prob_edge": min_edge,
        "trades": len(selected),
        "coverage": round(len(selected) / len(predictions) * 100.0, 2) if predictions else 0.0,
        "wins": wins,
        "win_rate": round(wins / len(selected) * 100.0, 2) if selected else 0.0,
        "pnl": round(pnl, 2),
        "wagered": round(wagered, 2),
        "roi": round(pnl / wagered * 100.0, 2) if wagered else 0.0,
        "regimes": {
            regime: {
                "trades": stats["trades"],
                "wins": stats["wins"],
                "win_rate": round(stats["wins"] / stats["trades"] * 100.0, 2) if stats["trades"] else 0.0,
                "pnl": round(stats["pnl"], 2),
                "roi": round(stats["pnl"] / stats["wagered"] * 100.0, 2) if stats["wagered"] else 0.0,
            }
            for regime, stats in sorted(regime_stats.items())
        },
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Foundation Rolling OOS Backtest",
        "",
        f"- Candidate rule: `{result['candidate_rule']}`",
        f"- Entry price source: `{result['entry_price_source']}`",
        f"- Warm-up: `{result['warm_up']}`",
        f"- Folds: `{result['folds']}`",
        f"- Markets: `{result['markets']}`",
        f"- Eligible markets: `{result['eligible_markets']}`",
        f"- Eval predictions: `{result['eval_predictions']}`",
        "",
        "## Thresholds",
        "",
        "| Min prob edge | Trades | Coverage | WR | ROI | PnL |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["thresholds"]:
        lines.append(
            f"| {row['min_prob_edge']:.4f} | {row['trades']} | {row['coverage']:.2f}% | "
            f"{row['win_rate']:.2f}% | {row['roi']:+.2f}% | {row['pnl']:+.2f} |"
        )
    lines.extend(["", "## Folds", "", "| Fold | Train | Eval | Accuracy | Brier | Model | Calibrated |", "| ---: | ---: | ---: | ---: | ---: | --- | --- |"])
    for fold in result["fold_summaries"]:
        lines.append(
            f"| {fold['fold']} | {fold['train_contexts']} | {fold['eval_contexts']} | "
            f"{fold['accuracy']:.2f}% | {fold['avg_brier']:.6f} | {fold['primary_model_name']} | {fold['calibrated']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    result = run_rolling(args)
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_markdown(result))


if __name__ == "__main__":
    main()
