"""
Validate shortlisted rule candidates against a single L2 replay market.

This is the next gate after coarse backtests: if a candidate's historical edge
only works with proxy prices, this module checks whether the same signal could
have bought the selected outcome at the contemporaneous order book.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from src.strategies.regime import compute_regime_from_candles
from src.v3.backtest_rules import Candle, load_candles
from src.v3.foundation_shadow_rolling import DEFAULT_DB as DEFAULT_BACKTEST_DB
from src.v3.foundation_shadow_rolling import build_rolling_contexts
from src.v3.l2_replay import BookMetrics, SimulatedFill, book_at_timestamp, read_replay_parquet
from src.v3.probability_baseline import BaselineProbabilityEnsemble
from src.v3.rule_variants import available_rules


MARKET_SLUG_RE = re.compile(r"btc-updown-(?P<minutes>\d+)m-(?P<end_ts>\d+)")
DEFAULT_BTC_CANDLES = Path("/home/ubuntu/migration/Data/btc_5m.parquet")


@dataclass(frozen=True)
class MarketWindowSpec:
    slug: str
    minutes: int
    start_ts: int
    end_ts: int


@dataclass(frozen=True)
class CandidateReplayResult:
    rule_name: str
    market_slug: str
    decision_ts: int
    market_start_ts: int
    market_end_ts: int
    reference_price: float
    final_price: float
    actual_outcome: str
    predicted_outcome: str | None
    should_trade: bool
    estimate: float
    predicted_prob: float
    prior_prob: float | None
    prior_direction: str | None
    prior_edge: float | None
    prior_gate_passed: bool | None
    regime: str
    reason: str
    book_metrics: BookMetrics
    fill: SimulatedFill | None
    edge_after_fill: float | None
    pnl: float
    status: str
    warnings: tuple[str, ...]


def parse_market_slug(slug: str, *, slug_ts: str = "start") -> MarketWindowSpec:
    match = MARKET_SLUG_RE.search(slug)
    if not match:
        raise ValueError(f"Unsupported BTC up/down market slug: {slug}")
    minutes = int(match.group("minutes"))
    raw_ts = int(match.group("end_ts"))
    if slug_ts == "start":
        start_ts = raw_ts
        end_ts = raw_ts + minutes * 60
    elif slug_ts == "end":
        start_ts = raw_ts - minutes * 60
        end_ts = raw_ts
    else:
        raise ValueError(f"Unsupported slug_ts={slug_ts!r}; expected 'start' or 'end'")
    return MarketWindowSpec(
        slug=slug,
        minutes=minutes,
        start_ts=start_ts,
        end_ts=end_ts,
    )


def _naive_utc_timestamp(epoch_seconds: int) -> pd.Timestamp:
    return pd.to_datetime(int(epoch_seconds), unit="s")


def _completed_context(candles: list[Candle], *, decision_ts: int, lookback: int) -> list[dict[str, Any]]:
    completed = [c for c in candles if int(c.ts) + 300 <= decision_ts]
    if len(completed) < lookback:
        raise ValueError(f"Need {lookback} completed candles before decision_ts={decision_ts}, got {len(completed)}")
    return [
        {
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in completed[-lookback:]
    ]


def _market_reference_and_outcome(candles: list[Candle], window: MarketWindowSpec) -> tuple[float, float, str]:
    start = next((c for c in candles if int(c.ts) == window.start_ts), None)
    if start is None:
        previous = [c for c in candles if int(c.ts) < window.start_ts]
        if not previous:
            raise ValueError(f"No BTC candle available before market start {window.start_ts}")
        reference_price = float(previous[-1].close)
    else:
        reference_price = float(start.open)

    final_candidates = [c for c in candles if int(c.ts) + 300 <= window.end_ts]
    if not final_candidates:
        raise ValueError(f"No BTC candle available before market end {window.end_ts}")
    final_price = float(final_candidates[-1].close)
    outcome = "YES" if final_price >= reference_price else "NO"
    return reference_price, final_price, outcome


def _predicted_outcome(signal: dict[str, Any], estimate: float) -> str | None:
    direction = str(signal.get("direction") or "").upper()
    if direction in {"UP", "YES"}:
        return "YES"
    if direction in {"DOWN", "NO"}:
        return "NO"
    if estimate > 0.5:
        return "YES"
    if estimate < 0.5:
        return "NO"
    return None


def validate_candidate_on_frames(
    *,
    rule_name: str,
    market_slug: str,
    l2_frame: pd.DataFrame,
    snapshot_frame: pd.DataFrame,
    btc_candles: list[Candle],
    decision_offset_seconds: int = 30,
    lookback: int = 20,
    bet_usd: float = 75.0,
    min_fill_ratio: float = 0.95,
    prior_model: Any | None = None,
    min_prior_edge: float = 0.0,
    slug_ts: str = "start",
) -> CandidateReplayResult:
    rules = available_rules()
    if rule_name not in rules:
        raise ValueError(f"Unknown rule: {rule_name}")
    window = parse_market_slug(market_slug, slug_ts=slug_ts)
    decision_ts = window.start_ts + int(decision_offset_seconds)
    context = _completed_context(btc_candles, decision_ts=decision_ts, lookback=lookback)
    reference_price, final_price, actual_outcome = _market_reference_and_outcome(btc_candles, window)
    regime = compute_regime_from_candles(context)
    signal = rules[rule_name](context, regime)
    estimate = max(0.0, min(1.0, float(signal.get("estimate", 0.5))))
    should_trade = bool(signal.get("should_trade", False))
    predicted = _predicted_outcome(signal, estimate) if should_trade else None
    predicted_prob = estimate if predicted == "YES" else (1.0 - estimate if predicted == "NO" else 0.5)
    prior_prob: float | None = None
    prior_direction: str | None = None
    prior_edge: float | None = None
    prior_gate_passed: bool | None = None

    if should_trade and predicted and prior_model is not None:
        prior_context = SimpleNamespace(
            formatted_candles=context,
            production_regime=regime,
            market={},
        )
        prior_prediction = prior_model.predict(prior_context)
        prior_prob = float(prior_prediction.prob_up)
        prior_direction = "YES" if prior_prob > 0.5 else "NO" if prior_prob < 0.5 else "SKIP"
        prior_edge = abs(prior_prob - 0.5)
        prior_gate_passed = prior_direction == predicted and prior_edge >= min_prior_edge
        if not prior_gate_passed:
            should_trade = False
            predicted = None

    decision_time = _naive_utc_timestamp(decision_ts)
    book = book_at_timestamp(l2_frame=l2_frame, snapshot_frame=snapshot_frame, timestamp=decision_time)
    metrics = book.metrics()
    warnings: list[str] = []
    fill: SimulatedFill | None = None
    edge_after_fill: float | None = None
    pnl = 0.0
    status = "no_trade_signal"
    if should_trade and predicted:
        fill = book.simulate_market_buy_outcome(predicted, bet_usd)
        if fill.average_price is None or fill.filled_ratio <= 0:
            status = "no_fill"
            warnings.append("no_l2_liquidity_at_decision")
        else:
            edge_after_fill = predicted_prob - fill.average_price
            if fill.filled_ratio < min_fill_ratio:
                status = "partial_fill_below_threshold"
                warnings.append("partial_fill_below_threshold")
            else:
                status = "filled"
            pnl = fill.shares - fill.spent_usdc if predicted == actual_outcome else -fill.spent_usdc

    return CandidateReplayResult(
        rule_name=rule_name,
        market_slug=market_slug,
        decision_ts=decision_ts,
        market_start_ts=window.start_ts,
        market_end_ts=window.end_ts,
        reference_price=reference_price,
        final_price=final_price,
        actual_outcome=actual_outcome,
        predicted_outcome=predicted,
        should_trade=should_trade,
        estimate=estimate,
        predicted_prob=predicted_prob,
        prior_prob=prior_prob,
        prior_direction=prior_direction,
        prior_edge=prior_edge,
        prior_gate_passed=prior_gate_passed,
        regime=str(regime["label"]),
        reason=str(signal.get("reason", "")),
        book_metrics=metrics,
        fill=fill,
        edge_after_fill=edge_after_fill,
        pnl=pnl,
        status=status,
        warnings=tuple(warnings),
    )


def result_to_dict(result: CandidateReplayResult) -> dict[str, Any]:
    data = asdict(result)
    return data


def render_markdown(result: CandidateReplayResult) -> str:
    fill = result.fill
    lines = [
        "# L2 Candidate Replay",
        "",
        f"- Rule: `{result.rule_name}`",
        f"- Market: `{result.market_slug}`",
        f"- Status: `{result.status}`",
        f"- Regime: `{result.regime}`",
        f"- Actual outcome: `{result.actual_outcome}` ({result.reference_price:.2f} -> {result.final_price:.2f})",
        f"- Signal: trade={result.should_trade} predicted=`{result.predicted_outcome}` estimate={result.estimate:.4f}",
        f"- Book: bid={result.book_metrics.best_bid} ask={result.book_metrics.best_ask} spread={result.book_metrics.spread}",
    ]
    if result.prior_prob is not None:
        lines.append(
            f"- Prior: prob={result.prior_prob:.4f} direction=`{result.prior_direction}` "
            f"edge={result.prior_edge:.4f} gate={result.prior_gate_passed}"
        )
    if fill:
        avg = "n/a" if fill.average_price is None else f"{fill.average_price:.4f}"
        edge = "n/a" if result.edge_after_fill is None else f"{result.edge_after_fill:+.4f}"
        lines.extend(
            [
                f"- Fill: spent={fill.spent_usdc:.2f}/{fill.requested_usdc:.2f} shares={fill.shares:.4f} avg={avg}",
                f"- Edge after fill: {edge}",
                f"- PnL if held to resolution: {result.pnl:+.2f}",
            ]
        )
    if result.warnings:
        lines.append(f"- Warnings: `{', '.join(result.warnings)}`")
    lines.extend(["", "Reason:", "", result.reason or "n/a"])
    return "\n".join(lines) + "\n"


def train_prior_before_decision(
    *,
    db_path: Path,
    btc_candles_path: Path,
    decision_ts: int,
    lookback: int,
    warm_up: int,
) -> BaselineProbabilityEnsemble | None:
    contexts, _ = build_rolling_contexts(
        db_path=db_path,
        btc_candles_path=btc_candles_path,
        from_date=None,
        to_date=None,
        lookback=lookback,
    )
    train_contexts = [
        context
        for context in contexts
        if int(context.market.get("timestamp", 0)) < int(decision_ts)
    ]
    if len(train_contexts) < warm_up:
        return None
    prior = BaselineProbabilityEnsemble()
    prior.fit(train_contexts)
    return prior


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate one shortlisted rule on one L2 replay market.")
    parser.add_argument("--rule", required=True, help="Rule name from v3.rule_variants")
    parser.add_argument("--market-slug", required=True, help="Example: btc-updown-15m-1774310400")
    parser.add_argument("--l2", required=True, help="L2 parquet path or URL")
    parser.add_argument("--snapshot", required=True, help="1s snapshot parquet path or URL")
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_BTC_CANDLES)
    parser.add_argument("--decision-offset-seconds", type=int, default=30)
    parser.add_argument("--slug-ts", choices=("start", "end"), default="start")
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--bet-usd", type=float, default=75.0)
    parser.add_argument("--min-fill-ratio", type=float, default=0.95)
    parser.add_argument("--prior-gate", choices=("none", "ensemble_logreg_raw_xgb"), default="none")
    parser.add_argument("--prior-min-edge", type=float, default=0.01)
    parser.add_argument("--prior-db", type=Path, default=DEFAULT_BACKTEST_DB)
    parser.add_argument("--prior-warm-up", type=int, default=5000)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args()

    l2 = read_replay_parquet(args.l2)
    snapshots = read_replay_parquet(args.snapshot)
    candles = load_candles(args.btc_candles)
    window = parse_market_slug(args.market_slug, slug_ts=args.slug_ts)
    decision_ts = window.start_ts + int(args.decision_offset_seconds)
    prior_model = None
    if args.prior_gate != "none":
        prior_model = train_prior_before_decision(
            db_path=args.prior_db,
            btc_candles_path=args.btc_candles,
            decision_ts=decision_ts,
            lookback=args.lookback,
            warm_up=args.prior_warm_up,
        )
    result = validate_candidate_on_frames(
        rule_name=args.rule,
        market_slug=args.market_slug,
        l2_frame=l2,
        snapshot_frame=snapshots,
        btc_candles=candles,
        decision_offset_seconds=args.decision_offset_seconds,
        lookback=args.lookback,
        bet_usd=args.bet_usd,
        min_fill_ratio=args.min_fill_ratio,
        prior_model=prior_model,
        min_prior_edge=args.prior_min_edge,
        slug_ts=args.slug_ts,
    )
    if args.format == "json":
        print(json.dumps(result_to_dict(result), indent=2, default=str, sort_keys=True))
    else:
        print(render_markdown(result), end="")


if __name__ == "__main__":
    main()
