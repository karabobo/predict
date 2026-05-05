import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from predict import alpha_router_signal


def _candles(pattern: list[tuple[float, float, float]]) -> list[dict]:
    candles = []
    price = 100.0
    for delta, rng, vol in pattern:
        open_ = price
        close = price + delta
        high = max(open_, close) + rng / 2
        low = min(open_, close) - rng / 2
        candles.append(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": vol,
            }
        )
        price = close
    return candles


def test_alpha_router_can_select_sparse_lvn_overlay():
    candles = _candles(
        [
            (0.02, 0.20, 8),
            (0.03, 0.19, 8),
            (0.04, 0.18, 8),
            (0.05, 0.17, 8),
            (0.06, 0.16, 20),
        ]
    )
    regime = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}

    result = alpha_router_signal(
        candles,
        regime,
        rule_names=["baseline_router_v1_plus_sparse_combo"],
    )

    assert result["should_trade"] is True
    assert result["direction"] == "UP"
    assert result["strategy_name"] == "baseline_router_v1_plus_sparse_combo"
    assert result["meta"]["strategy_name"] == "baseline_router_v1_plus_sparse_combo"
    assert "baseline_router_v1_plus_sparse_combo" in result["reason"]


def test_alpha_router_no_trade_fallback_is_safe():
    candles = _candles([(0.0, 0.20, 8) for _ in range(8)])
    regime = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}

    result = alpha_router_signal(candles, regime, rule_names=["missing_rule"])

    assert result["should_trade"] is False
    assert result["estimate"] == 0.5
    assert result["direction"] is None
    assert result["strategy_name"] == "alpha_router_no_trade"
    assert "alpha_router_no_trade" in result["reason"]
    assert "missing_rule:missing" in result["reason"]
