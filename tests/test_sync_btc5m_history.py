from pathlib import Path

import pandas as pd

from src.v3.sync_btc5m_history import (
    filter_btc5m_markets,
    merge_market_frames,
    sync_btc5m_history,
)


def test_filter_btc5m_markets_keeps_only_5m_slug_rows():
    frame = pd.DataFrame(
        [
            {"id": "1", "slug": "btc-updown-5m-1771002000", "end_date": "2026-02-13T17:05:00+00:00"},
            {"id": "2", "slug": "btc-updown-15m-1771002000", "end_date": "2026-02-13T17:15:00+00:00"},
            {"id": "3", "slug": "eth-updown-5m-1771002000", "end_date": "2026-02-13T17:05:00+00:00"},
        ]
    )

    filtered = filter_btc5m_markets(frame)

    assert len(filtered) == 1
    assert filtered.iloc[0]["id"] == "1"


def test_merge_market_frames_prefers_newer_duplicate():
    existing = pd.DataFrame(
        [
            {"id": "1", "slug": "btc-updown-5m-1", "updated_at": "2026-01-01T00:00:00+00:00", "closed": False},
        ]
    )
    incoming = pd.DataFrame(
        [
            {"id": "1", "slug": "btc-updown-5m-1", "updated_at": "2026-01-01T01:00:00+00:00", "closed": True},
            {"id": "2", "slug": "btc-updown-5m-2", "updated_at": "2026-01-01T02:00:00+00:00", "closed": True},
        ]
    )

    merged = merge_market_frames(existing, incoming)

    assert len(merged) == 2
    row1 = merged[merged["id"] == "1"].iloc[0]
    assert bool(row1["closed"]) is True


def test_sync_btc5m_history_from_local_source_rebuilds_dataset(tmp_path: Path):
    source = tmp_path / "markets.parquet"
    filtered = tmp_path / "btc5m.parquet"
    output_db = tmp_path / "polymarket_backtest.db"
    frame = pd.DataFrame(
        [
            {
                "id": "m1",
                "question": "q1",
                "slug": "btc-updown-5m-1771002000",
                "condition_id": "c1",
                "token1": "y1",
                "token2": "n1",
                "answer1": "Yes",
                "answer2": "No",
                "closed": True,
                "active": False,
                "archived": False,
                "outcome_prices": "[1.0, 0.0]",
                "volume": 10.0,
                "event_id": "e1",
                "event_slug": "btc-5m",
                "event_title": "BTC 5m",
                "created_at": "2026-02-13T17:00:00+00:00",
                "end_date": "2026-02-13T17:05:00+00:00",
                "updated_at": "2026-02-13T17:06:00+00:00",
                "neg_risk": False,
            },
            {
                "id": "m2",
                "question": "q2",
                "slug": "btc-updown-15m-1771002000",
                "condition_id": "c2",
                "token1": "y2",
                "token2": "n2",
                "answer1": "Yes",
                "answer2": "No",
                "closed": True,
                "active": False,
                "archived": False,
                "outcome_prices": "[0.0, 1.0]",
                "volume": 11.0,
                "event_id": "e2",
                "event_slug": "btc-15m",
                "event_title": "BTC 15m",
                "created_at": "2026-02-13T17:00:00+00:00",
                "end_date": "2026-02-13T17:15:00+00:00",
                "updated_at": "2026-02-13T17:16:00+00:00",
                "neg_risk": False,
            },
        ]
    )
    frame.to_parquet(source, index=False)

    summary = sync_btc5m_history(
        source_file=source,
        dataset_url="https://example.invalid/markets.parquet",
        download_path=tmp_path / "downloaded.parquet",
        filtered_output=filtered,
        output_db=output_db,
        rebuild_db=True,
    )

    assert summary["incoming_btc5m_rows"] == 1
    assert summary["merged_btc5m_rows"] == 1
    assert filtered.exists()
    assert output_db.exists()
