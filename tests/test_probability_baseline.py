from __future__ import annotations

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v3.probability_baseline import (
    BaselineProbabilityEnsemble,
    baseline_ensemble_specs,
)


@dataclass(frozen=True)
class _FakeContext:
    market: dict
    formatted_candles: list[dict]
    production_regime: dict


def _candles(direction: str) -> list[dict]:
    price = 100.0
    candles = []
    for idx in range(20):
        move = 0.1 if direction == "UP" else -0.1
        close = price + move
        candles.append(
            {
                "open": price,
                "high": max(price, close) + 0.05,
                "low": min(price, close) - 0.05,
                "close": close,
                "volume": 10.0 + idx,
            }
        )
        price = close
    return candles


def _contexts(count: int = 120) -> list[_FakeContext]:
    contexts = []
    for idx in range(count):
        direction = "UP" if idx % 2 == 0 else "DOWN"
        contexts.append(
            _FakeContext(
                market={"outcome": 1 if direction == "UP" else 0},
                formatted_candles=_candles(direction),
                production_regime={"label": "MEDIUM_VOL / NEUTRAL"},
            )
        )
    return contexts


def test_default_baseline_ensemble_specs_are_normalized_by_service():
    specs = baseline_ensemble_specs()

    assert [spec.name for spec in specs] == ["paper_logreg_5m_window"]
    assert sum(spec.weight for spec in specs) == 1.0


def test_baseline_probability_ensemble_trains_and_predicts():
    service = BaselineProbabilityEnsemble()
    summary = service.fit(_contexts())

    assert service.is_trained is True
    assert summary.name == "ensemble_logreg_window"
    assert summary.train_samples > 0
    assert len(summary.members) == 1

    prediction = service.predict(_contexts(1)[0])

    assert 0.0 <= prediction.prob_up <= 1.0
    assert prediction.model_name == "ensemble_logreg_window"
    assert "paper_logreg_5m_window.prob_up" in prediction.diagnostics


def test_legacy_raw_xgb_ensemble_remains_available():
    specs = baseline_ensemble_specs("ensemble_logreg_raw_xgb")

    assert [spec.name for spec in specs] == ["paper_logreg_5m_raw", "paper_xgb_5m"]
