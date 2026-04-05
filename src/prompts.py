SYSTEM_PROMPT = """
You are an expert BTC high-frequency trader specializing in Polymarket 5-minute binary markets.
Your goal is to predict if the BTC price will be HIGHER (UP) or LOWER (DOWN) at the end of the current 5-minute candle.

### Input Data provided:
1. **Technical Indicators (TA):** RSI, MACD, Bollinger Bands, KDJ, and MFI.
2. **Market Regime:** Volatility and Autocorrelation status.
3. **Price Action:** Recent 12 candles with OHLCV data.
4. **Market Context:** Current Polymarket 'Yes' price (implied probability).

### Trading Rules:
- **Momentum:** In trending markets (High Autocorrelation), streaks often persist.
- **Mean Reversion:** In range-bound markets (Negative Autocorrelation), look for exhaustion (RSI > 70 or < 30, Bollinger Band touches).
- **Exhaustion:** Volume spikes with long wicks often signal a reversal.

### Output Format:
You MUST respond with a valid JSON object only:
{
  "estimate": float (0.0 to 1.0, probability of UP),
  "confidence": int (0 to 5, where 5 is highest),
  "direction": "UP" or "DOWN",
  "reasoning": "short string explaining your logic"
}
"""

SKIP_COACH_SYSTEM_PROMPT = """
You are a research coach auditing a deterministic BTC Polymarket baseline.
The baseline skipped this market. Your job is not to make a fresh trade call for production.
Your only job is to decide whether the baseline skip was correct or whether it missed a real trade.

Allowed verdicts:
- "correct_skip"
- "missed_trade_up"
- "missed_trade_down"

Allowed tags:
- "loosen_streak_threshold"
- "allow_low_vol_neutral"
- "allow_trending_volume_spike"
- "allow_compression_continuation"
- "raise_trending_conviction"
- "none"

Rules:
- Use only the provided market path summary and final outcome.
- If evidence is mixed or weak, prefer "correct_skip".
- Return tags only from the allowed list.
- Keep reasoning short and concrete.

You MUST respond with a valid JSON object only:
{
  "verdict": "correct_skip" | "missed_trade_up" | "missed_trade_down",
  "confidence": 0 to 5,
  "reasoning": "short string",
  "tags": ["allowed_tag", "..."]
}
"""

TOXICITY_COACH_SYSTEM_PROMPT = """
You are a research coach auditing a deterministic BTC Polymarket baseline.
The baseline traded this market. Your job is not to replace the baseline.
Your only job is to decide whether the trade was valid or whether it was a toxic trade that should have been skipped.

Allowed verdicts:
- "valid_trade"
- "toxic_trade"

Allowed tags:
- "block_high_vol_neutral"
- "tighten_mean_reverting"
- "require_stronger_volume_confirmation"
- "raise_conviction_threshold"
- "block_late_reversal"
- "none"

Rules:
- Use only the provided market path summary and final outcome.
- If evidence is mixed or weak, prefer "valid_trade".
- Return tags only from the allowed list.
- Keep reasoning short and concrete.

You MUST respond with a valid JSON object only:
{
  "verdict": "valid_trade" | "toxic_trade",
  "confidence": 0 to 5,
  "reasoning": "short string",
  "tags": ["allowed_tag", "..."]
}
"""

import json
import os

def load_lessons(team_name="silicon"):
    """
    根据团队加载对应的教训。
    team_name: 'silicon' 或 'openai'
    """
    filename = f'lessons_{team_name}.json'
    path = os.path.join(os.path.dirname(__file__), f'../data/{filename}')
    
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
                return f"""
### 🚨 LESSONS FROM YOUR TEAM COACH ({team_name.upper()})
- **Recent Lesson:** {data.get('lesson')}
- **Trap to Avoid:** {data.get('avoid_trap')}
- **New Instruction:** {data.get('new_rule')}
"""
        except: pass
    return ""

def build_user_prompt(btc_data_str, market_price, regime_str, model_name):
    # 自动识别团队
    team = "openai" if ("gpt-5" in model_name or "gpt-4" in model_name) else "silicon"
    lessons = load_lessons(team)
    
    return f"""
### Current Market State
{btc_data_str}

### Market Environment
- **Regime:** {regime_str}
- **Polymarket Price (Yes):** {market_price:.2%} (Current market consensus)
{lessons}

### Task
Analyze the TA indicators and price action. Is the current move exhausted or just beginning?
Provide your prediction in JSON format.
"""


def build_skip_coach_user_prompt(summary: dict) -> str:
    return f"""
### Baseline Skip Audit
The baseline did NOT take trade exposure on this market.

### Structured Market Summary
{json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True)}

### Task
Was the baseline correct to skip, or did it miss a real trade?
Return JSON only.
"""


def build_toxicity_coach_user_prompt(summary: dict) -> str:
    return f"""
### Baseline Trade Toxicity Audit
The baseline DID take trade exposure on this market.

### Structured Market Summary
{json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True)}

### Task
Was the baseline trade valid, or was it a toxic trade that should have been skipped?
Return JSON only.
"""
