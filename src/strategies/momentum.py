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

    estimate = estimate_from_signal_features(
        last_direction=last_direction,
        streak=streak,
        compression=compression,
        volume_spike=volume_spike,
    )
    conviction = conviction_from_signal_features(
        streak=streak,
        compression=compression,
        volume_spike=volume_spike,
    )
    confidence = "high" if conviction >= 4 else "medium"

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


def estimate_from_signal_features(
    *,
    last_direction: str,
    streak: int,
    compression: bool,
    volume_spike: bool,
) -> float:
    edge = 0.05
    edge += min(max(streak - 3, 0), 4) * 0.015
    if compression:
        edge += 0.01
    if volume_spike:
        edge += 0.02
    edge = min(edge, 0.18)
    return 0.5 + edge if last_direction == "UP" else 0.5 - edge


def conviction_from_signal_features(
    *,
    streak: int,
    compression: bool,
    volume_spike: bool,
) -> int:
    if streak >= 4 and volume_spike:
        return 4
    if streak >= 5 and compression:
        return 4
    return 3


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
