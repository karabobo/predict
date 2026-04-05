"""
Audit local historical Polymarket BTC 5-minute coverage and optionally emit a
manifest of missing market slugs for a target time range.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "polymarket_backtest.db"


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CoverageSummary:
    markets: int
    resolved_markets: int
    first_end_date: str | None
    last_end_date: str | None
    first_created_at: str | None
    last_created_at: str | None
    source_names: tuple[str, ...]

    @property
    def coverage_days(self) -> int:
        if not self.first_end_date or not self.last_end_date:
            return 0
        return max(0, (_parse_dt(self.last_end_date) - _parse_dt(self.first_end_date)).days)


def load_coverage_summary(db_path: Path) -> CoverageSummary:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT
                count(*) AS markets,
                sum(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS resolved_markets,
                min(end_date) AS first_end_date,
                max(end_date) AS last_end_date,
                min(created_at) AS first_created_at,
                max(created_at) AS last_created_at,
                group_concat(DISTINCT source_file) AS source_files
            FROM historical_markets
            """
        ).fetchone()
        source_names = ()
        if row["source_files"]:
            source_names = tuple(
                Path(part).name for part in str(row["source_files"]).split(",") if part
            )
        return CoverageSummary(
            markets=int(row["markets"] or 0),
            resolved_markets=int(row["resolved_markets"] or 0),
            first_end_date=row["first_end_date"],
            last_end_date=row["last_end_date"],
            first_created_at=row["first_created_at"],
            last_created_at=row["last_created_at"],
            source_names=source_names,
        )
    finally:
        conn.close()


def build_missing_slug_manifest(
    db_path: Path,
    *,
    expected_from: str,
    expected_to: str,
) -> list[dict[str, Any]]:
    start_ts = int(_parse_dt(expected_from).timestamp())
    end_ts = int(_parse_dt(expected_to).timestamp())
    if end_ts < start_ts:
        raise ValueError("expected_to must be >= expected_from")

    conn = sqlite3.connect(db_path)
    try:
        present = {
            int(row[0])
            for row in conn.execute(
                """
                SELECT window_start_ts
                FROM historical_markets
                WHERE window_start_ts IS NOT NULL
                  AND window_start_ts BETWEEN ? AND ?
                """,
                (start_ts, end_ts),
            ).fetchall()
        }
    finally:
        conn.close()

    missing: list[dict[str, Any]] = []
    current = start_ts - (start_ts % 300)
    final = end_ts - (end_ts % 300)
    while current <= final:
        if current not in present:
            missing.append(
                {
                    "window_start_ts": current,
                    "window_start_at": datetime.fromtimestamp(current, tz=timezone.utc).isoformat(),
                    "slug": f"btc-updown-5m-{current}",
                }
            )
        current += 300
    return missing


def write_manifest(rows: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["window_start_ts", "window_start_at", "slug"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit local historical backtest coverage")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--expected-from", type=str, help="Expected UTC start (ISO-8601)")
    parser.add_argument("--expected-to", type=str, help="Expected UTC end (ISO-8601)")
    parser.add_argument("--write-manifest", type=Path, help="Write missing slug manifest CSV")
    args = parser.parse_args()

    summary = load_coverage_summary(args.db)
    print(f"markets={summary.markets}")
    print(f"resolved_markets={summary.resolved_markets}")
    print(f"first_end_date={summary.first_end_date}")
    print(f"last_end_date={summary.last_end_date}")
    print(f"coverage_days={summary.coverage_days}")
    print(f"source_files={','.join(summary.source_names) if summary.source_names else 'n/a'}")

    if args.expected_from and args.expected_to:
        missing = build_missing_slug_manifest(
            args.db,
            expected_from=args.expected_from,
            expected_to=args.expected_to,
        )
        print(f"missing_windows={len(missing)}")
        if args.write_manifest:
            write_manifest(missing, args.write_manifest)
            print(f"manifest={args.write_manifest}")
    elif args.write_manifest:
        raise SystemExit("--write-manifest requires --expected-from and --expected-to")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
