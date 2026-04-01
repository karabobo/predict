import sqlite3
import json
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
import ai_client

load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"

def get_failed_trades(team="silicon", limit=10):
    """提取特定团队的错误案例"""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    
    # 根据模型名称过滤团队
    team_filter = "NOT (agent LIKE 'gpt-5%')" if team == "silicon" else "(agent LIKE 'gpt-5%')"
    
    query = f"""
        SELECT p.*, m.question, m.outcome
        FROM predictions p
        JOIN markets m ON p.market_id = m.id
        WHERE m.resolved = 1 
          AND p.agent != 'contrarian_rule'
          AND {team_filter}
          AND p.conviction_score >= 3
          AND ((p.estimate >= 0.5 AND m.outcome = 0) OR (p.estimate < 0.5 AND m.outcome = 1))
        ORDER BY p.predicted_at DESC
        LIMIT ?
    """
    rows = db.execute(query, (limit,)).fetchall()
    db.close()
    return rows

def run_team_evolution(team="silicon"):
    print(f"\n--- Evolving Team: {team.upper()} ---")
    
    failures = get_failed_trades(team)
    if not failures:
        print(f"No failures for team {team} to analyze.")
        return

    # 选择该团队的教练模型
    coach_model = "deepseek-ai/DeepSeek-R1" if team == "silicon" else "gpt-5.4"
    
    case_studies = []
    for f in failures:
        case_studies.append({
            "market": f['question'],
            "ai_prediction": "UP" if f['estimate'] >= 0.5 else "DOWN",
            "reasoning_at_the_time": f['reasoning'],
            "actual_outcome": "UP" if f['outcome'] == 1 else "DOWN"
        })

    system_prompt = f"You are the Head Coach of Team {team.upper()}. Analyze your team's failures and provide a ONE-SENTENCE corrective lesson."
    user_prompt = f"Failed cases: {json.dumps(case_studies)}\nProvide your analysis in JSON format: {{'lesson': '...', 'avoid_trap': '...', 'new_rule': '...'}}"

    result = ai_client.client.predict(coach_model, system_prompt, user_prompt, coach_mode=True)
    
    if result and "error" not in result:
        lessons_path = Path(__file__).parent.parent / "data" / f"lessons_{team}.json"
        with open(lessons_path, 'w') as f:
            json.dump(result, f, indent=2)
        print(f"Team {team} evolved using coach {coach_model}.")
    else:
        print(f"Team {team} evolution failed: {result.get('error') if result else 'Empty response'}")

if __name__ == "__main__":
    # 并行/依次运行两个团队的进化
    run_team_evolution("silicon")
    run_team_evolution("openai")
