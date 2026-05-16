"""
Batch L2 replay gate for shortlisted candidates.

This is the first meaningful "does it still have edge?" check after coarse
backtests. It accepts an explicit manifest of replay markets so validation stays
deterministic and does not depend on scraping remote directory listings.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.v3.backtest_rules import Candle, load_candles
from src.v3.l2_candidate_replay import (
    CandidateReplayResult,
    DEFAULT_BTC_CANDLES,
    DEFAULT_BACKTEST_DB,
    BaselineProbabilityEnsemble,
    build_rolling_contexts,
    result_to_dict,
    parse_market_slug,
    validate_candidate_on_frames,
)
from src.v3.l2_replay import read_replay_parquet


@dataclass(frozen=True)
class ReplayMarketInput:
    market_slug: str
    l2: str
    snapshot: str
    rule: str | None = None


@dataclass(frozen=True)
class BatchReplaySummary:
    rule_name: str
    decision_offset_seconds: int
    markets: int
    signals: int
    filled: int
    no_fill: int
    partial_fills: int
    total_spent: float
    total_pnl: float
    roi: float
    avg_edge_after_fill: float | None
    fill_rate: float
    win_rate: float
    warnings: tuple[str, ...]


def parse_csv_ints(value: str) -> list[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def parse_csv_strings(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def load_manifest(path: Path) -> list[ReplayMarketInput]:
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("markets", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("JSON manifest must be a list or an object with a markets list")
        return [
            ReplayMarketInput(
                market_slug=str(item["market_slug"]),
                l2=str(item["l2"]),
                snapshot=str(item["snapshot"]),
                rule=str(item["rule"]) if item.get("rule") is not None else None,
            )
            for item in items
        ]

    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return [
                ReplayMarketInput(
                    market_slug=str(row["market_slug"]),
                    l2=str(row["l2"]),
                    snapshot=str(row["snapshot"]),
                    rule=str(row["rule"]) if row.get("rule") else None,
                )
                for row in reader
            ]

    raise ValueError("Manifest must be .json or .csv")


def summarize_results(results: list[CandidateReplayResult]) -> list[BatchReplaySummary]:
    grouped: dict[tuple[str, int], list[CandidateReplayResult]] = {}
    for result in results:
        offset = int(result.decision_ts - result.market_start_ts)
        grouped.setdefault((result.rule_name, offset), []).append(result)

    summaries: list[BatchReplaySummary] = []
    for (rule_name, offset), rows in sorted(grouped.items()):
        signal_rows = [row for row in rows if row.should_trade]
        filled_rows = [row for row in rows if row.status == "filled"]
        partial_rows = [row for row in rows if row.status == "partial_fill_below_threshold"]
        no_fill_rows = [row for row in rows if row.status == "no_fill"]
        spent = sum((row.fill.spent_usdc if row.fill else 0.0) for row in rows)
        pnl = sum(float(row.pnl) for row in rows)
        edges = [float(row.edge_after_fill) for row in rows if row.edge_after_fill is not None]
        wins = sum(
            1
            for row in filled_rows
            if row.predicted_outcome is not None and row.predicted_outcome == row.actual_outcome
        )
        warnings: list[str] = []
        if not signal_rows:
            warnings.append("no_trade_signals")
        if signal_rows and not filled_rows:
            warnings.append("signals_without_full_fills")
        if filled_rows and (sum(edges) / len(edges) if edges else 0.0) <= 0:
            warnings.append("non_positive_average_edge_after_fill")

        summaries.append(
            BatchReplaySummary(
                rule_name=rule_name,
                decision_offset_seconds=offset,
                markets=len(rows),
                signals=len(signal_rows),
                filled=len(filled_rows),
                no_fill=len(no_fill_rows),
                partial_fills=len(partial_rows),
                total_spent=spent,
                total_pnl=pnl,
                roi=(pnl / spent * 100.0) if spent > 0 else 0.0,
                avg_edge_after_fill=(sum(edges) / len(edges)) if edges else None,
                fill_rate=len(filled_rows) / len(signal_rows) if signal_rows else 0.0,
                win_rate=wins / len(filled_rows) if filled_rows else 0.0,
                warnings=tuple(warnings),
            )
        )
    return summaries


def run_batch_replay(
    *,
    markets: list[ReplayMarketInput],
    rules: list[str],
    decision_offsets_seconds: list[int],
    btc_candles: list[Candle],
    btc_candles_path: Path = DEFAULT_BTC_CANDLES,
    lookback: int = 20,
    bet_usd: float = 75.0,
    min_fill_ratio: float = 0.95,
    prior_gate: str = "none",
    prior_db: Path = DEFAULT_BACKTEST_DB,
    prior_min_edge: float = 0.01,
    prior_warm_up: int = 5000,
    use_manifest_rules: bool = False,
    slug_ts: str = "start",
) -> tuple[list[CandidateReplayResult], list[BatchReplaySummary]]:
    results: list[CandidateReplayResult] = []
    prior_cache: dict[int, object | None] = {}
    prior_contexts = None
    if prior_gate != "none":
        prior_contexts, _ = build_rolling_contexts(
            db_path=prior_db,
            btc_candles_path=btc_candles_path,
            from_date=None,
            to_date=None,
            lookback=lookback,
        )
    for market in markets:
        market_rules = [market.rule] if use_manifest_rules and market.rule else rules
        try:
            l2_frame = read_replay_parquet(market.l2)
            snapshot_frame = read_replay_parquet(market.snapshot)
        except Exception as exc:
            for rule in market_rules:
                if rule is None:
                    continue
                for offset in decision_offsets_seconds:
                    results.append(_error_result(rule, market.market_slug, offset, f"read_replay_failed: {exc}"))
            continue
        for rule in market_rules:
            if rule is None:
                continue
            for offset in decision_offsets_seconds:
                try:
                    prior_model = None
                    if prior_gate != "none":
                        window = parse_market_slug(market.market_slug, slug_ts=slug_ts)
                        decision_ts = window.start_ts + int(offset)
                        if decision_ts not in prior_cache:
                            prior_cache[decision_ts] = _train_prior_from_contexts(
                                contexts=prior_contexts or [],
                                decision_ts=decision_ts,
                                warm_up=prior_warm_up,
                            )
                        prior_model = prior_cache[decision_ts]
                    result = validate_candidate_on_frames(
                        rule_name=rule,
                        market_slug=market.market_slug,
                        l2_frame=l2_frame,
                        snapshot_frame=snapshot_frame,
                        btc_candles=btc_candles,
                        decision_offset_seconds=offset,
                        lookback=lookback,
                        bet_usd=bet_usd,
                        min_fill_ratio=min_fill_ratio,
                        prior_model=prior_model,
                        min_prior_edge=prior_min_edge,
                        slug_ts=slug_ts,
                    )
                except Exception as exc:
                    result = _error_result(rule, market.market_slug, offset, str(exc))
                results.append(result)
    return results, summarize_results(results)


def _train_prior_from_contexts(
    *,
    contexts: list[object],
    decision_ts: int,
    warm_up: int,
) -> BaselineProbabilityEnsemble | None:
    train_contexts = [
        context
        for context in contexts
        if int(getattr(context, "market", {}).get("timestamp", 0)) < int(decision_ts)
    ]
    if len(train_contexts) < warm_up:
        return None
    prior = BaselineProbabilityEnsemble()
    prior.fit(train_contexts)
    return prior


def _error_result(rule_name: str, market_slug: str, offset: int, error: str) -> CandidateReplayResult:
    from src.v3.l2_candidate_replay import CandidateReplayResult
    from src.v3.l2_replay import BookMetrics

    return CandidateReplayResult(
        rule_name=rule_name,
        market_slug=market_slug,
        decision_ts=offset,
        market_start_ts=0,
        market_end_ts=0,
        reference_price=0.0,
        final_price=0.0,
        actual_outcome="UNKNOWN",
        predicted_outcome=None,
        should_trade=False,
        estimate=0.5,
        predicted_prob=0.5,
        prior_prob=None,
        prior_direction=None,
        prior_edge=None,
        prior_gate_passed=None,
        regime="UNKNOWN",
        reason=error,
        book_metrics=BookMetrics(
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            spread_pct=None,
            bid_depth_5pct=0.0,
            ask_depth_5pct=0.0,
            depth_imbalance=0.0,
            book_hash="",
        ),
        fill=None,
        edge_after_fill=None,
        pnl=0.0,
        status="error",
        warnings=(error,),
    )


def report_to_dict(
    *,
    results: list[CandidateReplayResult],
    summaries: list[BatchReplaySummary],
) -> dict[str, Any]:
    return {
        "summaries": [asdict(summary) for summary in summaries],
        "results": [result_to_dict(result) for result in results],
    }


def render_markdown(summaries: list[BatchReplaySummary], *, max_rows: int = 100) -> str:
    lines = [
        "# Batch L2 Replay",
        "",
        "| Rule | Offset | Markets | Signals | Filled | Fill Rate | Win Rate | ROI | Avg Edge | Warnings |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for summary in summaries[:max_rows]:
        avg_edge = "n/a" if summary.avg_edge_after_fill is None else f"{summary.avg_edge_after_fill:+.4f}"
        warnings = ", ".join(summary.warnings) or "-"
        lines.append(
            f"| `{summary.rule_name}` | {summary.decision_offset_seconds}s | {summary.markets} | "
            f"{summary.signals} | {summary.filled} | {summary.fill_rate:.2%} | "
            f"{summary.win_rate:.2%} | {summary.roi:+.2f}% | {avg_edge} | {warnings} |"
        )
    if not summaries:
        lines.append("| _none_ |  |  |  |  |  |  |  |  |  |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch validate shortlisted rules over L2 replay markets.")
    parser.add_argument("--manifest", type=Path, required=True, help="JSON/CSV manifest with market_slug,l2,snapshot")
    parser.add_argument("--rules", required=True, help="Comma-separated rule names")
    parser.add_argument("--decision-offsets-seconds", default="30,60,120", help="Comma-separated offsets from market start")
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_BTC_CANDLES)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--bet-usd", type=float, default=75.0)
    parser.add_argument("--min-fill-ratio", type=float, default=0.95)
    parser.add_argument("--prior-gate", choices=("none", "ensemble_logreg_raw_xgb"), default="none")
    parser.add_argument("--prior-min-edge", type=float, default=0.01)
    parser.add_argument("--prior-db", type=Path, default=DEFAULT_BACKTEST_DB)
    parser.add_argument("--prior-warm-up", type=int, default=5000)
    parser.add_argument("--slug-ts", choices=("start", "end"), default="start")
    parser.add_argument(
        "--use-manifest-rules",
        action="store_true",
        help="Use each manifest row's rule field instead of applying all --rules to every market.",
    )
    parser.add_argument("--output", type=Path, help="Optional path to write the rendered report.")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args()

    markets = load_manifest(args.manifest)
    rules = parse_csv_strings(args.rules)
    offsets = parse_csv_ints(args.decision_offsets_seconds)
    btc_candles = load_candles(args.btc_candles)
    results, summaries = run_batch_replay(
        markets=markets,
        rules=rules,
        decision_offsets_seconds=offsets,
        btc_candles=btc_candles,
        btc_candles_path=args.btc_candles,
        lookback=args.lookback,
        bet_usd=args.bet_usd,
        min_fill_ratio=args.min_fill_ratio,
        prior_gate=args.prior_gate,
        prior_db=args.prior_db,
        prior_min_edge=args.prior_min_edge,
        prior_warm_up=args.prior_warm_up,
        use_manifest_rules=args.use_manifest_rules,
        slug_ts=args.slug_ts,
    )
    if args.format == "json":
        rendered = json.dumps(report_to_dict(results=results, summaries=summaries), indent=2, default=str, sort_keys=True)
    else:
        rendered = render_markdown(summaries)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="" if rendered.endswith("\n") else "\n")


if __name__ == "__main__":
    main()
