from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from src.metrics import bet_size_for_conviction
from src.v3.arena import build_blocked_folds
from src.v3.backtest_rules import choose_entry_price
from src.v3.foundation_shadow_rolling import (
    DEFAULT_CANDLES,
    DEFAULT_DB,
    build_rolling_contexts,
    parse_edges,
)
from src.v3.probability_baseline import BaselineProbabilityEnsemble
from src.v3.rule_registry import resolve_profile_rules
from src.v3.rule_variants import available_rules


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rolling OOS backtest for rule candidates gated by the broad probability prior.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--warm-up", type=int, default=5000)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--rules", default="baseline_router_v2,baseline_router_v1_plus_sparse_combo,baseline_v3_spike_reversal,baseline_v3_reversal_core,baseline_v3_momentum_shape")
    parser.add_argument("--min-prior-edges", default="0,0.005,0.01,0.02")
    parser.add_argument("--entry-price-source", default="model_edge_8", choices=["neutral_50", "recent_up_share", "market_final_yes", "model_edge_5", "model_edge_8"])
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--format", choices=["markdown", "json", "summary"], default="markdown")
    return parser.parse_args()


def parse_csv_strings(value: str) -> list[str]:
    if value.startswith("profile:"):
        return resolve_profile_rules(value.split(":", 1)[1])
    return [part.strip() for part in value.split(",") if part.strip()]


def run_backtest(args: argparse.Namespace) -> dict[str, Any]:
    contexts, data_stats = build_rolling_contexts(
        db_path=args.db,
        btc_candles_path=args.btc_candles,
        from_date=args.from_date,
        to_date=args.to_date,
        lookback=args.lookback,
    )
    fold_ranges = build_blocked_folds(contexts, warm_up=args.warm_up, folds=args.folds)
    rule_map = available_rules()
    rules = parse_csv_strings(args.rules)
    for rule_name in rules:
        if rule_name not in rule_map:
            raise ValueError(f"Unknown rule: {rule_name}")

    rows: list[dict[str, Any]] = []
    for fold_index, (train_contexts, eval_contexts) in enumerate(fold_ranges):
        prior = BaselineProbabilityEnsemble()
        prior.fit(train_contexts)
        for context in eval_contexts:
            prior_prediction = prior.predict(context)
            prior_prob = float(prior_prediction.prob_up)
            prior_direction = "UP" if prior_prob > 0.5 else "DOWN" if prior_prob < 0.5 else "SKIP"
            prior_edge = abs(prior_prob - 0.5)
            outcome = int(context.market["outcome"])
            for rule_name in rules:
                signal = rule_map[rule_name](context.formatted_candles, context.production_regime)
                estimate = float(signal.get("estimate", 0.5))
                rule_direction = str(signal.get("direction") or "")
                if rule_direction not in {"UP", "DOWN"}:
                    rule_direction = "UP" if estimate > 0.5 else "DOWN" if estimate < 0.5 else "SKIP"
                should_trade = bool(signal.get("should_trade", False))
                conviction = int(signal.get("conviction_score", 0))
                wager = bet_size_for_conviction(conviction)
                rows.append(
                    {
                        "fold": fold_index,
                        "rule": rule_name,
                        "regime": context.production_regime["label"],
                        "should_trade": should_trade,
                        "rule_direction": rule_direction,
                        "estimate": estimate,
                        "conviction": conviction,
                        "wager": wager,
                        "prior_prob": prior_prob,
                        "prior_direction": prior_direction,
                        "prior_edge": prior_edge,
                        "prior_agrees": prior_direction == rule_direction and prior_direction != "SKIP",
                        "outcome": outcome,
                        "final_price_yes": context.market["final_price_yes"],
                        "formatted_candles": context.formatted_candles,
                    }
                )

    thresholds = parse_edges(args.min_prior_edges)
    summaries = [
        summarize_rule_threshold(rows, rule_name=rule, min_prior_edge=edge, args=args)
        for rule in rules
        for edge in thresholds
    ]
    return {
        "db": str(args.db),
        "btc_candles": str(args.btc_candles),
        "warm_up": args.warm_up,
        "folds": len(fold_ranges),
        "entry_price_source": args.entry_price_source,
        "markets": data_stats["market_rows"],
        "eligible_markets": len(contexts),
        "eval_contexts": sum(len(eval_contexts) for _, eval_contexts in fold_ranges),
        **data_stats,
        "summaries": summaries,
    }


def summarize_rule_threshold(rows: list[dict[str, Any]], *, rule_name: str, min_prior_edge: float, args: argparse.Namespace) -> dict[str, Any]:
    rule_rows = [row for row in rows if row["rule"] == rule_name]
    raw_trades = [row for row in rule_rows if row["should_trade"] and row["wager"] > 0]
    raw_metrics = summarize_trade_rows(raw_trades, args=args)
    gated = [
        row
        for row in raw_trades
        if row["prior_agrees"] and float(row["prior_edge"]) >= min_prior_edge
    ]
    gated_metrics = summarize_trade_rows(gated, args=args)

    return {
        "rule": rule_name,
        "min_prior_edge": min_prior_edge,
        "raw_trades": len(raw_trades),
        "raw_win_rate": raw_metrics["win_rate"],
        "raw_pnl": raw_metrics["pnl"],
        "raw_roi": raw_metrics["roi"],
        "trades": len(gated),
        "kept": round(len(gated) / len(raw_trades) * 100.0, 2) if raw_trades else 0.0,
        **gated_metrics,
    }


def summarize_trade_rows(rows: list[dict[str, Any]], *, args: argparse.Namespace) -> dict[str, Any]:
    wins = 0
    pnl = 0.0
    wagered = 0.0
    regime_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "wagered": 0.0})
    for row in rows:
        direction = row["rule_direction"]
        won = (direction == "UP" and row["outcome"] == 1) or (direction == "DOWN" and row["outcome"] == 0)
        entry_price_yes = choose_entry_price(
            {"final_price_yes": row["final_price_yes"]},
            row["formatted_candles"],
            args.entry_price_source,
            row["estimate"],
        )
        entry_price = entry_price_yes if direction == "UP" else min(max(1.0 - entry_price_yes, 0.01), 0.99)
        trade_pnl = row["wager"] * (1.0 / entry_price - 1.0) if won else -row["wager"]
        wins += 1 if won else 0
        pnl += trade_pnl
        wagered += row["wager"]
        bucket = regime_stats[row["regime"]]
        bucket["trades"] += 1
        bucket["wins"] += 1 if won else 0
        bucket["pnl"] += trade_pnl
        bucket["wagered"] += row["wager"]

    return {
        "wins": wins,
        "win_rate": round(wins / len(rows) * 100.0, 2) if rows else 0.0,
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
        "# Prior-Gated Rule Backtest",
        "",
        f"- Prior: `ensemble_logreg_raw_xgb`",
        f"- Warm-up: `{result['warm_up']}`",
        f"- Folds: `{result['folds']}`",
        f"- Entry price source: `{result['entry_price_source']}`",
        f"- Eligible markets: `{result['eligible_markets']}`",
        f"- Eval contexts: `{result['eval_contexts']}`",
        "",
        "| Rule | Min prior edge | Raw trades | Kept | Trades | WR | ROI | PnL |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["summaries"]:
        lines.append(
            f"| `{row['rule']}` | {row['min_prior_edge']:.4f} | "
            f"{row['raw_trades']} ({row['raw_win_rate']:.2f}% / {row['raw_roi']:+.2f}% / {row['raw_pnl']:+.2f}) | "
            f"{row['kept']:.2f}% | {row['trades']} | {row['win_rate']:.2f}% | "
            f"{row['roi']:+.2f}% | {row['pnl']:+.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_summary(result: dict[str, Any]) -> str:
    from src.v3.rule_registry import get_rule_specs

    specs = get_rule_specs()
    rows = result["summaries"]
    raw_by_rule: dict[str, dict[str, Any]] = {}
    gated_001: list[dict[str, Any]] = []
    for row in rows:
        raw_by_rule.setdefault(row["rule"], row)
        if abs(float(row["min_prior_edge"]) - 0.01) < 1e-12:
            gated_001.append(row)

    def spec_text(rule: str) -> str:
        spec = specs[rule]
        return f"{spec.category}/{spec.family}/vol={spec.requires_volume}/{spec.source}"

    lines = [
        "# Prior-Gated Rule Summary",
        "",
        f"- Prior: `ensemble_logreg_raw_xgb`",
        f"- Warm-up: `{result['warm_up']}`",
        f"- Folds: `{result['folds']}`",
        f"- Entry: `{result['entry_price_source']}`",
        f"- Eval contexts: `{result['eval_contexts']}`",
        "",
        "## Raw Winners",
        "",
        "| Rule | Type | Trades | WR | ROI | PnL |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    raw_winners = sorted(
        [row for row in raw_by_rule.values() if row["raw_trades"] >= 20 and row["raw_roi"] > 0],
        key=lambda row: (row["raw_pnl"], row["raw_roi"]),
        reverse=True,
    )
    for row in raw_winners[:30]:
        lines.append(
            f"| `{row['rule']}` | {spec_text(row['rule'])} | {row['raw_trades']} | "
            f"{row['raw_win_rate']:.2f}% | {row['raw_roi']:+.2f}% | {row['raw_pnl']:+.2f} |"
        )

    lines.extend(
        [
            "",
            "## Gated Winners Edge 0.01",
            "",
            "| Rule | Type | Raw | Kept | Trades | WR | ROI | PnL |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    gated_winners = sorted(
        [row for row in gated_001 if row["trades"] >= 20 and row["roi"] > 0],
        key=lambda row: (row["pnl"], row["roi"]),
        reverse=True,
    )
    for row in gated_winners[:30]:
        raw_text = f"{row['raw_trades']} / {row['raw_win_rate']:.2f}% / {row['raw_roi']:+.2f}%"
        lines.append(
            f"| `{row['rule']}` | {spec_text(row['rule'])} | {raw_text} | {row['kept']:.2f}% | "
            f"{row['trades']} | {row['win_rate']:.2f}% | {row['roi']:+.2f}% | {row['pnl']:+.2f} |"
        )

    lines.extend(
        [
            "",
            "## Prior Improves",
            "",
            "| Rule | Type | Raw WR/ROI | Gated WR/ROI | Kept | Gated PnL |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    improved = sorted(
        [
            row
            for row in gated_001
            if row["trades"] >= 20
            and row["roi"] - row["raw_roi"] >= 3.0
            and row["win_rate"] - row["raw_win_rate"] >= 1.0
        ],
        key=lambda row: (row["roi"] - row["raw_roi"], row["pnl"]),
        reverse=True,
    )
    for row in improved[:30]:
        lines.append(
            f"| `{row['rule']}` | {spec_text(row['rule'])} | "
            f"{row['raw_win_rate']:.2f}% / {row['raw_roi']:+.2f}% | "
            f"{row['win_rate']:.2f}% / {row['roi']:+.2f}% | {row['kept']:.2f}% | {row['pnl']:+.2f} |"
        )

    lines.extend(
        [
            "",
            "## Volume Or Coach Survivors",
            "",
            "| Rule | Type | Raw | Trades | WR | ROI | PnL |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    survivors = sorted(
        [
            row
            for row in gated_001
            if row["trades"] >= 10
            and row["roi"] > 0
            and (specs[row["rule"]].requires_volume or specs[row["rule"]].category == "coach_candidate")
        ],
        key=lambda row: (row["pnl"], row["roi"]),
        reverse=True,
    )
    for row in survivors[:40]:
        raw_text = f"{row['raw_trades']} / {row['raw_win_rate']:.2f}% / {row['raw_roi']:+.2f}%"
        lines.append(
            f"| `{row['rule']}` | {spec_text(row['rule'])} | {raw_text} | "
            f"{row['trades']} | {row['win_rate']:.2f}% | {row['roi']:+.2f}% | {row['pnl']:+.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    result = run_backtest(args)
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.format == "summary":
        print(render_summary(result))
    else:
        print(render_markdown(result))


if __name__ == "__main__":
    main()
