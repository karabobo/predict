import pandas as pd

from src.v3.l2_replay import BookState, initialize_book_from_snapshots, replay_l2_events


def test_book_state_snapshot_metrics_and_price_change():
    book = BookState.from_snapshot(
        bid_prices=[0.49, 0.48],
        bid_sizes=[10, 20],
        ask_prices=[0.50, 0.51],
        ask_sizes=[12, 18],
    )

    metrics = book.metrics()
    assert metrics.best_bid == 0.49
    assert metrics.best_ask == 0.50
    assert metrics.midpoint == 0.495
    assert metrics.spread == 0.01

    book.apply_price_change(side="BUY", price=0.50, size=7)
    book.apply_price_change(side="SELL", price=0.50, size=0)
    updated = book.metrics()

    assert updated.best_bid == 0.50
    assert updated.best_ask == 0.51
    assert "0.5000:7.0000" in updated.book_hash


def test_simulate_market_buy_consumes_asks_without_mutating_book():
    book = BookState.from_snapshot(
        bid_prices=[0.49],
        bid_sizes=[10],
        ask_prices=[0.50, 0.55],
        ask_sizes=[10, 10],
    )

    fill = book.simulate_market_buy(8.0)

    assert fill.spent_usdc == 8.0
    assert round(fill.shares, 6) == round(10 + (3.0 / 0.55), 6)
    assert round(fill.average_price, 6) == round(8.0 / fill.shares, 6)
    assert fill.levels_consumed == 2
    assert book.metrics().best_ask == 0.50


def test_simulate_market_buy_no_uses_complementary_yes_bids():
    book = BookState.from_snapshot(
        bid_prices=[0.60, 0.55],
        bid_sizes=[10, 10],
        ask_prices=[0.62],
        ask_sizes=[10],
    )

    fill = book.simulate_market_buy_outcome("NO", 4.0)

    assert fill.outcome == "NO"
    assert fill.average_price == 0.4
    assert fill.shares == 10.0
    assert fill.levels_consumed == 1


def test_initialize_and_replay_l2_events_from_frames():
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-03-23T23:45:02Z"),
                "bid_prices": [0.49],
                "bid_sizes": [10],
                "ask_prices": [0.50],
                "ask_sizes": [10],
            }
        ]
    )
    initial = initialize_book_from_snapshots(snapshots)
    events = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-03-23T23:45:03Z"),
                "event_type": "price_change",
                "pc_side": "BUY",
                "pc_price": 0.50,
                "pc_size": 4,
            },
            {
                "timestamp": pd.Timestamp("2026-03-23T23:45:04Z"),
                "event_type": "price_change",
                "pc_side": "SELL",
                "pc_price": 0.50,
                "pc_size": 0,
            },
        ]
    )

    rows = replay_l2_events(events, initial_book=initial)

    assert len(rows) == 2
    assert rows[0]["best_bid"] == 0.50
    assert rows[1]["best_ask"] is None
