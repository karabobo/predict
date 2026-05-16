"""
L2 replay primitives for Polymarket BTC up/down markets.

The first use is validation: replay historical order-book data to check whether
shortlisted signals could have been filled at realistic prices before investing
in realtime or paper infrastructure.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import pandas as pd


BookSide = Literal["BUY", "SELL"]
OutcomeSide = Literal["YES", "NO"]


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class BookMetrics:
    best_bid: float | None
    best_ask: float | None
    midpoint: float | None
    spread: float | None
    spread_pct: float | None
    bid_depth_5pct: float
    ask_depth_5pct: float
    depth_imbalance: float
    book_hash: str


@dataclass(frozen=True)
class SimulatedFill:
    requested_usdc: float
    spent_usdc: float
    shares: float
    average_price: float | None
    filled_ratio: float
    worst_price: float | None
    levels_consumed: int
    outcome: OutcomeSide = "YES"


def _clean_price(value: Any) -> float:
    return round(float(value), 6)


def _clean_size(value: Any) -> float:
    return max(0.0, float(value or 0.0))


class BookState:
    """Mutable order-book state for a single outcome token."""

    def __init__(self, *, bids: dict[float, float] | None = None, asks: dict[float, float] | None = None):
        self.bids: dict[float, float] = dict(bids or {})
        self.asks: dict[float, float] = dict(asks or {})
        self._drop_empty()

    @classmethod
    def from_snapshot(
        cls,
        *,
        bid_prices: Iterable[Any] | None,
        bid_sizes: Iterable[Any] | None,
        ask_prices: Iterable[Any] | None,
        ask_sizes: Iterable[Any] | None,
    ) -> "BookState":
        return cls(
            bids=_levels_to_map(bid_prices, bid_sizes),
            asks=_levels_to_map(ask_prices, ask_sizes),
        )

    def copy(self) -> "BookState":
        return BookState(bids=self.bids, asks=self.asks)

    def apply_snapshot(
        self,
        *,
        bid_prices: Iterable[Any] | None,
        bid_sizes: Iterable[Any] | None,
        ask_prices: Iterable[Any] | None,
        ask_sizes: Iterable[Any] | None,
    ) -> None:
        self.bids = _levels_to_map(bid_prices, bid_sizes)
        self.asks = _levels_to_map(ask_prices, ask_sizes)
        self._drop_empty()

    def apply_price_change(self, *, side: BookSide | str | None, price: Any, size: Any) -> None:
        """Apply a single Polymarket price_change row.

        Polymarket CLOB side is expressed from the resting-order perspective:
        BUY updates bids, SELL updates asks.
        """
        if side is None or price is None:
            return
        normalized = str(side).upper()
        if normalized not in {"BUY", "SELL"}:
            return
        target = self.bids if normalized == "BUY" else self.asks
        px = _clean_price(price)
        sz = _clean_size(size)
        if sz <= 0:
            target.pop(px, None)
        else:
            target[px] = sz
        self._drop_empty()

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def metrics(self) -> BookMetrics:
        best_bid = self.best_bid()
        best_ask = self.best_ask()
        midpoint = round((best_bid + best_ask) / 2.0, 6) if best_bid is not None and best_ask is not None else None
        spread = round(best_ask - best_bid, 6) if best_bid is not None and best_ask is not None else None
        spread_pct = spread / midpoint if midpoint and midpoint > 0 else None
        bid_depth = self.depth_within_pct("BUY", 0.05)
        ask_depth = self.depth_within_pct("SELL", 0.05)
        total_depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0
        return BookMetrics(
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            spread_pct=spread_pct,
            bid_depth_5pct=bid_depth,
            ask_depth_5pct=ask_depth,
            depth_imbalance=imbalance,
            book_hash=self.book_hash(),
        )

    def depth_within_pct(self, side: BookSide, pct: float) -> float:
        if side == "BUY":
            best = self.best_bid()
            if best is None:
                return 0.0
            floor = best * (1.0 - pct)
            return sum(size for price, size in self.bids.items() if price >= floor)
        best = self.best_ask()
        if best is None:
            return 0.0
        ceiling = best * (1.0 + pct)
        return sum(size for price, size in self.asks.items() if price <= ceiling)

    def simulate_market_buy(self, amount_usdc: float) -> SimulatedFill:
        """Buy outcome shares against current asks without mutating the book."""
        return self.simulate_market_buy_outcome("YES", amount_usdc)

    def simulate_market_buy_outcome(self, outcome: OutcomeSide | str, amount_usdc: float) -> SimulatedFill:
        """Buy YES or approximate NO shares from the YES order book.

        The replay files expose one side of the binary market. YES buys consume
        YES asks. NO buys are approximated from YES bids because buying NO at
        price q is equivalent to taking a resting YES bid at 1-q.
        """
        amount_left = max(0.0, float(amount_usdc))
        spent = 0.0
        shares = 0.0
        levels = 0
        worst_price: float | None = None
        normalized_outcome = str(outcome).upper()
        if normalized_outcome in {"UP", "YES"}:
            levels_to_consume = sorted(self.asks.items())
            fill_outcome: OutcomeSide = "YES"
        elif normalized_outcome in {"DOWN", "NO"}:
            levels_to_consume = sorted(((round(1.0 - price, 6), size) for price, size in self.bids.items()))
            fill_outcome = "NO"
        else:
            raise ValueError("outcome must be YES/UP or NO/DOWN")
        for price, available_shares in levels_to_consume:
            if amount_left <= 1e-12:
                break
            if price <= 0:
                continue
            max_cost = available_shares * price
            cost = min(amount_left, max_cost)
            if cost <= 0:
                continue
            fill_shares = cost / price
            spent += cost
            shares += fill_shares
            amount_left -= cost
            levels += 1
            worst_price = price
        average_price = spent / shares if shares > 0 else None
        return SimulatedFill(
            requested_usdc=float(amount_usdc),
            spent_usdc=spent,
            shares=shares,
            average_price=average_price,
            filled_ratio=spent / float(amount_usdc) if amount_usdc > 0 else 0.0,
            worst_price=worst_price,
            levels_consumed=levels,
            outcome=fill_outcome,
        )

    def book_hash(self) -> str:
        bid_bits = ",".join(f"{price:.4f}:{size:.4f}" for price, size in sorted(self.bids.items(), reverse=True)[:10])
        ask_bits = ",".join(f"{price:.4f}:{size:.4f}" for price, size in sorted(self.asks.items())[:10])
        return f"b={bid_bits}|a={ask_bits}"

    def _drop_empty(self) -> None:
        self.bids = {price: size for price, size in self.bids.items() if size > 0}
        self.asks = {price: size for price, size in self.asks.items() if size > 0}


def _levels_to_map(prices: Iterable[Any] | None, sizes: Iterable[Any] | None) -> dict[float, float]:
    if prices is None or sizes is None:
        return {}
    levels: dict[float, float] = {}
    for raw_price, raw_size in zip(prices, sizes):
        price = _clean_price(raw_price)
        size = _clean_size(raw_size)
        if size > 0:
            levels[price] = size
    return levels


def read_replay_parquet(path_or_url: str | Path) -> pd.DataFrame:
    options = {"User-Agent": "Mozilla/5.0"} if str(path_or_url).startswith("http") else None
    return pd.read_parquet(path_or_url, storage_options=options)


def initialize_book_from_snapshots(snapshot_frame: pd.DataFrame) -> BookState:
    if snapshot_frame.empty:
        return BookState()
    first = snapshot_frame.sort_values("timestamp").iloc[0]
    return BookState.from_snapshot(
        bid_prices=first.get("bid_prices"),
        bid_sizes=first.get("bid_sizes"),
        ask_prices=first.get("ask_prices"),
        ask_sizes=first.get("ask_sizes"),
    )


def book_at_timestamp(
    *,
    l2_frame: pd.DataFrame,
    timestamp: Any,
    snapshot_frame: pd.DataFrame | None = None,
) -> BookState:
    """Build book state at a timestamp from snapshots plus L2 deltas."""
    ts = pd.Timestamp(timestamp)
    base = BookState()
    replay_start: pd.Timestamp | None = None
    if snapshot_frame is not None and not snapshot_frame.empty:
        snapshots = snapshot_frame.sort_values("timestamp")
        eligible = snapshots[snapshots["timestamp"] <= ts]
        if not eligible.empty:
            row = eligible.iloc[-1]
            base.apply_snapshot(
                bid_prices=row.get("bid_prices"),
                bid_sizes=row.get("bid_sizes"),
                ask_prices=row.get("ask_prices"),
                ask_sizes=row.get("ask_sizes"),
            )
            replay_start = pd.Timestamp(row["timestamp"])
        else:
            row = snapshots.iloc[0]
            base.apply_snapshot(
                bid_prices=row.get("bid_prices"),
                bid_sizes=row.get("bid_sizes"),
                ask_prices=row.get("ask_prices"),
                ask_sizes=row.get("ask_sizes"),
            )

    if l2_frame.empty:
        return base
    events = l2_frame[l2_frame["timestamp"] <= ts]
    if replay_start is not None:
        events = events[events["timestamp"] > replay_start]
    for _, row in events.sort_values("timestamp").iterrows():
        event_type = str(row.get("event_type") or "")
        if event_type == "book":
            base.apply_snapshot(
                bid_prices=row.get("bid_prices"),
                bid_sizes=row.get("bid_sizes"),
                ask_prices=row.get("ask_prices"),
                ask_sizes=row.get("ask_sizes"),
            )
        elif event_type == "price_change":
            base.apply_price_change(side=row.get("pc_side"), price=row.get("pc_price"), size=row.get("pc_size"))
    return base


def replay_l2_events(frame: pd.DataFrame, *, initial_book: BookState | None = None) -> list[dict[str, Any]]:
    """Replay a small L2 frame and return metrics after each book-changing event.

    This is deliberately simple and deterministic; large-scale replay can stream
    chunks later while using the same BookState methods.
    """
    book = initial_book.copy() if initial_book else BookState()
    rows: list[dict[str, Any]] = []
    if frame.empty:
        return rows
    for _, row in frame.sort_values("timestamp").iterrows():
        event_type = str(row.get("event_type") or "")
        if event_type == "book":
            book.apply_snapshot(
                bid_prices=row.get("bid_prices"),
                bid_sizes=row.get("bid_sizes"),
                ask_prices=row.get("ask_prices"),
                ask_sizes=row.get("ask_sizes"),
            )
        elif event_type == "price_change":
            book.apply_price_change(side=row.get("pc_side"), price=row.get("pc_price"), size=row.get("pc_size"))
        else:
            continue
        metrics = book.metrics()
        rows.append(
            {
                "timestamp": row.get("timestamp"),
                "event_type": event_type,
                **asdict(metrics),
            }
        )
    return rows


def summarize_replay_inputs(
    *,
    l2_path: str | Path,
    snapshot_path: str | Path | None = None,
    trades_path: str | Path | None = None,
    max_l2_rows: int | None = None,
) -> dict[str, Any]:
    l2 = read_replay_parquet(l2_path)
    if max_l2_rows:
        l2 = l2.head(max_l2_rows)
    snapshot = read_replay_parquet(snapshot_path) if snapshot_path else pd.DataFrame()
    trades = read_replay_parquet(trades_path) if trades_path else pd.DataFrame()
    initial_book = initialize_book_from_snapshots(snapshot) if not snapshot.empty else None
    replay_rows = replay_l2_events(l2, initial_book=initial_book)
    final_metrics = replay_rows[-1] if replay_rows else {}
    return {
        "l2_rows": int(len(l2)),
        "snapshot_rows": int(len(snapshot)),
        "trade_rows": int(len(trades)),
        "book_metric_rows": int(len(replay_rows)),
        "first_timestamp": str(l2["timestamp"].min()) if "timestamp" in l2 and not l2.empty else None,
        "last_timestamp": str(l2["timestamp"].max()) if "timestamp" in l2 and not l2.empty else None,
        "final_metrics": final_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and replay Polymarket L2 parquet data.")
    parser.add_argument("--l2", required=True, help="L2 parquet path or URL")
    parser.add_argument("--snapshot", default=None, help="1s snapshot parquet path or URL")
    parser.add_argument("--trades", default=None, help="Polygon trades parquet path or URL")
    parser.add_argument("--max-l2-rows", type=int, default=5000)
    args = parser.parse_args()
    summary = summarize_replay_inputs(
        l2_path=args.l2,
        snapshot_path=args.snapshot,
        trades_path=args.trades,
        max_l2_rows=args.max_l2_rows,
    )
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
