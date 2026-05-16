import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from realtime_wss import (
    BtcTickerStore,
    LiveBookStore,
    build_coinbase_ticker_subscription,
    build_polymarket_market_subscription,
)


def test_live_book_store_applies_polymarket_book_and_price_change():
    store = LiveBookStore()
    count = store.apply_message(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.51", "size": "12"}],
            "timestamp": "2026-05-15T00:00:00Z",
        }
    )

    assert count == 1
    assert store.get("yes-token").metrics.best_bid == 0.49
    assert store.get("yes-token").metrics.best_ask == 0.51

    count = store.apply_message(
        json.dumps(
            {
                "event_type": "price_change",
                "changes": [
                    {"asset_id": "yes-token", "side": "BUY", "price": "0.50", "size": "8"},
                    {"asset_id": "yes-token", "side": "SELL", "price": "0.51", "size": "0"},
                ],
            }
        )
    )

    assert count == 2
    assert store.get("yes-token").metrics.best_bid == 0.50
    assert store.get("yes-token").metrics.best_ask is None


def test_btc_ticker_store_accepts_coinbase_ticker():
    store = BtcTickerStore()

    ticker = store.apply_coinbase_message(
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "101000.12",
            "time": "2026-05-15T00:00:00Z",
        }
    )

    assert ticker is not None
    assert ticker.price == 101000.12
    assert ticker.source == "coinbase_wss"


def test_wss_subscription_payloads_match_runtime_requirements():
    assert build_polymarket_market_subscription(["yes", "no"]) == {
        "assets_ids": ["yes", "no"],
        "type": "market",
        "custom_feature_enabled": True,
    }
    assert build_coinbase_ticker_subscription() == {
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channels": ["ticker"],
    }
