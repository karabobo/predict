"""
Replay deterministic baseline variants on historical Polymarket BTC 5-minute markets.

Requires:
- a market-level historical SQLite DB built by build_polymarket_dataset.py
- a BTC 5-minute OHLCV file (parquet/csv) on local disk
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.metrics import bet_size_for_conviction
from src.strategies.regime import compute_regime_from_candles
from src.v3.rule_variants import available_rules


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "polymarket_backtest.db"


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def load_candles(path: Path) -> list[Candle]:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported BTC candle file: {path.suffix}")

    cols = {c.lower(): c for c in frame.columns}
    ts_col = next((cols[c] for c in ("ts", "timestamp", "time", "datetime") if c in cols), None)
    if ts_col is None:
        raise ValueError("BTC candle file must include ts/timestamp/time column")

    def to_ts(value: Any) -> int:
        if isinstance(value, pd.Timestamp):
            if value.tzinfo is None:
                value = value.tz_localize("UTC")
            return int(value.timestamp())
        text = str(value)
        try:
            return int(float(text))
        except ValueError:
            parsed = pd.Timestamp(text)
            if parsed.tzinfo is None:
                parsed = parsed.tz_localize("UTC")
            return int(parsed.timestamp())

    candles: list[Candle] = []
    for record in frame.to_dict(orient="records"):
        candles.append(
            Candle(
                ts=to_ts(record[ts_col]),
                open=float(record[cols.get("open", "open")]),
                high=float(record[cols.get("high", "high")]),
                low=float(record[cols.get("low", "low")]),
                close=float(record[cols.get("close", "close")]),
                volume=float(record.get(cols.get("volume", "volume"), 0.0) or 0.0),
            )
        )
    candles.sort(key=lambda c: c.ts)
    return candles


def init_backtest_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL,
            btc_candles_file TEXT NOT NULL,
            entry_price_source TEXT NOT NULL,
            lookback INTEGER NOT NULL,
            markets INTEGER NOT NULL,
            eligible_markets INTEGER NOT NULL,
            trades INTEGER NOT NULL,
            trade_wins INTEGER NOT NULL,
            signal_wins INTEGER NOT NULL,
            signal_calls INTEGER NOT NULL,
            trade_pnl REAL NOT NULL,
            trade_wagered REAL NOT NULL,
            trade_roi REAL NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS backtest_trades (
            run_id INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            end_date TEXT,
            rule_name TEXT NOT NULL,
            regime TEXT NOT NULL,
            estimate REAL NOT NULL,
            should_trade INTEGER NOT NULL,
            conviction_score INTEGER NOT NULL,
            reason TEXT NOT NULL,
            outcome INTEGER,
            entry_price_yes REAL,
            direction TEXT NOT NULL,
            wager REAL NOT NULL,
            pnl REAL NOT NULL,
            won INTEGER NOT NULL,
            FOREIGN KEY(run_id) REFERENCES backtest_runs(run_id)
        )
        """
    )
    db.commit()


def select_markets(
    db: sqlite3.Connection,
    *,
    from_date: str | None,
    to_date: str | None,
) -> list[sqlite3.Row]:
    clauses = ["1=1"]
    params: list[Any] = []
    if from_date:
        clauses.append("end_date >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("end_date <= ?")
        params.append(to_date)
    query = f"""
        SELECT *
        FROM historical_markets
        WHERE {' AND '.join(clauses)}
        ORDER BY end_date ASC, market_id ASC
    """
    return db.execute(query, params).fetchall()


def choose_entry_price(
    market: sqlite3.Row,
    context: list[dict[str, Any]],
    source: str,
    estimate: float,
) -> float:
    if source == "market_final_yes":
        value = market["final_price_yes"]
        if value is None:
            return 0.5
        return min(max(float(value), 0.01), 0.99)
    if source == "neutral_50":
        return 0.5
    if source == "recent_up_share":
        recent = context[-12:]
        if not recent:
            return 0.5
        ups = sum(1 for candle in recent if candle["close"] >= candle["open"])
        implied = ups / len(recent)
        return min(max(implied, 0.05), 0.95)
    if source == "model_edge_5":
        if estimate >= 0.5:
            return min(max(estimate - 0.05, 0.05), 0.95)
        return min(max(estimate + 0.05, 0.05), 0.95)
    if source == "model_edge_8":
        if estimate >= 0.5:
            return min(max(estimate - 0.08, 0.05), 0.95)
        return min(max(estimate + 0.08, 0.05), 0.95)
    raise ValueError(f"Unsupported entry price source: {source}")


def build_contexts(candles: list[Candle], lookback: int) -> tuple[dict[int, dict[str, Any]], Counter]:
    by_start: dict[int, dict[str, Any]] = {}
    stats = Counter()
    for idx in range(lookback, len(candles)):
        target = candles[idx]
        context = candles[idx - lookback:idx]
        by_start[target.ts] = {
            "context_candles": [
                {
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in context
            ],
            "target_candle": {
                "ts": target.ts,
                "open": target.open,
                "high": target.high,
                "low": target.low,
                "close": target.close,
                "volume": target.volume,
            },
        }
        stats["contexts"] += 1
    return by_start, stats


def run_backtest(
    *,
    db_path: Path,
    btc_candles_path: Path,
    rule_name: str,
    lookback: int,
    entry_price_source: str,
    from_date: str | None,
    to_date: str | None,
) -> dict[str, Any]:
    rule_map = available_rules()
    if rule_name not in rule_map:
        raise ValueError(f"Unknown rule: {rule_name}. Available: {', '.join(sorted(rule_map))}")

    candles = load_candles(btc_candles_path)
    contexts_by_start, build_stats = build_contexts(candles, lookback)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    init_backtest_tables(db)
    markets = select_markets(db, from_date=from_date, to_date=to_date)

    trades: list[dict[str, Any]] = []
    signal_calls = 0
    signal_wins = 0
    eligible_markets = 0
    skipped_missing_context = 0

    for market in markets:
        start_ts = market["window_start_ts"]
        outcome = market["outcome"]
        if start_ts is None or outcome is None:
            skipped_missing_context += 1
            continue
        context_pack = contexts_by_start.get(int(start_ts))
        if context_pack is None:
            skipped_missing_context += 1
            continue

        context_candles = context_pack["context_candles"]
        regime = compute_regime_from_candles(context_candles)
        decision = rule_map[rule_name](context_candles, regime)
        estimate = float(decision.get("estimate", 0.5))
        should_trade = bool(decision.get("should_trade", False))
        conviction = int(decision.get("conviction_score", 0))
        reason = str(decision.get("reason", ""))
        eligible_markets += 1

        if abs(estimate - 0.5) > 1e-9:
            signal_calls += 1
            if (estimate > 0.5 and int(outcome) == 1) or (estimate < 0.5 and int(outcome) == 0):
                signal_wins += 1

        wager = bet_size_for_conviction(conviction)
        if not should_trade or wager <= 0:
            trades.append(
                {
                    "market_id": str(market["market_id"]),
                    "question": str(market["question"]),
                    "end_date": market["end_date"],
                    "rule_name": rule_name,
                    "regime": regime["label"],
                    "estimate": estimate,
                    "should_trade": 0,
                    "conviction_score": conviction,
                    "reason": reason,
                    "outcome": int(outcome),
                    "entry_price_yes": None,
                    "direction": "SKIP",
                    "wager": 0.0,
                    "pnl": 0.0,
                    "won": 0,
                }
            )
            continue

        entry_price_yes = choose_entry_price(market, context_candles, entry_price_source, estimate)
        direction = "UP" if estimate > 0.5 else "DOWN"
        entry_price = entry_price_yes if direction == "UP" else min(max(1.0 - entry_price_yes, 0.01), 0.99)
        won = (direction == "UP" and int(outcome) == 1) or (direction == "DOWN" and int(outcome) == 0)
        pnl = wager * (1.0 / entry_price - 1.0) if won else -wager
        trades.append(
            {
                "market_id": str(market["market_id"]),
                "question": str(market["question"]),
                "end_date": market["end_date"],
                "rule_name": rule_name,
                "regime": regime["label"],
                "estimate": estimate,
                "should_trade": 1,
                "conviction_score": conviction,
                "reason": reason,
                "outcome": int(outcome),
                "entry_price_yes": entry_price_yes,
                "direction": direction,
                "wager": wager,
                "pnl": pnl,
                "won": 1 if won else 0,
            }
        )

    trade_rows = [row for row in trades if row["should_trade"] == 1]
    trade_wagered = sum(row["wager"] for row in trade_rows)
    trade_pnl = sum(row["pnl"] for row in trade_rows)
    trade_wins = sum(row["won"] for row in trade_rows)
    trade_roi = (trade_pnl / trade_wagered * 100.0) if trade_wagered > 0 else 0.0

    notes = (
        f"entry_price_source={entry_price_source}; "
        f"skipped_missing_context={skipped_missing_context}; "
        f"contexts_built={build_stats['contexts']}"
    )

    cursor = db.execute(
        """
        INSERT INTO backtest_runs (
            rule_name, btc_candles_file, entry_price_source, lookback, markets,
            eligible_markets, trades, trade_wins, signal_wins, signal_calls,
            trade_pnl, trade_wagered, trade_roi, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rule_name,
            str(btc_candles_path),
            entry_price_source,
            lookback,
            len(markets),
            eligible_markets,
            len(trade_rows),
            trade_wins,
            signal_wins,
            signal_calls,
            trade_pnl,
            trade_wagered,
            trade_roi,
            notes,
        ),
    )
    run_id = cursor.lastrowid
    db.executemany(
        """
        INSERT INTO backtest_trades (
            run_id, market_id, question, end_date, rule_name, regime, estimate,
            should_trade, conviction_score, reason, outcome, entry_price_yes,
            direction, wager, pnl, won
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                row["market_id"],
                row["question"],
                row["end_date"],
                row["rule_name"],
                row["regime"],
                row["estimate"],
                row["should_trade"],
                row["conviction_score"],
                row["reason"],
                row["outcome"],
                row["entry_price_yes"],
                row["direction"],
                row["wager"],
                row["pnl"],
                row["won"],
            )
            for row in trades
        ],
    )
    db.commit()
    db.close()

    regime_counter = defaultdict(lambda: {"markets": 0, "trades": 0, "wins": 0, "pnl": 0.0})
    for row in trades:
        bucket = regime_counter[row["regime"]]
        bucket["markets"] += 1
        if row["should_trade"]:
            bucket["trades"] += 1
            bucket["wins"] += row["won"]
            bucket["pnl"] += row["pnl"]

    regime_summary = {
        regime: {
            "markets": stats["markets"],
            "trades": stats["trades"],
            "wins": stats["wins"],
            "trade_wr": round(stats["wins"] / stats["trades"] * 100, 2) if stats["trades"] else 0.0,
            "pnl": round(stats["pnl"], 2),
        }
        for regime, stats in sorted(regime_counter.items())
    }

    return {
        "run_id": run_id,
        "rule_name": rule_name,
        "markets": len(markets),
        "eligible_markets": eligible_markets,
        "signal_calls": signal_calls,
        "signal_wins": signal_wins,
        "signal_wr": round(signal_wins / signal_calls * 100, 2) if signal_calls else 0.0,
        "trades": len(trade_rows),
        "trade_wins": trade_wins,
        "trade_wr": round(trade_wins / len(trade_rows) * 100, 2) if trade_rows else 0.0,
        "trade_pnl": round(trade_pnl, 2),
        "trade_wagered": round(trade_wagered, 2),
        "trade_roi": round(trade_roi, 2),
        "entry_price_source": entry_price_source,
        "notes": notes,
        "regimes": regime_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest deterministic rules on historical Polymarket BTC 5-minute markets.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Historical Polymarket market DB")
    parser.add_argument("--btc-candles", type=Path, required=True, help="BTC 5-minute candles parquet/csv")
    parser.add_argument("--rule", type=str, default="baseline_current", choices=sorted(available_rules()), help="Rule variant")
    parser.add_argument("--lookback", type=int, default=20, help="Number of prior candles for context")
    parser.add_argument(
        "--entry-price-source",
        type=str,
        default="neutral_50",
        choices=["neutral_50", "recent_up_share", "market_final_yes", "model_edge_5", "model_edge_8"],
        help="How to approximate entry price from market data",
    )
    parser.add_argument("--from", dest="from_date", type=str, default=None, help="Inclusive end_date lower bound")
    parser.add_argument("--to", dest="to_date", type=str, default=None, help="Inclusive end_date upper bound")
    args = parser.parse_args()

    result = run_backtest(
        db_path=args.db,
        btc_candles_path=args.btc_candles,
        rule_name=args.rule,
        lookback=args.lookback,
        entry_price_source=args.entry_price_source,
        from_date=args.from_date,
        to_date=args.to_date,
    )
    print("Backtest complete")
    for key, value in result.items():
        if key == "regimes":
            print("- regimes:")
            for regime, stats in value.items():
                print(f"  - {regime}: {stats}")
        else:
            print(f"- {key}: {value}")


if __name__ == "__main__":
    main()
