from pathlib import Path


def test_aops_bundle_example_config_defaults_busy_input_mode_queue():
    script = Path("packaging/offline/build_aops_bundle.sh").read_text(encoding="utf-8")
    assert 'display:' in script
    assert 'busy_input_mode: queue' in script
