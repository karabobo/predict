from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run probability arena across multiple BTC candle sources.")
    parser.add_argument("--sources", nargs="+", required=True, help="name=path pairs")
    parser.add_argument("--warm-up", type=int, default=500)
    parser.add_argument("--folds", type=int, default=6)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--min-prob-edges", default="0,0.005,0.01,0.02,0.03,0.05")
    parser.add_argument(
        "--contenders",
        nargs="+",
        default=["paper_logreg_5m_window", "paper_logreg_5m_raw", "ensemble_logreg_raw_xgb"],
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def run_source(name: str, path: Path, args: argparse.Namespace) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "src.v3.probability_arena",
        "--btc-candles",
        str(path),
        "--warm-up",
        str(args.warm_up),
        "--folds",
        str(args.folds),
        "--lookback",
        str(args.lookback),
        "--min-prob-edges",
        args.min_prob_edges,
        "--contenders",
        *args.contenders,
        "--format",
        "json",
    ]
    completed = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    result["source_name"] = name
    result["source_path"] = str(path)
    return result


def compact(result: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for contender in result["contenders"]:
        best = contender["best_threshold"]
        threshold_0 = next((row for row in contender["thresholds"] if row["min_prob_edge"] == 0.0), None)
        threshold_005 = next((row for row in contender["thresholds"] if abs(row["min_prob_edge"] - 0.005) < 1e-12), None)
        rows.append(
            {
                "name": contender["name"],
                "direction_wr": contender["direction_wr"],
                "avg_brier": contender["avg_brier"],
                "best_edge": best["min_prob_edge"],
                "best_trades": best["trades"],
                "best_wr": best["win_rate"],
                "best_roi": best["roi"],
                "best_pnl": best["pnl"],
                "edge0_wr": threshold_0["win_rate"] if threshold_0 else None,
                "edge0_pnl": threshold_0["pnl"] if threshold_0 else None,
                "edge005_wr": threshold_005["win_rate"] if threshold_005 else None,
                "edge005_pnl": threshold_005["pnl"] if threshold_005 else None,
            }
        )
    return {
        "source_name": result["source_name"],
        "source_path": result["source_path"],
        "eligible_markets": result["eligible_markets"],
        "winner": result["winner"],
        "contenders": rows,
    }


def render_markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Probability Cross Source Validation",
        "",
        "| Source | Eligible | Winner | Contender | Direction WR | Brier | Best edge | Best trades | Best WR | Best ROI | Best PnL | Edge 0 WR | Edge 0 PnL | Edge .005 WR | Edge .005 PnL |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        source = result["source_name"]
        for row in result["contenders"]:
            lines.append(
                f"| {source} | {result['eligible_markets']} | `{result['winner']}` | `{row['name']}` | "
                f"{row['direction_wr']:.2f}% | {row['avg_brier']:.6f} | {row['best_edge']:.4f} | "
                f"{row['best_trades']} | {row['best_wr']:.2f}% | {row['best_roi']:+.2f}% | "
                f"{row['best_pnl']:+.2f} | {row['edge0_wr']:.2f}% | {row['edge0_pnl']:+.2f} | "
                f"{row['edge005_wr']:.2f}% | {row['edge005_pnl']:+.2f} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    results = []
    for raw in args.sources:
        if "=" not in raw:
            raise ValueError(f"Source must be name=path: {raw}")
        name, path = raw.split("=", 1)
        results.append(compact(run_source(name, Path(path), args)))
    markdown = render_markdown(results)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
