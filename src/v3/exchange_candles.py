from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_US_KLINES = "https://api.binance.us/api/v3/klines"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def parse_utc(value: str) -> datetime:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.to_pydatetime().astimezone(timezone.utc)


def fetch_binance_5m(
    *,
    start: datetime,
    end: datetime,
    base_url: str = BINANCE_KLINES,
    symbol: str = "BTCUSDT",
    sleep_seconds: float = 0.1,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while start_ms < end_ms:
        response = requests.get(
            base_url,
            params={
                "symbol": symbol,
                "interval": "5m",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            break
        for item in payload:
            rows.append(
                {
                    "timestamp": int(item[0] // 1000),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
            )
        next_ms = int(payload[-1][0]) + 5 * 60 * 1000
        if next_ms <= start_ms:
            break
        start_ms = next_ms
        time.sleep(sleep_seconds)
    return normalize_candles(pd.DataFrame(rows))


def fetch_coinbase_5m(
    *,
    start: datetime,
    end: datetime,
    sleep_seconds: float = 0.15,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    cursor = start
    step = pd.Timedelta(minutes=5 * 300)
    while cursor < end:
        chunk_end = min(cursor + step, end)
        response = requests.get(
            COINBASE_CANDLES,
            params={
                "granularity": 300,
                "start": cursor.isoformat().replace("+00:00", "Z"),
                "end": chunk_end.isoformat().replace("+00:00", "Z"),
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload:
            rows.append(
                {
                    "timestamp": int(item[0]),
                    "low": float(item[1]),
                    "high": float(item[2]),
                    "open": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
            )
        cursor = chunk_end
        time.sleep(sleep_seconds)
    return normalize_candles(pd.DataFrame(rows))


def normalize_candles(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    output = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    output["timestamp"] = output["timestamp"].astype("int64")
    for column in ("open", "high", "low", "close", "volume"):
        output[column] = output[column].astype(float)
    output = output.drop_duplicates(subset=["timestamp"], keep="last")
    output = output.sort_values("timestamp").reset_index(drop=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Download BTC 5m candles from exchange APIs.")
    parser.add_argument("--provider", choices=["binance", "binance_us", "coinbase"], required=True)
    parser.add_argument("--start", required=True, help="UTC start timestamp, e.g. 2025-12-18T00:00:00Z")
    parser.add_argument("--end", required=True, help="UTC end timestamp, e.g. 2026-03-20T00:00:00Z")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sleep", type=float, default=0.1)
    args = parser.parse_args()

    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if args.provider == "binance":
        frame = fetch_binance_5m(start=start, end=end, base_url=BINANCE_KLINES, sleep_seconds=args.sleep)
    elif args.provider == "binance_us":
        frame = fetch_binance_5m(start=start, end=end, base_url=BINANCE_US_KLINES, sleep_seconds=args.sleep)
    else:
        frame = fetch_coinbase_5m(start=start, end=end, sleep_seconds=args.sleep)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(args.output, index=False)
    print(
        {
            "provider": args.provider,
            "output": str(args.output),
            "rows": len(frame),
            "first_ts": int(frame["timestamp"].min()) if not frame.empty else None,
            "last_ts": int(frame["timestamp"].max()) if not frame.empty else None,
        }
    )


if __name__ == "__main__":
    main()
