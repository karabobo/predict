import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v3.probability_foundation import (
    Paper5MModelService,
    ProbabilityFoundationService,
    build_probability_dataset,
    build_paper_5m_dataset,
    extract_foundation_features,
    extract_low_dim_features,
    extract_paper_5m_features,
    extract_paper_5m_raw_features,
    low_dim_feature_names,
    foundation_feature_names,
    paper_5m_feature_names,
    paper_5m_raw_feature_names,
)


@dataclass(frozen=True)
class _FakeContext:
    market: dict
    formatted_candles: list[dict]
    production_regime: dict


def _candles(direction: str) -> list[dict]:
    candles = []
    price = 100.0
    for idx in range(20):
        move = 0.10 if direction == "UP" else -0.10
        close = price + move
        candles.append(
            {
                "open": price,
                "high": max(price, close) + 0.04,
                "low": min(price, close) - 0.04,
                "close": close,
                "volume": 10.0 + idx,
            }
        )
        price = close
    return candles


def _contexts(count: int = 100) -> list[_FakeContext]:
    contexts = []
    for idx in range(count):
        direction = "UP" if idx % 2 == 0 else "DOWN"
        outcome = 1 if direction == "UP" else 0
        contexts.append(
            _FakeContext(
                market={"outcome": outcome},
                formatted_candles=_candles(direction),
                production_regime={"label": "MEDIUM_VOL / NEUTRAL"},
            )
        )
    return contexts


def test_low_dim_feature_extraction_returns_expected_keys():
    features = extract_low_dim_features(_candles("UP"), "MEDIUM_VOL / NEUTRAL")

    assert set(low_dim_feature_names()).issubset(features.keys())
    assert features["is_medium_vol"] == 1.0
    assert features["is_neutral"] == 1.0


def test_build_probability_dataset_shapes_match_contexts():
    X, y = build_probability_dataset(_contexts(12))

    assert X.shape == (12, len(foundation_feature_names()))
    assert y.shape == (12,)


def test_foundation_features_include_rule_shape_fields():
    signal = {
        "direction": "DOWN",
        "confidence": "medium",
        "conviction_score": 3,
        "meta": {
            "branch_name": "medium_neutral_down_continuation_balanced",
            "exact_rule_name": "medium_neutral_down_continuation_balanced",
            "original_estimate": 0.63,
            "has_continuation": True,
            "has_reversal": False,
            "has_volume_spike": True,
            "has_range_compression": False,
            "has_wick_signal": True,
            "has_near_extreme": False,
        },
    }

    features = extract_foundation_features(_candles("DOWN"), "MEDIUM_VOL / NEUTRAL", signal)

    assert set(foundation_feature_names()).issubset(features.keys())
    assert features["candidate_direction_down"] == 1.0
    assert features["candidate_confidence_medium"] == 1.0
    assert features["branch_medium_vol"] == 1.0
    assert features["branch_continuation"] == 1.0
    assert features["shape_volume_spike"] == 1.0
    assert features["original_estimate"] == 0.63
    assert features["original_edge_abs"] == 0.13
    assert features["rule_medium_neutral_down_continuation_balanced"] == 1.0


def test_probability_foundation_service_trains_and_predicts():
    service = ProbabilityFoundationService()
    summary = service.fit(_contexts(120))

    assert summary.train_samples > 0
    assert service.is_trained is True

    prediction = service.predict(_contexts(1)[0], require_agreement=True)
    assert 0.0 <= prediction.prob_up <= 1.0
    assert "primary_raw" in prediction.diagnostics


def test_probability_foundation_service_exposes_training_summary():
    service = ProbabilityFoundationService()
    service.fit(_contexts(120))

    summary = service.summary

    assert summary is not None
    assert summary.primary_model_name
    assert summary.secondary_model_name


def test_paper_5m_feature_extraction_returns_expected_keys():
    features = extract_paper_5m_features(_candles("UP"))

    assert set(paper_5m_feature_names()).issubset(features.keys())
    assert features["volume_level"] > 0


def test_build_paper_5m_dataset_shapes_match_contexts():
    X, y = build_paper_5m_dataset(_contexts(12))

    assert X.shape == (12, len(paper_5m_feature_names()))
    assert y.shape == (12,)


def test_paper_5m_raw_feature_extraction_returns_expected_keys():
    features = extract_paper_5m_raw_features(_candles("UP"))

    assert set(paper_5m_raw_feature_names()).issubset(features.keys())
    assert features["price"] == features["close"]


def test_build_paper_5m_raw_dataset_shapes_match_contexts():
    X, y = build_paper_5m_dataset(_contexts(12), feature_set="raw")

    assert X.shape == (12, len(paper_5m_raw_feature_names()))
    assert y.shape == (12,)


def test_paper_5m_model_service_trains_and_predicts():
    service = Paper5MModelService(model_kind="xgboost")
    summary = service.fit(_contexts(120))

    assert summary.train_samples > 0
    assert service.is_trained is True

    prediction = service.predict(_contexts(1)[0])
    assert 0.0 <= prediction.prob_up <= 1.0
    assert prediction.model_name


def test_paper_5m_raw_model_service_trains_and_predicts():
    service = Paper5MModelService(model_kind="logreg", feature_set="raw", use_calibration=False)
    summary = service.fit(_contexts(120))

    assert summary.train_samples > 0
    assert service.is_trained is True
    assert summary.calibrated is False

    prediction = service.predict(_contexts(1)[0])
    assert 0.0 <= prediction.prob_up <= 1.0
    assert prediction.diagnostics["feature_set"] == "raw"
