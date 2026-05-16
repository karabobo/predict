import json
from pathlib import Path

from src.v3.l2_batch_replay import (
    BatchReplaySummary,
    ReplayMarketInput,
    load_manifest,
    render_markdown,
    summarize_results,
)
from src.v3.l2_candidate_replay import CandidateReplayResult
from src.v3.l2_replay import BookMetrics, SimulatedFill


def _result(
    *,
    rule_name="r1",
    offset=30,
    should_trade=True,
    status="filled",
    predicted="YES",
    actual="YES",
    edge=0.10,
    pnl=10.0,
    spent=75.0,
) -> CandidateReplayResult:
    return CandidateReplayResult(
        rule_name=rule_name,
        market_slug=f"btc-updown-15m-{1_000_000_900 + offset}",
        decision_ts=1_000_000_000 + offset,
        market_start_ts=1_000_000_000,
        market_end_ts=1_000_000_900,
        reference_price=100.0,
        final_price=101.0 if actual == "YES" else 99.0,
        actual_outcome=actual,
        predicted_outcome=predicted,
        should_trade=should_trade,
        estimate=0.6,
        predicted_prob=0.6,
        prior_prob=None,
        prior_direction=None,
        prior_edge=None,
        prior_gate_passed=None,
        regime="LOW_VOL / NEUTRAL",
        reason="test",
        book_metrics=BookMetrics(
            best_bid=0.49,
            best_ask=0.50,
            midpoint=0.495,
            spread=0.01,
            spread_pct=0.02,
            bid_depth_5pct=100.0,
            ask_depth_5pct=100.0,
            depth_imbalance=0.0,
            book_hash="h",
        ),
        fill=SimulatedFill(
            requested_usdc=75.0,
            spent_usdc=spent,
            shares=150.0,
            average_price=0.5 if spent else None,
            filled_ratio=1.0 if spent else 0.0,
            worst_price=0.5 if spent else None,
            levels_consumed=1 if spent else 0,
            outcome=predicted or "YES",
        ) if should_trade else None,
        edge_after_fill=edge,
        pnl=pnl,
        status=status,
        warnings=(),
    )


def test_load_manifest_accepts_json_object(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"markets": [{"market_slug": "m1", "l2": "l2.parquet", "snapshot": "snap.parquet"}]}),
        encoding="utf-8",
    )

    markets = load_manifest(path)

    assert markets == [ReplayMarketInput(market_slug="m1", l2="l2.parquet", snapshot="snap.parquet", rule=None)]


def test_load_manifest_reads_optional_rule_field(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"markets": [{"market_slug": "m1", "l2": "l2.parquet", "snapshot": "snap.parquet", "rule": "r1"}]}),
        encoding="utf-8",
    )

    markets = load_manifest(path)

    assert markets == [ReplayMarketInput(market_slug="m1", l2="l2.parquet", snapshot="snap.parquet", rule="r1")]


def test_summarize_results_groups_by_rule_and_offset():
    rows = [
        _result(rule_name="r1", offset=30, actual="YES", predicted="YES", pnl=75.0, spent=75.0),
        _result(rule_name="r1", offset=30, actual="NO", predicted="YES", pnl=-75.0, spent=75.0),
        _result(rule_name="r1", offset=60, should_trade=False, status="no_trade_signal", predicted=None, edge=None, pnl=0.0, spent=0.0),
    ]

    summaries = summarize_results(rows)

    first = next(s for s in summaries if s.rule_name == "r1" and s.decision_offset_seconds == 30)
    assert first.markets == 2
    assert first.signals == 2
    assert first.filled == 2
    assert first.win_rate == 0.5
    assert first.total_pnl == 0.0
    assert first.roi == 0.0

    second = next(s for s in summaries if s.decision_offset_seconds == 60)
    assert second.signals == 0
    assert "no_trade_signals" in second.warnings


def test_render_markdown_includes_replay_metrics():
    markdown = render_markdown(
        [
            BatchReplaySummary(
                rule_name="r1",
                decision_offset_seconds=30,
                markets=2,
                signals=2,
                filled=1,
                no_fill=0,
                partial_fills=1,
                total_spent=75.0,
                total_pnl=10.0,
                roi=13.333,
                avg_edge_after_fill=0.04,
                fill_rate=0.5,
                win_rate=1.0,
                warnings=("partial_fill_below_threshold",),
            )
        ]
    )

    assert "# Batch L2 Replay" in markdown
    assert "r1" in markdown
    assert "+13.33%" in markdown
