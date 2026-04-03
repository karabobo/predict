from __future__ import annotations

from typing import Any

from .types import StrategyDecision


def contrarian_signal(candles: list[dict[str, Any]], regime_label: str = "UNKNOWN") -> dict[str, Any]:
    """
    Current production baseline: inverted contrarian momentum.

    Rules:
    - require at least 5 candles
    - require a streak of 3+ in one direction
    - require either shrinking ranges or a terminal volume spike
    - ride the streak when confirmed
    """
    if len(candles) < 5:
        return _no_trade("insufficient_data", regime_label)

    last_direction = _direction(candles[-1])
    streak = _streak_length(candles, last_direction)

    if streak < 3:
        return _no_trade("streak_too_short", regime_label)

    recent = candles[-max(streak, 3):]
    compression = _has_shrinking_ranges(recent)
    volume_spike = _has_volume_spike(candles[-5:])

    if not (compression or volume_spike):
        return _no_trade("no_exhaustion", regime_label)

    confidence = "high" if streak >= 5 else "medium"
    conviction = 4 if confidence == "high" else 3
    estimate = 0.62 if last_direction == "UP" else 0.38

    reasons = [f"streak_{streak}", f"direction_{last_direction.lower()}"]
    if compression:
        reasons.append("compression")
    if volume_spike:
        reasons.append("volume_spike")

    decision = StrategyDecision(
        estimate=estimate,
        should_trade=True,
        direction=last_direction,
        confidence=confidence,
        conviction_score=conviction,
        reason=" | ".join(reasons),
        regime_label=regime_label,
        meta={"streak": streak, "compression": compression, "volume_spike": volume_spike},
    )
    return decision.to_record()


def _no_trade(reason: str, regime_label: str) -> dict[str, Any]:
    decision = StrategyDecision(
        estimate=0.5,
        should_trade=False,
        direction=None,
        confidence="low",
        conviction_score=0,
        reason=reason,
        regime_label=regime_label,
        meta={},
    )
    return decision.to_record()


def _direction(candle: dict[str, Any]) -> str:
    return "UP" if float(candle["close"]) >= float(candle["open"]) else "DOWN"


def _streak_length(candles: list[dict[str, Any]], last_direction: str) -> int:
    streak = 0
    for candle in reversed(candles):
        if _direction(candle) != last_direction:
            break
        streak += 1
    return streak


def _has_shrinking_ranges(candles: list[dict[str, Any]]) -> bool:
    if len(candles) < 3:
        return False
    ranges = [
        float(c["high"]) - float(c["low"])
        for c in candles[-3:]
    ]
    return ranges[0] > ranges[1] > ranges[2]


def _has_volume_spike(candles: list[dict[str, Any]]) -> bool:
    if len(candles) < 2:
        return False
    volumes = [float(c.get("volume", 0.0) or 0.0) for c in candles]
    baseline = sum(volumes[:-1]) / max(1, len(volumes) - 1)
    if baseline <= 0:
        return False
    return volumes[-1] / baseline >= 1.8
