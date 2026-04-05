"""
Summarize historical backtest runs from polymarket_backtest.db.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "polymarket_backtest.db"


def fetch_runs(
    db: sqlite3.Connection,
    *,
    rules: list[str] | None,
    entry_price_sources: list[str] | None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str] = []
    if rules:
        clauses.append(f"rule_name IN ({','.join(['?'] * len(rules))})")
        params.extend(rules)
    if entry_price_sources:
        clauses.append(f"entry_price_source IN ({','.join(['?'] * len(entry_price_sources))})")
        params.extend(entry_price_sources)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT run_id, rule_name, entry_price_source, markets, eligible_markets,
               signal_calls, signal_wins, trades, trade_wins, trade_pnl,
               trade_wagered, trade_roi, created_at
        FROM backtest_runs
        {where}
        ORDER BY trade_roi DESC, run_id ASC
    """
    return db.execute(query, params).fetchall()


def print_table(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("No matching backtest runs found.")
        return

    headers = [
        "run_id",
        "rule_name",
        "entry",
        "trades",
        "trade_wr",
        "trade_pnl",
        "trade_roi",
    ]
    data: list[list[str]] = []
    for row in rows:
        trade_wr = (float(row["trade_wins"]) / float(row["trades"]) * 100.0) if row["trades"] else 0.0
        data.append(
            [
                str(row["run_id"]),
                str(row["rule_name"]),
                str(row["entry_price_source"]),
                str(row["trades"]),
                f"{trade_wr:.2f}%",
                f"{float(row['trade_pnl']):.2f}",
                f"{float(row['trade_roi']):.2f}%",
            ]
        )

    widths = [len(h) for h in headers]
    for row in data:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    print(" ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)))
    print(" ".join("-" * widths[idx] for idx in range(len(headers))))
    for row in data:
        print(" ".join(value.ljust(widths[idx]) for idx, value in enumerate(row)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize rule backtest runs.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Backtest SQLite DB")
    parser.add_argument("--rules", type=str, default=None, help="Comma-separated rule names")
    parser.add_argument("--entry-price-sources", type=str, default=None, help="Comma-separated entry price sources")
    args = parser.parse_args()

    rules = [item.strip() for item in args.rules.split(",") if item.strip()] if args.rules else None
    sources = [item.strip() for item in args.entry_price_sources.split(",") if item.strip()] if args.entry_price_sources else None

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    try:
        rows = fetch_runs(db, rules=rules, entry_price_sources=sources)
    finally:
        db.close()

    print_table(rows)


if __name__ == "__main__":
    main()
