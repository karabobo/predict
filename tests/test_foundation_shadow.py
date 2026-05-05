import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from foundation_shadow import (
    ensure_shadow_schema,
    foundation_shadow_prediction,
    store_order_book_snapshot,
    store_shadow_prediction,
)


def test_shadow_schema_and_writes_do_not_touch_prediction_decision():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE markets (id TEXT PRIMARY KEY)")
    db.execute(
        """
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            estimate REAL,
            should_trade INTEGER,
            model_version TEXT
        )
        """
    )
    ensure_shadow_schema(db)
    db.execute(
        "INSERT INTO markets (id) VALUES ('m1')"
    )
    db.execute(
        "INSERT INTO predictions (market_id, estimate, should_trade, model_version) VALUES ('m1', 0.62, 1, 'alpha_router')"
    )
    prediction_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    store_order_book_snapshot(
        db,
        market_id="m1",
        token_id="token",
        book={
            "midpoint": 0.51,
            "best_bid": 0.50,
            "best_ask": 0.52,
            "spread_pct": 0.0392,
            "bid_depth_5pct": 1000,
            "ask_depth_5pct": 800,
            "depth_imbalance": 0.1111,
        },
    )
    store_shadow_prediction(
        db,
        prediction_id=prediction_id,
        market_id="m1",
        shadow={
            "status": "ok",
            "model_family": "foundation_shadow",
            "model_name": "xgboost_primary",
            "prob_up": 0.57,
            "primary_raw": 0.56,
            "secondary_prob": 0.55,
            "agreement_passed": True,
            "candidate_direction": "UP",
            "final_direction": "UP",
            "direction_match": True,
            "diagnostics": {"backend": "xgboost"},
        },
    )

    prediction = db.execute("SELECT estimate, should_trade, model_version FROM predictions").fetchone()
    assert prediction == (0.62, 1, "alpha_router")
    assert db.execute("SELECT COUNT(*) FROM order_book_snapshots").fetchone()[0] == 1
    shadow = db.execute("SELECT prob_up, direction_match FROM prediction_shadow_models").fetchone()
    assert shadow == (0.57, 1)


def test_missing_foundation_artifact_returns_safe_shadow(tmp_path):
    result = foundation_shadow_prediction(
        candles=[],
        regime={"label": "LOW_VOL / NEUTRAL"},
        candidate_signal=None,
        final_signal={"direction": "SKIP"},
        artifact_path=tmp_path / "missing.pkl",
    )

    assert result["status"] == "model_not_loaded"
    assert result["diagnostics"]["reason"] == "artifact_missing"
