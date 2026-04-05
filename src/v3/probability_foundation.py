from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProbabilityPrediction:
    prob_up: float
    model_name: str
    calibrated: bool
    agreement_passed: bool
    diagnostics: dict[str, float | int | str]
