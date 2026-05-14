from __future__ import annotations

import statistics
from typing import Any


def compute_regime_from_candles(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify candles into a simple volatility/autocorrelation regime."""
    if not candles or len(candles) < 3:
        return {
            "autocorrelation": 0.0,
            "volatility": 0.0,
            "label": "UNKNOWN",
            "is_mean_reverting": False,
        }

    closes = [float(c["close"]) for c in candles]
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        for i in range(1, len(closes))
        if closes[i - 1]
    ]

    if len(returns) < 2:
        return {
            "autocorrelation": 0.0,
            "volatility": 0.0,
            "label": "UNKNOWN",
            "is_mean_reverting": False,
        }

    volatility = round(statistics.stdev(returns), 4) if len(returns) >= 2 else 0.0
    autocorr = _lag1_autocorrelation(returns)

    if volatility < 0.05:
        vol_label = "LOW_VOL"
    elif volatility < 0.12:
        vol_label = "MEDIUM_VOL"
    else:
        vol_label = "HIGH_VOL"

    if autocorr > 0.15:
        trend_label = "TRENDING"
    elif autocorr < -0.15:
        trend_label = "MEAN_REVERTING"
    else:
        trend_label = "NEUTRAL"

    return {
        "autocorrelation": round(autocorr, 4),
        "volatility": volatility,
        "label": f"{vol_label} / {trend_label}",
        "is_mean_reverting": autocorr < -0.15,
    }


def _lag1_autocorrelation(returns: list[float]) -> float:
    """Stable lag-1 autocorrelation for short candle windows."""
    n = len(returns)
    if n < 3:
        return 0.0

    mean_r = sum(returns) / n
    var = sum((r - mean_r) ** 2 for r in returns) / n

    if var == 0:
        if all(r > 0 for r in returns) or all(r < 0 for r in returns):
            return 1.0
        return 0.0

    cov = sum(
        (returns[i] - mean_r) * (returns[i - 1] - mean_r)
        for i in range(1, n)
    ) / (n - 1)
    return cov / var
