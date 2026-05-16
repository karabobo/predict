import pandas as pd
from types import SimpleNamespace

from src.v3.backtest_rules import Candle
from src.v3.l2_candidate_replay import parse_market_slug, validate_candidate_on_frames


def _candles_for_window(start_ts: int, end_ts: int) -> list[Candle]:
    candles: list[Candle] = []
    ts = start_ts - 20 * 300
    price = 100.0
    while ts < end_ts:
        if ts == start_ts:
            open_price = 100.0
            close = 98.0
        elif ts == end_ts - 300:
            open_price = 92.0
            close = 90.0
        else:
            open_price = price
            close = price + 0.1
        candles.append(
            Candle(
                ts=ts,
                open=open_price,
                high=max(open_price, close) + 0.5,
                low=min(open_price, close) - 0.5,
                close=close,
                volume=10.0,
            )
        )
        price = close
        ts += 300
    return candles


def test_parse_market_slug_returns_window_bounds():
    spec = parse_market_slug("btc-updown-15m-1774310400")

    assert spec.minutes == 15
    assert spec.start_ts == 1774310400
    assert spec.end_ts == 1774311300


def test_parse_market_slug_can_use_legacy_end_timestamp():
    spec = parse_market_slug("btc-updown-15m-1774310400", slug_ts="end")

    assert spec.minutes == 15
    assert spec.start_ts == 1774309500
    assert spec.end_ts == 1774310400


def test_validate_candidate_uses_l2_fill_for_predicted_no(monkeypatch):
    start_ts = 1_000_000_000
    end_ts = start_ts + 900
    slug = f"btc-updown-15m-{start_ts}"

    def fake_rule(_candles, _regime):
        return {
            "should_trade": True,
            "direction": "DOWN",
            "estimate": 0.40,
            "confidence": "medium",
            "conviction_score": 3,
            "reason": "fake_down_rule",
        }

    monkeypatch.setattr(
        "src.v3.l2_candidate_replay.available_rules",
        lambda: {"fake_down": fake_rule},
    )
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.to_datetime(start_ts + 30, unit="s"),
                "bid_prices": [0.60],
                "bid_sizes": [500.0],
                "ask_prices": [0.62],
                "ask_sizes": [500.0],
            }
        ]
    )
    l2 = pd.DataFrame(columns=["timestamp", "event_type"])

    result = validate_candidate_on_frames(
        rule_name="fake_down",
        market_slug=slug,
        l2_frame=l2,
        snapshot_frame=snapshots,
        btc_candles=_candles_for_window(start_ts, end_ts),
        decision_offset_seconds=30,
        bet_usd=75.0,
    )

    assert result.status == "filled"
    assert result.actual_outcome == "NO"
    assert result.predicted_outcome == "NO"
    assert result.fill is not None
    assert result.fill.average_price == 0.4
    assert result.edge_after_fill == 0.19999999999999996
    assert result.pnl == 112.5


def test_validate_candidate_prior_gate_blocks_disagreement(monkeypatch):
    start_ts = 1_000_000_000
    end_ts = start_ts + 900
    slug = f"btc-updown-15m-{start_ts}"

    def fake_rule(_candles, _regime):
        return {
            "should_trade": True,
            "direction": "DOWN",
            "estimate": 0.40,
            "confidence": "medium",
            "conviction_score": 3,
            "reason": "fake_down_rule",
        }

    class FakePrior:
        def predict(self, _context):
            return SimpleNamespace(prob_up=0.60)

    monkeypatch.setattr(
        "src.v3.l2_candidate_replay.available_rules",
        lambda: {"fake_down": fake_rule},
    )
    snapshots = pd.DataFrame(
        [
            {
                "timestamp": pd.to_datetime(start_ts + 30, unit="s"),
                "bid_prices": [0.60],
                "bid_sizes": [500.0],
                "ask_prices": [0.62],
                "ask_sizes": [500.0],
            }
        ]
    )
    l2 = pd.DataFrame(columns=["timestamp", "event_type"])

    result = validate_candidate_on_frames(
        rule_name="fake_down",
        market_slug=slug,
        l2_frame=l2,
        snapshot_frame=snapshots,
        btc_candles=_candles_for_window(start_ts, end_ts),
        decision_offset_seconds=30,
        bet_usd=75.0,
        prior_model=FakePrior(),
        min_prior_edge=0.01,
    )

    assert result.status == "no_trade_signal"
    assert result.should_trade is False
    assert result.predicted_outcome is None
    assert result.prior_prob == 0.60
    assert result.prior_direction == "YES"
    assert result.prior_gate_passed is False
