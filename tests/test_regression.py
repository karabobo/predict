"""
Regression tests — one per past production incident.
Each test prevents the exact failure from recurring.
"""
import sys
import os
import glob
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

ROOT = os.path.join(os.path.dirname(__file__), "..")


# ── Incident 1: Binance 451 — data provider returns usable data ─────────

def test_kraken_response_parsing():
    """Kraken response parser handles the actual response format.
    Incident 1: CoinGecko fallback returned 30-min candles with no volume.
    """
    from btc_data import _compute_summary

    # Simulate Kraken-style candles (5-min, with volume)
    candles = []
    price = 74000.0
    for i in range(12):
        o = price
        c = o + 10 * (1 if i % 2 == 0 else -1)
        candles.append({
            "time": f"12:{i*5:02d}",
            "open": o, "high": max(o, c) + 5, "low": min(o, c) - 5,
            "close": c, "volume": 5.0 + i,  # MUST have volume > 0
            "direction": "UP" if c >= o else "DOWN",
            "body_pct": round((c - o) / o * 100, 4),
            "wick_ratio": 0.5,
        })
        price = c

    result = _compute_summary(candles)
    # Volume must be present and nonzero
    assert result["avg_volume"] > 0, "Data provider must return volume data"
    assert result["last_volume_ratio"] > 0, "Volume ratio must be computable"


# ── Incident 2: Inverted conviction — P&L math correctness ─────────────

def test_winning_bets_always_profit():
    """A correct prediction at any market price must produce positive P&L.
    Incident 2: Conviction was inverted — 26% accuracy on bets, 69% on skips.
    """
    from dashboard import compute_pnl

    # Test across different market prices
    for price_yes in [0.20, 0.35, 0.50, 0.65, 0.80]:
        # Predict UP, outcome UP
        rows = [{
            "market_id": f"test_up_{price_yes}",
            "agent": "contrarian_rule",
            "estimate": 0.62,
            "price_yes": price_yes,
            "outcome": 1,
            "conviction_score": 3,
        }]
        result = compute_pnl(rows)
        pnl = result["contrarian_rule"]["total_pnl"]
        assert pnl > 0, f"Winning UP bet at price {price_yes} should profit, got {pnl}"

        # Predict DOWN, outcome DOWN
        rows2 = [{
            "market_id": f"test_down_{price_yes}",
            "agent": "contrarian_rule",
            "estimate": 0.38,
            "price_yes": price_yes,
            "outcome": 0,
            "conviction_score": 3,
        }]
        result2 = compute_pnl(rows2)
        pnl2 = result2["contrarian_rule"]["total_pnl"]
        assert pnl2 > 0, f"Winning DOWN bet at price {price_yes} should profit, got {pnl2}"


def test_losing_bets_always_lose_exactly_bet_size():
    """A wrong prediction must lose exactly the bet size.
    Incident 2: P&L asymmetry confused the accounting.
    """
    from dashboard import compute_pnl

    for price_yes in [0.20, 0.50, 0.80]:
        rows = [{
            "market_id": "test",
            "agent": "contrarian_rule",
            "estimate": 0.62,
            "price_yes": price_yes,
            "outcome": 0,  # wrong
            "conviction_score": 3,
        }]
        result = compute_pnl(rows)
        pnl = result["contrarian_rule"]["total_pnl"]
        assert pnl == -75, f"Losing bet should be exactly -$75, got {pnl}"


# ── Incident 3: CI references deleted paths ─────────────────────────────

def test_ci_workflow_no_deleted_paths():
    """CI workflow must not reference paths that don't exist.
    Incident 3: git add prompts/ failed because directory was deleted.
    """
    workflow_dir = os.path.join(ROOT, ".github", "workflows")
    if not os.path.isdir(workflow_dir):
        return  # skip if no workflows (shouldn't happen)

    for yml_file in glob.glob(os.path.join(workflow_dir, "*.yml")):
        content = open(yml_file).read()

        # Check for known deleted directories
        deleted_dirs = ["prompts/", "prompts/*"]
        for d in deleted_dirs:
            assert d not in content, \
                f"{yml_file} references deleted path '{d}'"


def test_no_evolve_imports():
    """No production code should import from deleted evolve.py.
    Incident 3: evolve.py was deleted but run_cycle.py imported it.
    """
    src_dir = os.path.join(ROOT, "src")
    production_files = ["run_cycle.py", "predict.py", "dashboard.py",
                        "fetch_markets.py", "score.py", "btc_data.py"]

    for fname in production_files:
        fpath = os.path.join(src_dir, fname)
        if not os.path.exists(fpath):
            continue
        content = open(fpath).read()
        assert "from evolve import" not in content, \
            f"{fname} still imports from deleted evolve.py"
        assert "import evolve" not in content, \
            f"{fname} still imports deleted evolve module"


def test_trade_exposure_survives_later_skip():
    """Trade stats must keep the first actionable exposure even if later rows skip.
    Incident: continuous prediction loop can emit TRADE first, then later SKIP on the
    same market. Latest-snapshot scoring would hide that operational risk.
    """
    from score import calculate_trade_metrics, calculate_path_risk_metrics

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            price_yes REAL,
            resolved INTEGER,
            outcome INTEGER,
            end_date TEXT
        )
    """)
    db.execute("""
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            agent TEXT,
            estimate REAL,
            confidence TEXT,
            reasoning TEXT,
            predicted_at TEXT,
            cycle INTEGER,
            conviction_score TEXT,
            should_trade INTEGER,
            market_price_yes_snapshot REAL
        )
    """)
    db.execute(
        "INSERT INTO markets (id, question, price_yes, resolved, outcome, end_date) VALUES (?, ?, ?, ?, ?, ?)",
        ("m1", "BTC market", 0.64, 1, 0, "2026-04-03T08:25:00Z"),
    )
    db.execute(
        """
        INSERT INTO predictions (
            market_id, agent, estimate, confidence, reasoning, predicted_at, cycle,
            conviction_score, should_trade, market_price_yes_snapshot
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", "contrarian_rule", 0.38, "medium", "first trade", "2026-04-03T08:20:11Z", 1, "3", 1, 0.475),
    )
    db.execute(
        """
        INSERT INTO predictions (
            market_id, agent, estimate, confidence, reasoning, predicted_at, cycle,
            conviction_score, should_trade, market_price_yes_snapshot
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m1", "contrarian_rule", 0.50, "low", "later skip", "2026-04-03T08:24:57Z", 2, "0", 0, 0.645),
    )
    db.commit()

    trade = calculate_trade_metrics(db)["contrarian_rule"]
    risk = calculate_path_risk_metrics(db)["contrarian_rule"]

    assert trade["num_bets"] == 1
    assert round(trade["total_pnl"], 2) == 67.86
    assert risk["ever_trade_markets"] == 1
    assert risk["trade_then_skip_markets"] == 1
