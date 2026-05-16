from types import SimpleNamespace
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from live_prior import evaluate_prior_gate


class _FakePrior:
    def __init__(self, prob_up):
        self.prob_up = prob_up

    def predict(self, _context):
        return SimpleNamespace(prob_up=self.prob_up)


def test_prior_gate_passes_when_direction_and_edge_agree():
    result = evaluate_prior_gate(
        model=_FakePrior(0.62),
        context=object(),
        predicted_direction="UP",
        min_edge=0.01,
    )

    assert result.passed is True
    assert result.direction == "UP"
    assert result.reason == "prior_passed"


def test_prior_gate_fails_closed_when_missing():
    result = evaluate_prior_gate(
        model=None,
        context=object(),
        predicted_direction="UP",
        min_edge=0.01,
    )

    assert result.passed is False
    assert result.reason == "prior_missing"


def test_prior_gate_blocks_disagreement():
    result = evaluate_prior_gate(
        model=_FakePrior(0.38),
        context=object(),
        predicted_direction="UP",
        min_edge=0.01,
    )

    assert result.passed is False
    assert result.reason == "prior_direction_disagreement"


def test_live_prior_artifact_roundtrip(tmp_path):
    from live_prior import load_prior_artifact, save_prior_artifact

    model = _FakePrior(0.62)
    path = tmp_path / "prior.pkl"

    save_prior_artifact(model, path, {"name": "test"})
    loaded, metadata = load_prior_artifact(path)

    assert metadata == {"name": "test"}
    assert loaded.predict(object()).prob_up == 0.62
