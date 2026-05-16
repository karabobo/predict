"""
v8_realtime_daemon.py - WSS-driven paper runtime for the v8 candidate profile.

The daemon keeps websocket market data in memory and invokes the deterministic
paper runtime around the 5s/15s entry windows.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from btc_data import fetch_btc_candles, fetch_btc_spot_price
from fetch_markets import DB_PATH, fetch_active_markets, init_db, store_markets
from live_prior import DEFAULT_PRIOR_ARTIFACT, load_prior_artifact
from paper_trading import load_paper_trading_config
from realtime_paper_runtime import run_v8_realtime_paper_once
from realtime_signal import select_realtime_market
from realtime_wss import BtcTickerStore, CoinbaseTickerWssClient, LiveBookStore, PolymarketMarketWssClient


@dataclass(frozen=True)
class DaemonConfig:
    loop_sleep_seconds: float = 1.0
    market_refresh_seconds: float = 20.0
    btc_context_refresh_seconds: float = 20.0
    once: bool = False
    use_wss: bool = True
    refresh_markets: bool = True
    prior_artifact: Path = DEFAULT_PRIOR_ARTIFACT


class V8RealtimeDaemon:
    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        config: DaemonConfig,
        book_store: LiveBookStore | None = None,
        ticker_store: BtcTickerStore | None = None,
        polymarket_client_factory: Callable[..., Any] = PolymarketMarketWssClient,
        coinbase_client_factory: Callable[..., Any] = CoinbaseTickerWssClient,
    ) -> None:
        self.db = db
        self.config = config
        self.book_store = book_store or LiveBookStore()
        self.ticker_store = ticker_store or BtcTickerStore()
        self.polymarket_client_factory = polymarket_client_factory
        self.coinbase_client_factory = coinbase_client_factory
        self._market_client = None
        self._ticker_client = None
        self._subscribed_market_id: str | None = None
        self._last_market_refresh = 0.0
        self._last_btc_context_refresh = 0.0
        self._btc_context: dict[str, Any] | None = None
        self._cycle = 0
        self._prior_model, self._prior_metadata = load_prior_artifact(config.prior_artifact)

    def start(self) -> None:
        if self.config.use_wss:
            self._start_ticker_wss()
        while True:
            result = self.step()
            print(self._format_result(result))
            if self.config.once:
                return
            time.sleep(max(self.config.loop_sleep_seconds, 0.1))

    def stop(self) -> None:
        if self._market_client is not None:
            self._market_client.stop()
        if self._ticker_client is not None:
            self._ticker_client.stop()

    def step(self, *, now: datetime | None = None) -> dict[str, Any]:
        self._cycle += 1
        current = now or datetime.now(timezone.utc)
        self._refresh_markets_if_needed()
        market = select_realtime_market(self.db, now=current)
        if market is None:
            return {"status": "no_market", "cycle": self._cycle, "prior_loaded": self._prior_model is not None}
        self._ensure_market_wss(market)
        btc_context = self._btc_context_if_needed()
        if not btc_context or not btc_context.get("candles"):
            return {
                "status": "no_btc_context",
                "cycle": self._cycle,
                "market_id": market["id"],
                "prior_loaded": self._prior_model is not None,
            }
        ticker = self._ticker_payload(current=current)
        result = run_v8_realtime_paper_once(
            self.db,
            book_store=self.book_store,
            btc_ticker=ticker,
            btc_context=btc_context,
            prior_model=self._prior_model,
            now=current,
            config=load_paper_trading_config(),
            refresh_markets=False,
            cycle=self._cycle,
        )
        result["cycle"] = self._cycle
        result["market_id"] = result.get("market_id", market["id"])
        result["ticker_source"] = ticker.get("source")
        result["prior_loaded"] = self._prior_model is not None
        return result

    def _refresh_markets_if_needed(self) -> None:
        if not self.config.refresh_markets:
            return
        now = time.monotonic()
        if now - self._last_market_refresh < self.config.market_refresh_seconds:
            return
        store_markets(self.db, fetch_active_markets())
        self._last_market_refresh = now

    def _btc_context_if_needed(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._btc_context is None or now - self._last_btc_context_refresh >= self.config.btc_context_refresh_seconds:
            self._btc_context = fetch_btc_candles(limit=20)
            self._last_btc_context_refresh = now
        return self._btc_context

    def _ticker_payload(self, *, current: datetime | None = None) -> dict[str, Any]:
        ticker = self.ticker_store.latest()
        if ticker is not None:
            now = current or datetime.now(timezone.utc)
            age_seconds = max((now - ticker.observed_at).total_seconds(), 0.0)
            if age_seconds <= 10:
                return {"price": ticker.price, "source": ticker.source, "observed_at": ticker.observed_at.isoformat()}
        return fetch_btc_spot_price()

    def _start_ticker_wss(self) -> None:
        if self._ticker_client is not None:
            return
        self._ticker_client = self.coinbase_client_factory(
            on_message=lambda message: self.ticker_store.apply_coinbase_message(message)
        )
        self._ticker_client.start()

    def _ensure_market_wss(self, market: dict[str, Any]) -> None:
        if not self.config.use_wss:
            return
        if self._subscribed_market_id == market["id"]:
            return
        if self._market_client is not None:
            self._market_client.stop()
        asset_ids = [asset_id for asset_id in (market.get("token_yes"), market.get("token_no")) if asset_id]
        self._market_client = self.polymarket_client_factory(
            asset_ids=asset_ids,
            on_message=lambda message: self.book_store.apply_message(message),
        )
        self._market_client.start()
        self._subscribed_market_id = market["id"]

    def _format_result(self, result: dict[str, Any]) -> str:
        return (
            "[v8-realtime] "
            f"cycle={result.get('cycle')} market={result.get('market_id')} "
            f"status={result.get('status')} offset={result.get('entry_offset_seconds')} "
            f"profile={result.get('rule_profile')} "
            f"prior={int(bool(result.get('prior_loaded')))} ticker={result.get('ticker_source')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run v8 WSS paper runtime.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-wss", action="store_true", help="Run without opening websocket clients; useful for smoke tests.")
    parser.add_argument("--no-refresh", action="store_true", help="Skip Gamma market refresh and use markets already in SQLite.")
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--prior-artifact", type=Path, default=DEFAULT_PRIOR_ARTIFACT)
    args = parser.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()
    daemon = V8RealtimeDaemon(
        db=db,
        config=DaemonConfig(
            loop_sleep_seconds=args.sleep,
            once=args.once,
            use_wss=not args.no_wss,
            refresh_markets=not args.no_refresh,
            prior_artifact=args.prior_artifact,
        ),
    )
    try:
        daemon.start()
    finally:
        daemon.stop()
        db.close()


if __name__ == "__main__":
    main()
