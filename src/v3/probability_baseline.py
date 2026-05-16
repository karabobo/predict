from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.v3.probability_foundation import (
    FoundationTrainingSummary,
    Paper5MModelService,
    ProbabilityPrediction,
)


DEFAULT_BASELINE_ENSEMBLE = "ensemble_logreg_window"


@dataclass(frozen=True)
class EnsembleMemberSpec:
    name: str
    weight: float
    model_kind: str
    feature_set: str
    use_calibration: bool


@dataclass(frozen=True)
class EnsembleTrainingSummary:
    name: str
    train_samples: int
    calibration_samples: int
    members: tuple[dict[str, Any], ...]


def baseline_ensemble_specs(name: str = DEFAULT_BASELINE_ENSEMBLE) -> tuple[EnsembleMemberSpec, ...]:
    if name == "ensemble_logreg_window":
        return (
            EnsembleMemberSpec(
                name="paper_logreg_5m_window",
                weight=1.00,
                model_kind="logreg",
                feature_set="window",
                use_calibration=False,
            ),
        )
    if name == "ensemble_logreg_raw_xgb":
        return (
            EnsembleMemberSpec(
                name="paper_logreg_5m_raw",
                weight=0.60,
                model_kind="logreg",
                feature_set="raw",
                use_calibration=False,
            ),
            EnsembleMemberSpec(
                name="paper_xgb_5m",
                weight=0.40,
                model_kind="xgboost",
                feature_set="derived",
                use_calibration=True,
            ),
        )
    raise ValueError(f"Unknown baseline probability ensemble: {name}")


class BaselineProbabilityEnsemble:
    def __init__(self, name: str = DEFAULT_BASELINE_ENSEMBLE):
        self.name = name
        self.specs = baseline_ensemble_specs(name)
        total_weight = sum(spec.weight for spec in self.specs)
        if total_weight <= 0:
            raise ValueError("Ensemble weights must sum to a positive value")
        self._normalized_specs = tuple(
            EnsembleMemberSpec(
                name=spec.name,
                weight=spec.weight / total_weight,
                model_kind=spec.model_kind,
                feature_set=spec.feature_set,
                use_calibration=spec.use_calibration,
            )
            for spec in self.specs
        )
        self._members: list[tuple[EnsembleMemberSpec, Paper5MModelService, FoundationTrainingSummary]] = []
        self._summary: EnsembleTrainingSummary | None = None

    @property
    def is_trained(self) -> bool:
        return bool(self._members) and all(service.is_trained for _, service, _ in self._members)

    @property
    def summary(self) -> EnsembleTrainingSummary | None:
        return self._summary

    def fit(self, contexts: list[Any]) -> EnsembleTrainingSummary:
        members: list[tuple[EnsembleMemberSpec, Paper5MModelService, FoundationTrainingSummary]] = []
        member_summaries: list[dict[str, Any]] = []
        for spec in self._normalized_specs:
            service = Paper5MModelService(
                model_kind=spec.model_kind,
                feature_set=spec.feature_set,
                use_calibration=spec.use_calibration,
            )
            summary = service.fit(contexts)
            members.append((spec, service, summary))
            member_summaries.append(
                {
                    "name": spec.name,
                    "weight": spec.weight,
                    "model_kind": spec.model_kind,
                    "feature_set": spec.feature_set,
                    "use_calibration": spec.use_calibration,
                    "train_samples": summary.train_samples,
                    "calibration_samples": summary.calibration_samples,
                    "model_name": summary.primary_model_name,
                    "calibrated": summary.calibrated,
                    "diagnostics": dict(summary.diagnostics),
                }
            )
        self._members = members
        self._summary = EnsembleTrainingSummary(
            name=self.name,
            train_samples=min((summary.train_samples for _, _, summary in members), default=0),
            calibration_samples=sum(summary.calibration_samples for _, _, summary in members),
            members=tuple(member_summaries),
        )
        return self._summary

    def predict(self, context: Any) -> ProbabilityPrediction:
        if not self.is_trained:
            return ProbabilityPrediction(
                prob_up=0.5,
                model_name=self.name,
                calibrated=False,
                agreement_passed=False,
                diagnostics={"reason": "ensemble_not_trained"},
            )

        weighted_prob = 0.0
        diagnostics: dict[str, float | int | str] = {}
        calibrated_count = 0
        for spec, service, _ in self._members:
            prediction = service.predict(context)
            weighted_prob += float(prediction.prob_up) * spec.weight
            diagnostics[f"{spec.name}.prob_up"] = round(float(prediction.prob_up), 6)
            diagnostics[f"{spec.name}.weight"] = round(float(spec.weight), 6)
            if prediction.calibrated:
                calibrated_count += 1

        prob_up = max(0.0, min(1.0, weighted_prob))
        diagnostics["edge"] = round(prob_up - 0.5, 6)
        diagnostics["direction"] = "UP" if prob_up > 0.5 else "DOWN" if prob_up < 0.5 else "FLAT"
        return ProbabilityPrediction(
            prob_up=prob_up,
            model_name=self.name,
            calibrated=calibrated_count > 0,
            agreement_passed=False,
            diagnostics=diagnostics,
        )
