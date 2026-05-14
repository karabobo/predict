"""
coaches.py — Parallel research-coach audits for the production baseline.

These audits do not alter live decisions. They evaluate ended markets after the
fact and surface structured rule-candidate tags for later deterministic testing.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:  # pragma: no cover - import path depends on entrypoint style
    from src import prompts
    from src.ai_client import client as ai_client
    from src.metrics import prediction_direction, trade_eligible
    from src.v3.config import RESEARCH_DB_NAME
except ModuleNotFoundError:  # pragma: no cover - direct `python src/dashboard.py`
    import prompts  # type: ignore[no-redef]
    from ai_client import client as ai_client  # type: ignore[no-redef]
    from metrics import prediction_direction, trade_eligible  # type: ignore[no-redef]
    from v3.config import RESEARCH_DB_NAME  # type: ignore[no-redef]

BASELINE_AGENT = "contrarian_rule"
DEFAULT_COACH_MODEL = os.getenv("RESEARCH_COACH_MODEL", "deepseek-ai/DeepSeek-V3")
COACH_AUDIT_LIMIT = int(os.getenv("COACH_AUDIT_LIMIT", "6"))
COACH_MODEL_TIMEOUT_SECONDS = int(os.getenv("MODEL_TIMEOUT_SECONDS", "120"))
DB_PATH = Path(__file__).parent.parent.parent / "data" / RESEARCH_DB_NAME

SKIP_COACH = "skip_coach"
TOXICITY_COACH = "toxicity_coach"

SKIP_VERDICTS = {"correct_skip", "missed_trade_up", "missed_trade_down"}
TOXICITY_VERDICTS = {"valid_trade", "toxic_trade"}
SKIP_TAGS = {
    "loosen_streak_threshold",
    "allow_low_vol_neutral",
    "allow_trending_volume_spike",
    "allow_compression_continuation",
    "raise_trending_conviction",
}
TOXICITY_TAGS = {
    "block_high_vol_neutral",
    "tighten_mean_reverting",
    "require_stronger_volume_confirmation",
    "raise_conviction_threshold",
    "block_late_reversal",
}

RULE_CANDIDATE_TEMPLATES: dict[tuple[str, str], dict[str, str]] = {
    (SKIP_COACH, "loosen_streak_threshold"): {
        "family": "threshold_loosen",
        "label": "Loosen streak threshold",
        "action": "loosen_streak_threshold",
    },
    (SKIP_COACH, "allow_low_vol_neutral"): {
        "family": "regime_allow",
        "label": "Allow baseline in observed regime",
        "action": "allow_regime",
    },
    (SKIP_COACH, "allow_trending_volume_spike"): {
        "family": "feature_allow",
        "label": "Allow volume-spike continuation",
        "action": "allow_volume_spike_continuation",
    },
    (SKIP_COACH, "allow_compression_continuation"): {
        "family": "feature_allow",
        "label": "Allow compression continuation",
        "action": "allow_compression_continuation",
    },
    (SKIP_COACH, "raise_trending_conviction"): {
        "family": "conviction_raise",
        "label": "Raise conviction in observed regime",
        "action": "raise_conviction",
    },
    (TOXICITY_COACH, "block_high_vol_neutral"): {
        "family": "regime_block",
        "label": "Block baseline in observed regime",
        "action": "block_regime",
    },
    (TOXICITY_COACH, "tighten_mean_reverting"): {
        "family": "regime_tighten",
        "label": "Tighten mean-reverting branch",
        "action": "tighten_regime",
    },
    (TOXICITY_COACH, "require_stronger_volume_confirmation"): {
        "family": "feature_confirm",
        "label": "Require stronger volume confirmation",
        "action": "raise_volume_confirmation",
    },
    (TOXICITY_COACH, "raise_conviction_threshold"): {
        "family": "conviction_raise",
        "label": "Raise conviction threshold",
        "action": "raise_conviction",
    },
    (TOXICITY_COACH, "block_late_reversal"): {
        "family": "pattern_block",
        "label": "Block late reversal pattern",
        "action": "block_late_reversal",
    },
}


@dataclass(frozen=True)
class CoachAuditCandidate:
    market_id: str
    coach_type: str
    resolution_scope: str
    resolution_at: str
    summary: dict[str, Any]


class BaseCoachModel:
    model_name = "base"

    def audit(self, coach_type: str, summary: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class DeepSeekCoachModel(BaseCoachModel):
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or DEFAULT_COACH_MODEL

    def audit(self, coach_type: str, summary: dict[str, Any]) -> dict[str, Any]:
        if coach_type == SKIP_COACH:
            result = ai_client.predict(
                self.model_name,
                prompts.SKIP_COACH_SYSTEM_PROMPT,
                prompts.build_skip_coach_user_prompt(summary),
                coach_mode=True,
            )
            return normalize_coach_result(coach_type, result)
        if coach_type == TOXICITY_COACH:
            result = ai_client.predict(
                self.model_name,
                prompts.TOXICITY_COACH_SYSTEM_PROMPT,
                prompts.build_toxicity_coach_user_prompt(summary),
                coach_mode=True,
            )
            return normalize_coach_result(coach_type, result)
        raise ValueError(f"Unknown coach type: {coach_type}")


def build_default_coach_model() -> BaseCoachModel:
    return DeepSeekCoachModel()


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS coach_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            coach_model TEXT NOT NULL,
            coach_type TEXT NOT NULL,
            baseline_agent TEXT NOT NULL,
            baseline_action TEXT NOT NULL,
            baseline_direction TEXT,
            baseline_reason TEXT,
            baseline_trade_won INTEGER,
            regime TEXT,
            market_question TEXT,
            market_end_date TEXT,
            outcome INTEGER NOT NULL,
            resolution_scope TEXT NOT NULL,
            outcome_source TEXT,
            verdict TEXT NOT NULL,
            verdict_direction TEXT,
            confidence INTEGER NOT NULL DEFAULT 0,
            rationale TEXT,
            helpful INTEGER NOT NULL DEFAULT 0,
            harmful INTEGER NOT NULL DEFAULT 0,
            audited_at TEXT NOT NULL,
            UNIQUE (market_id, coach_model, coach_type)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS coach_audit_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_id INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            coach_model TEXT NOT NULL,
            coach_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            regime TEXT,
            resolution_scope TEXT NOT NULL,
            helpful INTEGER NOT NULL DEFAULT 0,
            harmful INTEGER NOT NULL DEFAULT 0,
            audited_at TEXT NOT NULL,
            UNIQUE (audit_id, tag),
            FOREIGN KEY (audit_id) REFERENCES coach_audits(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS coach_candidate_rollups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coach_model TEXT NOT NULL,
            coach_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            regime TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            support_count INTEGER NOT NULL,
            helpful_count INTEGER NOT NULL,
            harmful_count INTEGER NOT NULL,
            precision REAL NOT NULL,
            net_helpful INTEGER NOT NULL,
            eligible_for_ablation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE (coach_model, coach_type, tag, regime, window_start, window_end)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS coach_rule_candidate_specs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coach_model TEXT NOT NULL,
            coach_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            regime TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            spec_name TEXT NOT NULL,
            spec_label TEXT NOT NULL,
            family TEXT NOT NULL,
            target_scope TEXT NOT NULL,
            template_action TEXT NOT NULL,
            implementation_hint TEXT,
            config_json TEXT NOT NULL,
            support_count INTEGER NOT NULL,
            helpful_count INTEGER NOT NULL,
            harmful_count INTEGER NOT NULL,
            precision REAL NOT NULL,
            net_helpful INTEGER NOT NULL,
            eligible_for_ablation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE (coach_model, coach_type, tag, regime, window_start, window_end, spec_name)
        )
        """
    )
    db.commit()


def normalize_coach_result(coach_type: str, result: dict[str, Any] | None) -> dict[str, Any]:
    payload = result or {}
    error_text = str(payload.get("error", "")).strip()
    if error_text:
        return {
            "verdict": None,
            "confidence": 0,
            "reasoning": f"error:{error_text[:180]}",
            "tags": [],
            "verdict_direction": None,
            "error": error_text,
        }
    if coach_type == SKIP_COACH:
        default_verdict = "correct_skip"
        valid_verdicts = SKIP_VERDICTS
        valid_tags = SKIP_TAGS
    elif coach_type == TOXICITY_COACH:
        default_verdict = "valid_trade"
        valid_verdicts = TOXICITY_VERDICTS
        valid_tags = TOXICITY_TAGS
    else:
        raise ValueError(f"Unknown coach type: {coach_type}")

    verdict = str(payload.get("verdict") or default_verdict).strip().lower()
    if verdict not in valid_verdicts:
        verdict = default_verdict

    confidence_raw = payload.get("confidence", 0)
    try:
        confidence = max(0, min(5, int(float(confidence_raw))))
    except (TypeError, ValueError):
        confidence = 0

    reasoning = str(payload.get("reasoning") or payload.get("reason") or "").strip()
    if not reasoning:
        reasoning = "n/a"

    raw_tags = payload.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags = []
    for tag in raw_tags if isinstance(raw_tags, list) else []:
        tag_value = str(tag).strip()
        if tag_value in valid_tags and tag_value not in tags:
            tags.append(tag_value)

    if verdict in {"correct_skip", "valid_trade"}:
        tags = []

    verdict_direction = None
    if verdict.endswith("_up"):
        verdict_direction = "UP"
    elif verdict.endswith("_down"):
        verdict_direction = "DOWN"

    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": reasoning[:240],
        "tags": tags,
        "verdict_direction": verdict_direction,
    }


def load_audit_candidates(
    predictions_db: sqlite3.Connection,
    research_db: sqlite3.Connection,
    *,
    include_provisional: bool = True,
    limit: int = COACH_AUDIT_LIMIT,
    coach_model: str = DEFAULT_COACH_MODEL,
) -> list[CoachAuditCandidate]:
    predictions_db.row_factory = sqlite3.Row
    research_db.row_factory = sqlite3.Row
    where_clause = "m.resolved = 1"
    if include_provisional:
        where_clause = "(m.resolved = 1 OR m.provisional_outcome IS NOT NULL)"

    rows = predictions_db.execute(
        """
        SELECT
            m.id,
            m.end_date,
            m.resolved,
            m.outcome,
            m.provisional_outcome,
            m.official_resolved_at,
            m.provisional_resolved_at
        FROM markets m
        WHERE """
        + where_clause
        + """
          AND EXISTS (
              SELECT 1
              FROM predictions p
              WHERE p.market_id = m.id
                AND p.agent = ?
          )
        ORDER BY COALESCE(m.official_resolved_at, m.provisional_resolved_at, m.end_date) DESC
        LIMIT ?
        """,
        (BASELINE_AGENT, max(limit * 4, limit)),
    ).fetchall()

    candidates: list[CoachAuditCandidate] = []
    for row in rows:
        summary = build_market_audit_summary(predictions_db, str(row["id"]))
        if summary is None:
            continue
        coach_type = summary["coach_type"]
        resolution_scope = str(summary["resolution_scope"])
        resolution_at = str(summary["resolution_at"])
        if not _needs_audit(
            research_db,
            market_id=str(row["id"]),
            coach_model=coach_model,
            coach_type=coach_type,
            resolution_scope=resolution_scope,
            resolution_at=resolution_at,
        ):
            continue
        candidates.append(
            CoachAuditCandidate(
                market_id=str(row["id"]),
                coach_type=coach_type,
                resolution_scope=resolution_scope,
                resolution_at=resolution_at,
                summary=summary,
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def build_market_audit_summary(
    predictions_db: sqlite3.Connection,
    market_id: str,
) -> dict[str, Any] | None:
    predictions_db.row_factory = sqlite3.Row
    market = predictions_db.execute(
        """
        SELECT
            id,
            question,
            end_date,
            price_yes,
            resolved,
            outcome,
            provisional_outcome,
            provisional_source,
            official_resolved_at,
            provisional_resolved_at
        FROM markets
        WHERE id = ?
        """,
        (market_id,),
    ).fetchone()
    if market is None:
        return None

    resolved = int(market["resolved"] or 0)
    if resolved:
        outcome = int(market["outcome"])
        resolution_scope = "official"
        resolution_at = str(market["official_resolved_at"] or market["end_date"])
        outcome_source = "official"
    elif market["provisional_outcome"] is not None:
        outcome = int(market["provisional_outcome"])
        resolution_scope = "provisional"
        resolution_at = str(market["provisional_resolved_at"] or market["end_date"])
        outcome_source = str(market["provisional_source"] or "provisional")
    else:
        return None

    rows = predictions_db.execute(
        """
        SELECT
            market_id,
            agent,
            estimate,
            reasoning,
            predicted_at,
            conviction_score,
            should_trade,
            regime,
            market_price_yes_snapshot,
            seconds_to_expiry
        FROM predictions
        WHERE market_id = ?
          AND agent = ?
        ORDER BY predicted_at ASC
        """,
        (market_id, BASELINE_AGENT),
    ).fetchall()
    if not rows:
        return None

    records = [_decorate_prediction_row(dict(row), outcome=outcome) for row in rows]
    first = records[0]
    latest = records[-1]
    first_trade = next((row for row in records if row["is_trade"]), None)
    coach_type = TOXICITY_COACH if first_trade else SKIP_COACH
    baseline_action = "trade" if first_trade else "skip"
    baseline_record = first_trade or latest
    regime_counter = Counter(str(row.get("regime") or "UNKNOWN") for row in records)
    top_regime = regime_counter.most_common(1)[0][0] if regime_counter else "UNKNOWN"
    reasons = [str(row.get("reasoning") or "").strip() for row in records if str(row.get("reasoning") or "").strip()]
    unique_reasons = []
    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(reason)
    direction_flips = 0
    non_skip_directions = [row["direction"] for row in records if row["direction"] != "SKIP"]
    for before, after in zip(non_skip_directions, non_skip_directions[1:]):
        if before != after:
            direction_flips += 1

    return {
        "market_id": str(market["id"]),
        "question": str(market["question"]),
        "end_date": str(market["end_date"]),
        "outcome": outcome,
        "outcome_label": "UP" if outcome == 1 else "DOWN",
        "resolution_scope": resolution_scope,
        "resolution_at": resolution_at,
        "outcome_source": outcome_source,
        "coach_type": coach_type,
        "regime": top_regime,
        "market_price_yes_final": _safe_float(market["price_yes"], default=0.5),
        "baseline": {
            "agent": BASELINE_AGENT,
            "action": baseline_action,
            "direction": baseline_record["direction"],
            "reason": baseline_record["reasoning"],
            "estimate": baseline_record["estimate"],
            "conviction_score": baseline_record["conviction_score"],
            "predicted_at": baseline_record["predicted_at"],
            "market_price_yes_snapshot": baseline_record["market_price_yes_snapshot"],
            "seconds_to_expiry": baseline_record["seconds_to_expiry"],
            "trade_won": first_trade["won"] if first_trade else None,
        },
        "path": {
            "updates": len(records),
            "called_rows": sum(1 for row in records if row["direction"] != "SKIP"),
            "trade_rows": sum(1 for row in records if row["is_trade"]),
            "first_predicted_at": first["predicted_at"],
            "latest_predicted_at": latest["predicted_at"],
            "latest_direction": latest["direction"],
            "latest_estimate": latest["estimate"],
            "latest_conviction_score": latest["conviction_score"],
            "trade_then_skip": bool(first_trade and not latest["is_trade"]),
            "direction_flips": direction_flips,
            "reasons": unique_reasons[:4],
        },
        "snapshots": _compress_snapshots(records),
    }


def run_coach_audits(
    predictions_db: sqlite3.Connection,
    research_db: sqlite3.Connection,
    *,
    coach_model: BaseCoachModel | None = None,
    include_provisional: bool = True,
    limit: int = COACH_AUDIT_LIMIT,
    judge: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, int]:
    ensure_schema(research_db)
    coach_model = coach_model or build_default_coach_model()
    candidates = load_audit_candidates(
        predictions_db,
        research_db,
        include_provisional=include_provisional,
        limit=limit,
        coach_model=coach_model.model_name,
    )

    counts = {
        "checked": len(candidates),
        "audited": 0,
        "skip_audits": 0,
        "toxicity_audits": 0,
        "official": 0,
        "provisional": 0,
        "errors": 0,
    }

    for candidate in candidates:
        try:
            result = judge(candidate.coach_type, candidate.summary) if judge else coach_model.audit(candidate.coach_type, candidate.summary)
        except Exception:
            counts["errors"] += 1
            continue

        result = normalize_coach_result(candidate.coach_type, result)
        if not result or result.get("verdict") is None or result.get("error"):
            counts["errors"] += 1
            continue

        upsert_coach_audit(
            research_db,
            coach_model=coach_model.model_name,
            candidate=candidate,
            result=result,
        )
        counts["audited"] += 1
        if candidate.coach_type == SKIP_COACH:
            counts["skip_audits"] += 1
        else:
            counts["toxicity_audits"] += 1
        if candidate.resolution_scope == "official":
            counts["official"] += 1
        else:
            counts["provisional"] += 1

    if counts["audited"] > 0:
        refresh_candidate_rollups(research_db, days=7)
    return counts


def upsert_coach_audit(
    db: sqlite3.Connection,
    *,
    coach_model: str,
    candidate: CoachAuditCandidate,
    result: dict[str, Any],
) -> None:
    ensure_schema(db)
    summary = candidate.summary
    helpful, harmful = evaluate_helpfulness(candidate.coach_type, summary, result)
    verdict_direction = result.get("verdict_direction")
    baseline = summary["baseline"]
    now_iso = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO coach_audits (
            market_id, coach_model, coach_type, baseline_agent, baseline_action,
            baseline_direction, baseline_reason, baseline_trade_won, regime,
            market_question, market_end_date, outcome, resolution_scope, outcome_source,
            verdict, verdict_direction, confidence, rationale, helpful, harmful, audited_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id, coach_model, coach_type) DO UPDATE SET
            baseline_action=excluded.baseline_action,
            baseline_direction=excluded.baseline_direction,
            baseline_reason=excluded.baseline_reason,
            baseline_trade_won=excluded.baseline_trade_won,
            regime=excluded.regime,
            market_question=excluded.market_question,
            market_end_date=excluded.market_end_date,
            outcome=excluded.outcome,
            resolution_scope=excluded.resolution_scope,
            outcome_source=excluded.outcome_source,
            verdict=excluded.verdict,
            verdict_direction=excluded.verdict_direction,
            confidence=excluded.confidence,
            rationale=excluded.rationale,
            helpful=excluded.helpful,
            harmful=excluded.harmful,
            audited_at=excluded.audited_at
        """,
        (
            summary["market_id"],
            coach_model,
            candidate.coach_type,
            BASELINE_AGENT,
            baseline["action"],
            baseline["direction"],
            baseline["reason"],
            _as_int_bool(baseline.get("trade_won")),
            summary["regime"],
            summary["question"],
            summary["end_date"],
            int(summary["outcome"]),
            candidate.resolution_scope,
            summary["outcome_source"],
            result["verdict"],
            verdict_direction,
            int(result["confidence"]),
            str(result["reasoning"]),
            int(helpful),
            int(harmful),
            now_iso,
        ),
    )
    audit_id = db.execute(
        """
        SELECT id
        FROM coach_audits
        WHERE market_id = ?
          AND coach_model = ?
          AND coach_type = ?
        """,
        (summary["market_id"], coach_model, candidate.coach_type),
    ).fetchone()["id"]
    db.execute("DELETE FROM coach_audit_tags WHERE audit_id = ?", (audit_id,))
    for tag in result.get("tags", []):
        db.execute(
            """
            INSERT OR IGNORE INTO coach_audit_tags (
                audit_id, market_id, coach_model, coach_type, tag, regime,
                resolution_scope, helpful, harmful, audited_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                summary["market_id"],
                coach_model,
                candidate.coach_type,
                tag,
                summary["regime"],
                candidate.resolution_scope,
                int(helpful),
                int(harmful),
                now_iso,
            ),
        )
    db.commit()


def refresh_candidate_rollups(db: sqlite3.Connection, *, days: int = 7) -> list[dict[str, Any]]:
    ensure_schema(db)
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=days)
    cutoff = window_start.isoformat()
    rows = db.execute(
        """
        SELECT coach_model, coach_type, tag, COALESCE(regime, 'UNKNOWN') AS regime, helpful, harmful
        FROM coach_audit_tags
        WHERE resolution_scope = 'official'
          AND audited_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["coach_model"], row["coach_type"], row["tag"], row["regime"])].append(row)

    db.execute("DELETE FROM coach_candidate_rollups")
    rollups: list[dict[str, Any]] = []
    for (coach_model, coach_type, tag, regime), members in grouped.items():
        support = len(members)
        helpful = sum(int(row["helpful"] or 0) for row in members)
        harmful = sum(int(row["harmful"] or 0) for row in members)
        precision = helpful / support if support else 0.0
        net_helpful = helpful - harmful
        eligible = support >= 5 and precision >= 0.60 and net_helpful >= 3
        record = {
            "coach_model": coach_model,
            "coach_type": coach_type,
            "tag": tag,
            "regime": regime,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "support_count": support,
            "helpful_count": helpful,
            "harmful_count": harmful,
            "precision": precision,
            "net_helpful": net_helpful,
            "eligible_for_ablation": int(eligible),
            "updated_at": window_end.isoformat(),
        }
        rollups.append(record)
        db.execute(
            """
            INSERT INTO coach_candidate_rollups (
                coach_model, coach_type, tag, regime, window_start, window_end,
                support_count, helpful_count, harmful_count, precision, net_helpful,
                eligible_for_ablation, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                coach_model,
                coach_type,
                tag,
                regime,
                record["window_start"],
                record["window_end"],
                support,
                helpful,
                harmful,
                precision,
                net_helpful,
                int(eligible),
                record["updated_at"],
            ),
        )
    db.commit()
    refresh_rule_candidate_specs(db, rollups=rollups)
    return sorted(rollups, key=lambda row: (-row["eligible_for_ablation"], -row["net_helpful"], row["tag"]))


def refresh_rule_candidate_specs(
    db: sqlite3.Connection,
    *,
    rollups: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(db)
    if rollups is None:
        rows = db.execute(
            """
            SELECT coach_model, coach_type, tag, regime, window_start, window_end,
                   support_count, helpful_count, harmful_count, precision, net_helpful,
                   eligible_for_ablation, updated_at
            FROM coach_candidate_rollups
            """
        ).fetchall()
        rollups = [dict(row) for row in rows]

    db.execute("DELETE FROM coach_rule_candidate_specs")
    specs: list[dict[str, Any]] = []
    for rollup in rollups:
        spec = build_rule_candidate_spec(rollup)
        if spec is None:
            continue
        specs.append(spec)
        db.execute(
            """
            INSERT INTO coach_rule_candidate_specs (
                coach_model, coach_type, tag, regime, window_start, window_end,
                spec_name, spec_label, family, target_scope, template_action,
                implementation_hint, config_json, support_count, helpful_count,
                harmful_count, precision, net_helpful, eligible_for_ablation, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spec["coach_model"],
                spec["coach_type"],
                spec["tag"],
                spec["regime"],
                spec["window_start"],
                spec["window_end"],
                spec["spec_name"],
                spec["spec_label"],
                spec["family"],
                spec["target_scope"],
                spec["template_action"],
                spec["implementation_hint"],
                spec["config_json"],
                spec["support_count"],
                spec["helpful_count"],
                spec["harmful_count"],
                spec["precision"],
                spec["net_helpful"],
                spec["eligible_for_ablation"],
                spec["updated_at"],
            ),
        )
    db.commit()
    return sorted(specs, key=lambda row: (-row["eligible_for_ablation"], -row["net_helpful"], row["spec_name"]))


def build_rule_candidate_spec(rollup: dict[str, Any]) -> dict[str, Any] | None:
    template = RULE_CANDIDATE_TEMPLATES.get((str(rollup["coach_type"]), str(rollup["tag"])))
    if template is None:
        return None

    observed_regime = str(rollup["regime"] or "UNKNOWN")
    regime_slug = (
        observed_regime.lower()
        .replace(" / ", "_")
        .replace("/", "_")
        .replace(" ", "_")
    )
    tag = str(rollup["tag"])
    coach_type = str(rollup["coach_type"])
    spec_name = f"{coach_type}__{tag}__{regime_slug}"
    implementation_hint = (
        f"Map tag `{tag}` into a deterministic patch scoped to the observed rollup regime "
        f"`{observed_regime}`. Do not trust the tag name alone; observed regime is canonical."
    )
    config = {
        "template_key": tag,
        "coach_type": coach_type,
        "family": template["family"],
        "template_action": template["action"],
        "observed_regime": observed_regime,
        "target_scope": observed_regime,
    }
    return {
        "coach_model": str(rollup["coach_model"]),
        "coach_type": coach_type,
        "tag": tag,
        "regime": observed_regime,
        "window_start": str(rollup["window_start"]),
        "window_end": str(rollup["window_end"]),
        "spec_name": spec_name,
        "spec_label": f"{template['label']} [{observed_regime}]",
        "family": template["family"],
        "target_scope": observed_regime,
        "template_action": template["action"],
        "implementation_hint": implementation_hint,
        "config_json": json.dumps(config, sort_keys=True),
        "support_count": int(rollup["support_count"] or 0),
        "helpful_count": int(rollup["helpful_count"] or 0),
        "harmful_count": int(rollup["harmful_count"] or 0),
        "precision": float(rollup["precision"] or 0.0),
        "net_helpful": int(rollup["net_helpful"] or 0),
        "eligible_for_ablation": int(rollup["eligible_for_ablation"] or 0),
        "updated_at": str(rollup["updated_at"]),
    }


def evaluate_helpfulness(
    coach_type: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> tuple[bool, bool]:
    outcome = int(summary["outcome"])
    verdict = str(result.get("verdict") or "")
    if coach_type == SKIP_COACH:
        if verdict == "missed_trade_up":
            return outcome == 1, outcome == 0
        if verdict == "missed_trade_down":
            return outcome == 0, outcome == 1
        return False, False
    if coach_type == TOXICITY_COACH:
        baseline_trade_won = bool(summary["baseline"].get("trade_won"))
        if verdict == "toxic_trade":
            return not baseline_trade_won, baseline_trade_won
        return False, False
    return False, False


def _needs_audit(
    research_db: sqlite3.Connection,
    *,
    market_id: str,
    coach_model: str,
    coach_type: str,
    resolution_scope: str,
    resolution_at: str,
) -> bool:
    row = research_db.execute(
        """
        SELECT resolution_scope, audited_at
        FROM coach_audits
        WHERE market_id = ?
          AND coach_model = ?
          AND coach_type = ?
        """,
        (market_id, coach_model, coach_type),
    ).fetchone()
    if row is None:
        return True
    if resolution_scope == "official" and row["resolution_scope"] != "official":
        return True
    audited_at = str(row["audited_at"] or "")
    return resolution_scope == "provisional" and audited_at < resolution_at


def _decorate_prediction_row(row: dict[str, Any], *, outcome: int) -> dict[str, Any]:
    estimate = _safe_float(row.get("estimate"), default=0.5)
    conviction = _safe_int(row.get("conviction_score"), default=0)
    should_trade = row.get("should_trade")
    decorated = {
        "predicted_at": str(row.get("predicted_at") or ""),
        "estimate": estimate,
        "direction": prediction_direction({"estimate": estimate}),
        "conviction_score": conviction,
        "should_trade": should_trade,
        "is_trade": trade_eligible({"conviction_score": conviction, "should_trade": should_trade}),
        "regime": str(row.get("regime") or "UNKNOWN"),
        "reasoning": str(row.get("reasoning") or ""),
        "market_price_yes_snapshot": _safe_float(row.get("market_price_yes_snapshot"), default=0.5),
        "seconds_to_expiry": _safe_int(row.get("seconds_to_expiry"), default=0),
    }
    decorated["won"] = (
        (decorated["direction"] == "UP" and outcome == 1)
        or (decorated["direction"] == "DOWN" and outcome == 0)
    )
    return decorated


def _compress_snapshots(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(records) <= 4:
        keep = records
    else:
        keep = records[:2] + records[-2:]
    return [
        {
            "predicted_at": row["predicted_at"],
            "direction": row["direction"],
            "estimate": round(float(row["estimate"]), 4),
            "conviction_score": row["conviction_score"],
            "is_trade": row["is_trade"],
            "regime": row["regime"],
            "reasoning": row["reasoning"][:120],
            "market_price_yes_snapshot": round(float(row["market_price_yes_snapshot"]), 4),
            "seconds_to_expiry": row["seconds_to_expiry"],
        }
        for row in keep
    ]


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_int_bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0
