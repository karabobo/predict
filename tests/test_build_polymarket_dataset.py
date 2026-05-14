from src.v3.build_polymarket_dataset import (
    _infer_outcome,
    _parse_prices,
    _parse_window_start,
)


def test_parse_prices_from_json_string():
    yes, no = _parse_prices('["0.99", "0.01"]')
    assert yes == 0.99
    assert no == 0.01


def test_infer_outcome_from_terminal_prices():
    assert _infer_outcome(1.0, 0.0) == 1
    assert _infer_outcome(0.0, 1.0) == 0
    assert _infer_outcome(0.52, 0.48) is None


def test_parse_window_start_from_slug():
    ts, dt = _parse_window_start("btc-updown-5m-1772933400")
    assert ts == 1772933400
    assert dt is not None
    assert dt.startswith("2026-03-08T01:30:00")
