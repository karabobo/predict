"""
live_prior.py - artifact-backed probability gate for realtime/paper execution.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PRIOR_ARTIFACT = Path(__file__).parent.parent / "data" / "models" / "baseline_prior.pkl"


@dataclass(frozen=True)
class PriorGateResult:
    passed: bool
    prob_up: float | None
    direction: str | None
    edge: float | None
    reason: str


def save_prior_artifact(model: Any, path: Path = DEFAULT_PRIOR_ARTIFACT, metadata: dict[str, Any] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pickle.dump({"model": model, "metadata": dict(metadata or {})}, handle)
    return path


def load_prior_artifact(path: Path = DEFAULT_PRIOR_ARTIFACT) -> tuple[Any | None, dict[str, Any]]:
    if not path.exists():
        return None, {"reason": "artifact_missing", "path": str(path)}
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    if isinstance(payload, dict) and "model" in payload:
        return payload["model"], dict(payload.get("metadata") or {})
    return payload, {}


def evaluate_prior_gate(
    *,
    model: Any | None,
    context: Any,
    predicted_direction: str | None,
    min_edge: float = 0.01,
) -> PriorGateResult:
    if model is None:
        return PriorGateResult(False, None, None, None, "prior_missing")
    if not predicted_direction:
        return PriorGateResult(False, None, None, None, "missing_predicted_direction")
    prediction = model.predict(context)
    prob_up = float(prediction.prob_up)
    direction = "UP" if prob_up > 0.5 else "DOWN" if prob_up < 0.5 else "FLAT"
    edge = abs(prob_up - 0.5)
    normalized = str(predicted_direction).upper()
    passed = direction == normalized and edge >= min_edge
    if not passed and direction != normalized:
        reason = "prior_direction_disagreement"
    elif not passed:
        reason = "prior_edge_below_threshold"
    else:
        reason = "prior_passed"
    return PriorGateResult(passed, prob_up, direction, edge, reason)
