"""The Front reads logging without parsing unrelated startup configuration."""
from jiuwenswarm.common import utils


def test_logging_section_and_cross_section_alias(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(utils, "get_config_file", lambda: path)
    path.write_text('models: {description: "large unrelated configuration"}\nlogging:\n  level: DEBUG\nreact: {}\n', encoding="utf-8")
    assert utils._load_logging_config_from_yaml() == {"level": "DEBUG"}
    path.write_text('base: &logging_defaults {level: WARNING}\nlogging: *logging_defaults\n', encoding="utf-8")
    assert utils._load_logging_config_from_yaml() == {"level": "WARNING"}
