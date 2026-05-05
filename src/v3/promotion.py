"""
promotion.py — Out-of-sample promotion harness for v3 challengers.

Usage:
    PYTHONPATH=. python src/v3/promotion.py --list-challengers
    PYTHONPATH=. python src/v3/promotion.py --challenger legacy_regime_filtered
    PYTHONPATH=. python src/v3/promotion.py --challenger v3_ml --days 21 --folds 5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.notifier import notify_deepseek_promotion
from src.v3.arena import (
    PromotionGate,
    build_research_dataset,
    contender_factories,
    evaluate_head_to_head,
    print_head_to_head_report,
)
from src.v3.config import MIN_EDGE, RESEARCH_DB_NAME

DB_PATH = Path(__file__).parent.parent.parent / "data" / RESEARCH_DB_NAME
REPORTS_DIR = Path(__file__).parent.parent.parent / "docs" / "research"


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS arena_runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            baseline TEXT NOT NULL,
            challenger TEXT NOT NULL,
            days INTEGER NOT NULL,
            warm_up INTEGER NOT NULL,
            folds INTEGER NOT NULL,
            bet_size REAL NOT NULL,
            min_edge REAL NOT NULL,
            max_eval_contexts INTEGER NOT NULL DEFAULT 24,
            gate_passed INTEGER NOT NULL,
            summary_json TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS arena_fold_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            fold_index INTEGER NOT NULL,
            contender TEXT NOT NULL,
            eval_markets INTEGER NOT NULL,
            trades INTEGER NOT NULL,
            directional_accuracy REAL NOT NULL,
            avg_brier REAL NOT NULL,
            roi REAL NOT NULL,
            pnl REAL NOT NULL,
            max_drawdown REAL NOT NULL,
            summary_json TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES arena_runs(run_id)
        )
        """
    )
    db.commit()
    try:
        db.execute("ALTER TABLE arena_runs ADD COLUMN max_eval_contexts INTEGER NOT NULL DEFAULT 24")
        db.commit()
    except sqlite3.OperationalError:
        pass
    return db


def store_run(db: sqlite3.Connection, args: argparse.Namespace, results: dict) -> str:
    run_id = uuid.uuid4().hex[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO arena_runs
        (run_id, created_at, baseline, challenger, days, warm_up, folds, bet_size, min_edge, max_eval_contexts, gate_passed, summary_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            created_at,
            args.baseline,
            args.challenger,
            args.days,
            args.warm_up,
            args.folds,
            args.bet_size,
            args.min_edge,
            args.max_eval_contexts,
            int(results["gate"]["passed"]),
            json.dumps(results),
        ),
    )

    for contender_key in ("baseline_folds", "challenger_folds"):
        for fold in results[contender_key]:
            db.execute(
                """
                INSERT INTO arena_fold_results
                (run_id, fold_index, contender, eval_markets, trades, directional_accuracy, avg_brier, roi, pnl, max_drawdown, summary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    fold["fold_index"],
                    fold["name"],
                    fold["eval_markets"],
                    fold["trades"],
                    fold["directional_accuracy"],
                    fold["avg_brier"],
                    fold["roi"],
                    fold["pnl"],
                    fold["max_drawdown"],
                    json.dumps(fold),
                ),
            )

    db.commit()
    return run_id


def write_report(run_id: str, args: argparse.Namespace, results: dict) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    latest_path = REPORTS_DIR / "latest.md"
    run_path = REPORTS_DIR / f"run-{run_id}.md"

    content = _render_report(run_id, timestamp, args, results)
    latest_path.write_text(content, encoding="utf-8")
    run_path.write_text(content, encoding="utf-8")
    return latest_path, run_path


def _render_report(run_id: str, timestamp: str, args: argparse.Namespace, results: dict) -> str:
    baseline = results["baseline"]
    challenger = results["challenger"]
    gate = results["gate"]
    regime_findings = results.get("regime_findings", {})
    challenger_meta = challenger.get("contender_metadata", {})

    lines = [
        "# Research Promotion Report",
        "",
        f"- Run ID: `{run_id}`",
        f"- Generated: `{timestamp}`",
        f"- Baseline: `{args.baseline}`",
        f"- Challenger: `{args.challenger}`",
        f"- Dataset days: `{args.days}`",
        f"- Warm-up markets: `{args.warm_up}`",
        f"- Folds: `{args.folds}`",
        f"- Max eval contexts per fold: `{args.max_eval_contexts}`",
        f"- Promotion gate: `{'PASS' if gate['passed'] else 'FAIL'}`",
        "",
        "## Summary",
        "",
        "| Metric | Baseline | Challenger | Delta |",
        "| --- | ---: | ---: | ---: |",
        f"| Eval markets | {baseline['eval_markets']} | {challenger['eval_markets']} | {challenger['eval_markets'] - baseline['eval_markets']} |",
        f"| Trades | {baseline['trades']} | {challenger['trades']} | {challenger['trades'] - baseline['trades']} |",
        f"| Directional accuracy | {_pct(baseline['directional_accuracy'])} | {_pct(challenger['directional_accuracy'])} | {_pp(challenger['directional_accuracy'] - baseline['directional_accuracy'])} |",
        f"| Avg Brier | {baseline['avg_brier']:.4f} | {challenger['avg_brier']:.4f} | {challenger['avg_brier'] - baseline['avg_brier']:+.4f} |",
        f"| Win rate | {_pct(baseline['win_rate'])} | {_pct(challenger['win_rate'])} | {_pp(challenger['win_rate'] - baseline['win_rate'])} |",
        f"| ROI | {baseline['roi']:+.2f}% | {challenger['roi']:+.2f}% | {challenger['roi'] - baseline['roi']:+.2f}pp |",
        f"| P&L | {_usd(baseline['pnl'])} | {_usd(challenger['pnl'])} | {_usd(challenger['pnl'] - baseline['pnl'])} |",
        f"| Max drawdown | {_usd(baseline['max_drawdown'])} | {_usd(challenger['max_drawdown'])} | {_usd(challenger['max_drawdown'] - baseline['max_drawdown'])} |",
        "",
        "## Gate Decision",
        "",
        f"- Result: `{'PASS' if gate['passed'] else 'FAIL'}`",
        f"- Passing folds: `{gate['passing_folds']}/{len(gate['fold_checks'])}`",
        f"- Aggregate ROI delta: `{gate['aggregate_roi_delta']:+.2f}pp`",
        f"- Aggregate win-rate delta: `{gate['aggregate_win_rate_delta']:+.2f}pp`",
        f"- Trade ratio: `{gate['trade_ratio']:.2f}`",
        f"- Drawdown ratio: `{gate['drawdown_ratio']:.2f}`",
        "",
    ]

    if gate["reasons"]:
        lines.extend(["### Why It Failed", ""])
        for reason in gate["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")
    else:
        lines.extend(["### Why It Passed", "", "- Challenger cleared every aggregate gate and fold check.", ""])

    lines.extend(
        [
            "## Fold Checks",
            "",
            "| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for fold in gate["fold_checks"]:
        lines.append(
            f"| {fold['fold_index']} | {'PASS' if fold['pass'] else 'FAIL'} | "
            f"{fold['roi_delta']:+.2f}pp | {fold['win_rate_delta']:+.2f}pp | "
            f"{fold['trade_ratio']:.2f} | {'yes' if fold['drawdown_ok'] else 'no'} |"
        )

    regime_rows = regime_findings.get("rows", [])
    if regime_rows:
        lines.extend(
            [
                "",
                "## Regime Breakdown",
                "",
                "| Regime | Baseline Trades | Challenger Trades | ROI Delta | WR Delta | P&L Delta |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in regime_rows:
            lines.append(
                f"| {row['regime']} | {row['baseline_trades']} | {row['challenger_trades']} | "
                f"{row['roi_delta']:+.2f}pp | {row['win_rate_delta']:+.2f}pp | {_usd(row['pnl_delta'])} |"
            )
        takeaways = regime_findings.get("takeaways", [])
        if takeaways:
            lines.extend(["", "### Regime Takeaways", ""])
            for takeaway in takeaways:
                lines.append(f"- {takeaway}")

    lines.extend(["", "## Recommendation", ""])
    if gate["passed"]:
        lines.append(f"- Promote `{args.challenger}` for the next production challenge round.")
    else:
        lines.append(f"- Keep production pinned to `{args.baseline}`.")
        if gate["reasons"]:
            lines.append(f"- Primary blocker: {gate['reasons'][0]}")

    if challenger_meta:
        lines.extend(["", "## Challenger Metadata", ""])
        for key in (
            "foundation_status",
            "train_samples",
            "calibration_samples",
            "primary_model_name",
            "secondary_model_name",
            "calibrated",
        ):
            if key in challenger_meta:
                lines.append(f"- {key}: `{challenger_meta[key]}`")
        diagnostics = challenger_meta.get("diagnostics")
        if isinstance(diagnostics, dict) and diagnostics:
            for key, value in diagnostics.items():
                lines.append(f"- diagnostics.{key}: `{value}`")

    lines.append("")
    return "\n".join(lines)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _pp(value: float) -> str:
    return f"{value * 100:+.2f}pp"


def _usd(value: float) -> str:
    return f"${value:+.2f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V3 sample-out promotion harness")
    parser.add_argument("--baseline", default="production_baseline", help="Baseline contender name")
    parser.add_argument("--challenger", default="legacy_regime_filtered", help="Challenger contender name")
    parser.add_argument("--days", type=int, default=14, help="Days of BTC history to evaluate")
    parser.add_argument("--warm-up", type=int, default=500, help="Markets reserved for initial training context")
    parser.add_argument("--folds", type=int, default=4, help="Blocked time-series evaluation folds")
    parser.add_argument("--bet-size", type=float, default=75.0, help="Fixed research bet size")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE, help="Minimum edge after fees/slippage")
    parser.add_argument("--seed", type=int, default=42, help="Seed for deterministic slippage")
    parser.add_argument("--btc-candles-file", type=str, default=None, help="Optional local BTC 5m candles parquet/csv")
    parser.add_argument(
        "--max-eval-contexts",
        type=int,
        default=24,
        help="Cap evaluation samples per fold to keep LLM challengers tractable",
    )
    parser.add_argument("--list-challengers", action="store_true", help="List built-in contenders and exit")
    parser.add_argument("--min-roi-delta", type=float, default=None, help="Override aggregate ROI delta gate (pp)")
    parser.add_argument("--min-win-rate-delta", type=float, default=None, help="Override aggregate win-rate delta gate (pp)")
    parser.add_argument("--min-trade-ratio", type=float, default=None, help="Override minimum challenger trade ratio gate")
    parser.add_argument("--max-drawdown-worsening", type=float, default=None, help="Override allowed drawdown worsening ratio")
    parser.add_argument("--min-fold-pass-rate", type=float, default=None, help="Override required passing fold rate")
    return parser.parse_args()


def build_gate(args: argparse.Namespace) -> PromotionGate:
    gate = PromotionGate()
    return PromotionGate(
        min_total_roi_delta=args.min_roi_delta if args.min_roi_delta is not None else gate.min_total_roi_delta,
        min_total_win_rate_delta=(
            args.min_win_rate_delta if args.min_win_rate_delta is not None else gate.min_total_win_rate_delta
        ),
        min_trade_ratio=args.min_trade_ratio if args.min_trade_ratio is not None else gate.min_trade_ratio,
        max_drawdown_worsening=(
            args.max_drawdown_worsening if args.max_drawdown_worsening is not None else gate.max_drawdown_worsening
        ),
        min_fold_pass_rate=args.min_fold_pass_rate if args.min_fold_pass_rate is not None else gate.min_fold_pass_rate,
    )


def main() -> None:
    args = parse_args()
    factories = contender_factories()

    if args.list_challengers:
        print("Available contenders:")
        for name in sorted(factories):
            print(f"  - {name}")
        return

    if args.baseline not in factories:
        raise SystemExit(f"Unknown baseline contender: {args.baseline}")
    if args.challenger not in factories:
        raise SystemExit(f"Unknown challenger contender: {args.challenger}")

    dataset = build_research_dataset(days=args.days, candles_file=args.btc_candles_file)
    print(
        f"Dataset: {len(dataset['markets'])} markets from "
        f"{dataset['start_date'].strftime('%Y-%m-%d')} to {dataset['end_date'].strftime('%Y-%m-%d')}"
    )

    results = evaluate_head_to_head(
        dataset["contexts"],
        baseline_name=args.baseline,
        challenger_name=args.challenger,
        warm_up=args.warm_up,
        folds=args.folds,
        bet_size=args.bet_size,
        min_edge=args.min_edge,
        seed=args.seed,
        max_eval_contexts=args.max_eval_contexts,
        gate=build_gate(args),
    )
    print_head_to_head_report(results)

    db = init_db()
    try:
        run_id = store_run(db, args, results)
    finally:
        db.close()

    latest_path, run_path = write_report(run_id, args, results)
    notify_deepseek_promotion(
        run_id=run_id,
        baseline=args.baseline,
        challenger=args.challenger,
        results=results,
        report_path=run_path,
    )

    print(f"\nStored research run: {run_id}")
    print(f"Database: {DB_PATH}")
    print(f"Latest report: {latest_path}")
    print(f"Run report: {run_path}")


if __name__ == "__main__":
    main()
