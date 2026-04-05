"""
Build a local BTC 5-minute Polymarket backtest dataset from exported parquet/csv.

This script is intentionally market-level only. It standardizes historical market
metadata so the next backtest step can replay baseline and rule variants against
real Polymarket market pricing instead of synthetic markets.
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_INPUT = Path("/root/Data/hf_polymarket_btc_5m_markets.parquet")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "data" / "polymarket_backtest.db"


@dataclass(frozen=True)
class ParsedMarket:
    market_id: str
    question: str
    slug: str
    condition_id: str
    token_yes: str
    token_no: str
    answer_yes: str
    answer_no: str
    closed: int
    active: int
    archived: int
    volume: float
    event_id: str
    event_slug: str
    event_title: str
    created_at: str | None
    end_date: str | None
    updated_at: str | None
    neg_risk: int
    final_price_yes: float | None
    final_price_no: float | None
    outcome: int | None
    window_start_ts: int | None
    window_start_at: str | None
    source_file: str


def _parse_prices(raw: Any) -> tuple[float | None, float | None]:
    if raw is None:
        return None, None
    if isinstance(raw, (list, tuple)):
        prices = list(raw)
    elif isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except json.JSONDecodeError:
            try:
                prices = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                return None, None
    else:
        return None, None

    if not prices:
        return None, None

    try:
        yes = float(prices[0]) if prices[0] is not None else None
        no = float(prices[1]) if len(prices) > 1 and prices[1] is not None else None
    except (TypeError, ValueError):
        return None, None

    if no is None and yes is not None:
        no = round(1.0 - yes, 6)
    return yes, no


def _infer_outcome(price_yes: float | None, price_no: float | None) -> int | None:
    if price_yes is None and price_no is None:
        return None
    if price_yes == 1.0 or price_no == 0.0:
        return 1
    if price_yes == 0.0 or price_no == 1.0:
        return 0
    return None


def _parse_window_start(slug: str) -> tuple[int | None, str | None]:
    try:
        ts = int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None, None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return ts, dt


def _normalize_timestamp(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is None:
            value = value.tz_localize("UTC")
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _row_to_market(row: dict[str, Any], source_file: str) -> ParsedMarket:
    price_yes, price_no = _parse_prices(row.get("outcome_prices"))
    window_start_ts, window_start_at = _parse_window_start(str(row.get("slug", "")))
    return ParsedMarket(
        market_id=str(row.get("id", "")),
        question=str(row.get("question", "")),
        slug=str(row.get("slug", "")),
        condition_id=str(row.get("condition_id", "")),
        token_yes=str(row.get("token1", "")),
        token_no=str(row.get("token2", "")),
        answer_yes=str(row.get("answer1", "")),
        answer_no=str(row.get("answer2", "")),
        closed=int(bool(row.get("closed", 0))),
        active=int(bool(row.get("active", 0))),
        archived=int(bool(row.get("archived", 0))),
        volume=float(row.get("volume", 0.0) or 0.0),
        event_id=str(row.get("event_id", "")),
        event_slug=str(row.get("event_slug", "")),
        event_title=str(row.get("event_title", "")),
        created_at=_normalize_timestamp(row.get("created_at")),
        end_date=_normalize_timestamp(row.get("end_date")),
        updated_at=_normalize_timestamp(row.get("updated_at")),
        neg_risk=int(bool(row.get("neg_risk", 0))),
        final_price_yes=price_yes,
        final_price_no=price_no,
        outcome=_infer_outcome(price_yes, price_no),
        window_start_ts=window_start_ts,
        window_start_at=window_start_at,
        source_file=source_file,
    )


def load_markets(path: Path) -> list[ParsedMarket]:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported input format: {path.suffix}")

    records = frame.to_dict(orient="records")
    markets = [_row_to_market(record, str(path)) for record in records]
    markets = [m for m in markets if m.slug.startswith("btc-updown-5m-")]
    markets.sort(key=lambda item: (item.end_date or "", item.market_id))
    return markets


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_markets (
            market_id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            slug TEXT NOT NULL,
            condition_id TEXT,
            token_yes TEXT,
            token_no TEXT,
            answer_yes TEXT,
            answer_no TEXT,
            closed INTEGER NOT NULL,
            active INTEGER NOT NULL,
            archived INTEGER NOT NULL,
            volume REAL NOT NULL,
            event_id TEXT,
            event_slug TEXT,
            event_title TEXT,
            created_at TEXT,
            end_date TEXT,
            updated_at TEXT,
            neg_risk INTEGER NOT NULL,
            final_price_yes REAL,
            final_price_no REAL,
            outcome INTEGER,
            window_start_ts INTEGER,
            window_start_at TEXT,
            source_file TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_historical_markets_end_date
        ON historical_markets(end_date)
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_historical_markets_window_start
        ON historical_markets(window_start_ts)
        """
    )
    db.commit()
    return db


def store_markets(db: sqlite3.Connection, markets: list[ParsedMarket]) -> None:
    db.executemany(
        """
        INSERT INTO historical_markets (
            market_id, question, slug, condition_id, token_yes, token_no,
            answer_yes, answer_no, closed, active, archived, volume,
            event_id, event_slug, event_title, created_at, end_date, updated_at,
            neg_risk, final_price_yes, final_price_no, outcome, window_start_ts,
            window_start_at, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            question = excluded.question,
            slug = excluded.slug,
            condition_id = excluded.condition_id,
            token_yes = excluded.token_yes,
            token_no = excluded.token_no,
            answer_yes = excluded.answer_yes,
            answer_no = excluded.answer_no,
            closed = excluded.closed,
            active = excluded.active,
            archived = excluded.archived,
            volume = excluded.volume,
            event_id = excluded.event_id,
            event_slug = excluded.event_slug,
            event_title = excluded.event_title,
            created_at = excluded.created_at,
            end_date = excluded.end_date,
            updated_at = excluded.updated_at,
            neg_risk = excluded.neg_risk,
            final_price_yes = excluded.final_price_yes,
            final_price_no = excluded.final_price_no,
            outcome = excluded.outcome,
            window_start_ts = excluded.window_start_ts,
            window_start_at = excluded.window_start_at,
            source_file = excluded.source_file
        """,
        [
            (
                m.market_id,
                m.question,
                m.slug,
                m.condition_id,
                m.token_yes,
                m.token_no,
                m.answer_yes,
                m.answer_no,
                m.closed,
                m.active,
                m.archived,
                m.volume,
                m.event_id,
                m.event_slug,
                m.event_title,
                m.created_at,
                m.end_date,
                m.updated_at,
                m.neg_risk,
                m.final_price_yes,
                m.final_price_no,
                m.outcome,
                m.window_start_ts,
                m.window_start_at,
                m.source_file,
            )
            for m in markets
        ],
    )
    db.commit()


def summarize(markets: list[ParsedMarket]) -> dict[str, Any]:
    resolved = [m for m in markets if m.outcome is not None]
    unresolved = len(markets) - len(resolved)
    return {
        "markets": len(markets),
        "resolved_markets": len(resolved),
        "unresolved_markets": unresolved,
        "first_end_date": markets[0].end_date if markets else None,
        "last_end_date": markets[-1].end_date if markets else None,
        "source_file": markets[0].source_file if markets else None,
    }


def build_dataset(input_path: Path, output_path: Path) -> dict[str, Any]:
    markets = load_markets(input_path)
    db = init_db(output_path)
    try:
        store_markets(db, markets)
    finally:
        db.close()
    summary = summarize(markets)
    summary["output_db"] = str(output_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build BTC 5-minute Polymarket backtest DB from parquet/csv.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input parquet/csv path")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output sqlite DB path")
    args = parser.parse_args()

    summary = build_dataset(args.input, args.output)
    print("Built Polymarket backtest dataset")
    for key, value in summary.items():
        print(f"- {key}: {value}")


if __name__ == "__main__":
    main()
