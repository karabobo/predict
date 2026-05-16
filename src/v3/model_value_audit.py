"""
Audit existing backtest and arena results before promoting any model into
realtime, paper, or execution work.

This is intentionally read-only. It turns the already-populated SQLite research
databases into a shortlist of candidates worth validating with L2 replay.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKTEST_DB = ROOT / "data" / "polymarket_backtest.db"
DEFAULT_RESEARCH_DB = ROOT / "data" / "v3_research.db"

REALISTIC_PROXY_ENTRY_SOURCES = {"neutral_50", "recent_up_share", "model_edge_5", "model_edge_8"}
LOOKAHEAD_ENTRY_SOURCES = {"market_final_yes"}


@dataclass(frozen=True)
class AuditThresholds:
    min_trades: int = 100
    min_roi: float = 5.0
    min_win_rate: float = 52.0
    min_arena_passes: int = 1
    min_coach_support: int = 5
    min_coach_precision: float = 0.70


@dataclass(frozen=True)
class BacktestRunCandidate:
    source: str
    name: str
    rule_name: str
    entry_price_source: str
    run_id: int
    trades: int
    win_rate: float
    roi: float
    pnl: float
    markets: int
    entry_realism: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ArenaCandidate:
    source: str
    name: str
    runs: int
    passed: int
    avg_roi_delta: float
    best_roi_delta: float
    avg_win_rate_delta: float
    avg_trade_ratio: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CoachCandidate:
    source: str
    name: str
    label: str
    family: str
    target_scope: str
    support_count: int
    helpful_count: int
    harmful_count: int
    precision: float
    eligible_for_ablation: bool
    warnings: tuple[str, ...]


def audit_backtest_runs(db_path: Path, thresholds: AuditThresholds) -> list[BacktestRunCandidate]:
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                run_id, rule_name, entry_price_source, markets, trades,
                trade_wins, trade_roi, trade_pnl, created_at
            FROM backtest_runs
            WHERE trades >= ?
              AND trade_roi >= ?
              AND (100.0 * trade_wins / NULLIF(trades, 0)) >= ?
            ORDER BY trade_roi DESC, trades DESC, created_at DESC
            """,
            (thresholds.min_trades, thresholds.min_roi, thresholds.min_win_rate),
        ).fetchall()
    finally:
        conn.close()

    best_by_rule_source: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["rule_name"]), str(row["entry_price_source"]))
        if key not in best_by_rule_source:
            best_by_rule_source[key] = row

    candidates = []
    for row in best_by_rule_source.values():
        source = str(row["entry_price_source"])
        warnings: list[str] = []
        if source in LOOKAHEAD_ENTRY_SOURCES:
            entry_realism = "lookahead"
            warnings.append("entry_price_source_uses_final_market_price")
        elif source in REALISTIC_PROXY_ENTRY_SOURCES:
            entry_realism = "proxy"
            warnings.append("entry_price_source_is_proxy_not_l2_fill")
        else:
            entry_realism = "unknown"
            warnings.append("entry_price_source_unknown")

        win_rate = 100.0 * int(row["trade_wins"]) / max(1, int(row["trades"]))
        candidates.append(
            BacktestRunCandidate(
                source="backtest_runs",
                name=str(row["rule_name"]),
                rule_name=str(row["rule_name"]),
                entry_price_source=source,
                run_id=int(row["run_id"]),
                trades=int(row["trades"]),
                win_rate=win_rate,
                roi=float(row["trade_roi"]),
                pnl=float(row["trade_pnl"]),
                markets=int(row["markets"]),
                entry_realism=entry_realism,
                warnings=tuple(warnings),
            )
        )
    return sorted(candidates, key=lambda item: (item.entry_realism == "lookahead", -item.roi, -item.trades))


def audit_arena_runs(db_path: Path, thresholds: AuditThresholds) -> list[ArenaCandidate]:
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    grouped: dict[str, list[dict[str, Any]]] = {}
    try:
        rows = conn.execute(
            """
            SELECT challenger, gate_passed, summary_json
            FROM arena_runs
            ORDER BY created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        challenger = str(row["challenger"])
        try:
            summary = json.loads(str(row["summary_json"]))
        except json.JSONDecodeError:
            continue
        gate = summary.get("gate", {}) if isinstance(summary, dict) else {}
        grouped.setdefault(challenger, []).append(
            {
                "passed": bool(row["gate_passed"] or gate.get("passed")),
                "roi_delta": float(gate.get("aggregate_roi_delta", 0.0) or 0.0),
                "win_rate_delta": float(gate.get("aggregate_win_rate_delta", 0.0) or 0.0),
                "trade_ratio": float(gate.get("trade_ratio", 0.0) or 0.0),
            }
        )

    candidates: list[ArenaCandidate] = []
    for challenger, runs in grouped.items():
        passed = sum(1 for run in runs if run["passed"])
        if passed < thresholds.min_arena_passes:
            continue
        roi_deltas = [float(run["roi_delta"]) for run in runs]
        win_rate_deltas = [float(run["win_rate_delta"]) for run in runs]
        trade_ratios = [float(run["trade_ratio"]) for run in runs]
        warnings: list[str] = []
        if sum(roi_deltas) / len(roi_deltas) < thresholds.min_roi:
            warnings.append("average_roi_delta_below_backtest_threshold")
        if sum(trade_ratios) / len(trade_ratios) < 0.60:
            warnings.append("average_trade_ratio_below_gate")
        candidates.append(
            ArenaCandidate(
                source="arena_runs",
                name=challenger,
                runs=len(runs),
                passed=passed,
                avg_roi_delta=sum(roi_deltas) / len(roi_deltas),
                best_roi_delta=max(roi_deltas),
                avg_win_rate_delta=sum(win_rate_deltas) / len(win_rate_deltas),
                avg_trade_ratio=sum(trade_ratios) / len(trade_ratios),
                warnings=tuple(warnings),
            )
        )
    return sorted(candidates, key=lambda item: (-item.passed, -item.avg_roi_delta, item.name))


def audit_coach_candidates(db_path: Path, thresholds: AuditThresholds) -> list[CoachCandidate]:
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                spec_name, spec_label, family, target_scope, support_count,
                helpful_count, harmful_count, precision, eligible_for_ablation
            FROM coach_rule_candidate_specs
            WHERE support_count >= ?
              AND precision >= ?
              AND eligible_for_ablation = 1
            ORDER BY precision DESC, support_count DESC, helpful_count DESC
            """,
            (thresholds.min_coach_support, thresholds.min_coach_precision),
        ).fetchall()
    finally:
        conn.close()

    candidates: list[CoachCandidate] = []
    for row in rows:
        warnings = ("requires_rule_backtest_and_l2_replay",)
        candidates.append(
            CoachCandidate(
                source="coach_rule_candidate_specs",
                name=str(row["spec_name"]),
                label=str(row["spec_label"]),
                family=str(row["family"]),
                target_scope=str(row["target_scope"]),
                support_count=int(row["support_count"]),
                helpful_count=int(row["helpful_count"]),
                harmful_count=int(row["harmful_count"]),
                precision=float(row["precision"]),
                eligible_for_ablation=bool(row["eligible_for_ablation"]),
                warnings=warnings,
            )
        )
    return candidates


def build_audit_report(
    *,
    backtest_db: Path = DEFAULT_BACKTEST_DB,
    research_db: Path = DEFAULT_RESEARCH_DB,
    thresholds: AuditThresholds | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or AuditThresholds()
    return {
        "thresholds": asdict(thresholds),
        "backtest_candidates": [asdict(item) for item in audit_backtest_runs(backtest_db, thresholds)],
        "arena_candidates": [asdict(item) for item in audit_arena_runs(research_db, thresholds)],
        "coach_candidates": [asdict(item) for item in audit_coach_candidates(research_db, thresholds)],
        "next_gate": "Run L2 replay only for shortlisted backtest/arena/coach candidates before realtime or paper integration.",
    }


def render_markdown(report: dict[str, Any], *, max_rows: int = 20) -> str:
    lines = [
        "# Model Value Audit",
        "",
        "This report is a pre-realtime shortlist. Candidates here still require L2 replay validation.",
        "",
        "## Thresholds",
        "",
    ]
    for key, value in report["thresholds"].items():
        lines.append(f"- `{key}`: `{value}`")

    lines.extend(["", "## Backtest Candidates", ""])
    lines.append("| Rule | Entry | Trades | WR | ROI | PnL | Warnings |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for item in report["backtest_candidates"][:max_rows]:
        warnings = ", ".join(item["warnings"]) or "-"
        lines.append(
            f"| `{item['rule_name']}` | `{item['entry_price_source']}` | {item['trades']} | "
            f"{item['win_rate']:.2f}% | {item['roi']:+.2f}% | {item['pnl']:+.2f} | {warnings} |"
        )
    if not report["backtest_candidates"]:
        lines.append("| _none_ |  |  |  |  |  |  |")

    lines.extend(["", "## Arena Candidates", ""])
    lines.append("| Challenger | Runs | Passed | Avg ROI Delta | Best ROI Delta | Avg Trade Ratio | Warnings |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for item in report["arena_candidates"][:max_rows]:
        warnings = ", ".join(item["warnings"]) or "-"
        lines.append(
            f"| `{item['name']}` | {item['runs']} | {item['passed']} | "
            f"{item['avg_roi_delta']:+.2f}pp | {item['best_roi_delta']:+.2f}pp | "
            f"{item['avg_trade_ratio']:.2f} | {warnings} |"
        )
    if not report["arena_candidates"]:
        lines.append("| _none_ |  |  |  |  |  |  |")

    lines.extend(["", "## Coach Candidates", ""])
    lines.append("| Spec | Family | Scope | Support | Precision | Warnings |")
    lines.append("|---|---|---|---:|---:|---|")
    for item in report["coach_candidates"][:max_rows]:
        warnings = ", ".join(item["warnings"]) or "-"
        lines.append(
            f"| `{item['name']}` | `{item['family']}` | `{item['target_scope']}` | "
            f"{item['support_count']} | {item['precision']:.2f} | {warnings} |"
        )
    if not report["coach_candidates"]:
        lines.append("| _none_ |  |  |  |  |  |")

    lines.extend(["", "## Next Gate", "", str(report["next_gate"])])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit existing model/rule value before realtime or paper integration.")
    parser.add_argument("--backtest-db", type=Path, default=DEFAULT_BACKTEST_DB)
    parser.add_argument("--research-db", type=Path, default=DEFAULT_RESEARCH_DB)
    parser.add_argument("--min-trades", type=int, default=AuditThresholds.min_trades)
    parser.add_argument("--min-roi", type=float, default=AuditThresholds.min_roi)
    parser.add_argument("--min-win-rate", type=float, default=AuditThresholds.min_win_rate)
    parser.add_argument("--min-arena-passes", type=int, default=AuditThresholds.min_arena_passes)
    parser.add_argument("--min-coach-support", type=int, default=AuditThresholds.min_coach_support)
    parser.add_argument("--min-coach-precision", type=float, default=AuditThresholds.min_coach_precision)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args()

    thresholds = AuditThresholds(
        min_trades=args.min_trades,
        min_roi=args.min_roi,
        min_win_rate=args.min_win_rate,
        min_arena_passes=args.min_arena_passes,
        min_coach_support=args.min_coach_support,
        min_coach_precision=args.min_coach_precision,
    )
    report = build_audit_report(backtest_db=args.backtest_db, research_db=args.research_db, thresholds=thresholds)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report), end="")


if __name__ == "__main__":
    main()
