from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StrategyInput:
    candles: list[dict[str, Any]]
    market_price: float
    regime_label: str = "UNKNOWN"
    market_question: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyDecision:
    estimate: float
    should_trade: bool
    direction: str | None
    confidence: str
    conviction_score: int
    reason: str
    regime_label: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def edge(self) -> float:
        return abs(self.estimate - 0.5)

    def to_record(self) -> dict[str, Any]:
        return {
            "estimate": self.estimate,
            "should_trade": self.should_trade,
            "direction": self.direction,
            "confidence": self.confidence,
            "conviction_score": self.conviction_score,
            "reason": self.reason,
            "regime_label": self.regime_label,
            "meta": self.meta,
        }
