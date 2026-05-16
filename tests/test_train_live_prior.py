import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@dataclass(frozen=True)
class _Context:
    market: dict
    formatted_candles: list[dict]
    production_regime: dict


def test_train_live_prior_writes_artifact(monkeypatch, tmp_path):
    from train_live_prior import train_live_prior
    from live_prior import load_prior_artifact

    contexts = [
        _Context(
            market={"outcome": idx % 2, "timestamp": idx},
            formatted_candles=[
                {
                    "open": 100.0 + idx,
                    "high": 101.0 + idx,
                    "low": 99.0 + idx,
                    "close": 100.5 + idx,
                    "volume": 10.0 + idx,
                }
                for _ in range(20)
            ],
            production_regime={"label": "LOW_VOL / NEUTRAL"},
        )
        for idx in range(80)
    ]

    monkeypatch.setattr("train_live_prior.build_rolling_contexts", lambda **_kwargs: (contexts, {"market_rows": 80}))

    output = tmp_path / "ensemble.pkl"
    metadata = train_live_prior(output=output)
    model, loaded_metadata = load_prior_artifact(output)

    assert output.exists()
    assert metadata["contexts"] == 80
    assert loaded_metadata["contexts"] == 80
    assert model.is_trained is True
