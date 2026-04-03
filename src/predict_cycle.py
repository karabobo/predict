"""
predict_cycle.py — High-frequency production prediction cycle.

This entrypoint is intended for local continuous loops. It keeps the work light:
fetch fresh markets, collect BTC context, and write at most one new production
prediction when an unseen market is available.
"""

from fetch_markets import DB_PATH, init_db
from ci_run import run_predict_phase


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()
    try:
        run_predict_phase(db)
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
