from __future__ import annotations

from dataclasses import dataclass, field
import statistics
from typing import Any

import numpy as np


LOW_DIM_FEATURE_NAMES = [
    "ret_1",
    "ret_3",
    "ret_5",
    "ret_10",
    "range_pos_10",
    "range_norm_10",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "up_ratio_10",
    "streak_signed",
    "streak_abs",
    "volume_ratio_5",
    "realized_vol_10",
    "return_autocorr_10",
    "is_low_vol",
    "is_medium_vol",
    "is_high_vol",
    "is_trending",
    "is_mean_reverting",
    "is_neutral",
]

RULE_SHAPE_FEATURE_NAMES = [
    "candidate_direction_up",
    "candidate_direction_down",
    "candidate_confidence_low",
    "candidate_confidence_medium",
    "candidate_confidence_high",
    "candidate_conviction_score",
    "original_estimate",
    "original_edge_abs",
    "branch_low_vol",
    "branch_medium_vol",
    "branch_high_vol",
    "branch_continuation",
    "branch_reversal",
    "shape_volume_spike",
    "shape_range_compression",
    "shape_wick_signal",
    "shape_near_extreme",
    "rule_low_vol_branch_v1",
    "rule_clean_continuation_up",
    "rule_clean_continuation_down",
    "rule_medium_neutral_down_continuation",
    "rule_medium_neutral_down_continuation_balanced",
    "rule_medium_neutral_down_continuation_core",
    "rule_flush_bounce_up",
    "rule_spike_reversal_down",
    "rule_spike_reversal_down_no_hvt",
]

FOUNDATION_FEATURE_NAMES = LOW_DIM_FEATURE_NAMES + RULE_SHAPE_FEATURE_NAMES
PAPER_5M_FEATURE_NAMES = [
    "open_close_return",
    "high_low_range",
    "close_level",
    "price_level",
    "volume_level",
    "upper_wick_ratio",
    "lower_wick_ratio",
]
PAPER_5M_RAW_FEATURE_NAMES = [
    "open",
    "close",
    "high",
    "low",
    "price",
    "volume",
]


@dataclass(frozen=True)
class ProbabilityPrediction:
    prob_up: float
    model_name: str
    calibrated: bool
    agreement_passed: bool
    diagnostics: dict[str, float | int | str]


@dataclass(frozen=True)
class ProbabilityFoundationConfig:
    min_train_samples: int = 80
    primary_calibration_ratio: float = 0.15
    primary_model_name: str = "xgboost_primary"
    fallback_model_name: str = "numpy_logistic_primary"
    secondary_model_name: str = "logreg_secondary"
    agreement_epsilon: float = 1e-9
    decisive_threshold: float = 0.03


@dataclass(frozen=True)
class FoundationTrainingSummary:
    train_samples: int
    calibration_samples: int
    primary_model_name: str
    secondary_model_name: str
    calibrated: bool
    diagnostics: dict[str, float | int | str] = field(default_factory=dict)


def low_dim_feature_names() -> list[str]:
    return list(LOW_DIM_FEATURE_NAMES)


def foundation_feature_names() -> list[str]:
    return list(FOUNDATION_FEATURE_NAMES)


def paper_5m_feature_names() -> list[str]:
    return list(PAPER_5M_FEATURE_NAMES)


def paper_5m_raw_feature_names() -> list[str]:
    return list(PAPER_5M_RAW_FEATURE_NAMES)


def extract_low_dim_features(
    candles: list[dict[str, Any]],
    regime_label: str,
) -> dict[str, float]:
    if not candles:
        return {name: 0.0 for name in LOW_DIM_FEATURE_NAMES}

    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    volumes = [float(c.get("volume", 0.0) or 0.0) for c in candles]
    last = candles[-1]

    window10 = candles[-10:]
    closes10 = [float(c["close"]) for c in window10]
    highs10 = [float(c["high"]) for c in window10]
    lows10 = [float(c["low"]) for c in window10]

    label = regime_label.upper()
    features = {
        "ret_1": _safe_return(closes, 1),
        "ret_3": _safe_return(closes, 3),
        "ret_5": _safe_return(closes, 5),
        "ret_10": _safe_return(closes, min(10, len(closes) - 1)),
        "range_pos_10": _range_position(closes10[-1], highs10, lows10),
        "range_norm_10": _normalized_range(highs10, lows10, closes10[-1]),
        "upper_wick_ratio": _upper_wick_ratio(last),
        "lower_wick_ratio": _lower_wick_ratio(last),
        "up_ratio_10": sum(1 for candle in window10 if _direction(candle) == "UP") / len(window10),
        "streak_signed": float(_signed_streak(candles)),
        "streak_abs": float(abs(_signed_streak(candles))),
        "volume_ratio_5": _volume_ratio(volumes[-5:]),
        "realized_vol_10": _realized_vol(closes10),
        "return_autocorr_10": _return_autocorr(closes10),
        "is_low_vol": 1.0 if label.startswith("LOW_VOL") else 0.0,
        "is_medium_vol": 1.0 if label.startswith("MEDIUM_VOL") else 0.0,
        "is_high_vol": 1.0 if label.startswith("HIGH_VOL") else 0.0,
        "is_trending": 1.0 if label.endswith("TRENDING") else 0.0,
        "is_mean_reverting": 1.0 if label.endswith("MEAN_REVERTING") else 0.0,
        "is_neutral": 1.0 if label.endswith("NEUTRAL") else 0.0,
    }
    return features


def extract_paper_5m_features(candles: list[dict[str, Any]]) -> dict[str, float]:
    if not candles:
        return {name: 0.0 for name in PAPER_5M_FEATURE_NAMES}
    last = candles[-1]
    open_px = float(last.get("open", 0.0) or 0.0)
    close_px = float(last.get("close", 0.0) or 0.0)
    high_px = float(last.get("high", close_px) or close_px)
    low_px = float(last.get("low", close_px) or close_px)
    volume = float(last.get("volume", 0.0) or 0.0)
    price_level = close_px
    base = open_px if open_px > 0 else close_px if close_px > 0 else 1.0
    return {
        "open_close_return": (close_px - open_px) / base,
        "high_low_range": (high_px - low_px) / base,
        "close_level": close_px / base,
        "price_level": price_level,
        "volume_level": volume,
        "upper_wick_ratio": _upper_wick_ratio(last),
        "lower_wick_ratio": _lower_wick_ratio(last),
    }


def extract_paper_5m_raw_features(candles: list[dict[str, Any]]) -> dict[str, float]:
    if not candles:
        return {name: 0.0 for name in PAPER_5M_RAW_FEATURE_NAMES}
    last = candles[-1]
    close_px = float(last.get("close", 0.0) or 0.0)
    return {
        "open": float(last.get("open", 0.0) or 0.0),
        "close": close_px,
        "high": float(last.get("high", close_px) or close_px),
        "low": float(last.get("low", close_px) or close_px),
        "price": close_px,
        "volume": float(last.get("volume", 0.0) or 0.0),
    }


def extract_rule_shape_features(signal: dict[str, Any] | None) -> dict[str, float]:
    features = {name: 0.0 for name in RULE_SHAPE_FEATURE_NAMES}
    if not signal:
        return features

    meta = signal.get("meta", {}) if isinstance(signal.get("meta", {}), dict) else {}
    direction = str(signal.get("direction", meta.get("direction", "SKIP")) or "SKIP").upper()
    confidence = str(signal.get("confidence", meta.get("confidence", "low")) or "low").lower()
    conviction_score = int(signal.get("conviction_score", meta.get("conviction_score", 0)) or 0)
    branch_name = str(meta.get("branch_name", "") or "").lower()
    exact_rule_name = str(meta.get("exact_rule_name", branch_name) or branch_name).lower()
    original_estimate = float(meta.get("original_estimate", 0.5) or 0.5)

    features.update(
        {
            "candidate_direction_up": 1.0 if direction == "UP" else 0.0,
            "candidate_direction_down": 1.0 if direction == "DOWN" else 0.0,
            "candidate_confidence_low": 1.0 if confidence == "low" else 0.0,
            "candidate_confidence_medium": 1.0 if confidence == "medium" else 0.0,
            "candidate_confidence_high": 1.0 if confidence == "high" else 0.0,
            "candidate_conviction_score": float(conviction_score),
            "original_estimate": original_estimate,
            "original_edge_abs": abs(original_estimate - 0.5),
            "branch_low_vol": 1.0 if "low_vol" in branch_name else 0.0,
            "branch_medium_vol": 1.0 if "medium_vol" in branch_name or "medium_neutral" in branch_name else 0.0,
            "branch_high_vol": 1.0 if "high_vol" in branch_name else 0.0,
            "branch_continuation": 1.0 if bool(meta.get("has_continuation")) else 0.0,
            "branch_reversal": 1.0 if bool(meta.get("has_reversal")) else 0.0,
            "shape_volume_spike": 1.0 if bool(meta.get("has_volume_spike")) else 0.0,
            "shape_range_compression": 1.0 if bool(meta.get("has_range_compression")) else 0.0,
            "shape_wick_signal": 1.0 if bool(meta.get("has_wick_signal")) else 0.0,
            "shape_near_extreme": 1.0 if bool(meta.get("has_near_extreme")) else 0.0,
            "rule_low_vol_branch_v1": 1.0 if exact_rule_name == "low_vol_branch_v1" else 0.0,
            "rule_clean_continuation_up": 1.0 if exact_rule_name == "clean_continuation_up" else 0.0,
            "rule_clean_continuation_down": 1.0 if exact_rule_name == "clean_continuation_down" else 0.0,
            "rule_medium_neutral_down_continuation": 1.0 if exact_rule_name == "medium_neutral_down_continuation" else 0.0,
            "rule_medium_neutral_down_continuation_balanced": 1.0 if exact_rule_name == "medium_neutral_down_continuation_balanced" else 0.0,
            "rule_medium_neutral_down_continuation_core": 1.0 if exact_rule_name == "medium_neutral_down_continuation_core" else 0.0,
            "rule_flush_bounce_up": 1.0 if exact_rule_name == "flush_bounce_up" else 0.0,
            "rule_spike_reversal_down": 1.0 if exact_rule_name == "spike_reversal_down" else 0.0,
            "rule_spike_reversal_down_no_hvt": 1.0 if exact_rule_name == "spike_reversal_down_no_hvt" else 0.0,
        }
    )
    return features


def extract_foundation_features(
    candles: list[dict[str, Any]],
    regime_label: str,
    signal: dict[str, Any] | None = None,
) -> dict[str, float]:
    features = extract_low_dim_features(candles, regime_label)
    features.update(extract_rule_shape_features(signal))
    return features


def features_to_row(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FOUNDATION_FEATURE_NAMES]


def paper_5m_features_to_row(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in PAPER_5M_FEATURE_NAMES]


def paper_5m_raw_features_to_row(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in PAPER_5M_RAW_FEATURE_NAMES]


class ProbabilityFoundationService:
    def __init__(self, config: ProbabilityFoundationConfig | None = None):
        self.config = config or ProbabilityFoundationConfig()
        self._primary_model: Any = None
        self._secondary_model: Any = None
        self._calibrator: _PlattCalibrator | None = None
        self._summary: FoundationTrainingSummary | None = None
        self._is_trained = False

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    @property
    def summary(self) -> FoundationTrainingSummary | None:
        return self._summary

    def fit(self, contexts: list[Any], signal_provider: Any | None = None) -> FoundationTrainingSummary:
        X, y = build_probability_dataset(contexts, signal_provider=signal_provider)
        sample_count = len(y)
        if sample_count < self.config.min_train_samples:
            self._primary_model = None
            self._secondary_model = None
            self._calibrator = None
            self._is_trained = False
            self._summary = FoundationTrainingSummary(
                train_samples=sample_count,
                calibration_samples=0,
                primary_model_name="untrained",
                secondary_model_name="untrained",
                calibrated=False,
                diagnostics={"reason": "insufficient_samples"},
            )
            return self._summary

        cal_size = max(10, int(sample_count * self.config.primary_calibration_ratio))
        if sample_count - cal_size < 20:
            cal_size = max(0, sample_count // 5)

        if cal_size > 0:
            X_train = X[:-cal_size]
            y_train = y[:-cal_size]
            X_cal = X[-cal_size:]
            y_cal = y[-cal_size:]
        else:
            X_train = X
            y_train = y
            X_cal = np.empty((0, X.shape[1]))
            y_cal = np.empty((0,))

        primary_model, primary_name, primary_diag = _fit_primary_model(X_train, y_train, self.config)
        secondary_model, secondary_name, secondary_diag = _fit_secondary_model(X_train, y_train, self.config)

        calibrator = None
        calibrated = False
        diagnostics = {
            "primary_backend": primary_diag["backend"],
            "secondary_backend": secondary_diag["backend"],
        }
        if len(y_cal) >= 10 and len(set(int(v) for v in y_cal.tolist())) >= 2:
            raw_probs = _predict_model_probs(primary_model, X_cal)
            calibrator = _PlattCalibrator()
            calibrator.fit(raw_probs, y_cal)
            calibrated = True

        self._primary_model = primary_model
        self._secondary_model = secondary_model
        self._calibrator = calibrator
        self._is_trained = True
        self._summary = FoundationTrainingSummary(
            train_samples=len(y_train),
            calibration_samples=len(y_cal),
            primary_model_name=primary_name,
            secondary_model_name=secondary_name,
            calibrated=calibrated,
            diagnostics=diagnostics,
        )
        return self._summary

    def predict(
        self,
        context: Any,
        *,
        candidate_signal: dict[str, Any] | None = None,
        require_agreement: bool = False,
    ) -> ProbabilityPrediction:
        if not self._is_trained or self._primary_model is None or self._secondary_model is None:
            return ProbabilityPrediction(
                prob_up=0.5,
                model_name="untrained",
                calibrated=False,
                agreement_passed=False,
                diagnostics={"reason": "model_not_trained"},
            )

        features = extract_foundation_features(
            context.formatted_candles,
            context.production_regime["label"],
            candidate_signal,
        )
        row = np.array([features_to_row(features)], dtype=float)
        primary_raw = float(_predict_model_probs(self._primary_model, row)[0])
        primary_prob = float(self._calibrator.predict(np.array([primary_raw]))[0]) if self._calibrator else primary_raw
        secondary_prob = float(_predict_model_probs(self._secondary_model, row)[0])

        primary_direction = _prob_direction(primary_prob, self.config.agreement_epsilon)
        secondary_direction = _prob_direction(secondary_prob, self.config.agreement_epsilon)
        agreement = primary_direction != "FLAT" and primary_direction == secondary_direction
        if require_agreement and not agreement:
            model_name = f"{self._summary.primary_model_name}+agreement"
        else:
            model_name = self._summary.primary_model_name

        return ProbabilityPrediction(
            prob_up=max(0.0, min(1.0, primary_prob)),
            model_name=model_name,
            calibrated=self._calibrator is not None,
            agreement_passed=agreement,
            diagnostics={
                "primary_raw": round(primary_raw, 6),
                "secondary_prob": round(secondary_prob, 6),
                "primary_direction": primary_direction,
                "secondary_direction": secondary_direction,
                "candidate_branch": str(
                    (candidate_signal or {}).get("meta", {}).get("branch_name", "")
                    if isinstance((candidate_signal or {}).get("meta", {}), dict) else ""
                ),
            },
        )


class Paper5MModelService:
    def __init__(
        self,
        *,
        model_kind: str,
        feature_set: str = "derived",
        min_train_samples: int = 80,
        calibration_ratio: float = 0.15,
        use_calibration: bool = True,
    ) -> None:
        self.model_kind = model_kind
        self.feature_set = feature_set
        self.min_train_samples = min_train_samples
        self.calibration_ratio = calibration_ratio
        self.use_calibration = use_calibration
        self._model: Any = None
        self._calibrator: _PlattCalibrator | None = None
        self._summary: FoundationTrainingSummary | None = None
        self._is_trained = False

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    @property
    def summary(self) -> FoundationTrainingSummary | None:
        return self._summary

    def fit(self, contexts: list[Any]) -> FoundationTrainingSummary:
        X, y = build_paper_5m_dataset(contexts, feature_set=self.feature_set)
        sample_count = len(y)
        if sample_count < self.min_train_samples:
            self._model = None
            self._calibrator = None
            self._is_trained = False
            self._summary = FoundationTrainingSummary(
                train_samples=sample_count,
                calibration_samples=0,
                primary_model_name="untrained",
                secondary_model_name="none",
                calibrated=False,
                diagnostics={
                    "reason": "insufficient_samples",
                    "model_kind": self.model_kind,
                    "feature_set": self.feature_set,
                    "use_calibration": self.use_calibration,
                },
            )
            return self._summary

        cal_size = max(10, int(sample_count * self.calibration_ratio))
        if sample_count - cal_size < 20:
            cal_size = max(0, sample_count // 5)
        if cal_size > 0:
            X_train = X[:-cal_size]
            y_train = y[:-cal_size]
            X_cal = X[-cal_size:]
            y_cal = y[-cal_size:]
        else:
            X_train = X
            y_train = y
            X_cal = np.empty((0, X.shape[1]))
            y_cal = np.empty((0,))

        if self.model_kind == "xgboost":
            model, model_name, diag = _fit_primary_model(X_train, y_train, ProbabilityFoundationConfig())
        elif self.model_kind == "logreg":
            model, model_name, diag = _fit_secondary_model(X_train, y_train, ProbabilityFoundationConfig())
        else:
            raise ValueError(f"Unsupported paper 5m model kind: {self.model_kind}")

        calibrator = None
        calibrated = False
        if self.use_calibration and len(y_cal) >= 10 and len(set(int(v) for v in y_cal.tolist())) >= 2:
            raw_probs = _predict_model_probs(model, X_cal)
            calibrator = _PlattCalibrator()
            calibrator.fit(raw_probs, y_cal)
            calibrated = True

        self._model = model
        self._calibrator = calibrator
        self._is_trained = True
        self._summary = FoundationTrainingSummary(
            train_samples=len(y_train),
            calibration_samples=len(y_cal),
            primary_model_name=model_name,
            secondary_model_name="none",
            calibrated=calibrated,
            diagnostics={
                "backend": diag["backend"],
                "model_kind": self.model_kind,
                "feature_set": self.feature_set,
                "use_calibration": self.use_calibration,
            },
        )
        return self._summary

    def predict(self, context: Any) -> ProbabilityPrediction:
        if not self._is_trained or self._model is None:
            return ProbabilityPrediction(
                prob_up=0.5,
                model_name="untrained",
                calibrated=False,
                agreement_passed=False,
                diagnostics={
                    "reason": "model_not_trained",
                    "model_kind": self.model_kind,
                    "feature_set": self.feature_set,
                },
            )
        if self.feature_set == "raw":
            features = extract_paper_5m_raw_features(context.formatted_candles)
            row = np.array([paper_5m_raw_features_to_row(features)], dtype=float)
        else:
            features = extract_paper_5m_features(context.formatted_candles)
            row = np.array([paper_5m_features_to_row(features)], dtype=float)
        raw_prob = float(_predict_model_probs(self._model, row)[0])
        prob_up = float(self._calibrator.predict(np.array([raw_prob]))[0]) if self._calibrator else raw_prob
        return ProbabilityPrediction(
            prob_up=max(0.0, min(1.0, prob_up)),
            model_name=self._summary.primary_model_name if self._summary else self.model_kind,
            calibrated=self._calibrator is not None,
            agreement_passed=False,
            diagnostics={
                "raw_prob": round(raw_prob, 6),
                "model_kind": self.model_kind,
                "feature_set": self.feature_set,
            },
        )


def build_probability_dataset(
    contexts: list[Any],
    signal_provider: Any | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    rows: list[list[float]] = []
    labels: list[int] = []
    for context in contexts:
        signal = None
        if signal_provider is not None:
            signal = signal_provider(context.formatted_candles, context.production_regime)
        rows.append(
            features_to_row(
                extract_foundation_features(
                    context.formatted_candles,
                    context.production_regime["label"],
                    signal,
                )
            )
        )
        labels.append(int(context.market["outcome"]))
    if not rows:
        return np.empty((0, len(FOUNDATION_FEATURE_NAMES))), np.empty((0,))
    return np.asarray(rows, dtype=float), np.asarray(labels, dtype=float)


def build_paper_5m_dataset(
    contexts: list[Any],
    *,
    feature_set: str = "derived",
) -> tuple[np.ndarray, np.ndarray]:
    rows: list[list[float]] = []
    labels: list[int] = []
    for context in contexts:
        if feature_set == "raw":
            rows.append(paper_5m_raw_features_to_row(extract_paper_5m_raw_features(context.formatted_candles)))
        else:
            rows.append(paper_5m_features_to_row(extract_paper_5m_features(context.formatted_candles)))
        labels.append(int(context.market["outcome"]))
    if not rows:
        width = len(PAPER_5M_RAW_FEATURE_NAMES) if feature_set == "raw" else len(PAPER_5M_FEATURE_NAMES)
        return np.empty((0, width)), np.empty((0,))
    return np.asarray(rows, dtype=float), np.asarray(labels, dtype=float)


class _NumpyLogisticModel:
    def __init__(self, *, l2: float, steps: int = 600, learning_rate: float = 0.1):
        self.l2 = l2
        self.steps = steps
        self.learning_rate = learning_rate
        self.weights: np.ndarray | None = None
        self.bias = 0.0
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        self._mean = X.mean(axis=0)
        std = X.std(axis=0)
        self._std = np.where(std < 1e-9, 1.0, std)
        Xn = (X - self._mean) / self._std
        self.weights = np.zeros(Xn.shape[1], dtype=float)
        self.bias = 0.0
        n = max(1, len(y))
        for _ in range(self.steps):
            logits = Xn @ self.weights + self.bias
            probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))
            error = probs - y
            grad_w = (Xn.T @ error) / n + self.l2 * self.weights
            grad_b = float(error.mean())
            self.weights -= self.learning_rate * grad_w
            self.bias -= self.learning_rate * grad_b

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.weights is None or self._mean is None or self._std is None:
            return np.full((len(X),), 0.5)
        Xn = (X - self._mean) / self._std
        logits = Xn @ self.weights + self.bias
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))
        return probs


class _PlattCalibrator:
    def __init__(self) -> None:
        self._model = _NumpyLogisticModel(l2=0.01, steps=400, learning_rate=0.2)

    def fit(self, raw_probs: np.ndarray, y: np.ndarray) -> None:
        self._model.fit(raw_probs.reshape(-1, 1), y)

    def predict(self, raw_probs: np.ndarray) -> np.ndarray:
        return self._model.predict_proba(raw_probs.reshape(-1, 1))


def _fit_primary_model(
    X: np.ndarray,
    y: np.ndarray,
    config: ProbabilityFoundationConfig,
) -> tuple[Any, str, dict[str, str]]:
    try:
        import xgboost as xgb  # type: ignore

        model = xgb.XGBClassifier(
            max_depth=3,
            n_estimators=120,
            learning_rate=0.08,
            reg_lambda=2.0,
            subsample=0.85,
            colsample_bytree=0.85,
            eval_metric="logloss",
            random_state=42,
            verbosity=0,
        )
        model.fit(X, y)
        return model, config.primary_model_name, {"backend": "xgboost"}
    except Exception:
        model = _NumpyLogisticModel(l2=0.15, steps=800, learning_rate=0.08)
        model.fit(X, y)
        return model, config.fallback_model_name, {"backend": "numpy_logistic_fallback"}


def _fit_secondary_model(
    X: np.ndarray,
    y: np.ndarray,
    config: ProbabilityFoundationConfig,
) -> tuple[Any, str, dict[str, str]]:
    try:
        from sklearn.linear_model import LogisticRegression  # type: ignore

        model = LogisticRegression(max_iter=1200, C=1.0, random_state=42)
        model.fit(X, y)
        return model, config.secondary_model_name, {"backend": "sklearn_logistic"}
    except Exception:
        model = _NumpyLogisticModel(l2=0.75, steps=700, learning_rate=0.06)
        model.fit(X, y)
        return model, config.secondary_model_name, {"backend": "numpy_logistic"}


def _predict_model_probs(model: Any, X: np.ndarray) -> np.ndarray:
    probs = getattr(model, "predict_proba")(X)
    if isinstance(probs, np.ndarray) and probs.ndim == 2:
        return probs[:, 1]
    return np.asarray(probs, dtype=float)


def _safe_return(closes: list[float], periods: int) -> float:
    if periods <= 0 or len(closes) <= periods:
        return 0.0
    prev = closes[-periods - 1]
    curr = closes[-1]
    if prev <= 0:
        return 0.0
    return curr / prev - 1.0


def _range_position(close: float, highs: list[float], lows: list[float]) -> float:
    if not highs or not lows:
        return 0.5
    high = max(highs)
    low = min(lows)
    width = high - low
    if width <= 0:
        return 0.5
    return (close - low) / width


def _normalized_range(highs: list[float], lows: list[float], ref: float) -> float:
    if not highs or not lows or ref <= 0:
        return 0.0
    return (max(highs) - min(lows)) / ref


def _direction(candle: dict[str, Any]) -> str:
    return "UP" if float(candle["close"]) >= float(candle["open"]) else "DOWN"


def _signed_streak(candles: list[dict[str, Any]]) -> int:
    if not candles:
        return 0
    last_direction = _direction(candles[-1])
    streak = 0
    for candle in reversed(candles):
        if _direction(candle) != last_direction:
            break
        streak += 1
    return streak if last_direction == "UP" else -streak


def _upper_wick_ratio(candle: dict[str, Any]) -> float:
    open_ = float(candle["open"])
    close = float(candle["close"])
    high = float(candle["high"])
    ref = close if close >= open_ else open_
    if ref <= 0:
        return 0.0
    return max(0.0, high - ref) / ref


def _lower_wick_ratio(candle: dict[str, Any]) -> float:
    open_ = float(candle["open"])
    close = float(candle["close"])
    low = float(candle["low"])
    ref = close if close <= open_ else open_
    if ref <= 0:
        return 0.0
    return max(0.0, ref - low) / ref


def _volume_ratio(volumes: list[float]) -> float:
    if len(volumes) < 2:
        return 1.0
    baseline = sum(volumes[:-1]) / max(1, len(volumes) - 1)
    if baseline <= 0:
        return 1.0
    return volumes[-1] / baseline


def _realized_vol(closes: list[float]) -> float:
    returns = _returns(closes)
    if len(returns) < 2:
        return 0.0
    return float(statistics.stdev(returns))


def _return_autocorr(closes: list[float]) -> float:
    returns = _returns(closes)
    n = len(returns)
    if n < 3:
        return 0.0
    mean_r = sum(returns) / n
    var = sum((r - mean_r) ** 2 for r in returns) / n
    if var <= 0:
        return 0.0
    cov = sum((returns[i] - mean_r) * (returns[i - 1] - mean_r) for i in range(1, n)) / (n - 1)
    return cov / var


def _returns(closes: list[float]) -> list[float]:
    values: list[float] = []
    for idx in range(1, len(closes)):
        prev = closes[idx - 1]
        curr = closes[idx]
        if prev <= 0:
            continue
        values.append(curr / prev - 1.0)
    return values


def _prob_direction(prob_up: float, epsilon: float) -> str:
    if prob_up > 0.5 + epsilon:
        return "UP"
    if prob_up < 0.5 - epsilon:
        return "DOWN"
    return "FLAT"
