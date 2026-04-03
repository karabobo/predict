"""
score.py — Signal and trade scorecards for resolved markets.

Signal metrics answer "how good were the probabilities and directions?"
Trade metrics answer "what happened after conviction gates and bet sizing?"
"""

import sqlite3
import json
import requests
from datetime import datetime, timezone
from pathlib import Path

from metrics import (
    compute_path_risk,
    compute_pnl,
    ensure_prediction_schema,
    select_exposure_rows,
    select_latest_rows,
)

PRODUCTION_AGENTS = {"contrarian_rule"}
RESEARCH_AGENTS = {
    "deepseek-ai/DeepSeek-V3",
    "Pro/zai-org/GLM-5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "contrarian",
    "volume_wick",
}

GAMMA_API = "https://gamma-api.polymarket.com"
DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
HTTP_TIMEOUT = 15


LATEST_PREDICTIONS_SUBQUERY = """
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


def _rows_as_dicts(cursor) -> list[dict]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def mark_resolved(db, market_id, outcome):
    """Mark a market as resolved with outcome 1 (UP) or 0 (DOWN)."""
    db.execute("UPDATE markets SET resolved = 1, outcome = ? WHERE id = ?", (outcome, market_id))
    db.commit()
    print(f"Marked market {market_id} as resolved: {'UP' if outcome == 1 else 'DOWN'}")


def auto_resolve(db):
    """Check the Polymarket API for resolved markets and update the database."""
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.execute("""
        SELECT id, question
        FROM markets
        WHERE resolved = 0
          AND end_date <= ?
        ORDER BY end_date ASC
    """, (now_iso,))
    unresolved = cursor.fetchall()
    if not unresolved:
        return 0

    resolved_count = 0
    for market_id, question in unresolved:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets/{market_id}",
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            market = resp.json()

            if not market.get("closed"):
                continue

            raw_prices = market.get("outcomePrices", "[]")
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            price_yes = float(prices[0])

            # Resolved markets snap to 0 or 1
            if price_yes == 1.0:
                outcome = 1
            elif price_yes == 0.0:
                outcome = 0
            else:
                continue  # Closed but not yet fully resolved

            mark_resolved(db, market_id, outcome)
            resolved_count += 1
        except (requests.RequestException, ValueError, KeyError, IndexError):
            continue

    return resolved_count


def calculate_signal_metrics(db):
    """Calculate per-agent signal metrics from the latest snapshot per market."""
    ensure_prediction_schema(db)
    cursor = db.execute("""
        SELECT p.agent,
               p.market_id,
               p.estimate,
               p.predicted_at,
               m.outcome,
               m.price_yes,
               m.question,
               COALESCE(p.conviction_score, 0) AS conviction_score,
               COALESCE(p.should_trade, 0) AS should_trade,
               COALESCE(p.market_price_yes_snapshot, m.price_yes) AS market_price_yes_snapshot
        FROM predictions p
        JOIN markets m ON p.market_id = m.id
        WHERE m.resolved = 1
        ORDER BY p.agent, m.end_date
    """)
    rows = select_latest_rows(_rows_as_dicts(cursor))

    results = {}
    for row in rows:
        agent = row["agent"]
        estimate = float(row["estimate"])
        outcome = int(row["outcome"])
        market_price = float(row.get("market_price_yes_snapshot", row["price_yes"]))
        question = row["question"]
        conviction_score = row["conviction_score"]
        should_trade = row["should_trade"]
        brier_agent = (estimate - outcome) * (estimate - outcome)
        brier_market = (market_price - outcome) * (market_price - outcome)
        if agent not in results:
            results[agent] = {
                "scores": [],
                "total_brier": 0.0,
                "resolved_count": 0,
                "called_markets": 0,
                "correct_calls": 0,
                "trade_candidates": 0,
                "vs_market": [],
            }
        is_called = abs(float(estimate) - 0.5) > 1e-9
        correct_call = (
            is_called
            and ((estimate >= 0.5 and outcome == 1) or (estimate < 0.5 and outcome == 0))
        )
        results[agent]["scores"].append({
            "question": question,
            "estimate": estimate,
            "outcome": outcome,
            "brier": brier_agent,
            "market_brier": brier_market,
        })
        results[agent]["total_brier"] += brier_agent
        results[agent]["resolved_count"] += 1
        results[agent]["called_markets"] += 1 if is_called else 0
        results[agent]["correct_calls"] += 1 if correct_call else 0
        results[agent]["trade_candidates"] += 1 if int(float(conviction_score or 0)) >= 3 and int(should_trade or 0) == 1 else 0
        results[agent]["vs_market"].append(brier_agent - brier_market)

    for data in results.values():
        resolved_count = data["resolved_count"]
        called_markets = data["called_markets"]
        data["avg_brier"] = data["total_brier"] / resolved_count if resolved_count else 0.0
        data["avg_vs_market"] = sum(data["vs_market"]) / len(data["vs_market"]) if data["vs_market"] else 0.0
        data["directional_accuracy"] = data["correct_calls"] / called_markets if called_markets else 0.0
        data["trade_rate"] = data["trade_candidates"] / resolved_count if resolved_count else 0.0

    return results


def calculate_brier_scores(db):
    """Backwards-compatible alias for signal metrics."""
    return calculate_signal_metrics(db)


def calculate_trade_metrics(db):
    """Calculate trade metrics from first exposure, not final snapshot."""
    ensure_prediction_schema(db)
    cursor = db.execute("""
        SELECT
            p.market_id,
            p.agent,
            p.estimate,
            p.predicted_at,
            COALESCE(p.market_price_yes_snapshot, m.price_yes) AS market_price_yes_snapshot,
            m.price_yes,
            m.outcome,
            COALESCE(p.conviction_score, 0) AS conviction_score,
            COALESCE(p.should_trade, 0) AS should_trade
        FROM predictions p
        JOIN markets m ON p.market_id = m.id
        WHERE m.resolved = 1
        ORDER BY m.end_date ASC, p.predicted_at ASC
    """)
    rows = select_exposure_rows(_rows_as_dicts(cursor))
    return compute_pnl(rows)


def calculate_path_risk_metrics(db):
    """Summarize intra-market churn and risk of withdrawn trade signals."""
    ensure_prediction_schema(db)
    cursor = db.execute(
        """
        SELECT
            p.market_id,
            p.agent,
            p.estimate,
            p.predicted_at,
            COALESCE(p.conviction_score, 0) AS conviction_score,
            COALESCE(p.should_trade, 0) AS should_trade
        FROM predictions p
        JOIN markets m ON p.market_id = m.id
        WHERE m.resolved = 1
        ORDER BY p.agent, m.end_date, p.predicted_at
        """
    )
    return compute_path_risk(_rows_as_dicts(cursor))


def print_scorecard(signal_metrics, trade_metrics=None, path_risk_metrics=None):
    """Pretty-print signal snapshot, trade exposure, and path-risk scorecards."""
    if not signal_metrics:
        print("No resolved markets yet. Mark some markets as resolved first.")
        return None

    print("\n" + "=" * 70)
    print("PRODUCTION SCORECARD")
    print("=" * 70)

    production_rows = [(agent, signal_metrics[agent]) for agent in sorted(signal_metrics) if agent in PRODUCTION_AGENTS]
    research_rows = [(agent, signal_metrics[agent]) for agent in sorted(signal_metrics) if agent not in PRODUCTION_AGENTS]

    for agent, data in production_rows:
        avg_brier = data["avg_brier"]
        avg_vs_market = data["avg_vs_market"]
        beat_market = "BEATING" if avg_vs_market < 0 else "LOSING TO"

        print(f"\n  {agent}")
        print(f"    Avg Brier:     {avg_brier:.4f}")
        print(f"    Resolved:      {data['resolved_count']}")
        print(f"    Call Acc:      {data['directional_accuracy'] * 100:.1f}%")
        print(f"    vs Market:     {avg_vs_market:+.4f} ({beat_market} market)")
        print(f"    Trade Rate:    {data['trade_rate'] * 100:.1f}%")
        print("    Signal Basis:  latest snapshot per market")

        if trade_metrics and agent in trade_metrics:
            trade = trade_metrics[agent]
            print(f"    Bets:          {trade['num_bets']}")
            print(f"    Trade WR:      {trade['win_rate'] * 100:.1f}%")
            print(f"    Trade P&L:     {trade['total_pnl']:+.2f}")
            print(f"    Trade ROI:     {trade['roi']:+.2f}%")
            print("    Trade Basis:   first trade exposure, else final skip")
        if path_risk_metrics and agent in path_risk_metrics:
            risk = path_risk_metrics[agent]
            print(f"    Path Risk:     trade->skip {risk['trade_then_skip_markets']} / {risk['ever_trade_markets']}")
            print(f"    Direction Flip:{risk['direction_flip_markets']} markets")
            print(f"    Avg Updates:   {risk['avg_updates_per_market']:.2f} per market")

    print("=" * 70)

    if research_rows:
        print("\n" + "=" * 70)
        print("HISTORICAL / RESEARCH SCORECARD")
        print("=" * 70)

        worst_agent = None
        worst_brier = -1.0
        for agent, data in research_rows:
            avg_brier = data["avg_brier"]
            avg_vs_market = data["avg_vs_market"]
            beat_market = "BEATING" if avg_vs_market < 0 else "LOSING TO"

            print(f"\n  {agent}")
            print(f"    Avg Brier:     {avg_brier:.4f}")
            print(f"    Resolved:      {data['resolved_count']}")
            print(f"    Call Acc:      {data['directional_accuracy'] * 100:.1f}%")
            print(f"    vs Market:     {avg_vs_market:+.4f} ({beat_market} market)")
            print(f"    Trade Rate:    {data['trade_rate'] * 100:.1f}%")

            if trade_metrics and agent in trade_metrics:
                trade = trade_metrics[agent]
                print(f"    Bets:          {trade['num_bets']}")
                print(f"    Trade WR:      {trade['win_rate'] * 100:.1f}%")
                print(f"    Trade P&L:     {trade['total_pnl']:+.2f}")
                print(f"    Trade ROI:     {trade['roi']:+.2f}%")

            if agent in RESEARCH_AGENTS and avg_brier > worst_brier:
                worst_brier = avg_brier
                worst_agent = agent

        if worst_agent is not None:
            print(f"\n  → WORST RESEARCH PERFORMER: {worst_agent} (Brier: {worst_brier:.4f})")
            print("  → Historical diagnostics only. Production remains pinned to contrarian_rule.")
    print("=" * 70)

    return "contrarian_rule"


def get_agent_brier(db, agent_name):
    """Get average Brier score for a specific agent."""
    signal_metrics = calculate_signal_metrics(db)
    agent = signal_metrics.get(agent_name)
    return agent["avg_brier"] if agent is not None else None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolve", nargs=2, metavar=("MARKET_ID", "OUTCOME"),
                        help="Mark a market as resolved: --resolve MARKET_ID 0|1")
    args = parser.parse_args()

    db = sqlite3.connect(DB_PATH)
    ensure_prediction_schema(db)

    if args.resolve:
        mark_resolved(db, args.resolve[0], int(args.resolve[1]))
    else:
        # 自动获取并结算已结束的市场
        print("Checking for newly resolved markets...")
        count = auto_resolve(db)
        if count > 0:
            print(f"Successfully resolved {count} new markets.")

    signal_metrics = calculate_brier_scores(db)
    trade_metrics = calculate_trade_metrics(db)
    path_risk_metrics = calculate_path_risk_metrics(db)
    print_scorecard(signal_metrics, trade_metrics, path_risk_metrics)
    db.close()
