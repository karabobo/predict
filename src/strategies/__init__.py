"""Strategy layer for production-safe signal generation."""

from .momentum import contrarian_signal
from .regime import compute_regime_from_candles
from .types import StrategyDecision, StrategyInput

__all__ = [
    "StrategyDecision",
    "StrategyInput",
    "compute_regime_from_candles",
    "contrarian_signal",
]
