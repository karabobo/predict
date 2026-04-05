"""
Batch backtest eligible coach-derived deterministic rule drafts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.v3.backtest_rules import DEFAULT_DB, run_backtest
from src.v3.rule_variants import load_dynamic_coach_rule_metadata


DEFAULT_BTC_CANDLES = Path("/root/Data/btc_5m.parquet")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest eligible coach-derived rule drafts.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Backtest SQLite DB")
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_BTC_CANDLES, help="BTC 5m candles parquet/csv")
    parser.add_argument(
        "--entry-price-sources",
        type=str,
        default="neutral_50,model_edge_8",
        help="Comma-separated entry price sources",
    )
    parser.add_argument("--lookback", type=int, default=20, help="Context lookback candles")
    parser.add_argument("--from-date", type=str, default=None, help="Optional lower bound on market end_date")
    parser.add_argument("--to-date", type=str, default=None, help="Optional upper bound on market end_date")
    args = parser.parse_args()

    metadata = load_dynamic_coach_rule_metadata()
    if not metadata:
        print("No eligible coach-derived rule drafts found.")
        return 0

    entry_price_sources = [item.strip() for item in args.entry_price_sources.split(",") if item.strip()]
    rows: list[dict[str, object]] = []
    for rule_name, spec in sorted(metadata.items()):
        for source in entry_price_sources:
            result = run_backtest(
                db_path=args.db,
                btc_candles_path=args.btc_candles,
                rule_name=rule_name,
                lookback=args.lookback,
                entry_price_source=source,
                from_date=args.from_date,
                to_date=args.to_date,
            )
            rows.append(
                {
                    "rule_name": rule_name,
                    "label": spec["spec_label"],
                    "entry": source,
                    "trades": result["trades"],
                    "trade_wr": result["trade_wr"],
                    "trade_roi": result["trade_roi"],
                    "run_id": result["run_id"],
                }
            )

    print("Backtested coach-derived rule drafts")
    for row in rows:
        print(
            f"- {row['label']} [{row['entry']}]: "
            f"trades={row['trades']} wr={row['trade_wr']:.2f}% roi={row['trade_roi']:+.2f}% run_id={row['run_id']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
