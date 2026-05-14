"""
ops_cycle.py — Lower-frequency operational cycle.

This entrypoint handles the slower path:
resolve markets, optionally place live orders, score results, and rebuild the
dashboard. It is meant to run less frequently than `predict_cycle.py`.
"""

from fetch_markets import DB_PATH, init_db
from ci_run import run_ops_phase


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()
    try:
        run_ops_phase(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
