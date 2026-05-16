from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.v3.arena import build_blocked_folds
from src.v3.foundation_shadow_rolling import (
    DEFAULT_CANDLES,
    DEFAULT_DB,
    build_rolling_contexts,
    parse_edges,
    summarize_threshold,
)
from src.v3.probability_baseline import BaselineProbabilityEnsemble
from src.v3.probability_foundation import Paper5MModelService, ProbabilityFoundationService
from src.v3.rule_variants import available_rules


def _summary_calibrated(summary: Any) -> bool:
    value = getattr(summary, "calibrated", None)
    if value is not None:
        return bool(value)
    members = getattr(summary, "members", ())
    return any(bool(member.get("calibrated")) for member in members if isinstance(member, dict))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rolling arena for broad BTC 5m probability baseline models.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--warm-up", type=int, default=5000)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument(
        "--contenders",
        nargs="+",
        default=[
            "foundation_none",
            "foundation_router_features",
            "paper_xgb_5m",
            "paper_logreg_5m",
            "paper_xgb_5m_raw",
            "paper_logreg_5m_raw",
            "paper_xgb_5m_window",
            "paper_logreg_5m_window",
            "ensemble_logreg_raw_foundation",
            "ensemble_logreg_raw_xgb",
            "ensemble_logreg_raw_window",
            "ensemble_logreg_raw_foundation_xgb",
        ],
    )
    parser.add_argument("--min-prob-edges", default="0,0.01,0.02,0.03,0.05")
    parser.add_argument("--entry-price-source", default="neutral_50", choices=["neutral_50", "recent_up_share", "market_final_yes", "model_edge_5", "model_edge_8"])
    parser.add_argument("--bet-size", type=float, default=75.0)
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    return parser.parse_args()


def service_for_contender(name: str) -> tuple[Any, Any | None]:
    rules = available_rules()
    if name == "foundation_none":
        return ProbabilityFoundationService(), None
    if name == "foundation_router_features":
        return ProbabilityFoundationService(), rules["baseline_router_v2_candidate_filter"]
    if name in {"paper_xgb_5m", "paper_xgb_5m_raw"}:
        return Paper5MModelService(
            model_kind="xgboost",
            feature_set="raw" if name.endswith("_raw") else "derived",
            use_calibration=not name.endswith("_raw"),
        ), None
    if name == "paper_xgb_5m_window":
        return Paper5MModelService(
            model_kind="xgboost",
            feature_set="window",
            use_calibration=True,
        ), None
    if name in {"paper_logreg_5m", "paper_logreg_5m_raw"}:
        return Paper5MModelService(
            model_kind="logreg",
            feature_set="raw" if name.endswith("_raw") else "derived",
            use_calibration=not name.endswith("_raw"),
        ), None
    if name == "paper_logreg_5m_window":
        return Paper5MModelService(
            model_kind="logreg",
            feature_set="window",
            use_calibration=False,
        ), None
    raise ValueError(f"Unknown probability contender: {name}")


def ensemble_specs(name: str) -> list[tuple[str, float]] | None:
    if name == "ensemble_logreg_raw_xgb":
        return []
    if name == "ensemble_logreg_raw_foundation":
        return [("paper_logreg_5m_raw", 0.65), ("foundation_none", 0.35)]
    if name == "ensemble_logreg_raw_window":
        return [("paper_logreg_5m_raw", 0.70), ("paper_logreg_5m_window", 0.30)]
    if name == "ensemble_logreg_raw_foundation_xgb":
        return [("paper_logreg_5m_raw", 0.50), ("foundation_none", 0.25), ("paper_xgb_5m", 0.25)]
    return None


def evaluate_contender(name: str, fold_ranges: list[tuple[list[Any], list[Any]]], args: argparse.Namespace) -> dict[str, Any]:
    predictions: list[dict[str, Any]] = []
    fold_summaries: list[dict[str, Any]] = []
    specs = ensemble_specs(name)
    for fold_index, (train_contexts, eval_contexts) in enumerate(fold_ranges):
        trained_models = []
        if name == "ensemble_logreg_raw_xgb":
            service = BaselineProbabilityEnsemble(name)
            primary_summary = service.fit(train_contexts)
            trained_models.append((name, 1.0, service, None, primary_summary))
        elif specs:
            for member_name, weight in specs:
                service, signal_provider = service_for_contender(member_name)
                if signal_provider is None:
                    summary = service.fit(train_contexts)
                else:
                    summary = service.fit(train_contexts, signal_provider=signal_provider)
                trained_models.append((member_name, weight, service, signal_provider, summary))
            primary_summary = trained_models[0][4]
        else:
            service, signal_provider = service_for_contender(name)
            if signal_provider is None:
                primary_summary = service.fit(train_contexts)
            else:
                primary_summary = service.fit(train_contexts, signal_provider=signal_provider)
            trained_models.append((name, 1.0, service, signal_provider, primary_summary))

        correct = 0
        brier = 0.0
        for context in eval_contexts:
            weighted_prob = 0.0
            weight_total = 0.0
            for _, weight, service, signal_provider, _ in trained_models:
                candidate_signal = signal_provider(context.formatted_candles, context.production_regime) if signal_provider else None
                if candidate_signal is None:
                    pred = service.predict(context)
                else:
                    pred = service.predict(context, candidate_signal=candidate_signal)
                weighted_prob += float(pred.prob_up) * weight
                weight_total += weight
            prob_up = weighted_prob / weight_total if weight_total else 0.5
            outcome = int(context.market["outcome"])
            direction = "UP" if prob_up > 0.5 else "DOWN" if prob_up < 0.5 else "SKIP"
            row_correct = (direction == "UP" and outcome == 1) or (direction == "DOWN" and outcome == 0)
            correct += 1 if row_correct else 0
            brier += (prob_up - outcome) ** 2
            predictions.append(
                {
                    "fold": fold_index,
                    "prob_up": prob_up,
                    "prob_edge": abs(prob_up - 0.5),
                    "direction": direction,
                    "outcome": outcome,
                    "correct": row_correct,
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
                "train_samples": primary_summary.train_samples,
                "calibration_samples": primary_summary.calibration_samples,
                "accuracy": round(correct / len(eval_contexts) * 100.0, 2) if eval_contexts else 0.0,
                "avg_brier": round(brier / len(eval_contexts), 6) if eval_contexts else 0.0,
                "model": "+".join(member[0] for member in trained_models),
                "calibrated": _summary_calibrated(primary_summary),
            }
        )

    direction_wins = sum(1 for row in predictions if row["correct"])
    brier_total = sum((float(row["prob_up"]) - int(row["outcome"])) ** 2 for row in predictions)
    thresholds = [
        summarize_threshold(predictions, min_edge=edge, args=args)
        for edge in parse_edges(args.min_prob_edges)
    ]
    best_threshold = max(thresholds, key=lambda row: (row["pnl"], row["roi"], row["trades"]))
    threshold_2 = next((row for row in thresholds if abs(row["min_prob_edge"] - 0.02) < 1e-12), best_threshold)
    score = (
        threshold_2["pnl"]
        + 1000.0 * (threshold_2["win_rate"] - 50.0)
        - max(0.0, 500.0 - threshold_2["trades"]) * 10.0
    )
    return {
        "name": name,
        "eval_predictions": len(predictions),
        "direction_wr": round(direction_wins / len(predictions) * 100.0, 2) if predictions else 0.0,
        "avg_brier": round(brier_total / len(predictions), 6) if predictions else 0.0,
        "fold_summaries": fold_summaries,
        "thresholds": thresholds,
        "best_threshold": best_threshold,
        "score": round(score, 2),
    }


def run_arena(args: argparse.Namespace) -> dict[str, Any]:
    contexts, data_stats = build_rolling_contexts(
        db_path=args.db,
        btc_candles_path=args.btc_candles,
        from_date=args.from_date,
        to_date=args.to_date,
        lookback=args.lookback,
    )
    fold_ranges = build_blocked_folds(contexts, warm_up=args.warm_up, folds=args.folds)
    contenders = [evaluate_contender(name, fold_ranges, args) for name in args.contenders]
    contenders.sort(key=lambda row: (row["score"], row["best_threshold"]["pnl"], row["direction_wr"]), reverse=True)
    return {
        "db": str(args.db),
        "btc_candles": str(args.btc_candles),
        "warm_up": args.warm_up,
        "folds": len(fold_ranges),
        "lookback": args.lookback,
        "entry_price_source": args.entry_price_source,
        "bet_size": args.bet_size,
        "markets": data_stats["market_rows"],
        "eligible_markets": len(contexts),
        **data_stats,
        "contenders": contenders,
        "winner": contenders[0]["name"] if contenders else None,
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Probability Baseline Arena",
        "",
        f"- Warm-up: `{result['warm_up']}`",
        f"- Folds: `{result['folds']}`",
        f"- Entry price source: `{result['entry_price_source']}`",
        f"- Eligible markets: `{result['eligible_markets']}`",
        f"- Winner: `{result['winner']}`",
        "",
        "## Ranking",
        "",
        "| Rank | Contender | Direction WR | Brier | Score | Best edge | Best trades | Best WR | Best ROI | Best PnL |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for index, row in enumerate(result["contenders"], start=1):
        best = row["best_threshold"]
        lines.append(
            f"| {index} | `{row['name']}` | {row['direction_wr']:.2f}% | {row['avg_brier']:.6f} | "
            f"{row['score']:+.2f} | {best['min_prob_edge']:.4f} | {best['trades']} | "
            f"{best['win_rate']:.2f}% | {best['roi']:+.2f}% | {best['pnl']:+.2f} |"
        )

    for row in result["contenders"]:
        lines.extend(
            [
                "",
                f"## {row['name']}",
                "",
                "| Min edge | Trades | Coverage | WR | ROI | PnL |",
                "| ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for threshold in row["thresholds"]:
            lines.append(
                f"| {threshold['min_prob_edge']:.4f} | {threshold['trades']} | {threshold['coverage']:.2f}% | "
                f"{threshold['win_rate']:.2f}% | {threshold['roi']:+.2f}% | {threshold['pnl']:+.2f} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    result = run_arena(args)
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_markdown(result))


if __name__ == "__main__":
    main()
