import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_predictions_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            category TEXT,
            end_date TEXT,
            volume REAL,
            price_yes REAL,
            price_no REAL,
            fetched_at TEXT,
            resolved INTEGER DEFAULT 0,
            outcome INTEGER DEFAULT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            agent TEXT,
            estimate REAL,
            edge REAL,
            confidence TEXT,
            reasoning TEXT,
            predicted_at TEXT,
            cycle INTEGER,
            conviction_score TEXT,
            regime TEXT,
            should_trade INTEGER,
            market_price_yes_snapshot REAL,
            seconds_to_expiry INTEGER
        )
        """
    )
    return db


def _insert_prediction(
    db: sqlite3.Connection,
    *,
    market_id: str,
    predicted_at: str,
    estimate: float,
    conviction_score: int,
    should_trade: int,
    regime: str,
    reasoning: str,
    market_price_yes_snapshot: float,
    seconds_to_expiry: int,
) -> None:
    db.execute(
        """
        INSERT INTO predictions (
            market_id, agent, estimate, edge, confidence, reasoning, predicted_at, cycle,
            conviction_score, regime, should_trade, market_price_yes_snapshot, seconds_to_expiry
        )
        VALUES (?, 'contrarian_rule', ?, ?, 'medium', ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            market_id,
            estimate,
            abs(estimate - 0.5),
            reasoning,
            predicted_at,
            conviction_score,
            regime,
            should_trade,
            market_price_yes_snapshot,
            seconds_to_expiry,
        ),
    )


def test_build_market_audit_summary_routes_skip_and_toxicity_coaches():
    from fetch_markets import ensure_market_schema
    from v3.coaches import SKIP_COACH, TOXICITY_COACH, build_market_audit_summary

    db = _make_predictions_db()
    ensure_market_schema(db)
    db.execute(
        """
        INSERT INTO markets (id, question, category, end_date, volume, price_yes, price_no, fetched_at, resolved, outcome)
        VALUES
        ('skip_m', 'skip market', 'crypto', '2026-04-03T08:00:00+00:00', 1000, 0.52, 0.48, '2026-04-03T07:55:00+00:00', 1, 1),
        ('trade_m', 'trade market', 'crypto', '2026-04-03T08:05:00+00:00', 1000, 0.61, 0.39, '2026-04-03T08:00:00+00:00', 1, 0)
        """
    )
    _insert_prediction(
        db,
        market_id="skip_m",
        predicted_at="2026-04-03T07:56:00+00:00",
        estimate=0.50,
        conviction_score=0,
        should_trade=0,
        regime="LOW_VOL / NEUTRAL",
        reasoning="streak_too_short | regime_filter",
        market_price_yes_snapshot=0.49,
        seconds_to_expiry=240,
    )
    _insert_prediction(
        db,
        market_id="trade_m",
        predicted_at="2026-04-03T08:01:00+00:00",
        estimate=0.38,
        conviction_score=3,
        should_trade=1,
        regime="HIGH_VOL / NEUTRAL",
        reasoning="streak_3 | direction_down | compression",
        market_price_yes_snapshot=0.47,
        seconds_to_expiry=210,
    )
    _insert_prediction(
        db,
        market_id="trade_m",
        predicted_at="2026-04-03T08:04:30+00:00",
        estimate=0.50,
        conviction_score=0,
        should_trade=0,
        regime="HIGH_VOL / NEUTRAL",
        reasoning="later skip",
        market_price_yes_snapshot=0.63,
        seconds_to_expiry=30,
    )
    db.commit()

    skip_summary = build_market_audit_summary(db, "skip_m")
    trade_summary = build_market_audit_summary(db, "trade_m")

    assert skip_summary["coach_type"] == SKIP_COACH
    assert skip_summary["baseline"]["action"] == "skip"
    assert trade_summary["coach_type"] == TOXICITY_COACH
    assert trade_summary["baseline"]["action"] == "trade"
    assert trade_summary["path"]["trade_then_skip"] is True


def test_run_coach_audits_upgrades_provisional_row_to_official():
    from fetch_markets import ensure_market_schema
    from v3.coaches import ensure_schema, run_coach_audits

    predictions_db = _make_predictions_db()
    ensure_market_schema(predictions_db)
    research_db = sqlite3.connect(":memory:")
    research_db.row_factory = sqlite3.Row
    ensure_schema(research_db)
    predictions_db.execute(
        """
        INSERT INTO markets (
            id, question, category, end_date, volume, price_yes, price_no, fetched_at,
            resolved, outcome, provisional_outcome, provisional_resolved_at, provisional_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "m1",
            "BTC Up or Down",
            "crypto",
            "2026-04-03T08:00:00+00:00",
            1000,
            0.51,
            0.49,
            "2026-04-03T07:59:00+00:00",
            0,
            None,
            1,
            "2026-04-03T08:01:00+00:00",
            "gamma_outcome_prices:0.99x2",
        ),
    )
    _insert_prediction(
        predictions_db,
        market_id="m1",
        predicted_at="2026-04-03T07:56:00+00:00",
        estimate=0.50,
        conviction_score=0,
        should_trade=0,
        regime="LOW_VOL / NEUTRAL",
        reasoning="baseline skip",
        market_price_yes_snapshot=0.49,
        seconds_to_expiry=240,
    )
    predictions_db.commit()

    def judge(_coach_type, _summary):
        return {
            "verdict": "missed_trade_up",
            "confidence": 4,
            "reasoning": "baseline should have traded",
            "tags": ["allow_low_vol_neutral"],
            "verdict_direction": "UP",
        }

    provisional = run_coach_audits(predictions_db, research_db, judge=judge)
    assert provisional["audited"] == 1
    row = research_db.execute(
        "SELECT resolution_scope, helpful, harmful FROM coach_audits WHERE market_id = 'm1'"
    ).fetchone()
    assert row["resolution_scope"] == "provisional"
    assert row["helpful"] == 1
    assert row["harmful"] == 0

    predictions_db.execute(
        """
        UPDATE markets
        SET resolved = 1,
            outcome = 1,
            official_resolved_at = '2026-04-03T08:02:00+00:00'
        WHERE id = 'm1'
        """
    )
    predictions_db.commit()

    official = run_coach_audits(predictions_db, research_db, judge=judge)
    assert official["audited"] == 1
    row = research_db.execute(
        "SELECT resolution_scope, outcome FROM coach_audits WHERE market_id = 'm1'"
    ).fetchone()
    assert row["resolution_scope"] == "official"
    assert row["outcome"] == 1


def test_refresh_candidate_rollups_marks_eligible_tags():
    from v3.coaches import ensure_schema, refresh_candidate_rollups

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    base_time = datetime.now(timezone.utc) - timedelta(hours=1)
    for idx in range(5):
        audited_at = (base_time + timedelta(minutes=idx)).isoformat()
        db.execute(
            """
            INSERT INTO coach_audits (
                market_id, coach_model, coach_type, baseline_agent, baseline_action,
                baseline_direction, baseline_reason, baseline_trade_won, regime,
                market_question, market_end_date, outcome, resolution_scope, outcome_source,
                verdict, verdict_direction, confidence, rationale, helpful, harmful, audited_at
            )
            VALUES (?, 'deepseek-ai/DeepSeek-V3', 'skip_coach', 'contrarian_rule', 'skip',
                    'SKIP', 'baseline skip', NULL, 'LOW_VOL / NEUTRAL',
                    'q', '2026-04-03T08:00:00+00:00', 1, 'official', 'official',
                    'missed_trade_up', 'UP', 4, 'reason', ?, ?, ?)
            """,
            (
                    f"m{idx}",
                    1 if idx < 4 else 0,
                    0 if idx < 4 else 1,
                    audited_at,
                ),
            )
        audit_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            """
            INSERT INTO coach_audit_tags (
                audit_id, market_id, coach_model, coach_type, tag, regime,
                resolution_scope, helpful, harmful, audited_at
            )
            VALUES (?, ?, 'deepseek-ai/DeepSeek-V3', 'skip_coach', 'allow_low_vol_neutral',
                    'LOW_VOL / NEUTRAL', 'official', ?, ?, ?)
            """,
            (
                audit_id,
                f"m{idx}",
                1 if idx < 4 else 0,
                0 if idx < 4 else 1,
                audited_at,
            ),
        )
    db.commit()

    rows = refresh_candidate_rollups(db, days=7)
    assert rows
    top = rows[0]
    assert top["tag"] == "allow_low_vol_neutral"
    assert top["eligible_for_ablation"] == 1
    assert top["support_count"] == 5
    assert round(top["precision"], 2) == 0.80
    assert top["net_helpful"] == 3

    spec = db.execute(
        """
        SELECT spec_name, spec_label, family, target_scope, eligible_for_ablation
        FROM coach_rule_candidate_specs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert spec is not None
    assert spec["spec_name"] == "skip_coach__allow_low_vol_neutral__low_vol_neutral"
    assert spec["family"] == "regime_allow"
    assert spec["target_scope"] == "LOW_VOL / NEUTRAL"
    assert spec["eligible_for_ablation"] == 1
