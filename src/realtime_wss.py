"""
realtime_wss.py - WSS market-data adapters for realtime paper execution.

The parser and in-memory book store are dependency-free and unit-testable. The
network client uses websocket-client when installed.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from v3.l2_replay import BookMetrics, BookState

POLYMARKET_MARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
COINBASE_WSS = "wss://ws-feed.exchange.coinbase.com"


@dataclass(frozen=True)
class LiveBookSnapshot:
    asset_id: str
    book: BookState
    metrics: BookMetrics
    updated_at: datetime
    source_event: str

    @property
    def age_seconds(self) -> float:
        return max((datetime.now(timezone.utc) - self.updated_at).total_seconds(), 0.0)


@dataclass(frozen=True)
class BtcTicker:
    price: float
    source: str
    observed_at: datetime

    @property
    def age_seconds(self) -> float:
        return max((datetime.now(timezone.utc) - self.observed_at).total_seconds(), 0.0)


class LiveBookStore:
    def __init__(self) -> None:
        self._books: dict[str, LiveBookSnapshot] = {}

    def apply_message(self, raw: str | dict[str, Any] | list[Any]) -> int:
        count = 0
        for event in _as_events(raw):
            count += self._apply_event(event)
        return count

    def get(self, asset_id: str | None) -> LiveBookSnapshot | None:
        if not asset_id:
            return None
        return self._books.get(str(asset_id))

    def all(self) -> dict[str, LiveBookSnapshot]:
        return dict(self._books)

    def _apply_event(self, event: dict[str, Any]) -> int:
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        if event_type == "book":
            asset_id = _asset_id(event)
            if not asset_id:
                return 0
            book = BookState.from_snapshot(
                bid_prices=_level_values(event.get("bids") or event.get("buys") or event.get("bid_prices"), "price"),
                bid_sizes=_level_values(event.get("bids") or event.get("buys") or event.get("bid_sizes"), "size"),
                ask_prices=_level_values(event.get("asks") or event.get("sells") or event.get("ask_prices"), "price"),
                ask_sizes=_level_values(event.get("asks") or event.get("sells") or event.get("ask_sizes"), "size"),
            )
            self._books[asset_id] = LiveBookSnapshot(
                asset_id=asset_id,
                book=book,
                metrics=book.metrics(),
                updated_at=_event_time(event),
                source_event="book",
            )
            return 1

        if event_type == "price_change":
            applied = 0
            changes = event.get("changes")
            if not isinstance(changes, list):
                changes = [event]
            for change in changes:
                if not isinstance(change, dict):
                    continue
                asset_id = _asset_id(change) or _asset_id(event)
                if not asset_id:
                    continue
                existing = self._books.get(asset_id)
                book = existing.book.copy() if existing else BookState()
                book.apply_price_change(
                    side=change.get("side"),
                    price=change.get("price"),
                    size=change.get("size"),
                )
                self._books[asset_id] = LiveBookSnapshot(
                    asset_id=asset_id,
                    book=book,
                    metrics=book.metrics(),
                    updated_at=_event_time(change, fallback=_event_time(event)),
                    source_event="price_change",
                )
                applied += 1
            return applied

        return 0


class BtcTickerStore:
    def __init__(self) -> None:
        self._ticker: BtcTicker | None = None

    def apply_coinbase_message(self, raw: str | dict[str, Any]) -> BtcTicker | None:
        event = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(event, dict) or event.get("type") != "ticker":
            return None
        product_id = str(event.get("product_id") or "")
        if product_id != "BTC-USD":
            return None
        price = event.get("price")
        if price is None:
            return None
        observed_at = _parse_datetime(event.get("time")) or datetime.now(timezone.utc)
        self._ticker = BtcTicker(price=float(price), source="coinbase_wss", observed_at=observed_at)
        return self._ticker

    def latest(self) -> BtcTicker | None:
        return self._ticker


class PolymarketMarketWssClient:
    def __init__(self, *, asset_ids: list[str], on_message: Callable[[Any], None], url: str = POLYMARKET_MARKET_WSS):
        self.asset_ids = [str(asset_id) for asset_id in asset_ids if asset_id]
        self.on_message = on_message
        self.url = url
        self._app = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - depends on optional runtime package
            raise RuntimeError("websocket-client is required for WSS runtime; install requirements.txt") from exc

        def _open(ws):
            ws.send(json.dumps(build_polymarket_market_subscription(self.asset_ids)))

        self._app = websocket.WebSocketApp(
            self.url,
            on_open=_open,
            on_message=lambda _ws, message: self.on_message(message),
        )
        self._thread = threading.Thread(target=self._app.run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._app is not None:
            self._app.close()


class CoinbaseTickerWssClient:
    def __init__(self, *, on_message: Callable[[Any], None], url: str = COINBASE_WSS):
        self.on_message = on_message
        self.url = url
        self._app = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - depends on optional runtime package
            raise RuntimeError("websocket-client is required for WSS runtime; install requirements.txt") from exc

        def _open(ws):
            ws.send(json.dumps(build_coinbase_ticker_subscription()))

        self._app = websocket.WebSocketApp(
            self.url,
            on_open=_open,
            on_message=lambda _ws, message: self.on_message(message),
        )
        self._thread = threading.Thread(target=self._app.run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._app is not None:
            self._app.close()


def _as_events(raw: str | dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def build_polymarket_market_subscription(asset_ids: list[str]) -> dict[str, Any]:
    return {
        "assets_ids": [str(asset_id) for asset_id in asset_ids if asset_id],
        "type": "market",
        "custom_feature_enabled": True,
    }


def build_coinbase_ticker_subscription() -> dict[str, Any]:
    return {
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channels": ["ticker"],
    }


def _asset_id(event: dict[str, Any]) -> str | None:
    value = event.get("asset_id") or event.get("assetId") or event.get("token_id") or event.get("tokenId")
    return str(value) if value else None


def _level_values(raw: Any, key: str) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = []
        for item in raw:
            if isinstance(item, dict):
                values.append(item.get(key))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                values.append(item[0] if key == "price" else item[1])
            else:
                values.append(item)
        return values
    return []


def _event_time(event: dict[str, Any], fallback: datetime | None = None) -> datetime:
    for key in ("timestamp", "time", "created_at"):
        parsed = _parse_datetime(event.get(key))
        if parsed is not None:
            return parsed
    return fallback or datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000.0
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def sleep_with_ping_interval(seconds: float = 10.0) -> None:
    time.sleep(max(float(seconds), 0.1))
