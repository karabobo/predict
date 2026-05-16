"""
train_live_prior.py - train and save the live ensemble prior artifact.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_prior import DEFAULT_PRIOR_ARTIFACT, save_prior_artifact
from v3.foundation_shadow_rolling import DEFAULT_CANDLES, DEFAULT_DB, build_rolling_contexts
from v3.probability_baseline import BaselineProbabilityEnsemble, DEFAULT_BASELINE_ENSEMBLE


def train_live_prior(
    *,
    db_path: Path = DEFAULT_DB,
    btc_candles_path: Path = DEFAULT_CANDLES,
    output: Path = DEFAULT_PRIOR_ARTIFACT,
    lookback: int = 20,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    contexts, data_stats = build_rolling_contexts(
        db_path=db_path,
        btc_candles_path=btc_candles_path,
        from_date=from_date,
        to_date=to_date,
        lookback=lookback,
    )
    if not contexts:
        raise ValueError("No eligible contexts found for live prior training")
    model = BaselineProbabilityEnsemble(DEFAULT_BASELINE_ENSEMBLE)
    summary = model.fit(contexts)
    metadata = {
        "model_name": DEFAULT_BASELINE_ENSEMBLE,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "btc_candles_path": str(btc_candles_path),
        "output": str(output),
        "lookback": lookback,
        "from_date": from_date,
        "to_date": to_date,
        "contexts": len(contexts),
        "data_stats": data_stats,
        "summary": asdict(summary),
    }
    save_prior_artifact(model, output, metadata)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and save ensemble_logreg_raw_xgb live prior artifact.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES)
    parser.add_argument("--output", type=Path, default=DEFAULT_PRIOR_ARTIFACT)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    args = parser.parse_args()

    metadata = train_live_prior(
        db_path=args.db,
        btc_candles_path=args.btc_candles,
        output=args.output,
        lookback=args.lookback,
        from_date=args.from_date,
        to_date=args.to_date,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
