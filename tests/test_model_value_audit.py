import json
import sqlite3
from pathlib import Path

from src.v3.model_value_audit import AuditThresholds, build_audit_report, render_markdown


def _make_backtest_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE backtest_runs (
            run_id INTEGER PRIMARY KEY,
            rule_name TEXT NOT NULL,
            btc_candles_file TEXT NOT NULL,
            entry_price_source TEXT NOT NULL,
            lookback INTEGER NOT NULL,
            markets INTEGER NOT NULL,
            eligible_markets INTEGER NOT NULL,
            trades INTEGER NOT NULL,
            trade_wins INTEGER NOT NULL,
            signal_wins INTEGER NOT NULL,
            signal_calls INTEGER NOT NULL,
            trade_pnl REAL NOT NULL,
            trade_wagered REAL NOT NULL,
            trade_roi REAL NOT NULL,
            notes TEXT,
            created_at TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO backtest_runs VALUES (
            ?, ?, 'candles.parquet', ?, 20, 1000, 900, ?, ?, 0, 0, ?, 1000, ?, '', ?
        )
        """,
        [
            (1, "weak_rule", "neutral_50", 120, 55, 10.0, 1.0, "2026-01-01"),
            (2, "strong_rule", "neutral_50", 130, 78, 200.0, 20.0, "2026-01-02"),
            (3, "leaky_rule", "market_final_yes", 140, 90, 300.0, 30.0, "2026-01-03"),
        ],
    )
    conn.commit()
    conn.close()


def _make_research_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE arena_runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            baseline TEXT NOT NULL,
            challenger TEXT NOT NULL,
            days INTEGER NOT NULL,
            warm_up INTEGER NOT NULL,
            folds INTEGER NOT NULL,
            bet_size REAL NOT NULL,
            min_edge REAL NOT NULL,
            gate_passed INTEGER NOT NULL,
            summary_json TEXT NOT NULL,
            max_eval_contexts INTEGER NOT NULL DEFAULT 24
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE coach_rule_candidate_specs (
            id INTEGER PRIMARY KEY,
            coach_model TEXT,
            coach_type TEXT,
            tag TEXT,
            regime TEXT,
            window_start TEXT,
            window_end TEXT,
            spec_name TEXT,
            spec_label TEXT,
            family TEXT,
            target_scope TEXT,
            template_action TEXT,
            implementation_hint TEXT,
            config_json TEXT,
            support_count INTEGER,
            helpful_count INTEGER,
            harmful_count INTEGER,
            precision REAL,
            net_helpful INTEGER,
            eligible_for_ablation INTEGER,
            updated_at TEXT
        )
        """
    )
    summary = {
        "gate": {
            "passed": True,
            "aggregate_roi_delta": 8.5,
            "aggregate_win_rate_delta": 3.0,
            "trade_ratio": 0.75,
        }
    }
    conn.execute(
        """
        INSERT INTO arena_runs VALUES (
            'r1', '2026-01-01', 'production_baseline', 'challenger_a', 3, 80, 2,
            75.0, 0.05, 1, ?, 24
        )
        """,
        (json.dumps(summary),),
    )
    conn.execute(
        """
        INSERT INTO coach_rule_candidate_specs VALUES (
            1, 'glm', 'skip_coach', 'allow', 'LOW_VOL / NEUTRAL', 'a', 'b',
            'coach_spec__allow_low_vol', 'Allow low vol', 'regime_allow',
            'LOW_VOL / NEUTRAL', 'allow_regime', '', '{}', 6, 5, 1,
            0.83, 4, 1, '2026-01-01'
        )
        """
    )
    conn.commit()
    conn.close()


def test_build_audit_report_shortlists_existing_result_sources(tmp_path):
    backtest_db = tmp_path / "backtest.db"
    research_db = tmp_path / "research.db"
    _make_backtest_db(backtest_db)
    _make_research_db(research_db)

    report = build_audit_report(
        backtest_db=backtest_db,
        research_db=research_db,
        thresholds=AuditThresholds(min_trades=100, min_roi=5, min_win_rate=52),
    )

    backtest_names = {item["name"] for item in report["backtest_candidates"]}
    assert "strong_rule" in backtest_names
    assert "weak_rule" not in backtest_names
    assert any(item["entry_realism"] == "lookahead" for item in report["backtest_candidates"])
    assert report["arena_candidates"][0]["name"] == "challenger_a"
    assert report["coach_candidates"][0]["name"] == "coach_spec__allow_low_vol"


def test_render_markdown_marks_next_l2_gate(tmp_path):
    backtest_db = tmp_path / "backtest.db"
    research_db = tmp_path / "research.db"
    _make_backtest_db(backtest_db)
    _make_research_db(research_db)

    markdown = render_markdown(build_audit_report(backtest_db=backtest_db, research_db=research_db))

    assert "# Model Value Audit" in markdown
    assert "L2 replay" in markdown
    assert "strong_rule" in markdown
