from __future__ import annotations

import json
import pickle
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_PATH = ROOT / "data" / "models" / "foundation_shadow.pkl"
DEFAULT_METADATA_PATH = ROOT / "data" / "models" / "foundation_shadow.json"

_ARTIFACT_CACHE: dict[str, Any] = {"path": None, "mtime": None, "payload": None}


def ensure_shadow_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS order_book_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            token_id TEXT,
            midpoint REAL,
            best_bid REAL,
            best_ask REAL,
            spread_pct REAL,
            bid_depth_5pct REAL,
            ask_depth_5pct REAL,
            depth_imbalance REAL,
            captured_at TEXT NOT NULL,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_market_time
        ON order_book_snapshots (market_id, captured_at DESC)
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_shadow_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER,
            market_id TEXT NOT NULL,
            model_family TEXT NOT NULL,
            model_name TEXT,
            status TEXT NOT NULL,
            prob_up REAL,
            primary_raw REAL,
            secondary_prob REAL,
            agreement_passed INTEGER,
            candidate_direction TEXT,
            final_direction TEXT,
            direction_match INTEGER,
            diagnostics_json TEXT,
            artifact_path TEXT,
            artifact_updated_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (prediction_id) REFERENCES predictions(id),
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prediction_shadow_models_market_time
        ON prediction_shadow_models (market_id, created_at DESC)
        """
    )
    db.commit()


def fetch_order_book_snapshot(token_id: str | None) -> dict[str, Any] | None:
    if not token_id:
        return None
    from v3.data_fetch import fetch_clob_book

    return fetch_clob_book(token_id)


def store_order_book_snapshot(
    db: sqlite3.Connection,
    *,
    market_id: str,
    token_id: str | None,
    book: dict[str, Any] | None,
) -> None:
    if not book:
        return
    db.execute(
        """
        INSERT INTO order_book_snapshots
        (market_id, token_id, midpoint, best_bid, best_ask, spread_pct,
         bid_depth_5pct, ask_depth_5pct, depth_imbalance, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_id,
            token_id,
            _to_float(book.get("midpoint")),
            _to_float(book.get("best_bid")),
            _to_float(book.get("best_ask")),
            _to_float(book.get("spread_pct")),
            _to_float(book.get("bid_depth_5pct")),
            _to_float(book.get("ask_depth_5pct")),
            _to_float(book.get("depth_imbalance")),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def load_foundation_artifact(path: Path = DEFAULT_ARTIFACT_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    if _ARTIFACT_CACHE["path"] == str(path) and _ARTIFACT_CACHE["mtime"] == mtime:
        return _ARTIFACT_CACHE["payload"]
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    _ARTIFACT_CACHE.update({"path": str(path), "mtime": mtime, "payload": payload})
    return payload


def foundation_shadow_prediction(
    *,
    candles: list[dict[str, Any]],
    regime: dict[str, Any],
    candidate_signal: dict[str, Any] | None,
    final_signal: dict[str, Any],
    artifact_path: Path = DEFAULT_ARTIFACT_PATH,
) -> dict[str, Any]:
    payload = load_foundation_artifact(artifact_path)
    if payload is None:
        return {
            "status": "model_not_loaded",
            "model_family": "foundation_shadow",
            "diagnostics": {"reason": "artifact_missing"},
            "artifact_path": str(artifact_path),
        }

    service = payload.get("service")
    metadata = payload.get("metadata", {})
    if service is None or not getattr(service, "is_trained", False):
        return {
            "status": "model_not_trained",
            "model_family": "foundation_shadow",
            "diagnostics": {"reason": "service_untrained"},
            "artifact_path": str(artifact_path),
            "artifact_updated_at": metadata.get("trained_at"),
        }

    context = SimpleNamespace(
        formatted_candles=candles,
        production_regime=regime,
        market={},
    )
    prediction = service.predict(
        context,
        candidate_signal=candidate_signal,
        require_agreement=False,
    )
    final_direction = str(final_signal.get("direction") or "SKIP")
    candidate_direction = str((candidate_signal or {}).get("direction") or "SKIP")
    model_direction = "UP" if prediction.prob_up > 0.5 else "DOWN" if prediction.prob_up < 0.5 else "SKIP"
    direction_match = final_direction != "SKIP" and final_direction == model_direction
    diagnostics = dict(prediction.diagnostics)
    diagnostics.update(
        {
            "model_direction": model_direction,
            "final_strategy": str(final_signal.get("strategy_name", "")),
            "candidate_strategy": str((candidate_signal or {}).get("strategy_name", "")),
        }
    )
    return {
        "status": "ok",
        "model_family": "foundation_shadow",
        "model_name": prediction.model_name,
        "prob_up": float(prediction.prob_up),
        "primary_raw": _to_float(diagnostics.get("primary_raw")),
        "secondary_prob": _to_float(diagnostics.get("secondary_prob")),
        "agreement_passed": bool(prediction.agreement_passed),
        "candidate_direction": candidate_direction,
        "final_direction": final_direction,
        "direction_match": direction_match,
        "diagnostics": diagnostics,
        "artifact_path": str(artifact_path),
        "artifact_updated_at": metadata.get("trained_at"),
    }


def store_shadow_prediction(
    db: sqlite3.Connection,
    *,
    prediction_id: int | None,
    market_id: str,
    shadow: dict[str, Any],
) -> None:
    diagnostics = shadow.get("diagnostics", {})
    db.execute(
        """
        INSERT INTO prediction_shadow_models
        (prediction_id, market_id, model_family, model_name, status, prob_up,
         primary_raw, secondary_prob, agreement_passed, candidate_direction,
         final_direction, direction_match, diagnostics_json, artifact_path,
         artifact_updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            market_id,
            str(shadow.get("model_family", "foundation_shadow")),
            shadow.get("model_name"),
            str(shadow.get("status", "unknown")),
            _to_float(shadow.get("prob_up")),
            _to_float(shadow.get("primary_raw")),
            _to_float(shadow.get("secondary_prob")),
            _to_int_bool(shadow.get("agreement_passed")),
            shadow.get("candidate_direction"),
            shadow.get("final_direction"),
            _to_int_bool(shadow.get("direction_match")),
            json.dumps(diagnostics, sort_keys=True),
            shadow.get("artifact_path"),
            shadow.get("artifact_updated_at"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0
