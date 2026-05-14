"""
dashboard.py — Shared reporting surface for signal metrics and trade metrics.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from flask import Flask, render_template_string, send_file
except ImportError:  # pragma: no cover - fallback for lightweight test environments
    from jinja2 import Environment

    class Flask:  # type: ignore[override]
        def __init__(self, *_args, **_kwargs):
            pass

        def route(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

        def app_context(self):
            return nullcontext()

        def run(self, *_args, **_kwargs):
            raise RuntimeError("Flask is not installed")

    def render_template_string(template: str, **_context) -> str:  # type: ignore[misc]
        return Environment(autoescape=True).from_string(template).render(**_context)

    def send_file(path: Path):  # type: ignore[misc]
        return Path(path).read_text(encoding="utf-8")

from metrics import (
    build_distribution_svg,
    compute_path_risk,
    compute_ensemble_pnl,
    compute_ev_breakeven,
    compute_pnl,
    ensure_prediction_schema,
    select_exposure_rows,
    select_latest_rows,
)
from foundation_shadow import ensure_shadow_schema
from score import calculate_path_risk_metrics, calculate_signal_metrics
from fetch_markets import ensure_market_schema
from time_display import format_et, format_et_short, now_et_label
from v3.coaches import ensure_schema as ensure_coach_schema
from v3.rule_variants import load_dynamic_coach_rule_metadata

app = Flask(__name__)
DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
RESEARCH_DB_PATH = Path(__file__).parent.parent / "data" / "v3_research.db"
BACKTEST_DB_PATH = Path(__file__).parent.parent / "data" / "polymarket_backtest.db"
RESEARCH_REPORT_PATH = Path(__file__).parent.parent / "docs" / "research" / "latest.md"
RULE_CANDIDATE_REPORT_PATH = Path(__file__).parent.parent / "docs" / "research" / "rule_candidates.md"
PRODUCTION_AGENTS = ["contrarian_rule"]

MODEL_COLORS = {
    "contrarian_rule": "#58a6ff",
    "deepseek-ai/DeepSeek-V3": "#3fb950",
    "baseline_router_v1": "#f2cc60",
    "baseline_router_v2": "#ff7b72",
    "baseline_v4_window_state": "#a371f7",
}

ARCHIVE_AGENT_MARKERS = ("glm", "gpt", "mini", "legacy", "v3_ml")
VOL_BUCKET_ORDER = ("LOW", "MEDIUM", "HIGH")
VOL_BUCKET_CN = {
    "LOW": "低波动",
    "MEDIUM": "中波动",
    "HIGH": "高波动",
    "UNKNOWN": "未知波动",
}
REGIME_STATE_CN = {
    "TRENDING": "趋势",
    "NEUTRAL": "中性",
    "MEAN_REVERTING": "均值回归",
    "UNKNOWN": "未知",
}


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _backtest_dataset_coverage(db: sqlite3.Connection) -> dict[str, object] | None:
    if not _table_exists(db, "historical_markets"):
        return None
    row = db.execute(
        """
        SELECT
            count(*) AS markets,
            sum(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS resolved_markets,
            min(created_at) AS first_created_at,
            max(created_at) AS last_created_at,
            min(end_date) AS first_end_date,
            max(end_date) AS last_end_date,
            count(DISTINCT source_file) AS source_files,
            group_concat(DISTINCT source_file) AS source_file_list
        FROM historical_markets
        """
    ).fetchone()
    if row is None or int(row["markets"] or 0) == 0:
        return None

    first_end = _iso_to_dt(row["first_end_date"])
    last_end = _iso_to_dt(row["last_end_date"])
    coverage_days = 0
    if first_end and last_end:
        coverage_days = max(0, (last_end - first_end).days)

    sources = []
    if row["source_file_list"]:
        sources = [Path(part).name for part in str(row["source_file_list"]).split(",") if part]

    return {
        "sample_label": "局部样本",
        "markets": int(row["markets"] or 0),
        "resolved_markets": int(row["resolved_markets"] or 0),
        "first_created_at": row["first_created_at"],
        "last_created_at": row["last_created_at"],
        "first_end_date": row["first_end_date"],
        "last_end_date": row["last_end_date"],
        "coverage_days": coverage_days,
        "source_files": int(row["source_files"] or 0),
        "source_names": sources,
    }


def _is_archived_agent(agent: str) -> bool:
    lowered = agent.lower()
    return any(marker in lowered for marker in ARCHIVE_AGENT_MARKERS)


def _vol_bucket(regime: str | None) -> str:
    text = str(regime or "UNKNOWN").upper()
    if "LOW_VOL" in text:
        return "LOW"
    if "MEDIUM_VOL" in text:
        return "MEDIUM"
    if "HIGH_VOL" in text:
        return "HIGH"
    return "UNKNOWN"


def _regime_state(regime: str | None) -> str:
    text = str(regime or "UNKNOWN").upper()
    if "TRENDING" in text:
        return "TRENDING"
    if "MEAN_REVERTING" in text:
        return "MEAN_REVERTING"
    if "NEUTRAL" in text:
        return "NEUTRAL"
    return "UNKNOWN"


def _format_regime_cn(regime: str | None) -> dict[str, str]:
    bucket = _vol_bucket(regime)
    state = _regime_state(regime)
    return {
        "raw": str(regime or "UNKNOWN"),
        "vol_bucket": bucket,
        "vol_label": bucket,
        "vol_label_cn": VOL_BUCKET_CN.get(bucket, "未知波动"),
        "state_label_cn": REGIME_STATE_CN.get(state, "未知"),
        "display_cn": f"{VOL_BUCKET_CN.get(bucket, '未知波动')} / {REGIME_STATE_CN.get(state, '未知')}",
        "badge_class": bucket.lower(),
    }


def _build_vol_overview(
    pending_breakdown: list[dict[str, object]],
    regime_breakdown_24h: list[dict[str, object]],
) -> dict[str, object]:
    pending_map = {bucket: {"markets": 0, "trade_markets": 0} for bucket in VOL_BUCKET_ORDER}
    recent_map = {bucket: {"trades": 0, "pnl": 0.0} for bucket in VOL_BUCKET_ORDER}

    for row in pending_breakdown:
        bucket = str(row.get("vol_bucket") or "UNKNOWN")
        if bucket not in pending_map:
            continue
        pending_map[bucket]["markets"] += int(row.get("count") or 0)
        pending_map[bucket]["trade_markets"] += int(row.get("trade_count") or 0)

    for row in regime_breakdown_24h:
        bucket = str(row.get("vol_bucket") or "UNKNOWN")
        if bucket not in recent_map:
            continue
        recent_map[bucket]["trades"] += int(row.get("num_bets") or 0)
        recent_map[bucket]["pnl"] += float(row.get("total_pnl") or 0.0)

    rows = []
    for bucket in VOL_BUCKET_ORDER:
        pending = pending_map[bucket]
        recent = recent_map[bucket]
        rows.append(
            {
                "bucket": bucket,
                "label": bucket,
                "label_cn": VOL_BUCKET_CN[bucket],
                "badge_class": bucket.lower(),
                "pending_markets": pending["markets"],
                "pending_trade_markets": pending["trade_markets"],
                "recent_trades": recent["trades"],
                "recent_pnl": recent["pnl"],
            }
        )

    dominant = max(rows, key=lambda row: (row["pending_markets"], row["recent_trades"]))
    return {
        "rows": rows,
        "dominant": dominant,
        "pending_total": sum(row["pending_markets"] for row in rows),
        "recent_trade_total": sum(row["recent_trades"] for row in rows),
    }


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_market_schema(db)
    ensure_prediction_schema(db)
    ensure_shadow_schema(db)
    return db


def get_status() -> dict[str, object]:
    db = get_db()
    try:
        now_utc = datetime.now(timezone.utc).isoformat()
        market_counts = db.execute(
            """
            SELECT
                COUNT(*) AS total_markets,
                SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) AS pending_markets,
                SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) AS resolved_markets
            FROM markets
            """
        ).fetchone()
        unresolved_breakdown = db.execute(
            """
            SELECT
                SUM(CASE WHEN resolved = 0 AND end_date > ? THEN 1 ELSE 0 END) AS open_future_markets,
                SUM(CASE WHEN resolved = 0 AND end_date <= ? AND provisional_outcome IS NULL THEN 1 ELSE 0 END) AS ended_unresolved_markets,
                SUM(CASE WHEN resolved = 0 AND end_date <= ? AND provisional_outcome IS NOT NULL THEN 1 ELSE 0 END) AS provisional_pending_markets
            FROM markets
            """,
            (now_utc, now_utc, now_utc),
        ).fetchone()
        prediction_counts = db.execute(
            """
            SELECT
                COUNT(*) AS total_predictions,
                COUNT(DISTINCT market_id) AS prediction_markets,
                MAX(predicted_at) AS last_prediction_at
            FROM predictions
            """
        ).fetchone()
        live_candidates = db.execute(
            f"""
            SELECT COUNT(*) AS trade_candidates
            FROM ({_latest_predictions_subquery()}) p
            JOIN markets m ON m.id = p.market_id
            WHERE m.resolved = 0
              AND COALESCE(p.should_trade, 0) = 1
              AND COALESCE(p.conviction_score, 0) >= 3
            """
        ).fetchone()
        realtime_trade = 0
        realtime_event_count = 0
        shadow_pending_exposures = 0
        if _table_exists(db, "realtime_signal_state"):
            row = db.execute(
                """
                SELECT COALESCE(should_trade, 0) AS should_trade
                FROM realtime_signal_state
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            realtime_trade = int(row["should_trade"] or 0) if row else 0
        if _table_exists(db, "realtime_signal_events"):
            row = db.execute("SELECT COUNT(*) AS n FROM realtime_signal_events").fetchone()
            realtime_event_count = int(row["n"] or 0)
        if _table_exists(db, "realtime_shadow_rule_events"):
            row = db.execute(
                """
                WITH first_trade AS (
                    SELECT MIN(id) AS id
                    FROM realtime_shadow_rule_events
                    WHERE profile_name = 'absorption_candidates_live'
                      AND would_trade = 1
                      AND current_price IS NOT NULL
                    GROUP BY market_id, profile_name, rule_name
                )
                SELECT COUNT(*) AS n
                FROM realtime_shadow_rule_events e
                JOIN first_trade ft ON ft.id = e.id
                LEFT JOIN markets m ON m.id = e.market_id
                WHERE COALESCE(m.resolved, 0) = 0
                  AND m.provisional_outcome IS NULL
                """
            ).fetchone()
            shadow_pending_exposures = int(row["n"] or 0)
        return {
            "total_markets": market_counts["total_markets"] or 0,
            "pending_markets": market_counts["pending_markets"] or 0,
            "open_future_markets": unresolved_breakdown["open_future_markets"] or 0,
            "ended_unresolved_markets": unresolved_breakdown["ended_unresolved_markets"] or 0,
            "provisional_pending_markets": unresolved_breakdown["provisional_pending_markets"] or 0,
            "resolved_markets": market_counts["resolved_markets"] or 0,
            "total_predictions": prediction_counts["total_predictions"] or 0,
            "prediction_markets": prediction_counts["prediction_markets"] or 0,
            "avg_snapshots_per_market": (
                float(prediction_counts["total_predictions"] or 0)
                / float(prediction_counts["prediction_markets"] or 1)
            ),
            "trade_candidates": live_candidates["trade_candidates"] or 0,
            "realtime_trade": realtime_trade,
            "realtime_event_count": realtime_event_count,
            "shadow_pending_exposures": shadow_pending_exposures,
            "last_prediction_at": prediction_counts["last_prediction_at"],
            "generated_at": now_et_label(),
        }
    finally:
        db.close()


DOCS_INDEX_PATH = Path(__file__).parent.parent / "docs" / "index.html"


def build_html(*, lite_homepage: bool = False) -> str:
    db = get_db()
    try:
        signal_metrics = calculate_signal_metrics(db)
        resolved_rows = _fetch_resolved_prediction_rows(db)
        exposure_rows = select_exposure_rows(resolved_rows)
        trade_metrics = compute_pnl(exposure_rows)
        ensemble = compute_ensemble_pnl(exposure_rows)
        ev = compute_ev_breakeven(trade_metrics)
        path_risk_metrics = calculate_path_risk_metrics(db)
        agents = _agent_order(db, signal_metrics, trade_metrics)
        production_agents = [agent for agent in agents if agent in PRODUCTION_AGENTS] or ["contrarian_rule"]
        research_agents = [agent for agent in agents if agent not in production_agents]
        challenger_agents, _ = _split_research_roles(research_agents)
        pending_breakdown = _pending_signal_breakdown(db, agent="contrarian_rule")
        regime_breakdown_24h = _production_regime_breakdown(db, hours=24, agent="contrarian_rule")
        latest_research = _latest_research_summary()
        recent_research_runs = _recent_research_runs(limit=5)
        rule_candidates = _rule_absorption_candidates()
        production_24h = _production_recent_summary(db, hours=24, agent="contrarian_rule")
        shadow_summary = _shadow_model_summary(db)
        realtime_summary = _realtime_dashboard_summary(db)
        context = {
            "status": get_status(),
            "agents": agents,
            "production_agents": production_agents,
            "research_agents": research_agents,
            "challenger_agents": challenger_agents,
            "coach_models": _coach_model_summaries(days=7),
            "signal_metrics": signal_metrics,
            "trade_metrics": trade_metrics,
            "path_risk_metrics": path_risk_metrics,
            "ensemble": ensemble,
            "ev": ev,
            "distribution_svg": build_distribution_svg(trade_metrics),
            "latest_research": latest_research,
            "recent_research_runs": recent_research_runs,
            "rule_candidates": rule_candidates,
            "backtest_coverage": (rule_candidates or {}).get("coverage") if rule_candidates else None,
            "provisional_settlements": _provisional_settlement_summary(db, limit=10),
            "coach_latest": _latest_coach_findings(limit=10),
            "coach_rollup": _coach_rollup(days=7),
            "production_24h": production_24h,
            "shadow_summary": shadow_summary,
            "realtime_summary": realtime_summary,
            "regime_breakdown_24h": regime_breakdown_24h,
            "recent_trades": _recent_trade_blotter(db, agent="contrarian_rule", limit=12),
            "pending_breakdown": pending_breakdown,
            "vol_overview": _build_vol_overview(pending_breakdown, regime_breakdown_24h),
            "pending": _fetch_market_matrix(db, resolved=False, limit=10, agents=production_agents),
            "recent": _fetch_market_matrix(db, resolved=True, limit=10, agents=production_agents),
        }
    finally:
        db.close()

    template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="10">
        <title>Polymarket BTC 5分钟作战看板</title>
        <style>
            :root {
                --bg: #0b0f14;
                --bg-accent-a: rgba(0,181,141,0.18);
                --bg-accent-b: rgba(255,184,77,0.10);
                --bg-grid: rgba(255,255,255,0.025);
                --panel: #151b20;
                --panel-soft: #10161b;
                --panel-strong: #1c241f;
                --border: #2c3836;
                --muted: #8ea09b;
                --text: #e7eee9;
                --green: #25d07d;
                --red: #ff5e57;
                --blue: #4db6ff;
                --yellow: #ffb84d;
                --amber: #ff8a3d;
                --shadow: inset 0 1px 0 rgba(255,255,255,0.03);
                --table-border: rgba(48,54,61,0.75);
                --pill-trade-bg: rgba(63,185,80,0.14);
                --pill-skip-bg: rgba(248,81,73,0.14);
            }
            body[data-theme="light"] {
                --bg: #f4f0e8;
                --bg-accent-a: rgba(0,151,124,0.14);
                --bg-accent-b: rgba(255,138,61,0.12);
                --bg-grid: rgba(19,32,51,0.035);
                --panel: #fffaf0;
                --panel-soft: #f4ead9;
                --panel-strong: #ecf6ed;
                --border: #d8cdb7;
                --muted: #716957;
                --text: #1e261f;
                --green: #0b7a45;
                --red: #b6312b;
                --blue: #176e9f;
                --yellow: #a66600;
                --amber: #c45118;
                --shadow: 0 14px 36px rgba(48,38,22,0.08);
                --table-border: rgba(215,221,232,0.95);
                --pill-trade-bg: rgba(18,124,67,0.12);
                --pill-skip-bg: rgba(187,45,59,0.10);
            }
            * { box-sizing: border-box; }
            body {
                margin: 0;
                background:
                    radial-gradient(circle at top left, var(--bg-accent-a), transparent 34%),
                    radial-gradient(circle at top right, var(--bg-accent-b), transparent 30%),
                    linear-gradient(var(--bg-grid) 1px, transparent 1px),
                    linear-gradient(90deg, var(--bg-grid) 1px, transparent 1px),
                    var(--bg);
                background-size: auto, auto, 36px 36px, 36px 36px, auto;
                color: var(--text);
                font: 14px/1.55 "PingFang SC", "Noto Sans SC", "Segoe UI", sans-serif;
                transition: background 0.2s ease, color 0.2s ease;
            }
            .page { max-width: 1440px; margin: 0 auto; padding: 24px 20px 56px; }
            h1, h2, h3 { margin: 0; }
            h1 { font-size: clamp(30px, 4vw, 56px); letter-spacing: -0.06em; line-height: 0.95; }
            h2 { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.12em; }
            .hero { display: flex; justify-content: space-between; gap: 20px; align-items: end; margin-bottom: 18px; }
            .hero p { margin: 8px 0 0; color: var(--muted); max-width: 760px; }
            .stamp { color: var(--muted); font-size: 12px; }
            .hero-actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
            .theme-toggle {
                border: 1px solid var(--border);
                background: var(--panel);
                color: var(--text);
                border-radius: 999px;
                padding: 8px 14px;
                font: inherit;
                cursor: pointer;
                box-shadow: var(--shadow);
            }
            .theme-toggle:hover { border-color: var(--blue); }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 18px; }
            .command-grid { display: grid; grid-template-columns: minmax(280px, 1.25fr) minmax(280px, 0.75fr); gap: 16px; margin-bottom: 18px; }
            .command-card {
                position: relative;
                border: 1px solid var(--border);
                border-radius: 24px;
                background:
                    linear-gradient(135deg, rgba(37,208,125,0.14), transparent 34%),
                    linear-gradient(180deg, var(--panel), var(--panel-soft));
                box-shadow: var(--shadow);
                padding: 20px;
                overflow: hidden;
            }
            .command-card::before {
                content: "";
                position: absolute;
                inset: 10px;
                border: 1px dashed rgba(142,160,155,0.18);
                border-radius: 18px;
                pointer-events: none;
            }
            .command-card.alert {
                background:
                    linear-gradient(135deg, rgba(255,94,87,0.18), transparent 36%),
                    linear-gradient(180deg, var(--panel), var(--panel-soft));
            }
            .command-top { position: relative; display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
            .status-mark { display: inline-flex; align-items: center; gap: 8px; font-weight: 900; letter-spacing: 0.08em; text-transform: uppercase; }
            .status-dot { width: 12px; height: 12px; border-radius: 99px; background: var(--green); box-shadow: 0 0 0 6px rgba(37,208,125,0.14); }
            .status-dot.off { background: var(--red); box-shadow: 0 0 0 6px rgba(255,94,87,0.14); }
            .market-title { position: relative; margin-top: 18px; font-size: clamp(18px, 2vw, 28px); font-weight: 900; line-height: 1.15; max-width: 920px; }
            .signal-strip { position: relative; display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px; margin-top: 18px; }
            .result-board { position: relative; display: grid; grid-template-columns: 1.1fr repeat(3, minmax(120px, 0.7fr)); gap: 10px; margin-top: 12px; }
            .signal-cell { border: 1px solid var(--table-border); background: rgba(255,255,255,0.035); border-radius: 16px; padding: 12px; }
            .signal-cell .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
            .signal-cell .v { margin-top: 4px; font-size: 22px; font-weight: 900; }
            .signal-cell.result .v { font-size: clamp(30px, 5vw, 54px); line-height: 0.95; letter-spacing: -0.06em; }
            .signal-cell.hot .v { color: var(--green); }
            .signal-cell.cold .v { color: var(--red); }
            .side-stack { display: grid; gap: 12px; }
            .side-card { border: 1px solid var(--border); border-radius: 20px; background: var(--panel); box-shadow: var(--shadow); padding: 16px; }
            .side-card .big { font-size: 30px; font-weight: 900; letter-spacing: -0.04em; }
            .source-pill { display: inline-flex; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); padding: 4px 10px; font-size: 12px; }
            .vol-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 18px; }
            .stat, .panel {
                background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.01));
                border: 1px solid var(--border);
                border-radius: 16px;
                box-shadow: var(--shadow);
            }
            .stat { padding: 16px; }
            .vol-card {
                position: relative;
                padding: 16px;
                border: 1px solid var(--border);
                border-radius: 18px;
                background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01));
                overflow: hidden;
            }
            .vol-card::after {
                content: "";
                position: absolute;
                right: -20px;
                bottom: -28px;
                width: 110px;
                height: 110px;
                border-radius: 999px;
                opacity: 0.08;
                background: currentColor;
            }
            .vol-card.low { color: var(--blue); }
            .vol-card.medium { color: var(--yellow); }
            .vol-card.high { color: var(--red); }
            .vol-label { font-size: 30px; font-weight: 900; line-height: 1; letter-spacing: 0.06em; }
            .vol-subtitle { margin-top: 6px; color: var(--text); font-size: 13px; }
            .vol-note { margin-top: 10px; color: var(--muted); font-size: 12px; }
            .stat .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
            .stat .value { font-size: 28px; font-weight: 700; margin-top: 6px; }
            .stat .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
            .grid { display: grid; grid-template-columns: 1.25fr 1fr; gap: 16px; margin-bottom: 16px; }
            .panel { padding: 18px; overflow: hidden; }
            .panel-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; margin-bottom: 14px; }
            table { width: 100%; border-collapse: collapse; }
            th, td { padding: 10px 8px; border-bottom: 1px solid var(--table-border); vertical-align: top; }
            th { text-align: left; color: var(--muted); font-size: 12px; font-weight: 600; }
            td strong { font-size: 13px; }
            .pill { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 700; }
            .pill.trade { background: var(--pill-trade-bg); color: var(--green); }
            .pill.skip { background: var(--pill-skip-bg); color: var(--red); }
            .pill.up { background: var(--pill-trade-bg); color: var(--green); }
            .pill.down { background: var(--pill-skip-bg); color: var(--red); }
            .vol-badge {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                min-width: 66px;
                border-radius: 999px;
                padding: 4px 10px;
                border: 1px solid currentColor;
                font-size: 12px;
                font-weight: 900;
                letter-spacing: 0.06em;
            }
            .vol-badge.low { color: var(--blue); background: rgba(88,166,255,0.10); }
            .vol-badge.medium { color: var(--yellow); background: rgba(242,204,96,0.12); }
            .vol-badge.high { color: var(--red); background: rgba(248,81,73,0.12); }
            .market { font-weight: 600; }
            .muted { color: var(--muted); }
            .metric-pos { color: var(--green); }
            .metric-neg { color: var(--red); }
            .agent-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
            .matrix { display: grid; grid-template-columns: 1fr; gap: 16px; }
            .logic { color: var(--muted); font-size: 12px; margin-top: 4px; white-space: pre-wrap; }
            .empty { color: var(--muted); padding: 8px 0; }
            .report-box { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-top: 12px; }
            .report-metric { background: var(--panel-soft); border: 1px solid var(--table-border); border-radius: 12px; padding: 12px; }
            .report-metric .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
            .report-metric .value { font-size: 18px; font-weight: 700; margin-top: 4px; }
            .subgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
            .mini-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-top: 10px; }
            .mini-card { background: var(--panel-soft); border: 1px solid var(--table-border); border-radius: 12px; padding: 10px 12px; }
            .mini-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
            .mini-card .value { font-size: 18px; font-weight: 700; margin-top: 4px; }
            .section-note { color: var(--muted); font-size: 12px; margin-top: 6px; }
            .nowrap { white-space: nowrap; }
            .table-wrap { overflow-x: auto; margin: 0 -4px; padding: 0 4px; }
            .table-wrap table { min-width: 620px; }
            a { color: var(--blue); text-decoration: none; }
            a:hover { text-decoration: underline; }
            @media (max-width: 960px) {
                .command-grid { grid-template-columns: 1fr; }
                .signal-strip { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
                .result-board { grid-template-columns: 1fr 1fr; }
                .grid { grid-template-columns: 1fr; }
                .subgrid { grid-template-columns: 1fr; }
                .hero { display: block; }
                .hero .stamp { margin-top: 10px; }
                .hero-actions { justify-content: flex-start; margin-top: 12px; }
            }
        </style>
    </head>
    <body>
        <div class="page">
            <div class="hero">
                <div>
                    <h1>BTC 5M<br>实时作战台</h1>
                    <p>先看实时市场、VOL、信号来源和通知状态；历史研究、候选规则和旧预测降级为辅助信息。</p>
                    {% if lite_homepage %}
                    <div class="muted" style="margin-top: 8px;"><a href="/live">打开完整实时页</a></div>
                    {% endif %}
                </div>
                <div class="hero-actions">
                    <button class="theme-toggle" id="theme-toggle" type="button">切换浅色</button>
                <div class="stamp">生成时间 {{ status.generated_at }}</div>
                </div>
            </div>

            <div class="command-grid">
                <div class="command-card {{ 'alert' if realtime_summary and realtime_summary.current and realtime_summary.current.should_trade else '' }}">
                    <div class="command-top">
                        <div>
                            <div class="status-mark">
                                <span class="status-dot {{ '' if realtime_summary and realtime_summary.current and realtime_summary.current.realtime_status == 'ONLINE' else 'off' }}"></span>
                                REALTIME {{ realtime_summary.current.realtime_status if realtime_summary and realtime_summary.current else 'MISSING' }}
                            </div>
                            <div class="muted" style="margin-top: 6px;">当前生产口径：{{ realtime_summary.production_profile if realtime_summary else 'production_current' }} / 当前市场 {{ realtime_summary.current_market_id if realtime_summary else 'n/a' }}</div>
                        </div>
                        <span class="source-pill">{{ realtime_summary.current.price_source if realtime_summary and realtime_summary.current else 'n/a' }}</span>
                    </div>
                    {% if realtime_summary and realtime_summary.current %}
                    <div class="market-title">{{ realtime_summary.current.question }}</div>
                    <div class="result-board">
                        <div class="signal-cell result {{ 'hot' if realtime_summary.current.live_direction == 'UP' else 'cold' if realtime_summary.current.live_direction == 'DOWN' else '' }}">
                            <div class="k">实时市场结果</div>
                            <div class="v">{{ realtime_summary.current.live_direction }}</div>
                        </div>
                        <div class="signal-cell">
                            <div class="k">参考价</div>
                            <div class="v">{{ realtime_summary.current.reference_price_label }}</div>
                        </div>
                        <div class="signal-cell">
                            <div class="k">当前价</div>
                            <div class="v">{{ realtime_summary.current.current_price_label }}</div>
                        </div>
                        <div class="signal-cell {{ 'hot' if realtime_summary.current.distance_from_reference_pct and realtime_summary.current.distance_from_reference_pct > 0 else 'cold' if realtime_summary.current.distance_from_reference_pct and realtime_summary.current.distance_from_reference_pct < 0 else '' }}">
                            <div class="k">距参考价</div>
                            <div class="v">{{ realtime_summary.current.distance_label }}</div>
                        </div>
                    </div>
                    <div class="signal-strip">
                        <div class="signal-cell {{ 'hot' if realtime_summary.current.should_trade else '' }}">
                            <div class="k">Signal</div>
                            <div class="v">{{ 'TRADE' if realtime_summary.current.should_trade else 'SKIP' }}</div>
                        </div>
                        <div class="signal-cell {{ 'hot' if realtime_summary.current.live_direction == 'UP' else 'cold' if realtime_summary.current.live_direction == 'DOWN' else '' }}">
                            <div class="k">Live Direction</div>
                            <div class="v">{{ realtime_summary.current.live_direction }}</div>
                        </div>
                        <div class="signal-cell">
                            <div class="k">TTE</div>
                            <div class="v">{{ realtime_summary.current.seconds_to_expiry if realtime_summary.current.seconds_to_expiry is not none else 'n/a' }}s</div>
                        </div>
                        <div class="signal-cell">
                            <div class="k">VOL / Regime</div>
                            <div class="v"><span class="vol-badge {{ realtime_summary.current.vol_badge_class }}">{{ realtime_summary.current.vol_label }}</span></div>
                        </div>
                    </div>
                    <div class="logic" style="position: relative; margin-top: 12px;">{{ realtime_summary.current.reason or 'no active trade reason' }}</div>
                    <div class="section-note">状态说明：ONLINE=当前市场状态15秒内更新；STALE=当前市场状态滞后；MISSING=当前市场还没有realtime-loop状态。</div>
                    {% else %}
                    <div class="market-title">等待实时市场状态</div>
                    <div class="logic" style="position: relative; margin-top: 12px;">realtime-loop 尚未写入状态，检查 PM2 或数据源。</div>
                    {% endif %}
                </div>
                <div class="side-stack">
                    <div class="side-card">
                        <div class="muted">Telegram Trade Events</div>
                        <div class="big">{{ realtime_summary.event_count if realtime_summary else 0 }}</div>
                        <div class="section-note">累计已通知 {{ realtime_summary.notified_count if realtime_summary else 0 }} / 当前市场 {{ realtime_summary.current_event_count if realtime_summary else 0 }} / 最近 {{ realtime_summary.latest_event.created_at_short if realtime_summary and realtime_summary.latest_event else 'n/a' }}</div>
                        <div class="section-note">说明：这是生产信号通知事件，不是交易结果，也不是候选规则表现。</div>
                    </div>
                    <div class="side-card">
                        <div class="muted">Shadow Candidate Monitor</div>
                        <div class="big">{{ realtime_summary.shadow_event_count if realtime_summary else 0 }}</div>
                        <div class="section-note">当前市场触发 {{ realtime_summary.shadow_current_trigger_count if realtime_summary else 0 }} / profile {{ realtime_summary.shadow_profile if realtime_summary else 'absorption_candidates_live' }}</div>
                        <div class="section-note">说明：Shadow 只观察不通知、不下单；用于候选规则实盘观察。</div>
                    </div>
                </div>
            </div>

            {% if realtime_summary and realtime_summary.shadow_event_count %}
            <div class="card">
                <h2>候选规则实时表现</h2>
                <div class="section-note">真实实时市场 shadow 观察：每个规则/市场只取第一次 would_trade 暴露，resolved 或 provisional 后计入结果。</div>
                {% if realtime_summary.shadow_performance %}
                <table>
                    <thead>
                        <tr>
                            <th>规则</th>
                            <th>已判</th>
                            <th>待判</th>
                            <th>胜率</th>
                            <th>单位 P&amp;L</th>
                            <th>单位 ROI</th>
                            <th>样本状态</th>
                            <th>最近触发</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in realtime_summary.shadow_performance %}
                        <tr>
                            <td><strong>{{ row.rule_name }}</strong><br><span class="muted">{{ row.profile_name }}</span></td>
                            <td>{{ row.resolved_trades }}</td>
                            <td>{{ row.pending_trades }}</td>
                            <td class="{{ 'metric-pos' if row.win_rate >= 50 else 'metric-neg' if row.resolved_trades else '' }}">{{ row.win_rate|round(1) }}%</td>
                            <td class="{{ 'metric-pos' if row.pnl >= 0 else 'metric-neg' }}">{{ row.pnl|round(2) }}</td>
                            <td class="{{ 'metric-pos' if row.roi >= 0 else 'metric-neg' }}">{{ row.roi|round(2) }}%</td>
                            <td>{{ row.sample_note }}</td>
                            <td>{{ row.latest_at_short }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="empty">当前 live profile 已接入实时数据，但还没有可计分的 would_trade 暴露；等候选规则在实盘流里触发并结算后会自动出现表现。</div>
                {% endif %}
            </div>
            {% endif %}

            <div class="card">
                <h2>当前市场候选触发</h2>
                <div class="section-note">当前 BTC 5m 市场下，候选规则 shadow would_trade 明细；只观察，不通知、不下单。</div>
                {% if realtime_summary and realtime_summary.shadow_current_triggers %}
                <table>
                    <thead>
                        <tr>
                            <th>规则</th>
                            <th>方向</th>
                            <th>概率</th>
                            <th>信心</th>
                            <th>触发时间</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in realtime_summary.shadow_current_triggers %}
                        <tr>
                            <td><strong>{{ row.rule_name }}</strong><br><span class="muted">{{ row.reason }}</span></td>
                            <td>{{ row.direction }}</td>
                            <td>{{ (row.estimate * 100)|round(1) }}%</td>
                            <td>{{ row.confidence }} / {{ row.conviction_score }}</td>
                            <td>{{ row.created_at_short }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="empty">当前市场暂无候选规则 would_trade 触发。</div>
                {% endif %}
            </div>

            <div class="card">
                <h2>生产待判市场</h2>
                <div class="section-note">realtime_loop 已产生生产 trade event，但还没有 official/provisional 结果的具体市场。</div>
                {% if realtime_summary and realtime_summary.production_pending_markets %}
                <table>
                    <thead>
                        <tr>
                            <th>市场</th>
                            <th>结束时间</th>
                            <th>状态</th>
                            <th>事件数</th>
                            <th>方向</th>
                            <th>最近触发</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in realtime_summary.production_pending_markets %}
                        <tr>
                            <td><strong>{{ row.market_id }}</strong><br><span class="muted">{{ row.question }}</span></td>
                            <td>{{ row.end_date_short }}</td>
                            <td>{{ row.time_state }}</td>
                            <td>{{ row.event_count }}</td>
                            <td>{{ row.directions }}</td>
                            <td>{{ row.latest_at_short }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="empty">暂无生产待判市场。</div>
                {% endif %}
            </div>

            <div class="card">
                <h2>Shadow 待判市场</h2>
                <div class="section-note">已触发候选规则但还没有 official/provisional 结果的具体市场。</div>
                {% if realtime_summary and realtime_summary.shadow_pending_markets %}
                <table>
                    <thead>
                        <tr>
                            <th>市场</th>
                            <th>结束时间</th>
                            <th>状态</th>
                            <th>规则数</th>
                            <th>最近触发</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in realtime_summary.shadow_pending_markets %}
                        <tr>
                            <td><strong>{{ row.market_id }}</strong><br><span class="muted">{{ row.question }}</span></td>
                            <td>{{ row.end_date_short }}</td>
                            <td>{{ row.time_state }}</td>
                            <td>{{ row.rule_count }}</td>
                            <td>{{ row.latest_at_short }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="empty">暂无 Shadow 候选待判市场。</div>
                {% endif %}
            </div>

            <div class="stats">
                <div class="stat">
                    <div class="label">当前市场</div>
                    <div class="value">{{ realtime_summary.current.market_id if realtime_summary and realtime_summary.current else 'n/a' }}</div>
                    <div class="meta">{% if realtime_summary and realtime_summary.current %}剩余 {{ realtime_summary.current.seconds_to_expiry }}s / 实时结果 {{ realtime_summary.current.live_direction or 'n/a' }} / 生产 {{ 1 if realtime_summary.current.should_trade else 0 }}{% else %}等待 realtime-loop 写入当前市场{% endif %}</div>
                    <div class="meta">Shadow待判 {{ status.shadow_pending_exposures }} / 已结束未判 {{ status.ended_unresolved_markets }}</div>
                    <div class="meta">说明：当前市场按时间窗口选择，不再用最新写入行替代。</div>
                </div>
                <div class="stat">
                    <div class="label">市场与预测快照</div>
                    <div class="value">{{ status.resolved_markets }}</div>
                    <div class="meta">预测快照 {{ status.total_predictions }} / 覆盖市场 {{ status.prediction_markets }} / 平均 {{ status.avg_snapshots_per_market|round(1) }} 次/市场</div>
                    <div class="meta">说明：预测快照是循环多次写入，不等于市场数量。</div>
                </div>
                <div class="stat">
                    <div class="label">24H 交易 ROI</div>
                    <div class="value {{ 'metric-pos' if production_24h and production_24h.trade_roi >= 0 else 'metric-neg' if production_24h else '' }}">{{ production_24h.trade_roi|round(2) if production_24h else 'n/a' }}{{ '%' if production_24h else '' }}</div>
                    <div class="meta">{% if production_24h %}交易 {{ production_24h.traded_predictions }} 笔 / P&amp;L {{ production_24h.trade_pnl|round(2) }}{% else %}暂无 24H 切片{% endif %}</div>
                </div>
                <div class="stat">
                    <div class="label">回测样本</div>
                    <div class="value">{{ backtest_coverage.markets if backtest_coverage else 'n/a' }}</div>
                    <div class="meta">{% if backtest_coverage %}{{ backtest_coverage.sample_label }} / {{ fmt_et(backtest_coverage.first_end_date, '%Y-%m-%d ET') }} → {{ fmt_et(backtest_coverage.last_end_date, '%Y-%m-%d ET') }}{% else %}暂无本地历史覆盖{% endif %}</div>
                </div>
                <div class="stat">
                    <div class="label">Foundation Shadow</div>
                    <div class="value">{{ shadow_summary.prob_up_label if shadow_summary else 'n/a' }}</div>
                    <div class="meta">{% if shadow_summary %}{{ shadow_summary.status }} / 市场 {{ shadow_summary.market_id }} / {{ shadow_summary.created_at_short }} / age {{ shadow_summary.age_seconds }}s{% else %}暂无 shadow 记录{% endif %}</div>
                    <div class="meta">{% if shadow_summary %}24H {{ shadow_summary.perf_24h.correct }}/{{ shadow_summary.perf_24h.resolved }} acc {{ shadow_summary.perf_24h.accuracy|round(1) }}% / Brier {{ shadow_summary.perf_24h.avg_brier_label }}{% endif %}</div>
                    <div class="meta">{% if shadow_summary %}spread {{ shadow_summary.spread_label }} / imb {{ shadow_summary.imbalance_label }}；这是最新快照，不是生产信号。{% endif %}</div>
                </div>
            </div>

            {% if vol_overview %}
            <div class="panel" style="margin-bottom: 18px;">
                <div class="panel-head">
                    <div>
                        <h2>当前 VOL 分层</h2>
                        <div class="muted">先看波动，再看信号。当前主导 VOL：<span class="vol-badge {{ vol_overview.dominant.badge_class }}">{{ vol_overview.dominant.label }}</span> {{ vol_overview.dominant.label_cn }}</div>
                    </div>
                </div>
                <div class="vol-strip">
                    {% for row in vol_overview.rows %}
                    <div class="vol-card {{ row.badge_class }}">
                        <div class="vol-label">{{ row.label }}</div>
                        <div class="vol-subtitle">{{ row.label_cn }}</div>
                        <div class="mini-grid" style="margin-top: 14px;">
                            <div class="mini-card"><div class="label">待判市场</div><div class="value">{{ row.pending_markets }}</div></div>
                            <div class="mini-card"><div class="label">可交易</div><div class="value">{{ row.pending_trade_markets }}</div></div>
                            <div class="mini-card"><div class="label">24H 成交</div><div class="value">{{ row.recent_trades }}</div></div>
                            <div class="mini-card"><div class="label">24H P&amp;L</div><div class="value {{ 'metric-pos' if row.recent_pnl >= 0 else 'metric-neg' }}">{{ row.recent_pnl|round(2) }}</div></div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}

            <div class="subgrid">
                {% if not lite_homepage %}
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>生产基线 24 小时</h2>
                            <div class="muted">生产只看当前主策略。信号质量按最新快照，交易质量按首次真实出手口径。</div>
                        </div>
                    </div>
                    {% if production_24h %}
                    <div class="mini-grid">
                        <div class="mini-card">
                            <div class="label">已结算</div>
                            <div class="value">{{ production_24h.resolved_predictions }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">有方向判断</div>
                            <div class="value">{{ production_24h.called_markets }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">真实交易</div>
                            <div class="value">{{ production_24h.traded_predictions }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">信号胜率</div>
                            <div class="value {{ 'metric-pos' if production_24h.signal_win_rate >= 0.5 else 'metric-neg' }}">{{ (production_24h.signal_win_rate * 100)|round(1) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">交易胜率</div>
                            <div class="value {{ 'metric-pos' if production_24h.trade_win_rate >= 0.5 else 'metric-neg' }}">{{ (production_24h.trade_win_rate * 100)|round(1) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">交易 ROI</div>
                            <div class="value {{ 'metric-pos' if production_24h.trade_roi >= 0 else 'metric-neg' }}">{{ production_24h.trade_roi|round(2) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">交易 P&amp;L</div>
                            <div class="value {{ 'metric-pos' if production_24h.trade_pnl >= 0 else 'metric-neg' }}">{{ production_24h.trade_pnl|round(2) }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">跳过率</div>
                            <div class="value">{{ (production_24h.skip_rate * 100)|round(1) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Trade→Skip</div>
                            <div class="value">{{ production_24h.trade_then_skip_markets }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">方向翻转</div>
                            <div class="value">{{ production_24h.direction_flip_markets }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">平均更新</div>
                            <div class="value">{{ production_24h.avg_updates_per_market|round(2) }}</div>
                        </div>
                    </div>
                    <div class="section-note">
                        时间窗口 {{ production_24h.window_start }} → {{ production_24h.window_end }}。
                        最近成交 {{ production_24h.last_trade_at or 'n/a' }}。
                    </div>
                    {% else %}
                    <div class="empty">暂无 24 小时生产切片。</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>当前待判市场拆分</h2>
                            <div class="muted">按 VOL / 状态拆开看当前未结算市场，先看哪里有量，再看哪里有交易。</div>
                        </div>
                    </div>
                    {% if pending_breakdown %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>VOL</th>
                                <th>状态</th>
                                <th>待判市场</th>
                                <th>可交易</th>
                                <th>跳过</th>
                                <th>平均 Yes</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in pending_breakdown %}
                            <tr>
                                <td><span class="vol-badge {{ row.vol_badge_class }}">{{ row.vol_label }}</span></td>
                                <td>{{ row.state_label_cn }}</td>
                                <td>{{ row.count }}</td>
                                <td class="metric-pos">{{ row.trade_count }}</td>
                                <td class="muted">{{ row.skip_count }}</td>
                                <td>{{ (row.avg_price_yes * 100)|round(1) }}%</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">当前没有待判生产市场。</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>提前结算观察</h2>
                            <div class="muted">这里只用于研究预览。正式统计仍然只认官方 resolved。</div>
                        </div>
                    </div>
                    {% if provisional_settlements and provisional_settlements.total > 0 %}
                    <div class="mini-grid">
                        <div class="mini-card">
                            <div class="label">等待官方确认</div>
                            <div class="value">{{ provisional_settlements.total }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">预解析 UP</div>
                            <div class="value metric-pos">{{ provisional_settlements.up_count }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">预解析 DOWN</div>
                            <div class="value metric-neg">{{ provisional_settlements.down_count }}</div>
                        </div>
                    </div>
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>市场</th>
                                <th>方向</th>
                                <th>最新 Yes</th>
                                <th>观察时间</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in provisional_settlements.rows %}
                            <tr>
                                <td>
                                    <div class="market">{{ row.question }}</div>
                                    <div class="muted">结束 {{ fmt_et(row.end_date) }}</div>
                                </td>
                                <td><span class="pill {{ 'up' if row.provisional_outcome == 1 else 'down' }}">{{ 'UP' if row.provisional_outcome == 1 else 'DOWN' }}</span></td>
                                <td>{{ (row.last_price_yes * 100)|round(1) }}%</td>
                                <td class="nowrap">{{ row.last_observed_at_short }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    <div class="section-note">来源：{{ provisional_settlements.source_label }}</div>
                    {% else %}
                    <div class="empty">暂无等待官方确认的预解析市场。</div>
                    {% endif %}
                </div>
                {% endif %}
            </div>

            <div class="grid">
                {% if not lite_homepage %}
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>最新研究晋级</h2>
                            <div class="muted">这里只保留当前活跃候选，不再显示过期模型的历史排行。</div>
                        </div>
                    </div>
                    {% if latest_research %}
                    <div>
                        <div>
                            <strong>{{ latest_research.challenger }}</strong>
                            对比
                            <strong>{{ latest_research.baseline }}</strong>
                        <span class="pill {{ 'trade' if latest_research.passed else 'skip' }}">{{ '通过' if latest_research.passed else '失败' }}</span>
                        </div>
                        <div class="muted">运行 {{ latest_research.run_id }} · {{ fmt_et(latest_research.created_at) }}</div>
                    </div>
                    <div class="report-box">
                        <div class="report-metric">
                            <div class="label">ROI 差值</div>
                            <div class="value {{ 'metric-pos' if latest_research.aggregate_roi_delta >= 0 else 'metric-neg' }}">{{ latest_research.aggregate_roi_delta|round(2) }}pp</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">胜率差值</div>
                            <div class="value {{ 'metric-pos' if latest_research.aggregate_win_rate_delta >= 0 else 'metric-neg' }}">{{ latest_research.aggregate_win_rate_delta|round(2) }}pp</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">通过 Fold</div>
                            <div class="value">{{ latest_research.passing_folds }}/{{ latest_research.required_fold_passes }}</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">交易比</div>
                            <div class="value">{{ latest_research.trade_ratio_display }}</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">回撤比</div>
                            <div class="value">{{ latest_research.drawdown_ratio_display }}</div>
                        </div>
                    </div>
                    {% if latest_research.reasons %}
                    <div class="logic" style="margin-top: 12px;">{{ latest_research.reasons|join('\n') }}</div>
                    {% endif %}
                    {% if latest_research.regime_takeaways %}
                    <div class="logic" style="margin-top: 12px;">{{ latest_research.regime_takeaways|join('\n') }}</div>
                    {% endif %}
                    {% if latest_research.report_href %}
                    <div class="muted" style="margin-top: 12px;"><a href="{{ latest_research.report_href }}">打开最新研究报告</a></div>
                    {% endif %}
                    {% if recent_research_runs %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>时间</th>
                                <th>候选</th>
                                <th>结果</th>
                                <th>ROI Δ</th>
                                <th>WR Δ</th>
                                <th>Folds</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for run in recent_research_runs %}
                            <tr>
                                <td>{{ run.created_at_short }}</td>
                                <td>{{ run.challenger }}</td>
                                <td><span class="pill {{ 'trade' if run.passed else 'skip' }}">{{ '通过' if run.passed else '失败' }}</span></td>
                                <td class="{{ 'metric-pos' if run.aggregate_roi_delta >= 0 else 'metric-neg' }}">{{ run.aggregate_roi_delta|round(2) }}pp</td>
                                <td class="{{ 'metric-pos' if run.aggregate_win_rate_delta >= 0 else 'metric-neg' }}">{{ run.aggregate_win_rate_delta|round(2) }}pp</td>
                                <td>{{ run.passing_folds }}/{{ run.required_fold_passes }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% else %}
                    <div class="empty">暂无最新研究晋级记录。</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>规则吸收候选</h2>
                            <div class="muted">这里只展示当前值得继续研究的大基线与叠加因子。结论都属于局部样本，不视作全历史定论。</div>
                            {% if rule_candidates.coverage %}
                            <div class="logic" style="margin-top: 8px;">
                                - 样本范围：<strong>{{ rule_candidates.coverage.sample_label|upper }}</strong>，
                                覆盖 {{ fmt_et(rule_candidates.coverage.first_end_date, '%Y-%m-%d ET') }} → {{ fmt_et(rule_candidates.coverage.last_end_date, '%Y-%m-%d ET') }}。
                                当前本地研究集含 {{ rule_candidates.coverage.markets }} 个市场（{{ rule_candidates.coverage.resolved_markets }} 个已结算），来源 {{ rule_candidates.coverage.source_names|join(', ') }}。
                            </div>
                            {% endif %}
                        </div>
                    </div>
                    {% if rule_candidates and rule_candidates.spotlight %}
                    <div>
                        <strong>{{ rule_candidates.spotlight.label }}</strong>
                        <div class="muted">{{ rule_candidates.spotlight.rule_name }}</div>
                    </div>
                    <div class="report-box">
                        <div class="report-metric">
                            <div class="label">中性入场 ROI</div>
                            <div class="value {{ 'metric-pos' if rule_candidates.spotlight.neutral_roi >= 0 else 'metric-neg' }}">{{ rule_candidates.spotlight.neutral_roi|round(2) }}%</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">保守入场 ROI</div>
                            <div class="value {{ 'metric-pos' if rule_candidates.spotlight.edge8_roi >= 0 else 'metric-neg' }}">{{ rule_candidates.spotlight.edge8_roi|round(2) }}%</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">交易数</div>
                            <div class="value">{{ rule_candidates.spotlight.trades }}</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">胜率</div>
                            <div class="value {{ 'metric-pos' if rule_candidates.spotlight.trade_wr >= 50 else 'metric-neg' }}">{{ rule_candidates.spotlight.trade_wr|round(2) }}%</div>
                        </div>
                    </div>
                    {% if rule_candidates.spotlight.neutral_recent_windows %}
                    <div class="mini-grid">
                        {% set recent14 = rule_candidates.spotlight.neutral_recent_windows.get(14) %}
                        {% set recent30 = rule_candidates.spotlight.neutral_recent_windows.get(30) %}
                        {% if recent14 %}
                        <div class="mini-card">
                            <div class="label">最近 14d 交易</div>
                            <div class="value">{{ recent14.trades }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">最近 14d ROI</div>
                            <div class="value {{ 'metric-pos' if recent14.roi >= 0 else 'metric-neg' }}">{{ recent14.roi|round(2) }}%</div>
                        </div>
                        {% endif %}
                        {% if recent30 %}
                        <div class="mini-card">
                            <div class="label">最近 30d 交易</div>
                            <div class="value">{{ recent30.trades }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">最近 30d ROI</div>
                            <div class="value {{ 'metric-pos' if recent30.roi >= 0 else 'metric-neg' }}">{{ recent30.roi|round(2) }}%</div>
                        </div>
                        {% endif %}
                    </div>
                    {% endif %}
                    {% if rule_candidates.takeaways %}
                    <div class="logic" style="margin-top: 12px;">{{ rule_candidates.takeaways|join('\n') }}</div>
                    {% endif %}
                    {% if rule_candidates.router_family %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>大基线家族</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>中性入场 ROI</th>
                                <th>保守入场 ROI</th>
                                <th>14d ROI</th>
                                <th>30d ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in rule_candidates.router_family %}
                            {% set recent14 = row.neutral_recent_windows.get(14) %}
                            {% set recent30 = row.neutral_recent_windows.get(30) %}
                            <tr>
                                <td>{{ row.label }}</td>
                                <td>{{ row.trades }}</td>
                                <td>{{ row.trade_wr|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.neutral_roi >= 0 else 'metric-neg' }}">{{ row.neutral_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.edge8_roi is not none and row.edge8_roi >= 0 else 'metric-neg' if row.edge8_roi is not none else '' }}">{{ row.edge8_roi|round(2) if row.edge8_roi is not none else 'n/a' }}{{ '%' if row.edge8_roi is not none else '' }}</td>
                                <td class="{{ 'metric-pos' if recent14 and recent14.roi >= 0 else 'metric-neg' if recent14 else '' }}">{{ recent14.roi|round(2) if recent14 else 'n/a' }}{{ '%' if recent14 else '' }}</td>
                                <td class="{{ 'metric-pos' if recent30 and recent30.roi >= 0 else 'metric-neg' if recent30 else '' }}">{{ recent30.roi|round(2) if recent30 else 'n/a' }}{{ '%' if recent30 else '' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if rule_candidates.router_overlays %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>叠加候选</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>中性入场 ROI</th>
                                <th>保守入场 ROI</th>
                                <th>14d ROI</th>
                                <th>30d ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in rule_candidates.router_overlays %}
                            {% set recent14 = row.neutral_recent_windows.get(14) %}
                            {% set recent30 = row.neutral_recent_windows.get(30) %}
                            <tr>
                                <td>{{ row.label }}</td>
                                <td>{{ row.trades }}</td>
                                <td>{{ row.trade_wr|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.neutral_roi >= 0 else 'metric-neg' }}">{{ row.neutral_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.edge8_roi is not none and row.edge8_roi >= 0 else 'metric-neg' if row.edge8_roi is not none else '' }}">{{ row.edge8_roi|round(2) if row.edge8_roi is not none else 'n/a' }}{{ '%' if row.edge8_roi is not none else '' }}</td>
                                <td class="{{ 'metric-pos' if recent14 and recent14.roi >= 0 else 'metric-neg' if recent14 else '' }}">{{ recent14.roi|round(2) if recent14 else 'n/a' }}{{ '%' if recent14 else '' }}</td>
                                <td class="{{ 'metric-pos' if recent30 and recent30.roi >= 0 else 'metric-neg' if recent30 else '' }}">{{ recent30.roi|round(2) if recent30 else 'n/a' }}{{ '%' if recent30 else '' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if rule_candidates.reversal_family %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>反转家族</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>中性入场 ROI</th>
                                <th>保守入场 ROI</th>
                                <th>14d ROI</th>
                                <th>30d ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in rule_candidates.reversal_family %}
                            {% set recent14 = row.neutral_recent_windows.get(14) %}
                            {% set recent30 = row.neutral_recent_windows.get(30) %}
                            <tr>
                                <td>{{ row.label }}</td>
                                <td>{{ row.trades }}</td>
                                <td>{{ row.trade_wr|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.neutral_roi >= 0 else 'metric-neg' }}">{{ row.neutral_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.edge8_roi >= 0 else 'metric-neg' }}">{{ row.edge8_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if recent14 and recent14.roi >= 0 else 'metric-neg' if recent14 else '' }}">{{ recent14.roi|round(2) if recent14 else 'n/a' }}{{ '%' if recent14 else '' }}</td>
                                <td class="{{ 'metric-pos' if recent30 and recent30.roi >= 0 else 'metric-neg' if recent30 else '' }}">{{ recent30.roi|round(2) if recent30 else 'n/a' }}{{ '%' if recent30 else '' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if rule_candidates.reversal_legs %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>V4 反转子腿</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>中性入场 ROI</th>
                                <th>保守入场 ROI</th>
                                <th>14d ROI</th>
                                <th>30d ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in rule_candidates.reversal_legs %}
                            {% set recent14 = row.neutral_recent_windows.get(14) %}
                            {% set recent30 = row.neutral_recent_windows.get(30) %}
                            <tr>
                                <td>{{ row.label }}</td>
                                <td>{{ row.trades }}</td>
                                <td>{{ row.trade_wr|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.neutral_roi >= 0 else 'metric-neg' }}">{{ row.neutral_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.edge8_roi >= 0 else 'metric-neg' }}">{{ row.edge8_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if recent14 and recent14.roi >= 0 else 'metric-neg' if recent14 else '' }}">{{ recent14.roi|round(2) if recent14 else 'n/a' }}{{ '%' if recent14 else '' }}</td>
                                <td class="{{ 'metric-pos' if recent30 and recent30.roi >= 0 else 'metric-neg' if recent30 else '' }}">{{ recent30.roi|round(2) if recent30 else 'n/a' }}{{ '%' if recent30 else '' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if rule_candidates.baseline_v2_family %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>Baseline V2</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>中性入场 ROI</th>
                                <th>保守入场 ROI</th>
                                <th>轻保守入场 ROI</th>
                                <th>14d ROI</th>
                                <th>30d ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in rule_candidates.baseline_v2_family %}
                            {% set recent14 = row.neutral_recent_windows.get(14) %}
                            {% set recent30 = row.neutral_recent_windows.get(30) %}
                            <tr>
                                <td>{{ row.label }}</td>
                                <td>{{ row.trades }}</td>
                                <td>{{ row.trade_wr|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.neutral_roi >= 0 else 'metric-neg' }}">{{ row.neutral_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.edge8_roi >= 0 else 'metric-neg' }}">{{ row.edge8_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.edge5_roi >= 0 else 'metric-neg' }}">{{ row.edge5_roi|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if recent14 and recent14.roi >= 0 else 'metric-neg' if recent14 else '' }}">{{ recent14.roi|round(2) if recent14 else 'n/a' }}{{ '%' if recent14 else '' }}</td>
                                <td class="{{ 'metric-pos' if recent30 and recent30.roi >= 0 else 'metric-neg' if recent30 else '' }}">{{ recent30.roi|round(2) if recent30 else 'n/a' }}{{ '%' if recent30 else '' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if rule_candidates.coach_rule_drafts %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>教练派生规则草案</th>
                                <th>家族</th>
                                <th>作用范围</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>中性入场 ROI</th>
                                <th>保守入场 ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in rule_candidates.coach_rule_drafts %}
                            <tr>
                                <td>
                                    <div>{{ row.label }}</div>
                                    <div class="logic">{{ row.rule_name }}</div>
                                </td>
                                <td>{{ row.family }}</td>
                                <td>{{ row.target_scope }}</td>
                                <td>{{ row.trades }}</td>
                                <td>{{ row.trade_wr|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.neutral_roi is not none and row.neutral_roi >= 0 else 'metric-neg' if row.neutral_roi is not none else '' }}">{{ row.neutral_roi|round(2) if row.neutral_roi is not none else 'n/a' }}{{ '%' if row.neutral_roi is not none else '' }}</td>
                                <td class="{{ 'metric-pos' if row.edge8_roi is not none and row.edge8_roi >= 0 else 'metric-neg' if row.edge8_roi is not none else '' }}">{{ row.edge8_roi|round(2) if row.edge8_roi is not none else 'n/a' }}{{ '%' if row.edge8_roi is not none else '' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if rule_candidates.rows %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>规则</th>
                                <th>入场口径</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>P&amp;L</th>
                                <th>ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in rule_candidates.rows %}
                            <tr>
                                <td>{{ row.label }}</td>
                                <td>{{ row.entry_price_source }}</td>
                                <td>{{ row.trades }}</td>
                                <td>{{ row.trade_wr|round(2) }}%</td>
                                <td class="{{ 'metric-pos' if row.trade_pnl >= 0 else 'metric-neg' }}">{{ row.trade_pnl|round(2) }}</td>
                                <td class="{{ 'metric-pos' if row.trade_roi >= 0 else 'metric-neg' }}">{{ row.trade_roi|round(2) }}%</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if rule_candidates.report_href %}
                    <div class="muted" style="margin-top: 12px;"><a href="{{ rule_candidates.report_href }}">打开规则候选报告</a></div>
                    {% endif %}
                    {% else %}
                    <div class="empty">暂无正式规则候选。</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>教练审计</h2>
                            <div class="muted">这里只保留当前教练审计结论，不再让旧模型榜单占主页面。</div>
                        </div>
                    </div>
                    {% if coach_rollup %}
                    <div class="mini-grid">
                        <div class="mini-card">
                            <div class="label">7d 官方审计</div>
                            <div class="value">{{ coach_rollup.summary.official_audits }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">有帮助</div>
                            <div class="value metric-pos">{{ coach_rollup.summary.helpful }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">有伤害</div>
                            <div class="value metric-neg">{{ coach_rollup.summary.harmful }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">中性</div>
                            <div class="value">{{ coach_rollup.summary.neutral }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">净帮助</div>
                            <div class="value {{ 'metric-pos' if coach_rollup.summary.net_helpful >= 0 else 'metric-neg' }}">{{ coach_rollup.summary.net_helpful }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">可做消融测试</div>
                            <div class="value">{{ coach_rollup.summary.eligible_candidates }}</div>
                        </div>
                    </div>
                    {% if coach_rollup.takeaways %}
                    <div class="logic" style="margin-top: 12px;">{{ coach_rollup.takeaways|join('\n') }}</div>
                    {% endif %}
                    {% if coach_rollup.preview_count > 0 %}
                    <div class="logic" style="margin-top: 12px;">
                        - 仍在等待官方确认的预解析审计：{{ coach_rollup.preview_count }}。
                    </div>
                    {% endif %}
                    {% if coach_latest %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>时间</th>
                                <th>范围</th>
                                <th>类型</th>
                                <th>VOL</th>
                                <th>结论</th>
                                <th>市场</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in coach_latest %}
                            <tr>
                                <td class="nowrap">{{ row.audited_at_short }}</td>
                                <td><span class="pill {{ 'trade' if row.resolution_scope == 'official' else 'skip' }}">{{ '正式' if row.resolution_scope == 'official' else '预解析' }}</span></td>
                                <td>{{ row.coach_type_label }}</td>
                                <td><span class="vol-badge {{ row.vol_badge_class }}">{{ row.vol_label }}</span></td>
                                <td>
                                    <div class="{{ 'metric-pos' if row.helpful else 'metric-neg' if row.harmful else 'muted' }}">{{ row.verdict }}</div>
                                    <div class="logic">{{ row.tag_text }}</div>
                                </td>
                                <td>
                                    <div class="market">{{ row.market_question }}</div>
                                    <div class="logic">{{ row.regime_cn }} | {{ row.rationale }}</div>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if coach_rollup.type_rows %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>教练类型</th>
                                <th>官方审计</th>
                                <th>有帮助</th>
                                <th>有伤害</th>
                                <th>净值</th>
                                <th>精度</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in coach_rollup.type_rows %}
                            <tr>
                                <td>{{ row.coach_type_label }}</td>
                                <td>{{ row.audits }}</td>
                                <td class="metric-pos">{{ row.helpful }}</td>
                                <td class="metric-neg">{{ row.harmful }}</td>
                                <td class="{{ 'metric-pos' if row.net_helpful >= 0 else 'metric-neg' }}">{{ row.net_helpful }}</td>
                                <td>{{ (row.precision * 100)|round(1) }}%</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% if coach_rollup.candidate_rows %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>规则草案</th>
                                <th>来源 Tag</th>
                                <th>类型</th>
                                <th>观测 Regime</th>
                                <th>支持数</th>
                                <th>精度</th>
                                <th>净值</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in coach_rollup.candidate_rows %}
                            <tr>
                                <td>
                                    <div>{{ row.spec_label or row.tag }}</div>
                                    {% if row.spec_name %}
                                    <div class="logic">{{ row.spec_name }}</div>
                                    {% endif %}
                                </td>
                                <td>
                                    <div>{{ row.tag }}</div>
                                    {% if row.family %}
                                    <div class="logic">{{ row.family }}</div>
                                    {% endif %}
                                </td>
                                <td>{{ row.coach_type_label }}</td>
                                <td>{{ row.regime }}</td>
                                <td>{{ row.support_count }}</td>
                                <td>{{ (row.precision * 100)|round(1) }}%</td>
                                <td class="{{ 'metric-pos' if row.net_helpful >= 0 else 'metric-neg' }}">{{ row.net_helpful }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% endif %}
                    {% else %}
                    <div class="empty">暂无教练审计记录。</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>信号质量</h2>
                            <div class="muted">这里只看当前生产基线，使用每个市场的最新快照。</div>
                        </div>
                    </div>
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>策略</th>
                                <th>已结算</th>
                                <th>方向准确率</th>
                                <th>平均 Brier</th>
                                <th>相对市场</th>
                                <th>交易率</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for agent in production_agents %}
                            {% set row = signal_metrics.get(agent) %}
                            <tr>
                                <td><span class="agent-dot" style="background: {{ model_colors.get(agent, '#8b949e') }}"></span>{{ agent }}</td>
                                {% if row %}
                                <td>{{ row.resolved_count }}</td>
                                <td>{{ (row.directional_accuracy * 100)|round(1) }}%</td>
                                <td>{{ row.avg_brier|round(4) }}</td>
                                <td class="{{ 'metric-pos' if row.avg_vs_market < 0 else 'metric-neg' }}">{{ row.avg_vs_market|round(4) }}</td>
                                <td>{{ (row.trade_rate * 100)|round(1) }}%</td>
                                {% else %}
                                <td colspan="5" class="muted">暂无已结算样本</td>
                                {% endif %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>交易质量</h2>
                            <div class="muted">这里只看当前生产基线，使用首次真实出手口径。</div>
                        </div>
                    </div>
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>策略</th>
                                <th>交易数</th>
                                <th>胜率</th>
                                <th>P&amp;L</th>
                                <th>ROI</th>
                                <th>最大回撤</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for agent in production_agents %}
                            {% set row = trade_metrics.get(agent) %}
                            <tr>
                                <td><span class="agent-dot" style="background: {{ model_colors.get(agent, '#8b949e') }}"></span>{{ agent }}</td>
                                {% if row %}
                                <td>{{ row.num_bets }}</td>
                                <td>{{ (row.win_rate * 100)|round(1) }}%</td>
                                <td class="{{ 'metric-pos' if row.total_pnl >= 0 else 'metric-neg' }}">{{ row.total_pnl|round(2) }}</td>
                                <td class="{{ 'metric-pos' if row.roi >= 0 else 'metric-neg' }}">{{ row.roi|round(2) }}%</td>
                                <td>{{ row.max_drawdown|round(2) }}</td>
                                {% else %}
                                <td colspan="5" class="muted">暂无交易样本</td>
                                {% endif %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                </div>
                {% endif %}
            </div>

            {% if not lite_homepage %}
            <div class="subgrid">
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>24 小时 VOL / Regime 复盘</h2>
                            <div class="muted">按波动层拆开看近 24 小时的真实交易，不再把旧研究模型混进主表。</div>
                        </div>
                    </div>
                    {% if regime_breakdown_24h %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>VOL</th>
                                <th>状态</th>
                                <th>交易数</th>
                                <th>WR</th>
                                <th>P&amp;L</th>
                                <th>ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in regime_breakdown_24h %}
                            <tr>
                                <td><span class="vol-badge {{ row.vol_badge_class }}">{{ row.vol_label }}</span></td>
                                <td>{{ row.state_label_cn }}</td>
                                <td>{{ row.num_bets }}</td>
                                <td>{{ (row.win_rate * 100)|round(1) }}%</td>
                                <td class="{{ 'metric-pos' if row.total_pnl >= 0 else 'metric-neg' }}">{{ row.total_pnl|round(2) }}</td>
                                <td class="{{ 'metric-pos' if row.roi >= 0 else 'metric-neg' }}">{{ row.roi|round(2) }}%</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">最近 24 小时暂无生产成交。</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>最近成交</h2>
                            <div class="muted">这里只看生产基线的最新真实交易。</div>
                        </div>
                    </div>
                    {% if recent_trades %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>时间</th>
                                <th>方向</th>
                                <th>VOL</th>
                                <th>状态</th>
                                <th>估计值</th>
                                <th>结果</th>
                                <th>P&amp;L</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for trade in recent_trades %}
                            <tr>
                                <td class="nowrap">{{ trade.end_date_short }}</td>
                                <td><span class="pill {{ 'up' if trade.direction == 'UP' else 'down' }}">{{ trade.direction }}</span></td>
                                <td><span class="vol-badge {{ trade.vol_badge_class }}">{{ trade.vol_label }}</span></td>
                                <td>{{ trade.state_label_cn }}</td>
                                <td>{{ (trade.estimate * 100)|round(1) }}%</td>
                                <td>
                                    <div class="{{ 'metric-pos' if trade.won else 'metric-neg' }}">{{ '命中' if trade.won else '失误' }}</div>
                                    <div class="logic">{{ trade.reasoning }}</div>
                                </td>
                                <td class="{{ 'metric-pos' if trade.pnl >= 0 else 'metric-neg' }}">{{ trade.pnl|round(2) }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">暂无最近成交。</div>
                    {% endif %}
                </div>
            </div>

            <div class="matrix">
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>待判市场</h2>
                            <div class="muted">这里只展示生产基线的最新信号。</div>
                        </div>
                    </div>
                    {% if pending %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>市场</th>
                                <th>当前 Yes</th>
                                {% for agent in production_agents %}
                                <th>{{ agent }}</th>
                                {% endfor %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in pending %}
                            <tr>
                                <td>
                                    <div class="market">{{ row.market.question }}</div>
                                    <div class="muted">结束 {{ fmt_et(row.market.end_date) }}</div>
                                </td>
                                <td>{{ (row.market.price_yes * 100)|round(1) }}%</td>
                                {% for agent in production_agents %}
                                {% set p = row.predictions.get(agent) %}
                                <td>
                                    {% if p %}
                                    <div><span class="pill {{ 'trade' if p.should_trade else 'skip' }}">{{ '交易' if p.should_trade else '跳过' }}</span></div>
                                    <div><strong>{{ (p.estimate * 100)|round(1) }}%</strong> {{ p.direction or '中性' }}</div>
                                    <div class="muted">{{ p.regime }} | conviction {{ p.conviction_score }}</div>
                                    <div class="logic">{{ p.reasoning }}</div>
                                    {% else %}
                                    <span class="muted">暂无信号</span>
                                    {% endif %}
                                </td>
                                {% endfor %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">暂无待判市场。</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>最近结算市场</h2>
                            <div class="muted">这里只展示生产基线在已结算市场上的最新结果。</div>
                        </div>
                    </div>
                    {% if recent %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>市场</th>
                                <th>最终结果</th>
                                {% for agent in production_agents %}
                                <th>{{ agent }}</th>
                                {% endfor %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in recent %}
                            <tr>
                                <td>
                                    <div class="market">{{ row.market.question }}</div>
                                    <div class="muted">结算 {{ fmt_et(row.market.end_date) }}</div>
                                </td>
                                <td><span class="pill {{ 'up' if row.market.outcome == 1 else 'down' }}">{{ 'UP' if row.market.outcome == 1 else 'DOWN' }}</span></td>
                                {% for agent in production_agents %}
                                {% set p = row.predictions.get(agent) %}
                                <td>
                                    {% if p %}
                                    <div><span class="pill {{ 'trade' if p.should_trade else 'skip' }}">{{ '交易' if p.should_trade else '跳过' }}</span></div>
                                    <div><strong>{{ (p.estimate * 100)|round(1) }}%</strong> {{ p.direction or '中性' }}</div>
                                    <div class="{{ 'metric-pos' if p.correct_call else 'metric-neg' }}">{{ '方向命中' if p.correct_call else '方向失误' }}</div>
                                    {% if p.trade_pnl is not none %}
                                    <div class="{{ 'metric-pos' if p.trade_pnl >= 0 else 'metric-neg' }}">交易 P&amp;L {{ p.trade_pnl|round(2) }}</div>
                                    {% endif %}
                                    <div class="logic">{{ p.reasoning }}</div>
                                    {% else %}
                                    <span class="muted">暂无信号</span>
                                    {% endif %}
                                </td>
                                {% endfor %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">暂无最近结算市场。</div>
                    {% endif %}
                </div>
            </div>
            {% endif %}
        </div>
        <script>
            (function () {
                const storageKey = "predict-dashboard-theme";
                const body = document.body;
                const button = document.getElementById("theme-toggle");

                function applyTheme(theme) {
                    body.setAttribute("data-theme", theme);
                    if (button) {
                        button.textContent = theme === "light" ? "切换深色" : "切换浅色";
                    }
                }

                const saved = localStorage.getItem(storageKey);
                const systemLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
                applyTheme(saved || (systemLight ? "light" : "dark"));

                if (button) {
                    button.addEventListener("click", function () {
                        const next = body.getAttribute("data-theme") === "light" ? "dark" : "light";
                        localStorage.setItem(storageKey, next);
                        applyTheme(next);
                    });
                }
            })();
        </script>
    </body>
    </html>
    """

    with app.app_context():
        return render_template_string(
            template,
            model_colors=MODEL_COLORS,
            lite_homepage=lite_homepage,
            fmt_et=format_et,
            **context,
        )


@app.route("/")
def index():
    if DOCS_INDEX_PATH.exists():
        return send_file(DOCS_INDEX_PATH)
    return build_html(lite_homepage=True)


@app.route("/live")
def live():
    return build_html()


def _agent_order(db: sqlite3.Connection, signal_metrics: dict, trade_metrics: dict) -> list[str]:
    agents = set(signal_metrics.keys()) | set(trade_metrics.keys())
    if not agents:
        rows = db.execute("SELECT DISTINCT agent FROM predictions ORDER BY agent").fetchall()
        agents = {row["agent"] for row in rows}
    if not agents:
        agents = {"contrarian_rule"}
    agents = {agent for agent in agents if not _is_archived_agent(agent) or agent in PRODUCTION_AGENTS}
    return sorted(agents, key=lambda agent: (0 if agent == "contrarian_rule" else 1, agent))


def _fetch_resolved_prediction_rows(db: sqlite3.Connection) -> list[dict[str, object]]:
    return _fetch_effective_prediction_rows(db, include_provisional=False)


def _fetch_effective_prediction_rows(
    db: sqlite3.Connection,
    *,
    include_provisional: bool,
) -> list[dict[str, object]]:
    where_clause = "m.resolved = 1"
    outcome_expr = "m.outcome"
    settlement_kind_expr = "'official'"
    if include_provisional:
        where_clause = "(m.resolved = 1 OR m.provisional_outcome IS NOT NULL)"
        outcome_expr = "CASE WHEN m.resolved = 1 THEN m.outcome ELSE m.provisional_outcome END"
        settlement_kind_expr = "CASE WHEN m.resolved = 1 THEN 'official' ELSE 'provisional' END"

    rows = db.execute(
        """
        SELECT
            p.market_id,
            p.agent,
            p.estimate,
            p.predicted_at,
            p.regime,
            p.conviction_score,
            p.should_trade,
            m.question,
            COALESCE(p.market_price_yes_snapshot, m.price_yes) AS market_price_yes_snapshot,
            m.price_yes,
            """
        + outcome_expr
        + """
            AS outcome,
            """
        + settlement_kind_expr
        + """
            AS settlement_kind
        FROM predictions p
        JOIN markets m ON m.id = p.market_id
        WHERE """
        + where_clause
        + """
        ORDER BY m.end_date ASC, p.predicted_at ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _fetch_market_matrix(
    db: sqlite3.Connection,
    *,
    resolved: bool,
    limit: int,
    agents: list[str],
) -> list[dict[str, object]]:
    market_rows = db.execute(
        f"""
        SELECT id, question, price_yes, end_date, outcome
        FROM markets
        WHERE resolved = ?
        ORDER BY end_date {'DESC' if resolved else 'ASC'}
        LIMIT ?
        """,
        (1 if resolved else 0, limit),
    ).fetchall()
    if not market_rows:
        return []

    latest_predictions = db.execute(
        """
        SELECT p.*
        FROM predictions p
        JOIN (
            SELECT market_id, agent, MAX(predicted_at) AS latest_predicted_at
            FROM predictions
            GROUP BY market_id, agent
        ) latest
          ON latest.market_id = p.market_id
         AND latest.agent = p.agent
         AND latest.latest_predicted_at = p.predicted_at
        """
    ).fetchall()

    prediction_map: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for row in latest_predictions:
        record = dict(row)
        estimate = float(record.get("estimate", 0.5))
        outcome = record.get("outcome")
        correct_call = None
        if resolved and outcome is not None:
            correct_call = (estimate >= 0.5 and outcome == 1) or (estimate < 0.5 and outcome == 0)

        trade_pnl = None
        if resolved and _trade_eligible(record):
            trade_row = {
                "market_id": record["market_id"],
                "agent": record["agent"],
                "estimate": estimate,
                "market_price_yes_snapshot": record.get("market_price_yes_snapshot"),
                "price_yes": next((m["price_yes"] for m in market_rows if m["id"] == record["market_id"]), 0.5),
                "outcome": next((m["outcome"] for m in market_rows if m["id"] == record["market_id"]), 0),
                "conviction_score": record.get("conviction_score", 0),
                "should_trade": record.get("should_trade", 0),
            }
            trade_metrics = compute_pnl([trade_row]).get(record["agent"], {})
            trade_pnl = trade_metrics.get("total_pnl")

        prediction_map[record["market_id"]][record["agent"]] = {
            "estimate": estimate,
            "direction": "UP" if estimate >= 0.5 else "DOWN" if estimate < 0.5 else None,
            "confidence": record.get("confidence"),
            "conviction_score": int(float(record.get("conviction_score") or 0)),
            "should_trade": _trade_eligible(record),
            "regime": record.get("regime") or "UNKNOWN",
            "reasoning": record.get("reasoning") or "",
            "correct_call": correct_call,
            "trade_pnl": trade_pnl,
        }

    matrix = []
    for market in market_rows:
        matrix.append({
            "market": dict(market),
            "predictions": {agent: prediction_map.get(market["id"], {}).get(agent) for agent in agents if prediction_map.get(market["id"], {}).get(agent)},
        })
    return matrix


def _trade_eligible(row: dict[str, object]) -> bool:
    try:
        conviction = int(float(row.get("conviction_score") or 0))
    except (TypeError, ValueError):
        conviction = 0
    should_trade = row.get("should_trade")
    if should_trade is None:
        should_trade = conviction >= 3
    return conviction >= 3 and str(should_trade).lower() not in {"0", "false", "none"}


def _prediction_direction(row: dict[str, object]) -> str:
    try:
        estimate = float(row.get("estimate") or 0.5)
    except (TypeError, ValueError):
        estimate = 0.5
    if estimate > 0.5:
        return "UP"
    if estimate < 0.5:
        return "DOWN"
    return "SKIP"


def _prediction_correct(row: dict[str, object]) -> bool:
    direction = _prediction_direction(row)
    if direction == "SKIP":
        return False
    try:
        outcome = int(float(row.get("outcome") or 0))
    except (TypeError, ValueError):
        outcome = 0
    return (direction == "UP" and outcome == 1) or (direction == "DOWN" and outcome == 0)


def _latest_research_summary() -> dict[str, object] | None:
    db = _open_research_db()
    if db is None or not _table_exists(db, "arena_runs"):
        if db is not None:
            db.close()
        return None
    try:
        row = db.execute(
            """
            SELECT run_id, created_at, baseline, challenger, gate_passed, summary_json
            FROM arena_runs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None

        summary = json.loads(row["summary_json"])
        gate = summary.get("gate", {})
        regime_findings = summary.get("regime_findings", {})
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "baseline": row["baseline"],
            "challenger": row["challenger"],
            "passed": bool(row["gate_passed"]),
            "aggregate_roi_delta": gate.get("aggregate_roi_delta", 0.0),
            "aggregate_win_rate_delta": gate.get("aggregate_win_rate_delta", 0.0),
            "passing_folds": gate.get("passing_folds", 0),
            "required_fold_passes": gate.get("required_fold_passes", 0),
            "trade_ratio_display": _format_ratio(gate.get("trade_ratio", 0.0)),
            "drawdown_ratio_display": _format_ratio(gate.get("drawdown_ratio", 0.0)),
            "reasons": [f"- {reason}" for reason in gate.get("reasons", [])],
            "regime_takeaways": [f"- {item}" for item in regime_findings.get("takeaways", [])],
            "report_href": "research/latest.md" if RESEARCH_REPORT_PATH.exists() else None,
        }
    finally:
        db.close()


def _recent_research_runs(limit: int = 5) -> list[dict[str, object]]:
    db = _open_research_db()
    if db is None or not _table_exists(db, "arena_runs"):
        if db is not None:
            db.close()
        return []
    try:
        rows = db.execute(
            """
            SELECT run_id, created_at, baseline, challenger, gate_passed, summary_json
            FROM arena_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        runs = []
        for row in rows:
            summary = json.loads(row["summary_json"])
            gate = summary.get("gate", {})
            runs.append(
                {
                    "run_id": row["run_id"],
                    "created_at": row["created_at"],
                    "created_at_short": format_et_short(row["created_at"]),
                    "baseline": row["baseline"],
                    "challenger": row["challenger"],
                    "passed": bool(row["gate_passed"]),
                    "aggregate_roi_delta": gate.get("aggregate_roi_delta", 0.0),
                    "aggregate_win_rate_delta": gate.get("aggregate_win_rate_delta", 0.0),
                    "passing_folds": gate.get("passing_folds", 0),
                    "required_fold_passes": gate.get("required_fold_passes", 0),
                }
            )
        return runs
    finally:
        db.close()


def _active_challenger_names(limit: int = 10) -> set[str]:
    runs = _recent_research_runs(limit=limit)
    names = {str(run["challenger"]) for run in runs if run.get("challenger")}
    latest = _latest_research_summary()
    if latest and latest.get("challenger"):
        names.add(str(latest["challenger"]))
    return names


def _split_research_roles(research_agents: list[str]) -> tuple[list[str], list[str]]:
    active = _active_challenger_names()
    visible_agents = [agent for agent in research_agents if not _is_archived_agent(agent)]
    challengers = [agent for agent in visible_agents if agent in active]
    coaches = [agent for agent in visible_agents if agent not in active]
    return challengers, coaches


def _open_research_db() -> sqlite3.Connection | None:
    if not RESEARCH_DB_PATH.exists():
        return None
    db = sqlite3.connect(RESEARCH_DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_coach_schema(db)
    return db


def _open_backtest_db() -> sqlite3.Connection | None:
    if not BACKTEST_DB_PATH.exists():
        return None
    db = sqlite3.connect(BACKTEST_DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def _rule_absorption_candidates() -> dict[str, object] | None:
    db = _open_backtest_db()
    if db is None or not _table_exists(db, "backtest_runs"):
        if db is not None:
            db.close()
        return None
    candidates = {
        "baseline_router_v1": "大基线 Router V1",
        "baseline_router_v2": "大基线 Router V2",
        "baseline_router_v1_plus_lvn_alpha3": "Router + LVN alpha=3 叠加",
        "baseline_router_v1_plus_v4": "Router + V4 稀疏叠加",
        "baseline_router_v1_plus_sparse_combo": "Router + 稀疏组合叠加",
        "low_vol_branch_v1": "低波动分支 V1",
        "medium_vol_branch_v1": "中波动分支 V1",
        "medium_vol_branch_v3": "中波动分支 V3",
        "high_vol_branch_v1": "高波动分支 V1",
        "baseline_v2_lvn_alpha2": "LVN Alpha≥2 骨架",
        "baseline_v2_lvn_alpha3": "LVN Alpha=3 骨架",
        "baseline_v3_reversal_core": "V3 反转核心",
        "baseline_v4_window_state": "V4 十窗口状态反转",
        "spike_reversal_down_no_hvt": "V4 子腿：冲高回落做空",
        "flush_bounce_up": "V4 子腿：宽底反弹做多",
        "candidate_lvn_up_volume_spike_streak4p": "LVN 做多 + 放量 + streak≥4",
        "only_lvn_up_pure_volume_spike": "LVN 纯放量做多",
        "only_lvn_up": "LVN 做多",
        "only_low_vol_neutral": "仅 LOW_VOL / NEUTRAL",
    }
    try:
        rows = []
        coverage = _backtest_dataset_coverage(db)
        dynamic_specs = load_dynamic_coach_rule_metadata()
        by_rule: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
        candidate_labels = {
            **candidates,
            **{name: str(spec["spec_label"]) for name, spec in dynamic_specs.items()},
        }
        query = f"""
            SELECT run_id, rule_name, entry_price_source, markets, eligible_markets, trades, trade_wins, trade_pnl, trade_roi
            FROM backtest_runs
            WHERE rule_name IN ({','.join(['?'] * len(candidate_labels))})
            ORDER BY run_id DESC
        """
        for row in db.execute(query, tuple(candidate_labels.keys())).fetchall():
            rule_name = str(row["rule_name"])
            entry = str(row["entry_price_source"])
            current = by_rule.get(rule_name, {}).get(entry)
            if current is None:
                by_rule[rule_name][entry] = row
                continue
            current_rank = (
                int(current["eligible_markets"] or 0),
                int(current["trades"] or 0),
                int(current["run_id"] or 0),
            )
            candidate_rank = (
                int(row["eligible_markets"] or 0),
                int(row["trades"] or 0),
                int(row["run_id"] or 0),
            )
            if candidate_rank > current_rank:
                by_rule[rule_name][entry] = row

        for rule_name, label in candidate_labels.items():
            for entry in ("neutral_50", "model_edge_8", "model_edge_5"):
                row = by_rule.get(rule_name, {}).get(entry)
                if row is None:
                    continue
                trades = int(row["trades"] or 0)
                wins = int(row["trade_wins"] or 0)
                rows.append(
                    {
                        "rule_name": rule_name,
                        "label": label,
                        "entry_price_source": entry,
                        "run_id": row["run_id"],
                        "trades": trades,
                        "trade_wr": (wins / trades * 100.0) if trades else 0.0,
                        "trade_pnl": float(row["trade_pnl"] or 0.0),
                        "trade_roi": float(row["trade_roi"] or 0.0),
                    }
                )

        spotlight_rule = "baseline_router_v2"
        spotlight_entry = {row["entry_price_source"]: row for row in rows if row["rule_name"] == spotlight_rule}
        spotlight = None
        if spotlight_entry:
            neutral = spotlight_entry.get("neutral_50")
            edge8 = spotlight_entry.get("model_edge_8")
            neutral_windows = _backtest_recent_windows(
                db,
                int(neutral["run_id"]),
                windows=(14, 30),
            ) if neutral else {}
            edge8_windows = _backtest_recent_windows(
                db,
                int(edge8["run_id"]),
                windows=(14, 30),
            ) if edge8 else {}
            spotlight = {
                "rule_name": spotlight_rule,
                "label": candidates[spotlight_rule],
                "neutral_roi": neutral["trade_roi"] if neutral else 0.0,
                "edge8_roi": edge8["trade_roi"] if edge8 else 0.0,
                "trades": neutral["trades"] if neutral else 0,
                "trade_wr": neutral["trade_wr"] if neutral else 0.0,
                "neutral_recent_windows": neutral_windows,
                "edge8_recent_windows": edge8_windows,
            }

        router_family = []
        for rule_name in (
            "baseline_router_v1",
            "baseline_router_v2",
            "low_vol_branch_v1",
            "medium_vol_branch_v1",
            "medium_vol_branch_v3",
            "high_vol_branch_v1",
        ):
            entry_rows = {row["entry_price_source"]: row for row in rows if row["rule_name"] == rule_name}
            if not entry_rows:
                continue
            neutral = entry_rows.get("neutral_50")
            edge8 = entry_rows.get("model_edge_8")
            neutral_windows = _backtest_recent_windows(
                db,
                int(neutral["run_id"]),
                windows=(14, 30),
            ) if neutral else {}
            router_family.append(
                {
                    "rule_name": rule_name,
                    "label": candidates[rule_name],
                    "trades": neutral["trades"] if neutral else 0,
                    "trade_wr": neutral["trade_wr"] if neutral else 0.0,
                    "neutral_roi": neutral["trade_roi"] if neutral else 0.0,
                    "edge8_roi": edge8["trade_roi"] if edge8 else None,
                    "neutral_recent_windows": neutral_windows,
                }
            )

        router_overlays = []
        for rule_name in (
            "baseline_router_v1_plus_lvn_alpha3",
            "baseline_router_v1_plus_v4",
            "baseline_router_v1_plus_sparse_combo",
        ):
            entry_rows = {row["entry_price_source"]: row for row in rows if row["rule_name"] == rule_name}
            if not entry_rows:
                continue
            neutral = entry_rows.get("neutral_50")
            edge8 = entry_rows.get("model_edge_8")
            neutral_windows = _backtest_recent_windows(
                db,
                int(neutral["run_id"]),
                windows=(14, 30),
            ) if neutral else {}
            router_overlays.append(
                {
                    "rule_name": rule_name,
                    "label": candidates[rule_name],
                    "trades": neutral["trades"] if neutral else 0,
                    "trade_wr": neutral["trade_wr"] if neutral else 0.0,
                    "neutral_roi": neutral["trade_roi"] if neutral else 0.0,
                    "edge8_roi": edge8["trade_roi"] if edge8 else None,
                    "neutral_recent_windows": neutral_windows,
                }
            )

        reversal_family = []
        for rule_name in ("baseline_v3_reversal_core", "baseline_v4_window_state"):
            entry_rows = {row["entry_price_source"]: row for row in rows if row["rule_name"] == rule_name}
            if not entry_rows:
                continue
            neutral = entry_rows.get("neutral_50")
            edge8 = entry_rows.get("model_edge_8")
            neutral_windows = _backtest_recent_windows(
                db,
                int(neutral["run_id"]),
                windows=(14, 30),
            ) if neutral else {}
            reversal_family.append(
                {
                    "rule_name": rule_name,
                    "label": candidates[rule_name],
                    "trades": neutral["trades"] if neutral else 0,
                    "trade_wr": neutral["trade_wr"] if neutral else 0.0,
                    "neutral_roi": neutral["trade_roi"] if neutral else 0.0,
                    "edge8_roi": edge8["trade_roi"] if edge8 else 0.0,
                    "neutral_recent_windows": neutral_windows,
                }
            )

        reversal_legs = []
        for rule_name in ("spike_reversal_down_no_hvt", "flush_bounce_up"):
            entry_rows = {row["entry_price_source"]: row for row in rows if row["rule_name"] == rule_name}
            if not entry_rows:
                continue
            neutral = entry_rows.get("neutral_50")
            edge8 = entry_rows.get("model_edge_8")
            neutral_windows = _backtest_recent_windows(
                db,
                int(neutral["run_id"]),
                windows=(14, 30),
            ) if neutral else {}
            reversal_legs.append(
                {
                    "rule_name": rule_name,
                    "label": candidates[rule_name],
                    "trades": neutral["trades"] if neutral else 0,
                    "trade_wr": neutral["trade_wr"] if neutral else 0.0,
                    "neutral_roi": neutral["trade_roi"] if neutral else 0.0,
                    "edge8_roi": edge8["trade_roi"] if edge8 else 0.0,
                    "neutral_recent_windows": neutral_windows,
                }
            )

        baseline_v2_family = []
        for rule_name in ("baseline_v2_lvn_alpha2", "baseline_v2_lvn_alpha3"):
            entry_rows = {row["entry_price_source"]: row for row in rows if row["rule_name"] == rule_name}
            if not entry_rows:
                continue
            neutral = entry_rows.get("neutral_50")
            edge8 = entry_rows.get("model_edge_8")
            edge5 = entry_rows.get("model_edge_5")
            neutral_windows = _backtest_recent_windows(
                db,
                int(neutral["run_id"]),
                windows=(14, 30),
            ) if neutral else {}
            baseline_v2_family.append(
                {
                    "rule_name": rule_name,
                    "label": candidates[rule_name],
                    "trades": neutral["trades"] if neutral else 0,
                    "trade_wr": neutral["trade_wr"] if neutral else 0.0,
                    "neutral_roi": neutral["trade_roi"] if neutral else 0.0,
                    "edge8_roi": edge8["trade_roi"] if edge8 else 0.0,
                    "edge5_roi": edge5["trade_roi"] if edge5 else 0.0,
                    "neutral_recent_windows": neutral_windows,
                }
            )

        coach_rule_drafts = []
        for rule_name, spec in dynamic_specs.items():
            entry_rows = {row["entry_price_source"]: row for row in rows if row["rule_name"] == rule_name}
            if not entry_rows:
                continue
            neutral = entry_rows.get("neutral_50")
            edge8 = entry_rows.get("model_edge_8")
            if neutral is None and edge8 is None:
                continue
            coach_rule_drafts.append(
                {
                    "rule_name": rule_name,
                    "label": str(spec["spec_label"]),
                    "family": str(spec["family"]),
                    "target_scope": str(spec["target_scope"]),
                    "trades": neutral["trades"] if neutral else 0,
                    "trade_wr": neutral["trade_wr"] if neutral else 0.0,
                    "neutral_roi": neutral["trade_roi"] if neutral else None,
                    "edge8_roi": edge8["trade_roi"] if edge8 else None,
                }
            )

        takeaways = []
        if coverage:
            takeaways.append(
                f"- 下方所有规则候选结果都来自当前本地历史样本（{coverage['first_end_date'][:10]} 至 {coverage['last_end_date'][:10]}），属于局部样本研究，不代表完整历史上的生产证据。"
            )
        if spotlight:
            takeaways.append(
                f"- Router V2 目前是这份本地样本里最像“大基线”的候选：{spotlight['trades']} 笔交易，中性入场 ROI {spotlight['neutral_roi']:+.2f}%，保守入场 ROI {spotlight['edge8_roi']:+.2f}%。"
            )
            recent_14d = spotlight["neutral_recent_windows"].get(14)
            recent_30d = spotlight["neutral_recent_windows"].get(30)
            if recent_14d:
                takeaways.append(
                    f"- 最近 14 天样本偏稀：{recent_14d['trades']} 笔，ROI {recent_14d['roi']:+.2f}%。"
                )
            if recent_30d:
                takeaways.append(
                    f"- 最近 30 天中性入场下共有 {recent_30d['trades']} 笔，ROI {recent_30d['roi']:+.2f}%。"
                )
            takeaways.append(
                f"- 相比 V3 / V4 这类稀疏高质量腿，Router V2 的样本宽度明显更大（{spotlight['trades']} 笔），因此更适合作为当前的大基线研究方向。"
            )
        if router_family:
            low_branch = next((row for row in router_family if row["rule_name"] == "low_vol_branch_v1"), None)
            med_branch = next((row for row in router_family if row["rule_name"] == "medium_vol_branch_v1"), None)
            med_branch_v3 = next((row for row in router_family if row["rule_name"] == "medium_vol_branch_v3"), None)
            high_branch = next((row for row in router_family if row["rule_name"] == "high_vol_branch_v1"), None)
            if low_branch and med_branch and high_branch:
                takeaways.append(
                    f"- Router 能成立，是因为三条分支职责不对称：低波动负责提供宽度（{low_branch['trades']} 笔，{low_branch['neutral_roi']:+.2f}%），中波动补中等宽度（{med_branch['trades']} 笔，{med_branch['neutral_roi']:+.2f}%），高波动则保持少而精（{high_branch['trades']} 笔，{high_branch['neutral_roi']:+.2f}%）。"
                )
            if med_branch and med_branch_v3:
                takeaways.append(
                    f"- 用平衡版中波动延续腿替换原始宽版后，中波动分支的中性入场 ROI 从 {med_branch['neutral_roi']:+.2f}% 提升到 {med_branch_v3['neutral_roi']:+.2f}%，保守入场 ROI 也从 {med_branch['edge8_roi']:+.2f}% 提升到 {med_branch_v3['edge8_roi']:+.2f}%。"
                )
        if router_overlays:
            lvn_overlay = next((row for row in router_overlays if row["rule_name"] == "baseline_router_v1_plus_lvn_alpha3"), None)
            v4_overlay = next((row for row in router_overlays if row["rule_name"] == "baseline_router_v1_plus_v4"), None)
            combo_overlay = next((row for row in router_overlays if row["rule_name"] == "baseline_router_v1_plus_sparse_combo"), None)
            if lvn_overlay:
                takeaways.append(
                    f"- LVN alpha=3 更适合作为研究叠加因子，而不是新基线：虽然全样本中性入场 ROI 提升到 {lvn_overlay['neutral_roi']:+.2f}%（{lvn_overlay['trades']} 笔），但在保守入场下回落到 {lvn_overlay['edge8_roi']:+.2f}%，最近 30 天也更弱。"
                )
            if v4_overlay:
                takeaways.append(
                    f"- V4 稀疏叠加更像质量兜底，而不是放大量能：全样本中性入场 ROI 为 {v4_overlay['neutral_roi']:+.2f}%，最近 30 天表现基本没有被破坏。"
                )
            if combo_overlay and lvn_overlay and v4_overlay:
                takeaways.append(
                    f"- 稀疏组合没有形成干净叠加：中性入场 ROI {combo_overlay['neutral_roi']:+.2f}% 只比单独 LVN 略高，但执行更保守时仍弱于基础 router。"
                )
        if reversal_family:
            v3 = next((row for row in reversal_family if row["rule_name"] == "baseline_v3_reversal_core"), None)
            v4 = next((row for row in reversal_family if row["rule_name"] == "baseline_v4_window_state"), None)
            if v3 and v4:
                takeaways.append(
                    f"- V4 仍然在质量上最强（{v4['trades']} 笔，{v4['neutral_roi']:+.2f}%），但 Router V2 因为能把宽度扩到 {spotlight['trades'] if spotlight else 0} 笔，更适合作为大基线候选。"
                )
        if reversal_legs:
            leg_down = next((row for row in reversal_legs if row["rule_name"] == "spike_reversal_down_no_hvt"), None)
            leg_up = next((row for row in reversal_legs if row["rule_name"] == "flush_bounce_up"), None)
            if leg_down and leg_up:
                takeaways.append(
                    f"- 两条最强反转腿职责不同：冲高回落做空负责提供宽度（{leg_down['trades']} 笔，{leg_down['neutral_roi']:+.2f}%），宽底反弹做多则提供最高的独立质量（{leg_up['trades']} 笔，{leg_up['neutral_roi']:+.2f}%）。"
                )
        if baseline_v2_family:
            alpha2 = next((row for row in baseline_v2_family if row["rule_name"] == "baseline_v2_lvn_alpha2"), None)
            alpha3 = next((row for row in baseline_v2_family if row["rule_name"] == "baseline_v2_lvn_alpha3"), None)
            if alpha2:
                takeaways.append(
                    f"- Baseline V2 alpha>=2 能改善宽骨架的中性入场表现（{alpha2['neutral_roi']:+.2f}%），但一旦按更保守的入场口径处理，就会掉到 {alpha2['edge8_roi']:+.2f}%。"
                )
            if alpha3:
                takeaways.append(
                    f"- Baseline V2 alpha=3 更像稀疏高质量叠加：中性入场（{alpha3['neutral_roi']:+.2f}%）、保守入场（{alpha3['edge8_roi']:+.2f}%）和轻保守入场（{alpha3['edge5_roi']:+.2f}%）都保持为正。"
                )

        return {
            "rows": rows,
            "coverage": coverage,
            "spotlight": spotlight,
            "router_family": router_family,
            "router_overlays": router_overlays,
            "reversal_family": reversal_family,
            "reversal_legs": reversal_legs,
            "baseline_v2_family": baseline_v2_family,
            "coach_rule_drafts": coach_rule_drafts,
            "takeaways": takeaways,
            "report_href": "research/rule_candidates.md" if rows else None,
        }
    finally:
        db.close()


def render_rule_candidate_markdown() -> str:
    summary = _rule_absorption_candidates()
    lines = [
        "# Rule Candidate Report",
        "",
        f"- Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
    ]
    if not summary or not summary.get("rows"):
        lines.append("No formal rule candidates recorded yet.")
        return "\n".join(lines)

    spotlight = summary.get("spotlight") or {}
    coverage = summary.get("coverage") or {}
    if coverage:
        lines.extend(
            [
                "## Sample Scope",
                "",
                f"- Scope: `{coverage['sample_label']}`",
                f"- Ended-market coverage: `{coverage['first_end_date']}` -> `{coverage['last_end_date']}`",
                f"- Markets in local study set: `{coverage['markets']}` total, `{coverage['resolved_markets']}` resolved",
                f"- Source files: `{', '.join(coverage['source_names'])}`",
                "",
                "> These rule results come from the current local partial historical sample, not a full-history production-equivalent archive.",
                "",
            ]
        )
    if spotlight:
        neutral_recent = spotlight.get("neutral_recent_windows", {})
        edge8_recent = spotlight.get("edge8_recent_windows", {})
        lines.extend(
            [
                "## Spotlight",
                "",
                f"- Candidate: `{spotlight['rule_name']}`",
                f"- Label: {spotlight['label']}",
                f"- Neutral-50 ROI: `{spotlight['neutral_roi']:+.2f}%`",
                f"- Model-Edge-8 ROI: `{spotlight['edge8_roi']:+.2f}%`",
                f"- Trades: `{spotlight['trades']}`",
                f"- Win rate: `{spotlight['trade_wr']:.2f}%`",
                "",
            ]
        )
        if neutral_recent or edge8_recent:
            lines.extend(["## Recent Windows", ""])
            if neutral_recent:
                lines.append("### Neutral-50")
                lines.append("")
                for days in (14, 30):
                    window = neutral_recent.get(days)
                    if not window:
                        continue
                    lines.append(
                        f"- Last {days}d: `{window['trades']}` trades, WR `{window['win_rate']:.2f}%`, ROI `{window['roi']:+.2f}%`, P&L `{window['pnl']:+.2f}`"
                    )
                lines.append("")
            if edge8_recent:
                lines.append("### Model-Edge-8")
                lines.append("")
                for days in (14, 30):
                    window = edge8_recent.get(days)
                    if not window:
                        continue
                    lines.append(
                        f"- Last {days}d: `{window['trades']}` trades, WR `{window['win_rate']:.2f}%`, ROI `{window['roi']:+.2f}%`, P&L `{window['pnl']:+.2f}`"
                    )
                lines.append("")
    if summary.get("takeaways"):
        lines.extend(["## Takeaways", ""])
        lines.extend(str(item) for item in summary["takeaways"])
        lines.append("")

    if summary.get("router_family"):
        lines.extend(
            [
                "## Baseline Router Family",
                "",
                "| Rule | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in summary["router_family"]:
            recent14 = row["neutral_recent_windows"].get(14, {})
            recent30 = row["neutral_recent_windows"].get(30, {})
            edge8_text = f"{row['edge8_roi']:+.2f}%" if row["edge8_roi"] is not None else "n/a"
            lines.append(
                f"| {row['label']} | {row['trades']} | {row['trade_wr']:.2f}% | {row['neutral_roi']:+.2f}% | {edge8_text} | {float(recent14.get('roi', 0.0)):+.2f}% | {float(recent30.get('roi', 0.0)):+.2f}% |"
        )
        lines.append("")

    if summary.get("router_overlays"):
        lines.extend(
            [
                "## Router Overlay Candidates",
                "",
                "| Overlay | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in summary["router_overlays"]:
            recent14 = row["neutral_recent_windows"].get(14, {})
            recent30 = row["neutral_recent_windows"].get(30, {})
            edge8_text = f"{row['edge8_roi']:+.2f}%" if row["edge8_roi"] is not None else "n/a"
            lines.append(
                f"| {row['label']} | {row['trades']} | {row['trade_wr']:.2f}% | {row['neutral_roi']:+.2f}% | {edge8_text} | {float(recent14.get('roi', 0.0)):+.2f}% | {float(recent30.get('roi', 0.0)):+.2f}% |"
            )
        lines.append("")

    if summary.get("reversal_family"):
        lines.extend(
            [
                "## Baseline V3 vs V4 Reversal Family",
                "",
                "| Rule | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in summary["reversal_family"]:
            recent14 = row["neutral_recent_windows"].get(14, {})
            recent30 = row["neutral_recent_windows"].get(30, {})
            lines.append(
                f"| {row['label']} | {row['trades']} | {row['trade_wr']:.2f}% | {row['neutral_roi']:+.2f}% | {row['edge8_roi']:+.2f}% | {float(recent14.get('roi', 0.0)):+.2f}% | {float(recent30.get('roi', 0.0)):+.2f}% |"
            )
        lines.append("")

    if summary.get("reversal_legs"):
        lines.extend(
            [
                "## Baseline V4 Reversal Legs",
                "",
                "| Leg | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in summary["reversal_legs"]:
            recent14 = row["neutral_recent_windows"].get(14, {})
            recent30 = row["neutral_recent_windows"].get(30, {})
            lines.append(
                f"| {row['label']} | {row['trades']} | {row['trade_wr']:.2f}% | {row['neutral_roi']:+.2f}% | {row['edge8_roi']:+.2f}% | {float(recent14.get('roi', 0.0)):+.2f}% | {float(recent30.get('roi', 0.0)):+.2f}% |"
            )
        lines.append("")

    if summary.get("baseline_v2_family"):
        lines.extend(
            [
                "## Baseline V2 Research Skeletons",
                "",
                "| Rule | Trades | WR | Neutral ROI | Edge-8 ROI | Edge-5 ROI | Last 14d ROI | Last 30d ROI |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in summary["baseline_v2_family"]:
            recent14 = row["neutral_recent_windows"].get(14, {})
            recent30 = row["neutral_recent_windows"].get(30, {})
            lines.append(
                f"| {row['label']} | {row['trades']} | {row['trade_wr']:.2f}% | {row['neutral_roi']:+.2f}% | {row['edge8_roi']:+.2f}% | {row['edge5_roi']:+.2f}% | {float(recent14.get('roi', 0.0)):+.2f}% | {float(recent30.get('roi', 0.0)):+.2f}% |"
            )
        lines.append("")

    if summary.get("coach_rule_drafts"):
        lines.extend(
            [
                "## Coach-Derived Rule Drafts",
                "",
                "| Draft | Family | Scope | Trades | WR | Neutral ROI | Edge-8 ROI |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in summary["coach_rule_drafts"]:
            neutral_text = f"{row['neutral_roi']:+.2f}%" if row["neutral_roi"] is not None else "n/a"
            edge8_text = f"{row['edge8_roi']:+.2f}%" if row["edge8_roi"] is not None else "n/a"
            lines.append(
                f"| {row['label']} (`{row['rule_name']}`) | {row['family']} | {row['target_scope']} | {row['trades']} | {row['trade_wr']:.2f}% | {neutral_text} | {edge8_text} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Latest Candidate Runs",
            "",
            "| Rule | Entry | Trades | WR | P&L | ROI |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary["rows"]:
        lines.append(
            f"| {row['label']} | {row['entry_price_source']} | {row['trades']} | {row['trade_wr']:.2f}% | {row['trade_pnl']:+.2f} | {row['trade_roi']:+.2f}% |"
        )
    lines.append("")
    return "\n".join(lines)


def _backtest_recent_windows(
    db: sqlite3.Connection,
    run_id: int,
    windows: tuple[int, ...] = (14, 30),
) -> dict[int, dict[str, float]]:
    max_end = db.execute(
        "SELECT max(end_date) FROM backtest_trades WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    if max_end is None:
        return {}

    stats: dict[int, dict[str, float]] = {}
    for days in windows:
        row = db.execute(
            """
            SELECT
                count(*) AS trades,
                sum(won) AS wins,
                sum(wager) AS wagered,
                sum(pnl) AS pnl
            FROM backtest_trades
            WHERE run_id = ?
              AND should_trade = 1
              AND end_date >= datetime(?, ?)
            """,
            (run_id, max_end, f"-{days} days"),
        ).fetchone()
        trades = int(row["trades"] or 0)
        wins = int(row["wins"] or 0)
        wagered = float(row["wagered"] or 0.0)
        pnl = float(row["pnl"] or 0.0)
        stats[days] = {
            "trades": trades,
            "wins": wins,
            "win_rate": (wins / trades * 100.0) if trades else 0.0,
            "pnl": pnl,
            "roi": (pnl / wagered * 100.0) if wagered else 0.0,
        }
    return stats


def _table_exists(db: sqlite3.Connection, table_name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _fmt_price(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_signed_pct(value: object) -> str:
    try:
        return f"{float(value) * 100:+.3f}%"
    except (TypeError, ValueError):
        return "n/a"


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_seconds(value: object, *, now: datetime | None = None) -> int | None:
    dt = _parse_dt(value)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(int((now - dt).total_seconds()), 0)


def _current_market_row(db: sqlite3.Connection, *, now: datetime | None = None) -> sqlite3.Row | None:
    now = now or datetime.now(timezone.utc)
    # For 5m markets, the current window is the earliest unresolved market whose
    # end is still in the future. This avoids showing a just-expired latest state.
    return db.execute(
        """
        SELECT id, question, end_date, price_yes, price_no
        FROM markets
        WHERE resolved = 0
          AND end_date > ?
        ORDER BY end_date ASC
        LIMIT 1
        """,
        (now.isoformat(),),
    ).fetchone()


def _realtime_dashboard_summary(db: sqlite3.Connection) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    current_market = _current_market_row(db, now=now)
    current = None
    if _table_exists(db, "realtime_signal_state"):
        row = None
        if current_market is not None:
            row = db.execute(
                """
                SELECT *
                FROM realtime_signal_state
                WHERE market_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (current_market["id"],),
            ).fetchone()
        if row is None and current_market is None:
            row = db.execute(
                """
                SELECT *
                FROM realtime_signal_state
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is not None:
            record = dict(row)
            regime = _format_regime_cn(record.get("regime"))
            distance = record.get("distance_from_reference_pct")
            age = _age_seconds(record.get("updated_at"), now=now)
            is_current_market = current_market is not None and str(record.get("market_id")) == str(current_market["id"])
            realtime_status = "ONLINE" if is_current_market and age is not None and age <= 15 else "STALE" if is_current_market else "MISMATCH"
            current = {
                **record,
                "should_trade": bool(record.get("should_trade")),
                "is_current_market": is_current_market,
                "realtime_status": realtime_status,
                "age_seconds": age,
                "vol_label": regime["vol_label"],
                "vol_badge_class": regime["badge_class"],
                "regime_cn": regime["display_cn"],
                "reference_price_label": _fmt_price(record.get("reference_price")),
                "current_price_label": _fmt_price(record.get("current_price")),
                "distance_label": _fmt_signed_pct(distance),
                "updated_at_short": format_et_short(record.get("updated_at")),
            }
    if current is None and current_market is not None:
        end_dt = _parse_dt(current_market["end_date"])
        current = {
            "market_id": current_market["id"],
            "question": current_market["question"],
            "seconds_to_expiry": max(int((end_dt - now).total_seconds()), 0) if end_dt else None,
            "live_direction": None,
            "should_trade": False,
            "reason": "current market selected, waiting for realtime-loop state",
            "price_source": "n/a",
            "realtime_status": "MISSING",
            "is_current_market": True,
            "age_seconds": None,
            "vol_label": "UNKNOWN",
            "vol_badge_class": "unknown",
            "regime_cn": "未知",
            "reference_price_label": "n/a",
            "current_price_label": "n/a",
            "distance_label": "n/a",
            "updated_at_short": "n/a",
        }

    event_count = 0
    notified_count = 0
    current_event_count = 0
    latest_event = None
    production_pending_markets = []
    if _table_exists(db, "realtime_signal_events"):
        row = db.execute(
            """
            SELECT COUNT(*) AS event_count,
                   SUM(CASE WHEN notified = 1 THEN 1 ELSE 0 END) AS notified_count
            FROM realtime_signal_events
            """
        ).fetchone()
        event_count = int(row["event_count"] or 0)
        notified_count = int(row["notified_count"] or 0)
        if current_market is not None:
            row = db.execute(
                """
                SELECT COUNT(*) AS n
                FROM realtime_signal_events
                WHERE market_id = ?
                """,
                (current_market["id"],),
            ).fetchone()
            current_event_count = int(row["n"] or 0)
        event = db.execute(
            """
            SELECT *
            FROM realtime_signal_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if event is not None:
            latest_event = dict(event)
            latest_event["created_at_short"] = format_et_short(latest_event.get("created_at"))
        production_pending_markets = _realtime_production_pending_markets(db)

    shadow_event_count = 0
    shadow_profile = "absorption_candidates_live"
    shadow_performance = []
    shadow_current_triggers = []
    shadow_pending_markets = []
    if _table_exists(db, "realtime_shadow_rule_events"):
        row = db.execute(
            """
            SELECT profile_name
            FROM realtime_shadow_rule_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            shadow_profile = str(row["profile_name"] or shadow_profile)
        count_row = db.execute(
            """
            SELECT COUNT(*) AS event_count
            FROM realtime_shadow_rule_events
            WHERE profile_name = ?
              AND current_price IS NOT NULL
            """,
            (shadow_profile,),
        ).fetchone()
        shadow_event_count = int(count_row["event_count"] or 0)
        shadow_performance = _realtime_shadow_rule_performance(db, profile_name=shadow_profile)
        if current_market is not None:
            shadow_current_triggers = _realtime_shadow_current_triggers(
                db,
                profile_name=shadow_profile,
                market_id=str(current_market["id"]),
            )
        shadow_pending_markets = _realtime_shadow_pending_markets(db, profile_name=shadow_profile)

    return {
        "current": current,
        "current_market_id": str(current_market["id"]) if current_market is not None else None,
        "event_count": event_count,
        "notified_count": notified_count,
        "current_event_count": current_event_count,
        "latest_event": latest_event,
        "production_pending_markets": production_pending_markets,
        "shadow_event_count": shadow_event_count,
        "shadow_profile": shadow_profile,
        "shadow_current_trigger_count": len(shadow_current_triggers),
        "shadow_current_triggers": shadow_current_triggers,
        "shadow_pending_markets": shadow_pending_markets,
        "shadow_performance": shadow_performance,
        "production_profile": "production_current",
    }


def _realtime_shadow_rule_performance(
    db: sqlite3.Connection,
    *,
    profile_name: str,
    limit: int = 12,
) -> list[dict[str, object]]:
    if not _table_exists(db, "realtime_shadow_rule_events") or not _table_exists(db, "markets"):
        return []
    rows = db.execute(
        """
        WITH first_trade AS (
            SELECT MIN(id) AS id
            FROM realtime_shadow_rule_events
            WHERE would_trade = 1
              AND profile_name = ?
              AND current_price IS NOT NULL
            GROUP BY market_id, profile_name, rule_name
        ),
        exposures AS (
            SELECT e.*,
                   COALESCE(m.outcome, m.provisional_outcome) AS effective_outcome,
                   CASE WHEN m.resolved = 1 OR m.provisional_outcome IS NOT NULL THEN 1 ELSE 0 END AS is_resolved
            FROM realtime_shadow_rule_events e
            JOIN first_trade ft ON ft.id = e.id
            LEFT JOIN markets m ON m.id = e.market_id
        ),
        scored AS (
            SELECT *,
                   CASE
                       WHEN is_resolved = 1
                            AND ((direction = 'UP' AND effective_outcome = 1)
                              OR (direction = 'DOWN' AND effective_outcome = 0))
                       THEN 1 ELSE 0
                   END AS won,
                   CASE
                       WHEN direction = 'UP' THEN market_price_yes
                       WHEN direction = 'DOWN' THEN 1.0 - market_price_yes
                       ELSE NULL
                   END AS entry_price
            FROM exposures
        )
        SELECT
            profile_name,
            rule_name,
            COUNT(*) AS exposures,
            SUM(CASE WHEN is_resolved = 1 THEN 1 ELSE 0 END) AS resolved_trades,
            SUM(CASE WHEN is_resolved = 0 THEN 1 ELSE 0 END) AS pending_trades,
            SUM(CASE WHEN is_resolved = 1 THEN won ELSE 0 END) AS wins,
            SUM(
                CASE
                    WHEN is_resolved = 0 OR entry_price IS NULL OR entry_price <= 0 OR entry_price >= 1 THEN 0.0
                    WHEN won = 1 THEN (1.0 / entry_price - 1.0)
                    ELSE -1.0
                END
            ) AS pnl,
            MAX(created_at) AS latest_at
        FROM scored
        GROUP BY profile_name, rule_name
        HAVING exposures > 0
        ORDER BY resolved_trades DESC, pnl DESC, latest_at DESC
        LIMIT ?
        """,
        (profile_name, limit),
    ).fetchall()

    performance = []
    for row in rows:
        resolved = int(row["resolved_trades"] or 0)
        wins = int(row["wins"] or 0)
        pnl = float(row["pnl"] or 0.0)
        performance.append(
            {
                "profile_name": str(row["profile_name"] or ""),
                "rule_name": str(row["rule_name"] or ""),
                "exposures": int(row["exposures"] or 0),
                "resolved_trades": resolved,
                "pending_trades": int(row["pending_trades"] or 0),
                "wins": wins,
                "win_rate": (wins / resolved * 100.0) if resolved else 0.0,
                "pnl": pnl,
                "roi": (pnl / resolved * 100.0) if resolved else 0.0,
                "sample_note": "样本很少" if resolved < 30 else "可观察",
                "latest_at_short": format_et_short(row["latest_at"]),
            }
        )
    return performance


def _realtime_production_pending_markets(
    db: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[dict[str, object]]:
    rows = db.execute(
        """
        SELECT
            e.market_id,
            m.question,
            m.end_date,
            COUNT(*) AS event_count,
            GROUP_CONCAT(DISTINCT e.direction) AS directions,
            MAX(e.created_at) AS latest_at
        FROM realtime_signal_events e
        LEFT JOIN markets m ON m.id = e.market_id
        WHERE COALESCE(m.resolved, 0) = 0
          AND m.provisional_outcome IS NULL
        GROUP BY e.market_id, m.question, m.end_date
        ORDER BY m.end_date ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    pending = []
    for row in rows:
        end_dt = _parse_dt(row["end_date"])
        seconds = int((end_dt - now).total_seconds()) if end_dt else None
        pending.append(
            {
                "market_id": row["market_id"],
                "question": row["question"] or "",
                "end_date_short": format_et_short(row["end_date"]),
                "time_state": f"剩余 {seconds}s" if seconds is not None and seconds >= 0 else f"已结束 {abs(seconds)}s" if seconds is not None else "n/a",
                "event_count": int(row["event_count"] or 0),
                "directions": row["directions"] or "n/a",
                "latest_at_short": format_et_short(row["latest_at"]),
            }
        )
    return pending


def _realtime_shadow_current_triggers(
    db: sqlite3.Connection,
    *,
    profile_name: str,
    market_id: str,
    limit: int = 8,
) -> list[dict[str, object]]:
    rows = db.execute(
        """
        SELECT rule_name, direction, estimate, confidence, conviction_score, reason, created_at
        FROM realtime_shadow_rule_events
        WHERE profile_name = ?
          AND market_id = ?
          AND would_trade = 1
          AND current_price IS NOT NULL
        ORDER BY id DESC
        LIMIT ?
        """,
        (profile_name, market_id, limit),
    ).fetchall()
    return [
        {
            "rule_name": row["rule_name"],
            "direction": row["direction"] or "n/a",
            "estimate": float(row["estimate"] or 0.5),
            "confidence": row["confidence"] or "low",
            "conviction_score": int(row["conviction_score"] or 0),
            "reason": row["reason"] or "",
            "created_at_short": format_et_short(row["created_at"]),
        }
        for row in rows
    ]


def _realtime_shadow_pending_markets(
    db: sqlite3.Connection,
    *,
    profile_name: str,
    limit: int = 10,
) -> list[dict[str, object]]:
    rows = db.execute(
        """
        WITH first_trade AS (
            SELECT MIN(id) AS id
            FROM realtime_shadow_rule_events
            WHERE profile_name = ?
              AND would_trade = 1
              AND current_price IS NOT NULL
            GROUP BY market_id, profile_name, rule_name
        )
        SELECT
            e.market_id,
            m.question,
            m.end_date,
            COUNT(*) AS rule_count,
            GROUP_CONCAT(e.rule_name, ', ') AS rule_names,
            MIN(e.created_at) AS first_at,
            MAX(e.created_at) AS latest_at
        FROM realtime_shadow_rule_events e
        JOIN first_trade ft ON ft.id = e.id
        LEFT JOIN markets m ON m.id = e.market_id
        WHERE COALESCE(m.resolved, 0) = 0
          AND m.provisional_outcome IS NULL
        GROUP BY e.market_id, m.question, m.end_date
        ORDER BY m.end_date ASC
        LIMIT ?
        """,
        (profile_name, limit),
    ).fetchall()
    now = datetime.now(timezone.utc)
    pending = []
    for row in rows:
        end_dt = _parse_dt(row["end_date"])
        seconds = int((end_dt - now).total_seconds()) if end_dt else None
        pending.append(
            {
                "market_id": row["market_id"],
                "question": row["question"] or "",
                "end_date_short": format_et_short(row["end_date"]),
                "seconds_to_end": seconds,
                "time_state": f"剩余 {seconds}s" if seconds is not None and seconds >= 0 else f"已结束 {abs(seconds)}s" if seconds is not None else "n/a",
                "rule_count": int(row["rule_count"] or 0),
                "rule_names": row["rule_names"] or "",
                "first_at_short": format_et_short(row["first_at"]),
                "latest_at_short": format_et_short(row["latest_at"]),
            }
        )
    return pending


def _shadow_model_summary(db: sqlite3.Connection) -> dict[str, object] | None:
    if not _table_exists(db, "prediction_shadow_models"):
        return None
    row = db.execute(
        """
        SELECT
            s.market_id,
            s.status,
            s.model_name,
            s.prob_up,
            s.primary_raw,
            s.secondary_prob,
            s.agreement_passed,
            s.direction_match,
            s.created_at,
            b.spread_pct,
            b.depth_imbalance,
            b.bid_depth_5pct,
            b.ask_depth_5pct
        FROM prediction_shadow_models s
        LEFT JOIN order_book_snapshots b
          ON b.market_id = s.market_id
         AND b.id = (
             SELECT MAX(id)
             FROM order_book_snapshots
             WHERE market_id = s.market_id
         )
        ORDER BY s.created_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    prob_up = row["prob_up"]
    spread = row["spread_pct"]
    imbalance = row["depth_imbalance"]
    created_age = _age_seconds(row["created_at"])
    perf_all = _foundation_shadow_performance(db, hours=None)
    perf_24h = _foundation_shadow_performance(db, hours=24)
    return {
        "market_id": row["market_id"],
        "status": row["status"],
        "model_name": row["model_name"] or "unloaded",
        "prob_up": prob_up,
        "prob_up_label": f"{float(prob_up) * 100:.1f}%" if prob_up is not None else "n/a",
        "primary_raw": row["primary_raw"],
        "secondary_prob": row["secondary_prob"],
        "agreement_passed": bool(row["agreement_passed"]) if row["agreement_passed"] is not None else None,
        "direction_match": bool(row["direction_match"]) if row["direction_match"] is not None else None,
        "created_at": row["created_at"],
        "created_at_short": format_et_short(row["created_at"]),
        "age_seconds": created_age,
        "spread_pct": spread,
        "spread_label": f"{float(spread) * 100:.1f}%" if spread is not None else "n/a",
        "depth_imbalance": imbalance,
        "imbalance_label": f"{float(imbalance):+.2f}" if imbalance is not None else "n/a",
        "bid_depth_5pct": row["bid_depth_5pct"],
        "ask_depth_5pct": row["ask_depth_5pct"],
        "perf_all": perf_all,
        "perf_24h": perf_24h,
    }


def _foundation_shadow_performance(db: sqlite3.Connection, *, hours: int | None) -> dict[str, object]:
    if not _table_exists(db, "prediction_shadow_models"):
        return {"markets": 0, "resolved": 0, "correct": 0, "accuracy": 0.0, "avg_brier": None}
    time_filter = ""
    params: tuple[object, ...] = ()
    if hours is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        time_filter = "AND created_at >= ?"
        params = (cutoff,)
    row = db.execute(
        f"""
        WITH latest AS (
            SELECT s.*
            FROM prediction_shadow_models s
            JOIN (
                SELECT market_id, MAX(created_at) AS created_at
                FROM prediction_shadow_models
                WHERE status = 'ok'
                  {time_filter}
                GROUP BY market_id
            ) x ON x.market_id = s.market_id AND x.created_at = s.created_at
        )
        SELECT
            COUNT(*) AS markets,
            SUM(CASE WHEN m.resolved = 1 OR m.provisional_outcome IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
            SUM(
                CASE WHEN (m.resolved = 1 OR m.provisional_outcome IS NOT NULL)
                      AND ((l.prob_up >= 0.5 AND COALESCE(m.outcome, m.provisional_outcome) = 1)
                        OR (l.prob_up < 0.5 AND COALESCE(m.outcome, m.provisional_outcome) = 0))
                     THEN 1 ELSE 0 END
            ) AS correct,
            AVG(
                CASE WHEN m.resolved = 1 OR m.provisional_outcome IS NOT NULL
                     THEN (l.prob_up - COALESCE(m.outcome, m.provisional_outcome))
                        * (l.prob_up - COALESCE(m.outcome, m.provisional_outcome))
                END
            ) AS avg_brier
        FROM latest l
        LEFT JOIN markets m ON m.id = l.market_id
        """,
        params,
    ).fetchone()
    resolved = int(row["resolved"] or 0) if row else 0
    correct = int(row["correct"] or 0) if row else 0
    avg_brier = row["avg_brier"] if row else None
    return {
        "markets": int(row["markets"] or 0) if row else 0,
        "resolved": resolved,
        "correct": correct,
        "accuracy": (correct / resolved * 100.0) if resolved else 0.0,
        "avg_brier": float(avg_brier) if avg_brier is not None else None,
        "avg_brier_label": f"{float(avg_brier):.4f}" if avg_brier is not None else "n/a",
    }


def _coach_type_label(coach_type: str) -> str:
    mapping = {
        "skip_coach": "漏单教练",
        "toxicity_coach": "毒性交易教练",
    }
    return mapping.get(coach_type, coach_type.replace("_", " ").title())


def _latest_coach_findings(limit: int = 10) -> list[dict[str, object]]:
    db = _open_research_db()
    if db is None or not _table_exists(db, "coach_audits"):
        if db is not None:
            db.close()
        return []
    try:
        rows = db.execute(
            """
            SELECT
                a.id,
                a.market_id,
                a.coach_model,
                a.coach_type,
                a.market_question,
                a.regime,
                a.outcome,
                a.resolution_scope,
                a.verdict,
                a.rationale,
                a.helpful,
                a.harmful,
                a.audited_at,
                COALESCE(GROUP_CONCAT(t.tag, ', '), '') AS tag_text
            FROM coach_audits a
            LEFT JOIN coach_audit_tags t ON t.audit_id = a.id
            GROUP BY a.id
            ORDER BY a.audited_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        findings = []
        for row in rows:
            regime_meta = _format_regime_cn(row["regime"] or "UNKNOWN")
            findings.append(
                {
                    "market_id": row["market_id"],
                    "coach_model": row["coach_model"],
                    "coach_type": row["coach_type"],
                    "coach_type_label": _coach_type_label(str(row["coach_type"])),
                    "market_question": row["market_question"],
                    "regime": row["regime"] or "UNKNOWN",
                    "regime_cn": regime_meta["display_cn"],
                    "vol_label": regime_meta["vol_label"],
                    "vol_label_cn": regime_meta["vol_label_cn"],
                    "vol_badge_class": regime_meta["badge_class"],
                    "outcome": int(row["outcome"]),
                    "resolution_scope": row["resolution_scope"],
                    "verdict": row["verdict"],
                    "rationale": row["rationale"] or "n/a",
                    "helpful": bool(row["helpful"]),
                    "harmful": bool(row["harmful"]),
                    "audited_at": row["audited_at"],
                    "audited_at_short": str(row["audited_at"])[5:16].replace("T", " "),
                    "tag_text": row["tag_text"] or "no candidate tags",
                }
            )
        return findings
    finally:
        db.close()


def _coach_rollup(days: int = 7) -> dict[str, object] | None:
    db = _open_research_db()
    if db is None or not _table_exists(db, "coach_audits"):
        if db is not None:
            db.close()
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        summary_row = db.execute(
            """
            SELECT
                COUNT(*) AS official_audits,
                SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) AS helpful,
                SUM(CASE WHEN harmful = 1 THEN 1 ELSE 0 END) AS harmful
            FROM coach_audits
            WHERE resolution_scope = 'official'
              AND audited_at >= ?
            """,
            (cutoff,),
        ).fetchone()
        preview_count = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM coach_audits
            WHERE resolution_scope = 'provisional'
            """
        ).fetchone()["count"]
        type_rows = db.execute(
            """
            SELECT
                coach_type,
                COUNT(*) AS audits,
                SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) AS helpful,
                SUM(CASE WHEN harmful = 1 THEN 1 ELSE 0 END) AS harmful
            FROM coach_audits
            WHERE resolution_scope = 'official'
              AND audited_at >= ?
            GROUP BY coach_type
            ORDER BY audits DESC, coach_type
            """,
            (cutoff,),
        ).fetchall()
        candidate_rows = []
        if _table_exists(db, "coach_rule_candidate_specs"):
            candidate_rows = db.execute(
                """
                SELECT coach_type, tag, regime, spec_name, spec_label, family, target_scope,
                       support_count, precision, net_helpful
                FROM coach_rule_candidate_specs
                WHERE eligible_for_ablation = 1
                ORDER BY net_helpful DESC, support_count DESC, spec_name ASC
                LIMIT 8
                """
            ).fetchall()
        if not candidate_rows and _table_exists(db, "coach_candidate_rollups"):
            candidate_rows = db.execute(
                """
                SELECT coach_type, tag, regime, support_count, precision, net_helpful
                FROM coach_candidate_rollups
                WHERE eligible_for_ablation = 1
                ORDER BY net_helpful DESC, support_count DESC, tag ASC
                LIMIT 8
                """
            ).fetchall()

        helpful = int(summary_row["helpful"] or 0)
        harmful = int(summary_row["harmful"] or 0)
        official_audits = int(summary_row["official_audits"] or 0)
        neutral = max(0, official_audits - helpful - harmful)
        summary = {
            "official_audits": official_audits,
            "helpful": helpful,
            "harmful": harmful,
            "neutral": neutral,
            "net_helpful": helpful - harmful,
            "eligible_candidates": len(candidate_rows),
        }

        type_data = []
        for row in type_rows:
            helpful_count = int(row["helpful"] or 0)
            harmful_count = int(row["harmful"] or 0)
            audits = int(row["audits"] or 0)
            interventions = helpful_count + harmful_count
            precision = helpful_count / interventions if interventions else 0.0
            type_data.append(
                {
                    "coach_type": row["coach_type"],
                    "coach_type_label": _coach_type_label(str(row["coach_type"])),
                    "audits": audits,
                    "helpful": helpful_count,
                    "harmful": harmful_count,
                    "net_helpful": helpful_count - harmful_count,
                    "precision": precision,
                }
            )

        candidate_data = [
            {
                "coach_type": row["coach_type"],
                "coach_type_label": _coach_type_label(str(row["coach_type"])),
                "tag": row["tag"],
                "regime": row["regime"],
                "spec_name": row["spec_name"] if "spec_name" in row.keys() else None,
                "spec_label": row["spec_label"] if "spec_label" in row.keys() else None,
                "family": row["family"] if "family" in row.keys() else None,
                "target_scope": row["target_scope"] if "target_scope" in row.keys() else None,
                "support_count": int(row["support_count"] or 0),
                "precision": float(row["precision"] or 0.0),
                "net_helpful": int(row["net_helpful"] or 0),
            }
            for row in candidate_rows
        ]

        takeaways = []
        if summary["eligible_candidates"]:
            takeaways.append(
                f"- {summary['eligible_candidates']} coach tag(s) cleared the 7-day ablation threshold."
            )
        if summary["net_helpful"] > 0:
            takeaways.append(
                f"- Coaches were net helpful over the last {days} days ({summary['net_helpful']:+d})."
            )
        elif summary["net_helpful"] < 0:
            takeaways.append(
                f"- Coaches added more noise than value over the last {days} days ({summary['net_helpful']:+d})."
            )

        return {
            "summary": summary,
            "type_rows": type_data,
            "candidate_rows": candidate_data,
            "preview_count": int(preview_count or 0),
            "takeaways": takeaways,
        }
    finally:
        db.close()


def _coach_model_summaries(days: int = 7) -> list[dict[str, object]]:
    db = _open_research_db()
    if db is None or not _table_exists(db, "coach_audits"):
        if db is not None:
            db.close()
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = db.execute(
            """
            SELECT
                coach_model,
                COUNT(*) AS audits,
                SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) AS helpful,
                SUM(CASE WHEN harmful = 1 THEN 1 ELSE 0 END) AS harmful,
                MAX(audited_at) AS last_audited_at
            FROM coach_audits
            WHERE resolution_scope = 'official'
              AND audited_at >= ?
            GROUP BY coach_model
            ORDER BY audits DESC, coach_model
            """,
            (cutoff,),
        ).fetchall()
        summaries = []
        for row in rows:
            coach_model = str(row["coach_model"])
            type_rows = db.execute(
                """
                SELECT DISTINCT coach_type
                FROM coach_audits
                WHERE coach_model = ?
                  AND resolution_scope = 'official'
                  AND audited_at >= ?
                ORDER BY coach_type
                """,
                (coach_model, cutoff),
            ).fetchall()
            eligible = 0
            if _table_exists(db, "coach_candidate_rollups"):
                eligible = db.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM coach_candidate_rollups
                    WHERE coach_model = ?
                      AND eligible_for_ablation = 1
                    """,
                    (coach_model,),
                ).fetchone()["count"]
            helpful = int(row["helpful"] or 0)
            harmful = int(row["harmful"] or 0)
            last_audited_at = row["last_audited_at"]
            summaries.append(
                {
                    "coach_model": coach_model,
                    "coach_types": [_coach_type_label(str(item["coach_type"])) for item in type_rows],
                    "audits": int(row["audits"] or 0),
                    "helpful": helpful,
                    "harmful": harmful,
                    "net_helpful": helpful - harmful,
                    "eligible_candidates": int(eligible or 0),
                    "last_audited_at": last_audited_at,
                    "last_audited_at_short": str(last_audited_at)[5:16].replace("T", " ") if last_audited_at else None,
                }
            )
        return summaries
    finally:
        db.close()


def _provisional_settlement_summary(db: sqlite3.Connection, limit: int = 10) -> dict[str, object]:
    rows = db.execute(
        """
        SELECT
            m.id,
            m.question,
            m.end_date,
            m.provisional_outcome,
            m.provisional_resolved_at,
            m.provisional_source,
            (
                SELECT s.price_yes
                FROM market_price_snapshots s
                WHERE s.market_id = m.id
                ORDER BY s.observed_at DESC
                LIMIT 1
            ) AS last_price_yes,
            (
                SELECT s.observed_at
                FROM market_price_snapshots s
                WHERE s.market_id = m.id
                ORDER BY s.observed_at DESC
                LIMIT 1
            ) AS last_observed_at
        FROM markets m
        WHERE m.resolved = 0
          AND m.provisional_outcome IS NOT NULL
        ORDER BY COALESCE(m.provisional_resolved_at, m.end_date) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    records = []
    up_count = 0
    down_count = 0
    sources = set()
    for row in rows:
        record = dict(row)
        if record["provisional_outcome"] == 1:
            up_count += 1
        elif record["provisional_outcome"] == 0:
            down_count += 1
        if record.get("provisional_source"):
            sources.add(str(record["provisional_source"]))
        observed_at = record.get("last_observed_at")
        if observed_at:
            record["last_observed_at_short"] = str(observed_at)[5:16].replace("T", " ")
        else:
            record["last_observed_at_short"] = "n/a"
        records.append(record)

    total = db.execute(
        """
        SELECT COUNT(*)
        FROM markets
        WHERE resolved = 0
          AND provisional_outcome IS NOT NULL
        """
    ).fetchone()[0]

    return {
        "total": int(total or 0),
        "up_count": up_count,
        "down_count": down_count,
        "rows": records,
        "source_label": ", ".join(sorted(sources)) if sources else "gamma outcomePrices",
    }


def _format_ratio(value: object) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if numeric == float("inf"):
        return "inf"
    return f"{numeric:.2f}"


def _latest_predictions_subquery() -> str:
    return """
        SELECT p.*
        FROM predictions p
        JOIN (
            SELECT market_id, agent, MAX(predicted_at) AS latest_predicted_at
            FROM predictions
            GROUP BY market_id, agent
        ) latest
          ON latest.market_id = p.market_id
         AND latest.agent = p.agent
         AND latest.latest_predicted_at = p.predicted_at
    """


def _ensure_prediction_source_schema(db: sqlite3.Connection) -> None:
    try:
        db.execute("ALTER TABLE predictions ADD COLUMN prediction_source TEXT DEFAULT 'legacy_predict_loop'")
        db.commit()
    except sqlite3.OperationalError:
        pass


def _production_recent_summary(
    db: sqlite3.Connection,
    *,
    hours: int,
    agent: str,
) -> dict[str, object] | None:
    _ensure_prediction_source_schema(db)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = db.execute(
        """
        SELECT p.market_id, p.agent, p.estimate, p.regime, p.conviction_score, p.should_trade,
               p.predicted_at, COALESCE(p.market_price_yes_snapshot, m.price_yes) AS market_price_yes_snapshot,
               p.prediction_source, m.price_yes, m.outcome, m.end_date
        FROM predictions p
        JOIN markets m ON m.id = p.market_id
        WHERE p.agent = ?
          AND m.resolved = 1
          AND m.end_date >= ?
          AND p.prediction_source = 'realtime_loop'
        ORDER BY m.end_date DESC
        """,
        (agent, cutoff),
    ).fetchall()
    if not rows:
        return None

    raw_rows = [dict(row) for row in rows]
    latest_rows = select_latest_rows(raw_rows)
    exposure_rows = select_exposure_rows(raw_rows)
    risk = compute_path_risk(raw_rows).get(agent, {})
    called = 0
    correct = 0
    last_trade_at = None

    for row in latest_rows:
        estimate = float(row["estimate"])
        outcome = int(row["outcome"])
        if abs(estimate - 0.5) > 1e-9:
            called += 1
            if (estimate >= 0.5 and outcome == 1) or (estimate < 0.5 and outcome == 0):
                correct += 1
    for row in exposure_rows:
        if _trade_eligible(row):
            last_trade_at = last_trade_at or row["end_date"]

    trade_metrics = compute_pnl(exposure_rows).get(agent, {}) if exposure_rows else {}
    resolved_count = len(latest_rows)
    trade_count = int(trade_metrics.get("num_bets", 0))

    return {
        "window_start": format_et_short(cutoff),
        "window_end": format_et_short(datetime.now(timezone.utc)),
        "resolved_predictions": resolved_count,
        "called_markets": called,
        "traded_predictions": trade_count,
        "signal_win_rate": (correct / called) if called else 0.0,
        "trade_win_rate": float(trade_metrics.get("win_rate", 0.0)),
        "trade_pnl": float(trade_metrics.get("total_pnl", 0.0)),
        "trade_roi": float(trade_metrics.get("roi", 0.0)),
        "skip_rate": 1 - (trade_count / resolved_count if resolved_count else 0.0),
        "last_trade_at": format_et_short(last_trade_at) if last_trade_at else None,
        "prediction_source": "realtime_loop",
        "trade_then_skip_markets": int(risk.get("trade_then_skip_markets", 0)),
        "direction_flip_markets": int(risk.get("direction_flip_markets", 0)),
        "avg_updates_per_market": float(risk.get("avg_updates_per_market", 0.0)),
    }


def _production_regime_breakdown(
    db: sqlite3.Connection,
    *,
    hours: int,
    agent: str,
) -> list[dict[str, object]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = db.execute(
        """
        SELECT p.market_id, p.agent, p.estimate, p.regime, p.conviction_score, p.should_trade,
               p.predicted_at, COALESCE(p.market_price_yes_snapshot, m.price_yes) AS market_price_yes_snapshot,
               m.price_yes, m.outcome, m.end_date
        FROM predictions p
        JOIN markets m ON m.id = p.market_id
        WHERE p.agent = ?
          AND m.resolved = 1
          AND m.end_date >= ?
        ORDER BY m.end_date DESC
        """,
        (agent, cutoff),
    ).fetchall()
    rows = select_exposure_rows([dict(row) for row in rows])
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(row["regime"] or "UNKNOWN")].append(dict(row))

    breakdown = []
    for regime, members in grouped.items():
        regime_meta = _format_regime_cn(regime)
        pnl = compute_pnl(members).get(agent, {})
        breakdown.append(
            {
                "regime": regime,
                "regime_cn": regime_meta["display_cn"],
                "vol_bucket": regime_meta["vol_bucket"],
                "vol_label": regime_meta["vol_label"],
                "vol_label_cn": regime_meta["vol_label_cn"],
                "vol_badge_class": regime_meta["badge_class"],
                "state_label_cn": regime_meta["state_label_cn"],
                "resolved_count": len(members),
                "num_bets": int(pnl.get("num_bets", 0)),
                "win_rate": float(pnl.get("win_rate", 0.0)),
                "total_pnl": float(pnl.get("total_pnl", 0.0)),
                "roi": float(pnl.get("roi", 0.0)),
            }
        )
    return sorted(breakdown, key=lambda row: (-row["num_bets"], row["regime"]))


def _recent_trade_blotter(
    db: sqlite3.Connection,
    *,
    agent: str,
    limit: int,
) -> list[dict[str, object]]:
    rows = db.execute(
        """
        SELECT p.market_id, p.agent, p.estimate, p.regime, p.conviction_score, p.should_trade,
               p.predicted_at, p.reasoning, COALESCE(p.market_price_yes_snapshot, m.price_yes) AS market_price_yes_snapshot,
               m.price_yes, m.outcome, m.end_date
        FROM predictions p
        JOIN markets m ON m.id = p.market_id
        WHERE p.agent = ?
          AND m.resolved = 1
        ORDER BY m.end_date DESC
        """,
        (agent,),
    ).fetchall()
    rows = select_exposure_rows([dict(row) for row in rows])
    trades = []
    for row in rows:
        record = dict(row)
        if not _trade_eligible(record):
            continue
        pnl = compute_pnl([record]).get(agent, {})
        estimate = float(record["estimate"])
        outcome = int(record["outcome"])
        direction = "UP" if estimate >= 0.5 else "DOWN"
        regime_meta = _format_regime_cn(record.get("regime"))
        trades.append(
            {
                "end_date_short": format_et_short(record["end_date"]),
                "direction": direction,
                "regime": record.get("regime") or "UNKNOWN",
                "regime_cn": regime_meta["display_cn"],
                "vol_label": regime_meta["vol_label"],
                "vol_label_cn": regime_meta["vol_label_cn"],
                "vol_badge_class": regime_meta["badge_class"],
                "state_label_cn": regime_meta["state_label_cn"],
                "estimate": estimate,
                "reasoning": record.get("reasoning") or "",
                "pnl": float(pnl.get("total_pnl", 0.0)),
                "won": (direction == "UP" and outcome == 1) or (direction == "DOWN" and outcome == 0),
            }
        )
        if len(trades) >= limit:
            break
    return trades


def _pending_signal_breakdown(
    db: sqlite3.Connection,
    *,
    agent: str,
) -> list[dict[str, object]]:
    rows = db.execute(
        f"""
        SELECT p.regime, p.conviction_score, p.should_trade, m.price_yes
        FROM ({_latest_predictions_subquery()}) p
        JOIN markets m ON m.id = p.market_id
        WHERE p.agent = ?
          AND m.resolved = 0
        ORDER BY m.end_date ASC
        """,
        (agent,),
    ).fetchall()
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(row["regime"] or "UNKNOWN")].append(dict(row))

    summary = []
    for regime, members in grouped.items():
        regime_meta = _format_regime_cn(regime)
        trade_count = sum(1 for row in members if _trade_eligible(row))
        summary.append(
            {
                "regime": regime,
                "regime_cn": regime_meta["display_cn"],
                "vol_bucket": regime_meta["vol_bucket"],
                "vol_label": regime_meta["vol_label"],
                "vol_label_cn": regime_meta["vol_label_cn"],
                "vol_badge_class": regime_meta["badge_class"],
                "state_label_cn": regime_meta["state_label_cn"],
                "count": len(members),
                "trade_count": trade_count,
                "skip_count": len(members) - trade_count,
                "avg_price_yes": sum(float(row["price_yes"]) for row in members) / len(members),
            }
        )
    return sorted(summary, key=lambda row: (-row["count"], row["regime"]))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
