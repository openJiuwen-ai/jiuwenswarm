# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Config readers must not block atomic startup writes during YAML parsing."""
from ruamel.yaml import YAML

from jiuwenswarm.common import config


def test_round_trip_reader_releases_handle_before_parsing(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("preferred_language: zh\nvalue: original\n", encoding="utf-8")
    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("preferred_language: en\nvalue: replacement\n", encoding="utf-8")

    class ConcurrentReader:
        def load(self, source):
            # This is a real atomic replacement: Windows rejects it while the
            # old reader still owns a handle, independently of Python locks.
            replacement.replace(path)
            return YAML().load(source)

    monkeypatch.setattr(config, "YAML", ConcurrentReader)
    actual = config.load_yaml_round_trip(path)
    assert actual["value"] == "original"
    assert YAML().load(path.read_text(encoding="utf-8"))["value"] == "replacement"


def test_startup_language_already_selected_does_not_rewrite(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    original = b"# user settings\npreferred_language: zh  # keep comment\nother: 7\n"
    path.write_bytes(original)
    before = path.stat().st_mtime_ns

    def unexpected_write(*args, **kwargs):
        raise AssertionError("Startup must not rewrite an unchanged language")

    monkeypatch.setattr(config, "dump_yaml_round_trip", unexpected_write)
    config.set_preferred_language_in_config_file(path, "ZH")
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before


def test_startup_language_change_keeps_comments_and_other_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("# user settings\npreferred_language: zh  # language\nother: 7\n", encoding="utf-8")
    config.set_preferred_language_in_config_file(path, "en")
    text = path.read_text(encoding="utf-8")
    assert YAML().load(text) == {"preferred_language": "en", "other": 7}
    assert "# user settings" in text and "# language" in text
