"""
dashboard.py — Dual-Matrix Dashboard: 10 Pending + 10 Settled Markets
"""

import sqlite3
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template_string

app = Flask(__name__)
DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
LESSONS_PATH = Path(__file__).parent.parent / "data" / "lessons_silicon.json"

MODEL_COLORS = {
    "deepseek-ai/DeepSeek-V3": "#3fb950",
    "Pro/zai-org/GLM-5": "#d2a8ff",
    "contrarian_rule": "#8b949e"
}

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def get_coach_lessons():
    if os.path.exists(LESSONS_PATH):
        try:
            with open(LESSONS_PATH, 'r') as f:
                return json.load(f)
        except: pass
    return None

def get_matrix_data():
    db = get_db()
    
    # 1. 顶部胜率统计
    summary = db.execute("""
        SELECT p.agent, 
               COUNT(*) as sample_size,
               SUM(CASE WHEN m.resolved = 1 THEN 1 ELSE 0 END) as resolved_count,
               SUM(CASE WHEN m.resolved = 1 AND ((p.estimate >= 0.5 AND m.outcome = 1) OR (p.estimate < 0.5 AND m.outcome = 0)) THEN 1 ELSE 0 END) as wins
        FROM predictions p
        JOIN markets m ON p.market_id = m.id
        WHERE p.agent NOT LIKE 'gpt-%'
        GROUP BY p.agent ORDER BY wins DESC
    """).fetchall()

    def fetch_matrix(filter_sql, order_sql, limit=10):
        rows = db.execute(f"""
            SELECT id, question, price_yes, resolved, outcome, end_date
            FROM markets WHERE {filter_sql}
            ORDER BY {order_sql} LIMIT {limit}
        """).fetchall()
        
        res = []
        for m in rows:
            preds = db.execute("SELECT agent, estimate, reasoning, conviction_score FROM predictions WHERE market_id = ?", (m['id'],)).fetchall()
            res.append({"market": m, "predictions": {p['agent']: p for p in preds}})
        return res

    # 2. 分别获取 Pending 和 Resolved
    pending = fetch_matrix("resolved = 0", "end_date ASC", 10)
    history = fetch_matrix("resolved = 1", "end_date DESC", 10)

    db.close()
    return {
        "summary": [dict(s) for s in summary], 
        "pending": pending,
        "history": history,
        "lessons": get_coach_lessons()
    }

@app.route("/")
def index():
    data = get_matrix_data()
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Arena V4 - Live + History Matrix</title>
        <meta charset="utf-8">
        <style>
            body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; padding: 20px; font-size: 0.9rem; }
            .container { max-width: 1400px; margin: 0 auto; }
            .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 0; margin-bottom: 30px; overflow: hidden; }
            h1 { color: #58a6ff; margin-top: 0; }
            h2 { font-size: 0.9rem; color: #8b949e; text-transform: uppercase; padding: 15px 20px; margin: 0; background: #21262d; border-bottom: 1px solid #30363d; }
            
            /* Stats Bar */
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px; margin-bottom: 25px; }
            .stat-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 15px; text-align: center; }
            .stat-val { font-size: 1.8rem; font-weight: bold; margin: 5px 0; }
            .stat-meta { font-size: 0.7rem; color: #484f58; }

            /* Table Styling */
            table { width: 100%; border-collapse: collapse; }
            th { background: #0d1117; color: #8b949e; text-align: left; padding: 12px 20px; font-size: 0.7rem; text-transform: uppercase; }
            td { padding: 12px 20px; border-bottom: 1px solid #21262d; vertical-align: top; }
            
            .market-q { font-weight: bold; font-size: 0.85rem; display: block; }
            .market-meta { font-size: 0.7rem; color: #484f58; margin-top: 4px; }
            
            .pred-box { padding: 10px; border-radius: 6px; border: 1px solid #21262d; background: #0d1117; min-height: 45px; }
            .win-border { border: 2px solid #238636 !important; background: rgba(35, 134, 54, 0.05); }
            .loss-border { border: 2px solid #da3633 !important; background: rgba(218, 54, 51, 0.05); }
            
            .dir-up { color: #3fb950; font-weight: bold; }
            .dir-down { color: #f44336; font-weight: bold; }
            
            .reasoning-toggle { cursor: pointer; color: #58a6ff; font-size: 0.7rem; margin-top: 5px; display: block; text-decoration: underline; }
            .deep-logic { display: none; margin-top: 10px; padding: 10px; background: #000; color: #3fb950; border-radius: 4px; font-family: monospace; font-size: 0.75rem; border-left: 2px solid #3fb950; white-space: pre-wrap; }
            
            .badge { font-size: 0.7rem; font-weight: bold; padding: 2px 8px; border-radius: 10px; }
            .badge-up { background: #238636; color: white; }
            .badge-down { background: #da3633; color: white; }
            .badge-pending { border: 1px solid #484f58; color: #8b949e; }
        </style>
        <script>
            function toggleLogic(id) {
                var el = document.getElementById(id);
                el.style.display = (el.style.display === 'none' || el.style.display === '') ? 'block' : 'none';
            }
        </script>
        <meta http-equiv="refresh" content="30">
    </head>
    <body>
        <div class="container">
            <h1 style="margin-bottom: 25px;">Polymarket Dual-Matrix Dashboard</h1>

            <!-- Standings -->
            <div class="stats-grid">
                {% for s in data.summary %}
                <div class="stat-box">
                    <div style="color: #8b949e; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1px;">{{ s.agent.split('/')[-1] }}</div>
                    <div class="stat-val" style="color: {{ MODEL_COLORS.get(s.agent, '#fff') }}">
                        {{ (s.wins/s.resolved_count*100)|round(1) if s.resolved_count > 0 else 0 }}%
                    </div>
                    <div class="stat-meta">Wins: {{ s.wins }} | Resolved: {{ s.resolved_count }}</div>
                </div>
                {% endfor %}
            </div>

            <!-- TABLE 1: LIVE ARENA -->
            <div class="card">
                <h2>🛰️ LIVE ARENA (Next 10 Markets)</h2>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 30%;">Market & Current Price</th>
                            <th style="width: 100px;">Outcome</th>
                            <th>DeepSeek V3 (Fast)</th>
                            <th>Zhipu GLM-5 (Pro)</th>
                            <th>Baseline Algorithm</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in data.pending %}
                        {% set m = row.market %}
                        <tr>
                            <td>
                                <span class="market-q">{{ m.question }}</span>
                                <div class="market-meta">Mkt Prob: {{ (m.price_yes*100)|int }}% | Ends: {{ m.end_date[11:16] }}</div>
                            </td>
                            <td><span class="badge badge-pending">PENDING</span></td>
                            {% for agent in ["deepseek-ai/DeepSeek-V3", "Pro/zai-org/GLM-5", "contrarian_rule"] %}
                            {% set p = row.predictions.get(agent) %}
                            <td>
                                {% if p %}
                                <div class="pred-box">
                                    <span class="{{ 'dir-up' if p.estimate >= 0.5 else 'dir-down' }}">
                                        {{ '▲ UP' if p.estimate >= 0.5 else '▼ DOWN' }}
                                    </span>
                                    <strong>{{ (p.estimate*100)|int }}%</strong>
                                    <span class="reasoning-toggle" onclick="toggleLogic('p-{{ m.id }}-{{ loop.index }}')">View Logic</span>
                                    <div id="p-{{ m.id }}-{{ loop.index }}" class="deep-logic">{{ p.reasoning }}</div>
                                </div>
                                {% else %}<span style="color: #30363d">-</span>{% endif %}
                            </td>
                            {% endfor %}
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            <!-- TABLE 2: RECENT SETTLEMENTS -->
            <div class="card">
                <h2>🏁 RECENT SETTLEMENTS (Last 10 Records)</h2>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 30%;">Market</th>
                            <th style="width: 100px;">Result</th>
                            <th>DeepSeek V3</th>
                            <th>Zhipu GLM-5</th>
                            <th>Baseline</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in data.history %}
                        {% set m = row.market %}
                        <tr>
                            <td>
                                <span class="market-q">{{ m.question }}</span>
                                <div class="market-meta">Resolved: {{ m.end_date[11:16] }}</div>
                            </td>
                            <td>
                                <span class="badge {{ 'badge-up' if m.outcome == 1 else 'badge-down' }}">
                                    {{ '▲ UP' if m.outcome == 1 else '▼ DOWN' }}
                                </span>
                            </td>
                            {% for agent in ["deepseek-ai/DeepSeek-V3", "Pro/zai-org/GLM-5", "contrarian_rule"] %}
                            {% set p = row.predictions.get(agent) %}
                            <td>
                                {% if p %}
                                <div class="pred-box {{ 'win-border' if ((p.estimate >= 0.5 and m.outcome == 1) or (p.estimate < 0.5 and m.outcome == 0)) else 'loss-border' }}">
                                    <span class="{{ 'dir-up' if p.estimate >= 0.5 else 'dir-down' }}">
                                        {{ '▲ UP' if p.estimate >= 0.5 else '▼ DOWN' }}
                                    </span>
                                    <strong>{{ (p.estimate*100)|int }}%</strong>
                                    <span class="reasoning-toggle" onclick="toggleLogic('h-{{ m.id }}-{{ loop.index }}')">View Logic</span>
                                    <div id="h-{{ m.id }}-{{ loop.index }}" class="deep-logic">{{ p.reasoning }}</div>
                                </div>
                                {% else %}<span style="color: #30363d">-</span>{% endif %}
                            </td>
                            {% endfor %}
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, data=data, MODEL_COLORS=MODEL_COLORS)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
