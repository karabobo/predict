"""
predict.py — Multi-Model Arena (AI vs Algorithm)

Combines:
1. Advanced TA Indicators (RSI, MACD, BB, etc.)
2. Regime Filtering (Autocorrelation)
3. Multi-Model LLM calls (Gemini 2.0 Flash, 1.5 Pro)
4. Traditional Rule-based logic (Contrarian/Momentum)
"""

import json
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import ai_client
import prompts
from btc_data import fetch_btc_candles, format_for_prompt

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"

# --- Models in the Arena ---
MODELS = [
    # Team A: Silicon Flow (Fully Functional)
    "deepseek-ai/DeepSeek-V3",
    "Pro/zai-org/GLM-5",
    
    # Baseline
    "contrarian_rule"
]

def compute_regime_from_candles(candles):
    """Compute regime indicators from candle list."""
    closes = [float(c["close"]) for c in candles]
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    if len(returns) < 3: return {"label": "UNKNOWN", "is_mean_reverting": False}
    
    import statistics
    vol = statistics.stdev(returns) * 100
    n = len(returns)
    mean_r = sum(returns) / n
    var = sum((r - mean_r) ** 2 for r in returns) / n
    autocorr = 0.0
    if var > 0:
        cov = sum((returns[i] - mean_r) * (returns[i-1] - mean_r) for i in range(1, n)) / (n - 1)
        autocorr = cov / var

    label = f"{'HIGH' if vol > 0.12 else 'LOW'}_VOL / {'TRENDING' if autocorr > 0.15 else 'MEAN_REVERTING' if autocorr < -0.15 else 'NEUTRAL'}"
    return {"label": label, "autocorr": autocorr, "is_mean_reverting": autocorr < -0.15}

def contrarian_signal(candles):
    """Traditional rule-based logic (Streak detector)."""
    last_dir = "UP" if float(candles[-1]["close"]) >= float(candles[-1]["open"]) else "DOWN"
    streak = 0
    for i in range(len(candles)-1, -1, -1):
        d = "UP" if float(candles[i]["close"]) >= float(candles[i]["open"]) else "DOWN"
        if d == last_dir: streak += 1
        else: break
    
    return {
        "estimate": 0.65 if last_dir == "UP" else 0.35,
        "confidence": 3 if streak >= 3 else 1,
        "reasoning": f"Streak of {streak} {last_dir}",
        "direction": last_dir
    }

def get_ai_prediction(model_name, btc_str, market_price, regime_label):
    if model_name == "contrarian_rule":
        return None 
    
    user_prompt = prompts.build_user_prompt(btc_str, market_price, regime_label, model_name)
    return ai_client.client.predict(model_name, prompts.SYSTEM_PROMPT, user_prompt)

def ensure_db_schema(db):
    cols = ["regime", "conviction_score", "model_version"]
    for col in cols:
        try:
            db.execute(f"ALTER TABLE predictions ADD COLUMN {col} TEXT")
            db.commit()
        except sqlite3.OperationalError: pass

def store_prediction(db, market_id, agent, signal, regime_label, cycle):
    predicted_at = datetime.now(timezone.utc).isoformat()
    estimate = signal.get("estimate", 0.5)
    confidence = signal.get("confidence", 0)
    reasoning = signal.get("reasoning", "")
    
    db.execute("""
        INSERT INTO predictions 
        (market_id, agent, estimate, edge, confidence, reasoning, predicted_at, cycle, conviction_score, regime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        market_id, agent, estimate, abs(estimate - 0.5), str(confidence),
        reasoning, predicted_at, cycle, confidence, regime_label
    ))
    db.commit()

def run_predictions(cycle=1, market_limit=5):
    print(f"--- Cycle {cycle}: Multi-Model Arena ---")
    db = sqlite3.connect(DB_PATH)
    ensure_db_schema(db)

    # 1. Fetch Data
    btc_data = fetch_btc_candles(limit=30)
    if not btc_data:
        print("Error: No BTC data")
        return

    btc_str = format_for_prompt(btc_data)
    regime = compute_regime_from_candles(btc_data["candles"])
    print(f"Regime: {regime['label']}")

    # 2. Get Markets
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.execute("""
        SELECT id, question, price_yes FROM markets 
        WHERE resolved = 0 AND end_date > ?
        ORDER BY end_date ASC LIMIT ?
    """, (now_iso, market_limit))
    markets = cursor.fetchall()

    if not markets:
        print("No active markets.")
        return

    # 3. Arena Execution
    for m_id, question, price_yes in markets:
        print(f"\nMarket: {question[:50]}... (${price_yes:.0%})")
        
        # Run AI models in parallel
        with ThreadPoolExecutor(max_workers=len(MODELS)) as executor:
            futures = {executor.submit(get_ai_prediction, m, btc_str, price_yes, regime['label']): m for m in MODELS if m != "contrarian_rule"}
            
            # Add Rule-based logic manually
            rule_sig = contrarian_signal(btc_data["candles"])
            store_prediction(db, m_id, "contrarian_rule", rule_sig, regime['label'], cycle)
            print(f"  [Rule] {rule_sig['direction']} ({rule_sig['estimate']:.0%})")

            for future in futures:
                model_name = futures[future]
                try:
                    res = future.result()
                    if res and "error" not in res:
                        store_prediction(db, m_id, model_name, res, regime['label'], cycle)
                        print(f"  [{model_name}] {res.get('direction')} ({res.get('estimate'):.0%}) - Conf: {res.get('confidence')}")
                    else:
                        print(f"  [{model_name}] Error: {res.get('error') if res else 'Empty'}")
                except Exception as e:
                    print(f"  [{model_name}] Failed: {e}")

    db.close()
    print(f"\nArena predictions stored in {DB_PATH}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle", type=int, default=1)
    args = parser.parse_args()
    run_predictions(cycle=args.cycle)
