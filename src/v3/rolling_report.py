"""
Report monthly and recent-window performance for a backtest rule.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "polymarket_backtest.db"


def latest_run(db: sqlite3.Connection, rule_name: str, entry_price_source: str) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT run_id, rule_name, entry_price_source, created_at
        FROM backtest_runs
        WHERE rule_name = ? AND entry_price_source = ?
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (rule_name, entry_price_source),
    ).fetchone()


def monthly_rows(db: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT substr(end_date, 1, 7) AS ym,
               count(*) AS trades,
               sum(won) AS wins,
               sum(wager) AS wagered,
               sum(pnl) AS pnl
        FROM backtest_trades
        WHERE run_id = ? AND should_trade = 1
        GROUP BY ym
        ORDER BY ym
        """,
        (run_id,),
    ).fetchall()


def window_rows(db: sqlite3.Connection, run_id: int, windows: list[int]) -> list[tuple[int, sqlite3.Row]]:
    max_end = db.execute(
        "SELECT max(end_date) FROM backtest_trades WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    if max_end is None:
        return []

    rows: list[tuple[int, sqlite3.Row]] = []
    for days in windows:
        row = db.execute(
            """
            SELECT count(*) AS trades,
                   sum(won) AS wins,
                   sum(wager) AS wagered,
                   sum(pnl) AS pnl
            FROM backtest_trades
            WHERE run_id = ?
              AND should_trade = 1
              AND end_date >= datetime(?, ?)
            """,
            (run_id, max_end, f"-{days} days"),
        ).fetchone()
        rows.append((days, row))
    return rows


def fmt_wr(wins: int | None, trades: int | None) -> str:
    if not trades:
        return "0.00%"
    return f"{(float(wins or 0) / float(trades) * 100.0):.2f}%"


def fmt_roi(pnl: float | None, wagered: float | None) -> str:
    if not wagered:
        return "0.00%"
    return f"{(float(pnl or 0.0) / float(wagered) * 100.0):.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="Show monthly and rolling-window performance for a rule.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--rule", required=True, type=str)
    parser.add_argument("--entry-price-source", type=str, default="neutral_50")
    parser.add_argument("--windows", type=str, default="14,30")
    args = parser.parse_args()

    windows = [int(item.strip()) for item in args.windows.split(",") if item.strip()]

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    try:
        run = latest_run(db, args.rule, args.entry_price_source)
        if run is None:
            print("No matching run found.")
            return

        print(f"run_id={run['run_id']} rule={run['rule_name']} entry={run['entry_price_source']}")
        print("\nMonthly")
        print("month trades wr roi pnl")
        for row in monthly_rows(db, int(run["run_id"])):
            print(
                row["ym"],
                row["trades"],
                fmt_wr(row["wins"], row["trades"]),
                fmt_roi(row["pnl"], row["wagered"]),
                f"{float(row['pnl'] or 0.0):.2f}",
            )

        print("\nRolling windows")
        print("days trades wr roi pnl")
        for days, row in window_rows(db, int(run["run_id"]), windows):
            print(
                days,
                row["trades"],
                fmt_wr(row["wins"], row["trades"]),
                fmt_roi(row["pnl"], row["wagered"]),
                f"{float(row['pnl'] or 0.0):.2f}",
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
