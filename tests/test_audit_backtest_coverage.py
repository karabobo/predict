import sqlite3
from pathlib import Path

from src.v3.audit_backtest_coverage import (
    build_missing_slug_manifest,
    load_coverage_summary,
)


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE historical_markets (
            market_id TEXT PRIMARY KEY,
            end_date TEXT,
            created_at TEXT,
            outcome INTEGER,
            window_start_ts INTEGER,
            source_file TEXT
        )
        """
    )
    rows = [
        ("m1", "2026-01-01T00:00:00+00:00", "2025-12-31T23:55:00+00:00", 1, 1767225600, "/root/Data/a.parquet"),
        ("m2", "2026-01-01T00:10:00+00:00", "2026-01-01T00:05:00+00:00", None, 1767226200, "/root/Data/a.parquet"),
    ]
    conn.executemany("INSERT INTO historical_markets VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_load_coverage_summary_reads_bounds(tmp_path: Path):
    db = tmp_path / "coverage.db"
    _seed_db(db)

    summary = load_coverage_summary(db)

    assert summary.markets == 2
    assert summary.resolved_markets == 1
    assert summary.first_end_date == "2026-01-01T00:00:00+00:00"
    assert summary.last_end_date == "2026-01-01T00:10:00+00:00"
    assert summary.source_names == ("a.parquet",)


def test_build_missing_slug_manifest_finds_missing_5m_windows(tmp_path: Path):
    db = tmp_path / "coverage.db"
    _seed_db(db)

    missing = build_missing_slug_manifest(
        db,
        expected_from="2026-01-01T00:00:00+00:00",
        expected_to="2026-01-01T00:10:00+00:00",
    )

    assert len(missing) == 1
    assert missing[0]["slug"] == "btc-updown-5m-1767225900"
