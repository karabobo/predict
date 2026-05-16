"""
signal_arena_app.py - focused frontend for BTC 5m realtime rule evaluation.

This view intentionally excludes backtest, coach, and ops pages. It reads the
current SQLite state and explains live decisions from decision_audit.
"""

from __future__ import annotations

import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, render_template_string

from fetch_markets import DB_PATH

app = Flask(__name__)


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def build_view_model(db: sqlite3.Connection) -> dict[str, Any]:
    current_market = fetch_current_market(db)
    latest_window = fetch_latest_decision_window(db)
    rows = fetch_decision_rows(db, latest_window)
    paper_summary = fetch_paper_summary(db)
    paper_orders = fetch_recent_paper_orders(db)
    rule_arena = fetch_rule_arena(db)
    status_counts = fetch_decision_status_counts(db)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_market": current_market,
        "latest_window": latest_window,
        "decision_rows": rows,
        "paper_summary": paper_summary,
        "paper_orders": paper_orders,
        "rule_arena": rule_arena,
        "status_counts": status_counts,
        "final_decision": next((row for row in rows if row["stage"] == "final"), None),
        "rule_rows": [row for row in rows if row["stage"] == "rule"],
        "prior_rows": [row for row in rows if row["stage"] == "prior"],
        "book_rows": [row for row in rows if row["stage"] == "book"],
    }


def fetch_current_market(db: sqlite3.Connection) -> dict[str, Any] | None:
    if not table_exists(db, "markets"):
        return None
    now_iso = datetime.now(timezone.utc).isoformat()
    row = db.execute(
        """
        SELECT id, question, price_yes, price_no, end_date, token_yes, token_no
        FROM markets
        WHERE COALESCE(resolved, 0) = 0
          AND end_date > ?
        ORDER BY end_date ASC
        LIMIT 1
        """,
        (now_iso,),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["seconds_to_expiry"] = _seconds_to(record.get("end_date"))
    return record


def fetch_latest_decision_window(db: sqlite3.Connection) -> dict[str, Any] | None:
    if not table_exists(db, "decision_audit"):
        return None
    row = db.execute(
        """
        SELECT market_id, cycle, rule_profile, entry_offset_seconds, MAX(created_at) AS latest_at
        FROM decision_audit
        GROUP BY market_id, cycle, rule_profile, entry_offset_seconds
        ORDER BY latest_at DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def fetch_decision_rows(
    db: sqlite3.Connection,
    latest_window: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if latest_window is None or not table_exists(db, "decision_audit"):
        return []
    rows = db.execute(
        """
        SELECT *
        FROM decision_audit
        WHERE market_id = ?
          AND cycle = ?
          AND rule_profile = ?
          AND entry_offset_seconds = ?
        ORDER BY
          CASE stage
            WHEN 'rule' THEN 1
            WHEN 'prior' THEN 2
            WHEN 'book' THEN 3
            WHEN 'final' THEN 4
            ELSE 9
          END,
          id ASC
        """,
        (
            latest_window["market_id"],
            latest_window["cycle"],
            latest_window["rule_profile"],
            latest_window["entry_offset_seconds"],
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_decision_status_counts(db: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(db, "decision_audit"):
        return []
    rows = db.execute(
        """
        SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS n, MAX(created_at) AS latest_at
        FROM decision_audit
        WHERE created_at >= datetime('now', '-6 hours')
        GROUP BY COALESCE(status, 'unknown')
        ORDER BY n DESC, latest_at DESC
        LIMIT 8
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_rule_arena(db: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(db, "decision_audit"):
        return []
    rows = db.execute(
        """
        WITH rule_rows AS (
            SELECT
                d.rule_profile,
                d.rule_name,
                d.should_trade,
                d.direction,
                d.estimate,
                d.status,
                d.created_at,
                COALESCE(m.outcome, m.provisional_outcome) AS effective_outcome,
                CASE WHEN m.resolved = 1 OR m.provisional_outcome IS NOT NULL THEN 1 ELSE 0 END AS is_resolved
            FROM decision_audit d
            LEFT JOIN markets m ON m.id = d.market_id
            WHERE d.stage = 'rule'
              AND d.rule_name IS NOT NULL
        )
        SELECT
            rule_profile,
            rule_name,
            COUNT(*) AS evaluations,
            SUM(CASE WHEN should_trade = 1 THEN 1 ELSE 0 END) AS signals,
            SUM(CASE WHEN should_trade = 1 AND is_resolved = 1 THEN 1 ELSE 0 END) AS resolved_signals,
            SUM(
                CASE
                    WHEN should_trade = 1 AND is_resolved = 1
                         AND ((direction = 'UP' AND effective_outcome = 1)
                           OR (direction = 'DOWN' AND effective_outcome = 0))
                    THEN 1 ELSE 0
                END
            ) AS wins,
            AVG(CASE WHEN should_trade = 1 THEN ABS(estimate - 0.5) ELSE NULL END) AS avg_model_edge,
            MAX(created_at) AS latest_at
        FROM rule_rows
        GROUP BY rule_profile, rule_name
        ORDER BY signals DESC, evaluations DESC, latest_at DESC
        LIMIT 24
        """
    ).fetchall()
    arena = []
    for row in rows:
        item = dict(row)
        resolved = int(item.get("resolved_signals") or 0)
        wins = int(item.get("wins") or 0)
        signals = int(item.get("signals") or 0)
        evaluations = int(item.get("evaluations") or 0)
        item["signal_rate"] = signals / evaluations if evaluations else 0.0
        item["win_rate"] = wins / resolved if resolved else None
        arena.append(item)
    return arena


def fetch_paper_summary(db: sqlite3.Connection) -> dict[str, Any]:
    if not table_exists(db, "paper_orders"):
        return {"orders": 0, "filled": 0, "settled": 0, "wins": 0, "pnl_usd": 0.0, "roi": 0.0}
    row = db.execute(
        """
        SELECT
            COUNT(*) AS orders,
            SUM(CASE WHEN status = 'paper_filled' THEN 1 ELSE 0 END) AS filled,
            SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(COALESCE(pnl_usd, 0.0)) AS pnl_usd,
            SUM(COALESCE(bet_amount_usd, 0.0)) AS stake
        FROM paper_orders
        """
    ).fetchone()
    summary = dict(row) if row else {}
    stake = float(summary.get("stake") or 0.0)
    summary["roi"] = float(summary.get("pnl_usd") or 0.0) / stake if stake else 0.0
    return summary


def fetch_recent_paper_orders(db: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(db, "paper_orders"):
        return []
    rows = db.execute(
        """
        SELECT
            id, market_id, rule_profile, rule_name, direction, status,
            entry_offset_seconds, market_price, expected_edge, fill_source,
            settlement_source, won, pnl_usd, created_at
        FROM paper_orders
        ORDER BY id DESC
        LIMIT 16
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _seconds_to(value: Any) -> int | None:
    if not value:
        return None
    try:
        end = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(int((end - datetime.now(timezone.utc)).total_seconds()), 0)
    except ValueError:
        return None


def _fmt_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_money(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"${float(value):+.2f}"
    except (TypeError, ValueError):
        return "-"


@app.route("/")
def index() -> str:
    db = connect()
    try:
        model = build_view_model(db)
    finally:
        db.close()
    return render_template_string(TEMPLATE, model=model, pct=_fmt_pct, num=_fmt_num, money=_fmt_money)


TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>BTC 5m Signal Arena</title>
  <style>
    :root {
      --bg: #0f1216;
      --panel: #171b21;
      --panel-2: #11151a;
      --line: #2b333d;
      --text: #e8edf2;
      --muted: #8e9aaa;
      --green: #34c77b;
      --red: #ff675f;
      --blue: #58a6ff;
      --yellow: #d6a84f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
    }
    .shell { display: grid; grid-template-columns: 220px 1fr; min-height: 100vh; }
    nav {
      border-right: 1px solid var(--line);
      background: #0c0f13;
      padding: 18px 14px;
      position: sticky;
      top: 0;
      height: 100vh;
    }
    .brand { font-size: 17px; font-weight: 800; margin-bottom: 18px; letter-spacing: .01em; }
    .nav-item {
      display: block;
      color: var(--muted);
      text-decoration: none;
      padding: 9px 10px;
      border-radius: 6px;
      margin-bottom: 4px;
    }
    .nav-item.active { color: var(--text); background: #1b222b; }
    main { padding: 18px 20px 44px; max-width: 1500px; width: 100%; }
    .topbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
    h1 { margin: 0; font-size: 24px; line-height: 1.1; }
    h2 { margin: 0 0 12px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }
    .stamp { color: var(--muted); font-size: 12px; white-space: nowrap; }
    .grid { display: grid; gap: 14px; }
    .grid.kpis { grid-template-columns: repeat(6, minmax(120px, 1fr)); margin-bottom: 14px; }
    .grid.main { grid-template-columns: minmax(520px, 1.4fr) minmax(360px, .8fr); }
    .panel {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .kpi {
      border: 1px solid var(--line);
      background: var(--panel-2);
      border-radius: 8px;
      padding: 12px;
      min-height: 82px;
    }
    .kpi .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .07em; }
    .kpi .value { font-size: 24px; font-weight: 800; margin-top: 5px; }
    .kpi .meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .market-title { font-size: 18px; font-weight: 800; margin-bottom: 8px; }
    .muted { color: var(--muted); }
    .status { display: inline-flex; align-items: center; gap: 7px; font-weight: 700; }
    .dot { width: 9px; height: 9px; border-radius: 99px; background: var(--green); }
    .dot.red { background: var(--red); }
    .pill {
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      white-space: nowrap;
    }
    .pill.good { color: var(--green); border-color: rgba(52,199,123,.45); background: rgba(52,199,123,.08); }
    .pill.bad { color: var(--red); border-color: rgba(255,103,95,.45); background: rgba(255,103,95,.08); }
    .pill.info { color: var(--blue); border-color: rgba(88,166,255,.45); background: rgba(88,166,255,.08); }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px 7px; border-bottom: 1px solid var(--line); vertical-align: top; text-align: left; }
    th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; font-weight: 700; }
    tr:last-child td { border-bottom: 0; }
    .table-wrap { overflow-x: auto; }
    .table-wrap table { min-width: 760px; }
    .reason { max-width: 520px; color: var(--muted); font-size: 12px; }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
    .pos { color: var(--green); }
    .neg { color: var(--red); }
    .empty { color: var(--muted); padding: 18px 0; }
    @media (max-width: 1050px) {
      .shell { grid-template-columns: 1fr; }
      nav { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
      .grid.kpis { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
      .grid.main, .split { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <nav>
      <div class="brand">BTC 5m Arena</div>
      <a class="nav-item active" href="#live">Live</a>
      <a class="nav-item" href="#audit">Decision Audit</a>
      <a class="nav-item" href="#rules">Signal Arena</a>
      <a class="nav-item" href="#paper">Paper</a>
    </nav>
    <main>
      <div class="topbar">
        <div>
          <h1>5分钟市场模型与规则信号台</h1>
          <div class="muted">专注当前阶段：BTC 5m、分流规则、alpha 候选、paper 反馈。</div>
        </div>
        <div class="stamp">UTC {{ model.generated_at }}</div>
      </div>

      <section class="grid kpis">
        <div class="kpi">
          <div class="label">Current Market</div>
          <div class="value">{{ model.current_market.id if model.current_market else "-" }}</div>
          <div class="meta">{{ model.current_market.seconds_to_expiry if model.current_market else "-" }}s to expiry</div>
        </div>
        <div class="kpi">
          <div class="label">Audit Window</div>
          <div class="value">{{ model.latest_window.entry_offset_seconds if model.latest_window else "-" }}s</div>
          <div class="meta">{{ model.latest_window.rule_profile if model.latest_window else "waiting" }}</div>
        </div>
        <div class="kpi">
          <div class="label">Final Decision</div>
          <div class="value">{{ model.final_decision.status if model.final_decision else "-" }}</div>
          <div class="meta">{{ model.final_decision.rule_name if model.final_decision else "-" }}</div>
        </div>
        <div class="kpi">
          <div class="label">Paper Orders</div>
          <div class="value">{{ model.paper_summary.orders or 0 }}</div>
          <div class="meta">filled {{ model.paper_summary.filled or 0 }}</div>
        </div>
        <div class="kpi">
          <div class="label">Paper PnL</div>
          <div class="value {{ 'pos' if (model.paper_summary.pnl_usd or 0) >= 0 else 'neg' }}">{{ money(model.paper_summary.pnl_usd) }}</div>
          <div class="meta">ROI {{ pct(model.paper_summary.roi) }}</div>
        </div>
        <div class="kpi">
          <div class="label">Settled</div>
          <div class="value">{{ model.paper_summary.settled or 0 }}</div>
          <div class="meta">wins {{ model.paper_summary.wins or 0 }}</div>
        </div>
      </section>

      <section id="live" class="grid main">
        <div class="panel">
          <h2>Live Market</h2>
          {% if model.current_market %}
            <div class="market-title">{{ model.current_market.question }}</div>
            <div class="muted">YES {{ num(model.current_market.price_yes, 2) }} / NO {{ num(model.current_market.price_no, 2) }} / Market {{ model.current_market.id }}</div>
            <div style="margin-top:10px;">
              {% if model.final_decision %}
                <span class="status"><span class="dot {{ 'red' if model.final_decision.status != 'paper_filled' else '' }}"></span>{{ model.final_decision.status }}</span>
                <span class="pill info">offset {{ model.final_decision.entry_offset_seconds }}s</span>
                <span class="pill">{{ model.final_decision.rule_profile }}</span>
              {% else %}
                <span class="status"><span class="dot red"></span>waiting for entry audit</span>
              {% endif %}
            </div>
            {% if model.final_decision and model.final_decision.reason %}
              <div class="reason" style="margin-top:10px;">{{ model.final_decision.reason }}</div>
            {% endif %}
          {% else %}
            <div class="empty">No active BTC 5m market found.</div>
          {% endif %}
        </div>

        <div class="panel">
          <h2>Recent Status</h2>
          {% if model.status_counts %}
            <table>
              <thead><tr><th>Status</th><th>Count</th><th>Latest</th></tr></thead>
              <tbody>
                {% for row in model.status_counts %}
                  <tr><td>{{ row.status }}</td><td>{{ row.n }}</td><td class="muted">{{ row.latest_at }}</td></tr>
                {% endfor %}
              </tbody>
            </table>
          {% else %}
            <div class="empty">No decision audit status yet.</div>
          {% endif %}
        </div>
      </section>

      <section id="audit" class="panel" style="margin-top:14px;">
        <h2>Decision Audit</h2>
        {% if model.decision_rows %}
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Stage</th><th>Rule</th><th>Status</th><th>Trade</th><th>Dir</th><th>Estimate</th>
                  <th>Prior</th><th>Book</th><th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {% for row in model.decision_rows %}
                <tr>
                  <td><span class="pill {{ 'good' if row.stage == 'final' and row.status == 'paper_filled' else 'info' if row.stage in ['prior','book'] else '' }}">{{ row.stage }}</span></td>
                  <td>{{ row.rule_name or "-" }}</td>
                  <td>{{ row.status or "-" }}</td>
                  <td>{{ "yes" if row.should_trade else "no" if row.should_trade is not none else "-" }}</td>
                  <td>{{ row.direction or "-" }}</td>
                  <td>{{ num(row.estimate) }}</td>
                  <td>{{ num(row.prior_prob) }} / edge {{ num(row.prior_edge) }}</td>
                  <td>ready {{ "yes" if row.book_ready else "no" if row.book_ready is not none else "-" }} / ask {{ num(row.best_ask) }}</td>
                  <td><div class="reason">{{ row.reason or "-" }}</div></td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% else %}
          <div class="empty">No audit rows yet. Wait for the next 5m decision evaluation.</div>
        {% endif %}
      </section>

      <section id="rules" class="panel" style="margin-top:14px;">
        <h2>5m Signal Arena</h2>
        {% if model.rule_arena %}
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Rule</th><th>Profile</th><th>Evaluations</th><th>Signals</th><th>Signal Rate</th>
                  <th>Resolved</th><th>Win Rate</th><th>Avg Model Edge</th><th>Latest</th>
                </tr>
              </thead>
              <tbody>
                {% for row in model.rule_arena %}
                  <tr>
                    <td><strong>{{ row.rule_name }}</strong></td>
                    <td class="muted">{{ row.rule_profile }}</td>
                    <td>{{ row.evaluations }}</td>
                    <td>{{ row.signals or 0 }}</td>
                    <td>{{ pct(row.signal_rate) }}</td>
                    <td>{{ row.resolved_signals or 0 }}</td>
                    <td>{{ pct(row.win_rate) }}</td>
                    <td>{{ num(row.avg_model_edge) }}</td>
                    <td class="muted">{{ row.latest_at }}</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% else %}
          <div class="empty">No rule audit rows yet.</div>
        {% endif %}
      </section>

      <section id="paper" class="panel" style="margin-top:14px;">
        <h2>Paper Orders</h2>
        {% if model.paper_orders %}
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th><th>Market</th><th>Rule</th><th>Dir</th><th>Status</th><th>Offset</th>
                  <th>Price</th><th>Edge</th><th>Fill</th><th>Settlement</th><th>PnL</th><th>Created</th>
                </tr>
              </thead>
              <tbody>
                {% for row in model.paper_orders %}
                  <tr>
                    <td>{{ row.id }}</td>
                    <td>{{ row.market_id }}</td>
                    <td>{{ row.rule_name or "-" }}<br><span class="muted">{{ row.rule_profile or "-" }}</span></td>
                    <td>{{ row.direction or "-" }}</td>
                    <td>{{ row.status }}</td>
                    <td>{{ row.entry_offset_seconds or "-" }}s</td>
                    <td>{{ num(row.market_price) }}</td>
                    <td>{{ num(row.expected_edge) }}</td>
                    <td>{{ row.fill_source or "-" }}</td>
                    <td>{{ row.settlement_source or "-" }} {{ "W" if row.won else "L" if row.won is not none else "" }}</td>
                    <td class="{{ 'pos' if (row.pnl_usd or 0) >= 0 else 'neg' }}">{{ money(row.pnl_usd) }}</td>
                    <td class="muted">{{ row.created_at }}</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% else %}
          <div class="empty">No paper orders yet.</div>
        {% endif %}
      </section>
    </main>
  </div>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.getenv("SIGNAL_ARENA_PORT", "8088"))
    app.run(host="0.0.0.0", port=port)
