from src.v3.rule_variants import load_dynamic_coach_rule_metadata


def test_dynamic_coach_specs_metadata_available_for_batch_backtest():
    metadata = load_dynamic_coach_rule_metadata()

    assert isinstance(metadata, dict)
    for name, spec in metadata.items():
        assert name.startswith("coach_spec__")
        assert spec["spec_label"]
        assert spec["target_scope"]
