"""
Sync BTC 5-minute Polymarket market history from the SII-WANGZJ dataset.

Reference:
- https://github.com/SII-WANGZJ/Polymarket_data
- https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data

Workflow:
1. Download or read the source `markets.parquet`
2. Filter to `btc-updown-5m-*`
3. Merge with any existing local filtered parquet
4. Optionally rebuild the local historical backtest DB
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.v3.build_polymarket_dataset import build_dataset


DEFAULT_DATASET_URL = (
    "https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main/markets.parquet?download=true"
)
DEFAULT_DOWNLOAD_PATH = ROOT / "data" / "external" / "hf_markets.parquet"
DEFAULT_FILTERED_PATH = ROOT / "data" / "external" / "hf_polymarket_btc_5m_markets.parquet"
DEFAULT_OUTPUT_DB = ROOT / "data" / "polymarket_backtest.db"


def download_file(url: str, output_path: Path, *, timeout: int = 60) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    headers = {"User-Agent": "Mozilla/5.0"}
    with requests.get(url, stream=True, timeout=timeout, headers=headers) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    tmp_path.replace(output_path)
    return output_path


def load_frame(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported file type: {path.suffix}")


def filter_btc5m_markets(frame: pd.DataFrame) -> pd.DataFrame:
    if "slug" not in frame.columns:
        raise ValueError("Source markets file must include a slug column")
    filtered = frame[frame["slug"].astype(str).str.startswith("btc-updown-5m-")].copy()
    if "end_date" in filtered.columns:
        filtered["_sort_end_date"] = pd.to_datetime(filtered["end_date"], utc=True, errors="coerce")
        filtered = filtered.sort_values(["_sort_end_date", "slug"], kind="stable")
        filtered = filtered.drop(columns=["_sort_end_date"])
    return filtered.reset_index(drop=True)


def merge_market_frames(existing: pd.DataFrame | None, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        return incoming.reset_index(drop=True)
    merged = pd.concat([existing, incoming], ignore_index=True)
    if "id" not in merged.columns:
        return merged.drop_duplicates().reset_index(drop=True)

    for column in ("updated_at", "created_at", "end_date"):
        if column in merged.columns:
            merged[f"_sort_{column}"] = pd.to_datetime(merged[column], utc=True, errors="coerce")
    sort_cols = [col for col in ("_sort_updated_at", "_sort_created_at", "_sort_end_date") if col in merged.columns]
    if sort_cols:
        merged = merged.sort_values(sort_cols, kind="stable")
    merged = merged.drop_duplicates(subset=["id"], keep="last")
    drop_cols = [col for col in merged.columns if col.startswith("_sort_")]
    if drop_cols:
        merged = merged.drop(columns=drop_cols)
    if "end_date" in merged.columns:
        merged = merged.sort_values("end_date", kind="stable")
    return merged.reset_index(drop=True)


def write_filtered_frame(frame: pd.DataFrame, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == ".parquet":
        frame.to_parquet(output_path, index=False)
    elif output_path.suffix.lower() == ".csv":
        frame.to_csv(output_path, index=False)
    else:
        raise ValueError(f"Unsupported output type: {output_path.suffix}")
    return output_path


def sync_btc5m_history(
    *,
    source_file: Path | None,
    dataset_url: str,
    download_path: Path,
    filtered_output: Path,
    output_db: Path,
    timeout: int = 60,
    rebuild_db: bool = True,
) -> dict[str, Any]:
    if source_file is not None:
        source_path = source_file
    else:
        source_path = download_file(dataset_url, download_path, timeout=timeout)

    source_frame = load_frame(source_path)
    incoming_filtered = filter_btc5m_markets(source_frame)
    existing_filtered = load_frame(filtered_output) if filtered_output.exists() else None
    merged = merge_market_frames(existing_filtered, incoming_filtered)
    write_filtered_frame(merged, filtered_output)

    db_summary = None
    if rebuild_db:
        db_summary = build_dataset(filtered_output, output_db)

    return {
        "source_path": str(source_path),
        "filtered_output": str(filtered_output),
        "source_rows": len(source_frame),
        "incoming_btc5m_rows": len(incoming_filtered),
        "merged_btc5m_rows": len(merged),
        "db_rebuilt": rebuild_db,
        "db_summary": db_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync BTC 5-minute Polymarket history from SII-WANGZJ dataset.")
    parser.add_argument("--source-file", type=Path, help="Local markets.parquet/csv source to use instead of downloading")
    parser.add_argument("--dataset-url", type=str, default=DEFAULT_DATASET_URL, help="Remote markets.parquet URL")
    parser.add_argument("--download-path", type=Path, default=DEFAULT_DOWNLOAD_PATH, help="Where to store downloaded markets.parquet")
    parser.add_argument("--filtered-output", type=Path, default=DEFAULT_FILTERED_PATH, help="Where to store merged BTC 5m markets parquet/csv")
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT_DB, help="Historical backtest sqlite DB path")
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout in seconds")
    parser.add_argument("--skip-db-rebuild", action="store_true", help="Only update parquet files; do not rebuild sqlite backtest DB")
    parser.add_argument("--copy-source-local", action="store_true", help="When --source-file is used, also copy it to --download-path")
    args = parser.parse_args()

    source_file = args.source_file
    if source_file is not None and args.copy_source_local:
        args.download_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, args.download_path)
        source_file = args.download_path

    summary = sync_btc5m_history(
        source_file=source_file,
        dataset_url=args.dataset_url,
        download_path=args.download_path,
        filtered_output=args.filtered_output,
        output_db=args.output_db,
        timeout=args.timeout,
        rebuild_db=not args.skip_db_rebuild,
    )

    print("Synced BTC 5m Polymarket history")
    for key, value in summary.items():
        if key == "db_summary" and isinstance(value, dict):
            print("- db_summary:")
            for inner_key, inner_value in value.items():
                print(f"  - {inner_key}: {inner_value}")
            continue
        print(f"- {key}: {value}")


if __name__ == "__main__":
    main()
