from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.metrics import bet_size_for_conviction
from src.strategies.regime import compute_regime_from_candles
from src.v3.backtest_rules import build_contexts, choose_entry_price, load_candles, select_markets
from src.v3.rule_variants import available_rules


DEFAULT_DB = ROOT / "data" / "polymarket_backtest.db"
DEFAULT_ARTIFACT = ROOT / "data" / "models" / "foundation_shadow.pkl"
DEFAULT_CANDLES = Path("/home/ubuntu/migration/Data/btc_5m.parquet")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the saved foundation_shadow artifact on BTC 5m historical markets.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--candidate-rule", default="baseline_router_v2_candidate_filter")
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--entry-price-source", default="model_edge_8", choices=["neutral_50", "recent_up_share", "market_final_yes", "model_edge_5", "model_edge_8"])
    parser.add_argument("--require-agreement", action="store_true")
    parser.add_argument("--trade-mode", choices=["standalone_probability", "model_only", "candidate_agreement"], default="candidate_agreement")
    parser.add_argument("--min-prob-edge", type=float, default=0.0, help="Standalone mode trades only when abs(prob_up - 0.5) is at least this value.")
    parser.add_argument("--bet-size", type=float, default=75.0, help="Fixed stake for standalone probability trades.")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    return parser.parse_args()


def load_artifact(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    service = payload.get("service")
    if service is None or not getattr(service, "is_trained", False):
        raise ValueError(f"Foundation shadow artifact is missing or untrained: {path}")
    return payload


def run_backtest(args: argparse.Namespace) -> dict[str, Any]:
    payload = load_artifact(args.artifact)
    service = payload["service"]
    metadata = payload.get("metadata", {})

    rules = available_rules()
    if args.candidate_rule not in rules:
        raise ValueError(f"Unknown candidate rule: {args.candidate_rule}")
    candidate_rule = rules[args.candidate_rule]

    candles = load_candles(args.btc_candles)
    contexts_by_start, build_stats = build_contexts(candles, args.lookback)

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    try:
        markets = select_markets(db, from_date=args.from_date, to_date=args.to_date)
    finally:
        db.close()

    rows: list[dict[str, Any]] = []
    eligible = 0
    skipped_missing_context = 0
    signal_calls = 0
    signal_wins = 0
    model_direction_calls = 0
    model_direction_wins = 0
    agreement_passes = 0
    direction_matches = 0

    for market in markets:
        start_ts = market["window_start_ts"]
        outcome = market["outcome"]
        if start_ts is None or outcome is None:
            skipped_missing_context += 1
            continue
        context_pack = contexts_by_start.get(int(start_ts))
        if context_pack is None:
            skipped_missing_context += 1
            continue

        context_candles = context_pack["context_candles"]
        regime = compute_regime_from_candles(context_candles)
        candidate_signal = candidate_rule(context_candles, regime)
        context = SimpleNamespace(formatted_candles=context_candles, production_regime=regime, market={})
        prediction = service.predict(
            context,
            candidate_signal=candidate_signal,
            require_agreement=args.require_agreement,
        )

        prob_up = float(prediction.prob_up)
        model_direction = "UP" if prob_up > 0.5 else "DOWN" if prob_up < 0.5 else "SKIP"
        candidate_direction = str(candidate_signal.get("direction") or "SKIP")
        candidate_should_trade = bool(candidate_signal.get("should_trade", False))
        direction_match = model_direction != "SKIP" and model_direction == candidate_direction
        should_trade = model_direction != "SKIP"
        if args.trade_mode == "candidate_agreement":
            should_trade = should_trade and candidate_should_trade and direction_match
        elif args.trade_mode == "standalone_probability":
            should_trade = should_trade and abs(prob_up - 0.5) >= args.min_prob_edge
        if args.require_agreement:
            should_trade = should_trade and bool(prediction.agreement_passed)

        eligible += 1
        if model_direction != "SKIP":
            model_direction_calls += 1
            if (model_direction == "UP" and int(outcome) == 1) or (model_direction == "DOWN" and int(outcome) == 0):
                model_direction_wins += 1
        if candidate_should_trade:
            signal_calls += 1
            if (candidate_direction == "UP" and int(outcome) == 1) or (candidate_direction == "DOWN" and int(outcome) == 0):
                signal_wins += 1
        if prediction.agreement_passed:
            agreement_passes += 1
        if direction_match:
            direction_matches += 1

        conviction = int(candidate_signal.get("conviction_score", 0))
        wager = float(args.bet_size) if args.trade_mode == "standalone_probability" else bet_size_for_conviction(conviction)
        if not should_trade or wager <= 0:
            pnl = 0.0
            won = 0
            entry_price_yes = None
            direction = "SKIP"
        else:
            entry_price_yes = choose_entry_price(market, context_candles, args.entry_price_source, prob_up)
            direction = model_direction
            entry_price = entry_price_yes if direction == "UP" else min(max(1.0 - entry_price_yes, 0.01), 0.99)
            won_bool = (direction == "UP" and int(outcome) == 1) or (direction == "DOWN" and int(outcome) == 0)
            pnl = wager * (1.0 / entry_price - 1.0) if won_bool else -wager
            won = 1 if won_bool else 0

        rows.append(
            {
                "regime": regime["label"],
                "should_trade": 1 if should_trade and wager > 0 else 0,
                "won": won,
                "pnl": pnl,
                "wager": wager if should_trade and wager > 0 else 0.0,
                "direction": direction,
                "prob_up": prob_up,
            }
        )

    trades = [row for row in rows if row["should_trade"]]
    wagered = sum(float(row["wager"]) for row in trades)
    pnl = sum(float(row["pnl"]) for row in trades)
    wins = sum(int(row["won"]) for row in trades)

    regime_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"markets": 0, "trades": 0, "wins": 0, "pnl": 0.0, "wagered": 0.0})
    for row in rows:
        bucket = regime_stats[row["regime"]]
        bucket["markets"] += 1
        if row["should_trade"]:
            bucket["trades"] += 1
            bucket["wins"] += row["won"]
            bucket["pnl"] += row["pnl"]
            bucket["wagered"] += row["wager"]

    return {
        "artifact": str(args.artifact),
        "metadata": metadata,
        "candidate_rule": args.candidate_rule,
        "trade_mode": args.trade_mode,
        "require_agreement": args.require_agreement,
        "min_prob_edge": args.min_prob_edge,
        "bet_size": args.bet_size,
        "entry_price_source": args.entry_price_source,
        "markets": len(markets),
        "eligible_markets": eligible,
        "skipped_missing_context": skipped_missing_context,
        "contexts_built": build_stats["contexts"],
        "candidate_signal_calls": signal_calls,
        "candidate_signal_wr": round(signal_wins / signal_calls * 100.0, 2) if signal_calls else 0.0,
        "model_direction_calls": model_direction_calls,
        "model_direction_wr": round(model_direction_wins / model_direction_calls * 100.0, 2) if model_direction_calls else 0.0,
        "agreement_passes": agreement_passes,
        "direction_matches": direction_matches,
        "trades": len(trades),
        "trade_wins": wins,
        "trade_wr": round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        "trade_pnl": round(pnl, 2),
        "trade_wagered": round(wagered, 2),
        "trade_roi": round(pnl / wagered * 100.0, 2) if wagered else 0.0,
        "regimes": {
            regime: {
                "markets": stats["markets"],
                "trades": stats["trades"],
                "wins": stats["wins"],
                "trade_wr": round(stats["wins"] / stats["trades"] * 100.0, 2) if stats["trades"] else 0.0,
                "pnl": round(stats["pnl"], 2),
                "roi": round(stats["pnl"] / stats["wagered"] * 100.0, 2) if stats["wagered"] else 0.0,
            }
            for regime, stats in sorted(regime_stats.items())
        },
        "warning": "artifact_replay_may_include_training_samples; use rolling/promotion/L2 replay before treating as live edge",
    }


def render_markdown(result: dict[str, Any]) -> str:
    meta = result.get("metadata", {})
    summary = meta.get("summary", {}) if isinstance(meta.get("summary"), dict) else {}
    lines = [
        "# Foundation Shadow Backtest",
        "",
        f"- Artifact: `{result['artifact']}`",
        f"- Trained at: `{meta.get('trained_at', 'unknown')}`",
        f"- Train samples: `{summary.get('train_samples', 'unknown')}`",
        f"- Calibration samples: `{summary.get('calibration_samples', 'unknown')}`",
        f"- Candidate rule: `{result['candidate_rule']}`",
        f"- Trade mode: `{result['trade_mode']}`",
        f"- Require agreement: `{result['require_agreement']}`",
        f"- Min probability edge: `{result['min_prob_edge']}`",
        f"- Bet size: `{result['bet_size']}`",
        f"- Entry price source: `{result['entry_price_source']}`",
        "",
        "## Summary",
        "",
        f"- Markets: `{result['markets']}`",
        f"- Eligible markets: `{result['eligible_markets']}`",
        f"- Candidate signal calls: `{result['candidate_signal_calls']}` / WR `{result['candidate_signal_wr']:.2f}%`",
        f"- Model direction calls: `{result['model_direction_calls']}` / WR `{result['model_direction_wr']:.2f}%`",
        f"- Agreement passes: `{result['agreement_passes']}`",
        f"- Direction matches: `{result['direction_matches']}`",
        f"- Trades: `{result['trades']}`",
        f"- Trade WR: `{result['trade_wr']:.2f}%`",
        f"- PnL: `{result['trade_pnl']:+.2f}`",
        f"- Wagered: `{result['trade_wagered']:.2f}`",
        f"- ROI: `{result['trade_roi']:+.2f}%`",
        "",
        "## Regimes",
        "",
        "| Regime | Markets | Trades | WR | ROI | PnL |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for regime, stats in result["regimes"].items():
        lines.append(
            f"| {regime} | {stats['markets']} | {stats['trades']} | "
            f"{stats['trade_wr']:.2f}% | {stats['roi']:+.2f}% | {stats['pnl']:+.2f} |"
        )
    lines.extend(["", f"Warning: `{result['warning']}`", ""])
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    result = run_backtest(args)
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_markdown(result))


if __name__ == "__main__":
    main()
