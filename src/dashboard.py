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
    from flask import Flask, render_template_string
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
from score import calculate_path_risk_metrics, calculate_signal_metrics

app = Flask(__name__)
DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
RESEARCH_DB_PATH = Path(__file__).parent.parent / "data" / "v3_research.db"
RESEARCH_REPORT_PATH = Path(__file__).parent.parent / "docs" / "research" / "latest.md"
PRODUCTION_AGENTS = ["contrarian_rule"]

MODEL_COLORS = {
    "contrarian_rule": "#58a6ff",
    "deepseek-ai/DeepSeek-V3": "#3fb950",
    "Pro/zai-org/GLM-5": "#f2cc60",
}


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_prediction_schema(db)
    return db


def get_status() -> dict[str, object]:
    db = get_db()
    try:
        market_counts = db.execute(
            """
            SELECT
                COUNT(*) AS total_markets,
                SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) AS pending_markets,
                SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) AS resolved_markets
            FROM markets
            """
        ).fetchone()
        prediction_counts = db.execute(
            """
            SELECT
                COUNT(*) AS total_predictions,
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
        return {
            "total_markets": market_counts["total_markets"] or 0,
            "pending_markets": market_counts["pending_markets"] or 0,
            "resolved_markets": market_counts["resolved_markets"] or 0,
            "total_predictions": prediction_counts["total_predictions"] or 0,
            "trade_candidates": live_candidates["trade_candidates"] or 0,
            "last_prediction_at": prediction_counts["last_prediction_at"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        db.close()


def build_html() -> str:
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
        context = {
            "status": get_status(),
            "agents": agents,
            "production_agents": production_agents,
            "research_agents": research_agents,
            "signal_metrics": signal_metrics,
            "trade_metrics": trade_metrics,
            "path_risk_metrics": path_risk_metrics,
            "ensemble": ensemble,
            "ev": ev,
            "distribution_svg": build_distribution_svg(trade_metrics),
            "latest_research": _latest_research_summary(),
            "recent_research_runs": _recent_research_runs(limit=5),
            "production_24h": _production_recent_summary(db, hours=24, agent="contrarian_rule"),
            "regime_breakdown_24h": _production_regime_breakdown(db, hours=24, agent="contrarian_rule"),
            "recent_trades": _recent_trade_blotter(db, agent="contrarian_rule", limit=12),
            "pending_breakdown": _pending_signal_breakdown(db, agent="contrarian_rule"),
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
        <meta http-equiv="refresh" content="30">
        <title>Polymarket BTC Dashboard</title>
        <style>
            :root {
                --bg: #0d1117;
                --bg-accent-a: rgba(88,166,255,0.14);
                --bg-accent-b: rgba(63,185,80,0.10);
                --panel: #161b22;
                --panel-soft: #11161d;
                --border: #30363d;
                --muted: #8b949e;
                --text: #c9d1d9;
                --green: #3fb950;
                --red: #f85149;
                --blue: #58a6ff;
                --yellow: #f2cc60;
                --shadow: inset 0 1px 0 rgba(255,255,255,0.03);
                --table-border: rgba(48,54,61,0.75);
                --pill-trade-bg: rgba(63,185,80,0.14);
                --pill-skip-bg: rgba(248,81,73,0.14);
            }
            body[data-theme="light"] {
                --bg: #f6f8fb;
                --bg-accent-a: rgba(47,109,246,0.10);
                --bg-accent-b: rgba(12,166,120,0.08);
                --panel: #ffffff;
                --panel-soft: #f2f5fa;
                --border: #d7dde8;
                --muted: #627085;
                --text: #132033;
                --green: #127c43;
                --red: #bb2d3b;
                --blue: #245dff;
                --yellow: #a56d00;
                --shadow: 0 10px 30px rgba(17,24,39,0.06);
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
                    var(--bg);
                color: var(--text);
                font: 14px/1.5 Inter, "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
                transition: background 0.2s ease, color 0.2s ease;
            }
            .page { max-width: 1380px; margin: 0 auto; padding: 28px 20px 56px; }
            h1, h2, h3 { margin: 0; }
            h1 { font-size: 34px; letter-spacing: -0.03em; }
            h2 { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.12em; }
            .hero { display: flex; justify-content: space-between; gap: 20px; align-items: end; margin-bottom: 22px; }
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
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 20px; }
            .stat, .panel {
                background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.01));
                border: 1px solid var(--border);
                border-radius: 16px;
                box-shadow: var(--shadow);
            }
            .stat { padding: 16px; }
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
                    <h1>Polymarket BTC Dashboard</h1>
                    <p>Signal quality and trade quality are tracked separately. A model can improve Brier without improving trade EV, so the production baseline is evaluated on both axes.</p>
                </div>
                <div class="hero-actions">
                    <button class="theme-toggle" id="theme-toggle" type="button">Switch to Light</button>
                    <div class="stamp">Generated {{ status.generated_at }}</div>
                </div>
            </div>

            <div class="stats">
                <div class="stat">
                    <div class="label">Pending Markets</div>
                    <div class="value">{{ status.pending_markets }}</div>
                    <div class="meta">Trade candidates: {{ status.trade_candidates }}</div>
                </div>
                <div class="stat">
                    <div class="label">Resolved Markets</div>
                    <div class="value">{{ status.resolved_markets }}</div>
                    <div class="meta">Predictions stored: {{ status.total_predictions }}</div>
                </div>
                <div class="stat">
                    <div class="label">Ensemble Bets</div>
                    <div class="value">{{ ensemble.num_bets }}</div>
                    <div class="meta">Skipped: {{ ensemble.num_skipped }}</div>
                </div>
                <div class="stat">
                    <div class="label">Breakeven Margin</div>
                    <div class="value {{ 'metric-pos' if ev.margin >= 0 else 'metric-neg' }}">{{ (ev.margin * 100)|round(1) }}%</div>
                    <div class="meta">Current WR {{ (ev.current_wr * 100)|round(1) }}% vs breakeven {{ (ev.breakeven_wr * 100)|round(1) }}%</div>
                </div>
            </div>

            <div class="subgrid">
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Production Last 24h</h2>
                            <div class="muted">Latest snapshot for signal quality, first exposure for trade risk</div>
                        </div>
                    </div>
                    {% if production_24h %}
                    <div class="mini-grid">
                        <div class="mini-card">
                            <div class="label">Resolved</div>
                            <div class="value">{{ production_24h.resolved_predictions }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Called</div>
                            <div class="value">{{ production_24h.called_markets }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Trades</div>
                            <div class="value">{{ production_24h.traded_predictions }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Signal WR</div>
                            <div class="value {{ 'metric-pos' if production_24h.signal_win_rate >= 0.5 else 'metric-neg' }}">{{ (production_24h.signal_win_rate * 100)|round(1) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Trade WR</div>
                            <div class="value {{ 'metric-pos' if production_24h.trade_win_rate >= 0.5 else 'metric-neg' }}">{{ (production_24h.trade_win_rate * 100)|round(1) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Trade ROI</div>
                            <div class="value {{ 'metric-pos' if production_24h.trade_roi >= 0 else 'metric-neg' }}">{{ production_24h.trade_roi|round(2) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Trade P&amp;L</div>
                            <div class="value {{ 'metric-pos' if production_24h.trade_pnl >= 0 else 'metric-neg' }}">{{ production_24h.trade_pnl|round(2) }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Skip Rate</div>
                            <div class="value">{{ (production_24h.skip_rate * 100)|round(1) }}%</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Trade-&gt;Skip</div>
                            <div class="value">{{ production_24h.trade_then_skip_markets }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Dir Flips</div>
                            <div class="value">{{ production_24h.direction_flip_markets }}</div>
                        </div>
                        <div class="mini-card">
                            <div class="label">Avg Updates</div>
                            <div class="value">{{ production_24h.avg_updates_per_market|round(2) }}</div>
                        </div>
                    </div>
                    <div class="section-note">
                        Window {{ production_24h.window_start }} -> {{ production_24h.window_end }}.
                        Last trade {{ production_24h.last_trade_at or 'n/a' }}.
                        Trade metrics use first actionable exposure; signal metrics use latest snapshot.
                    </div>
                    {% else %}
                    <div class="empty">No production slice available yet.</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Pending Decision Breakdown</h2>
                            <div class="muted">Current unresolved markets split by trade state and regime</div>
                        </div>
                    </div>
                    {% if pending_breakdown %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Regime</th>
                                <th>Markets</th>
                                <th>Trades</th>
                                <th>Skips</th>
                                <th>Avg Yes</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in pending_breakdown %}
                            <tr>
                                <td>{{ row.regime }}</td>
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
                    <div class="empty">No pending production markets.</div>
                    {% endif %}
                </div>
            </div>

            <div class="grid">
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Latest Research Promotion</h2>
                            <div class="muted">Most recent baseline vs challenger decision</div>
                        </div>
                    </div>
                    {% if latest_research %}
                    <div>
                        <div>
                            <strong>{{ latest_research.challenger }}</strong>
                            vs
                            <strong>{{ latest_research.baseline }}</strong>
                            <span class="pill {{ 'trade' if latest_research.passed else 'skip' }}">{{ 'PASS' if latest_research.passed else 'FAIL' }}</span>
                        </div>
                        <div class="muted">Run {{ latest_research.run_id }} at {{ latest_research.created_at }}</div>
                    </div>
                    <div class="report-box">
                        <div class="report-metric">
                            <div class="label">ROI Delta</div>
                            <div class="value {{ 'metric-pos' if latest_research.aggregate_roi_delta >= 0 else 'metric-neg' }}">{{ latest_research.aggregate_roi_delta|round(2) }}pp</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">WR Delta</div>
                            <div class="value {{ 'metric-pos' if latest_research.aggregate_win_rate_delta >= 0 else 'metric-neg' }}">{{ latest_research.aggregate_win_rate_delta|round(2) }}pp</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">Passing Folds</div>
                            <div class="value">{{ latest_research.passing_folds }}/{{ latest_research.required_fold_passes }}</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">Trade Ratio</div>
                            <div class="value">{{ latest_research.trade_ratio_display }}</div>
                        </div>
                        <div class="report-metric">
                            <div class="label">Drawdown Ratio</div>
                            <div class="value">{{ latest_research.drawdown_ratio_display }}</div>
                        </div>
                    </div>
                    {% if latest_research.reasons %}
                    <div class="logic" style="margin-top: 12px;">{{ latest_research.reasons|join('\n') }}</div>
                    {% endif %}
                    {% if latest_research.report_href %}
                    <div class="muted" style="margin-top: 12px;"><a href="{{ latest_research.report_href }}">Open latest research report</a></div>
                    {% endif %}
                    {% if recent_research_runs %}
                    <div class="table-wrap" style="margin-top: 14px;">
                    <table>
                        <thead>
                            <tr>
                                <th>When</th>
                                <th>Challenger</th>
                                <th>Gate</th>
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
                                <td><span class="pill {{ 'trade' if run.passed else 'skip' }}">{{ 'PASS' if run.passed else 'FAIL' }}</span></td>
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
                    <div class="empty">No research promotion run recorded yet.</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Signal Metrics</h2>
                            <div class="muted">Current production strategy only, latest snapshot per market</div>
                        </div>
                    </div>
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Agent</th>
                                <th>Resolved</th>
                                <th>Call Acc</th>
                                <th>Avg Brier</th>
                                <th>vs Market</th>
                                <th>Trade Rate</th>
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
                                <td colspan="5" class="muted">No resolved rows yet</td>
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
                            <h2>Trade Metrics</h2>
                            <div class="muted">Current production strategy only, first trade exposure per market</div>
                        </div>
                    </div>
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Agent</th>
                                <th>Bets</th>
                                <th>Win Rate</th>
                                <th>P&amp;L</th>
                                <th>ROI</th>
                                <th>Max DD</th>
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
                                <td colspan="5" class="muted">No traded rows yet</td>
                                {% endif %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                </div>
            </div>

            <div class="grid">
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Research Agents</h2>
                            <div class="muted">Historical challengers and retired agents. These do not drive live predictions.</div>
                        </div>
                    </div>
                    {% if research_agents %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Agent</th>
                                <th>Resolved</th>
                                <th>Call Acc</th>
                                <th>Avg Brier</th>
                                <th>Trade P&amp;L</th>
                                <th>Trade ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for agent in research_agents %}
                            {% set s = signal_metrics.get(agent) %}
                            {% set t = trade_metrics.get(agent) %}
                            <tr>
                                <td><span class="agent-dot" style="background: {{ model_colors.get(agent, '#8b949e') }}"></span>{{ agent }}</td>
                                <td>{{ s.resolved_count if s else 0 }}</td>
                                <td>{{ ((s.directional_accuracy * 100)|round(1) ~ '%') if s else 'n/a' }}</td>
                                <td>{{ s.avg_brier|round(4) if s else 'n/a' }}</td>
                                <td class="{{ 'metric-pos' if t and t.total_pnl >= 0 else 'metric-neg' if t else '' }}">{{ t.total_pnl|round(2) if t else 'n/a' }}</td>
                                <td class="{{ 'metric-pos' if t and t.roi >= 0 else 'metric-neg' if t else '' }}">{{ ((t.roi|round(2)) ~ '%') if t else 'n/a' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">No historical research agents yet.</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>P&amp;L Distribution</h2>
                            <div class="muted">Per-bet outcomes across all traded predictions</div>
                        </div>
                    </div>
                    {{ distribution_svg|safe }}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Ensemble Summary</h2>
                            <div class="muted">Consensus metrics using only trade-eligible rows</div>
                        </div>
                    </div>
                    <div class="table-wrap">
                    <table>
                        <tbody>
                            <tr><th>Total P&amp;L</th><td class="{{ 'metric-pos' if ensemble.total_pnl >= 0 else 'metric-neg' }}">{{ ensemble.total_pnl|round(2) }}</td></tr>
                            <tr><th>Total Wagered</th><td>{{ ensemble.total_wagered|round(2) }}</td></tr>
                            <tr><th>ROI</th><td class="{{ 'metric-pos' if ensemble.roi >= 0 else 'metric-neg' }}">{{ ensemble.roi|round(2) }}%</td></tr>
                            <tr><th>Bets / Skips</th><td>{{ ensemble.num_bets }} / {{ ensemble.num_skipped }}</td></tr>
                            <tr><th>EV per Bet</th><td class="{{ 'metric-pos' if ev.ev >= 0 else 'metric-neg' }}">{{ ev.ev|round(2) }}</td></tr>
                        </tbody>
                    </table>
                    </div>
                </div>
            </div>

            <div class="subgrid">
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>24h Regime Breakdown</h2>
                            <div class="muted">Production trades grouped by regime over the last 24 hours</div>
                        </div>
                    </div>
                    {% if regime_breakdown_24h %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Regime</th>
                                <th>Resolved</th>
                                <th>Trades</th>
                                <th>WR</th>
                                <th>P&amp;L</th>
                                <th>ROI</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in regime_breakdown_24h %}
                            <tr>
                                <td>{{ row.regime }}</td>
                                <td>{{ row.resolved_count }}</td>
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
                    <div class="empty">No traded production rows in the last 24 hours.</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Recent Trades</h2>
                            <div class="muted">Latest resolved production trades with realized outcome</div>
                        </div>
                    </div>
                    {% if recent_trades %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>When</th>
                                <th>Direction</th>
                                <th>Regime</th>
                                <th>Est</th>
                                <th>Market</th>
                                <th>P&amp;L</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for trade in recent_trades %}
                            <tr>
                                <td class="nowrap">{{ trade.end_date_short }}</td>
                                <td><span class="pill {{ 'up' if trade.direction == 'UP' else 'down' }}">{{ trade.direction }}</span></td>
                                <td>{{ trade.regime }}</td>
                                <td>{{ (trade.estimate * 100)|round(1) }}%</td>
                                <td>
                                    <div class="{{ 'metric-pos' if trade.won else 'metric-neg' }}">{{ 'win' if trade.won else 'loss' }}</div>
                                    <div class="logic">{{ trade.reasoning }}</div>
                                </td>
                                <td class="{{ 'metric-pos' if trade.pnl >= 0 else 'metric-neg' }}">{{ trade.pnl|round(2) }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">No recent production trades.</div>
                    {% endif %}
                </div>
            </div>

            <div class="matrix">
                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Pending Markets</h2>
                            <div class="muted">Latest signal per market and agent</div>
                        </div>
                    </div>
                    {% if pending %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Market</th>
                                <th>Market Price</th>
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
                                    <div class="muted">Ends {{ row.market.end_date }}</div>
                                </td>
                                <td>{{ (row.market.price_yes * 100)|round(1) }}%</td>
                                {% for agent in production_agents %}
                                {% set p = row.predictions.get(agent) %}
                                <td>
                                    {% if p %}
                                    <div><span class="pill {{ 'trade' if p.should_trade else 'skip' }}">{{ 'TRADE' if p.should_trade else 'SKIP' }}</span></div>
                                    <div><strong>{{ (p.estimate * 100)|round(1) }}%</strong> {{ p.direction or 'NEUTRAL' }}</div>
                                    <div class="muted">{{ p.regime }} | conviction {{ p.conviction_score }}</div>
                                    <div class="logic">{{ p.reasoning }}</div>
                                    {% else %}
                                    <span class="muted">No signal</span>
                                    {% endif %}
                                </td>
                                {% endfor %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">No pending markets.</div>
                    {% endif %}
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>Recent Settled Markets</h2>
                            <div class="muted">Latest resolved markets with signal and trade outcome</div>
                        </div>
                    </div>
                    {% if recent %}
                    <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Market</th>
                                <th>Outcome</th>
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
                                    <div class="muted">Resolved {{ row.market.end_date }}</div>
                                </td>
                                <td><span class="pill {{ 'up' if row.market.outcome == 1 else 'down' }}">{{ 'UP' if row.market.outcome == 1 else 'DOWN' }}</span></td>
                                {% for agent in production_agents %}
                                {% set p = row.predictions.get(agent) %}
                                <td>
                                    {% if p %}
                                    <div><span class="pill {{ 'trade' if p.should_trade else 'skip' }}">{{ 'TRADE' if p.should_trade else 'SKIP' }}</span></div>
                                    <div><strong>{{ (p.estimate * 100)|round(1) }}%</strong> {{ p.direction or 'NEUTRAL' }}</div>
                                    <div class="{{ 'metric-pos' if p.correct_call else 'metric-neg' }}">{{ 'signal correct' if p.correct_call else 'signal wrong' }}</div>
                                    {% if p.trade_pnl is not none %}
                                    <div class="{{ 'metric-pos' if p.trade_pnl >= 0 else 'metric-neg' }}">trade P&amp;L {{ p.trade_pnl|round(2) }}</div>
                                    {% endif %}
                                    <div class="logic">{{ p.reasoning }}</div>
                                    {% else %}
                                    <span class="muted">No signal</span>
                                    {% endif %}
                                </td>
                                {% endfor %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                    {% else %}
                    <div class="empty">No settled markets.</div>
                    {% endif %}
                </div>
            </div>
        </div>
        <script>
            (function () {
                const storageKey = "predict-dashboard-theme";
                const body = document.body;
                const button = document.getElementById("theme-toggle");

                function applyTheme(theme) {
                    body.setAttribute("data-theme", theme);
                    if (button) {
                        button.textContent = theme === "light" ? "Switch to Dark" : "Switch to Light";
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
        return render_template_string(template, model_colors=MODEL_COLORS, **context)


@app.route("/")
def index() -> str:
    return build_html()


def _agent_order(db: sqlite3.Connection, signal_metrics: dict, trade_metrics: dict) -> list[str]:
    agents = set(signal_metrics.keys()) | set(trade_metrics.keys())
    if not agents:
        rows = db.execute("SELECT DISTINCT agent FROM predictions ORDER BY agent").fetchall()
        agents = {row["agent"] for row in rows}
    if not agents:
        agents = {"contrarian_rule"}
    return sorted(agents, key=lambda agent: (0 if agent == "contrarian_rule" else 1, agent))


def _fetch_resolved_prediction_rows(db: sqlite3.Connection) -> list[dict[str, object]]:
    rows = db.execute(
        """
        SELECT
            p.market_id,
            p.agent,
            p.estimate,
            p.predicted_at,
            p.conviction_score,
            p.should_trade,
            COALESCE(p.market_price_yes_snapshot, m.price_yes) AS market_price_yes_snapshot,
            m.price_yes,
            m.outcome
        FROM predictions p
        JOIN markets m ON m.id = p.market_id
        WHERE m.resolved = 1
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


def _latest_research_summary() -> dict[str, object] | None:
    if not RESEARCH_DB_PATH.exists():
        return None

    db = sqlite3.connect(RESEARCH_DB_PATH)
    db.row_factory = sqlite3.Row
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
            "report_href": "research/latest.md" if RESEARCH_REPORT_PATH.exists() else None,
        }
    finally:
        db.close()


def _recent_research_runs(limit: int = 5) -> list[dict[str, object]]:
    if not RESEARCH_DB_PATH.exists():
        return []

    db = sqlite3.connect(RESEARCH_DB_PATH)
    db.row_factory = sqlite3.Row
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
                    "created_at_short": row["created_at"][5:16].replace("T", " "),
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


def _production_recent_summary(
    db: sqlite3.Connection,
    *,
    hours: int,
    agent: str,
) -> dict[str, object] | None:
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
        "window_start": cutoff[5:16].replace("T", " "),
        "window_end": datetime.now(timezone.utc).isoformat()[5:16].replace("T", " "),
        "resolved_predictions": resolved_count,
        "called_markets": called,
        "traded_predictions": trade_count,
        "signal_win_rate": (correct / called) if called else 0.0,
        "trade_win_rate": float(trade_metrics.get("win_rate", 0.0)),
        "trade_pnl": float(trade_metrics.get("total_pnl", 0.0)),
        "trade_roi": float(trade_metrics.get("roi", 0.0)),
        "skip_rate": 1 - (trade_count / resolved_count if resolved_count else 0.0),
        "last_trade_at": last_trade_at[5:16].replace("T", " ") if last_trade_at else None,
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
        pnl = compute_pnl(members).get(agent, {})
        breakdown.append(
            {
                "regime": regime,
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
        trades.append(
            {
                "end_date_short": record["end_date"][5:16].replace("T", " "),
                "direction": direction,
                "regime": record.get("regime") or "UNKNOWN",
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
        trade_count = sum(1 for row in members if _trade_eligible(row))
        summary.append(
            {
                "regime": regime,
                "count": len(members),
                "trade_count": trade_count,
                "skip_count": len(members) - trade_count,
                "avg_price_yes": sum(float(row["price_yes"]) for row in members) / len(members),
            }
        )
    return sorted(summary, key=lambda row: (-row["count"], row["regime"]))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
