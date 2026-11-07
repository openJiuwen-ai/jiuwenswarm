# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for bounded log previews and the user-content privacy gate."""

import pytest

from jiuwenswarm.common.log_preview import DEFAULT_PREVIEW_MAX_CHARS, preview_text

_PREVIEW_ENV = "JIUWENSWARM_LOG_PREVIEW_USER_CONTENT"


@pytest.fixture
def previews_enabled(monkeypatch):
    monkeypatch.setenv(_PREVIEW_ENV, "1")


class TestPreviewTextWhenEnabled:
    def test_short_text_is_returned_as_is(self, previews_enabled):
        assert preview_text("hello") == "hello"

    def test_text_at_limit_is_not_clipped(self, previews_enabled):
        text = "x" * DEFAULT_PREVIEW_MAX_CHARS
        assert preview_text(text) == text

    def test_long_text_is_clipped_with_exact_omitted_count(self, previews_enabled):
        text = "y" * (DEFAULT_PREVIEW_MAX_CHARS + 1)
        assert preview_text(text) == "y" * DEFAULT_PREVIEW_MAX_CHARS + "...(+1 chars)"

    def test_limit_parameter_overrides_default(self, previews_enabled):
        assert preview_text("abcdef", limit=3) == "abc...(+3 chars)"

    def test_non_string_values_are_stringified(self, previews_enabled):
        assert preview_text(12345) == "12345"

    def test_none_is_stringified(self, previews_enabled):
        assert preview_text(None) == "None"

    def test_unicode_length_counts_characters(self, previews_enabled):
        text = "汉" * 201
        assert preview_text(text) == "汉" * 200 + "...(+1 chars)"


class TestPreviewTextPrivacyGate:
    @pytest.mark.parametrize("disabled_value", ["0", "false", "no", "off", " FALSE "])
    def test_disabled_env_replaces_text_with_length_placeholder(
        self, monkeypatch, disabled_value
    ):
        monkeypatch.setenv(_PREVIEW_ENV, disabled_value)
        assert preview_text("hello") == "<5 chars omitted>"

    def test_disabled_env_applies_to_short_text_too(self, monkeypatch):
        monkeypatch.setenv(_PREVIEW_ENV, "0")
        assert preview_text("x") == "<1 chars omitted>"

    def test_disabled_env_strips_length_only_for_clipped_input(self, monkeypatch):
        monkeypatch.setenv(_PREVIEW_ENV, "off")
        assert preview_text("z" * 500, limit=100) == "<500 chars omitted>"

    def test_enabled_env_value_keeps_text(self, monkeypatch):
        monkeypatch.setenv(_PREVIEW_ENV, "true")
        assert preview_text("hello") == "hello"

    def test_config_disables_preview_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(_PREVIEW_ENV, raising=False)
        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_config",
            lambda: {"logging": {"preview_user_content": False}},
        )
        assert preview_text("hello") == "<5 chars omitted>"

    def test_config_enables_preview_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(_PREVIEW_ENV, raising=False)
        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_config",
            lambda: {"logging": {"preview_user_content": True}},
        )
        assert preview_text("hello") == "hello"

    def test_config_without_logging_section_defaults_to_enabled(self, monkeypatch):
        monkeypatch.delenv(_PREVIEW_ENV, raising=False)
        monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
        assert preview_text("hello") == "hello"

    def test_config_failure_falls_back_to_enabled(self, monkeypatch):
        monkeypatch.delenv(_PREVIEW_ENV, raising=False)

        def broken_get_config():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_config", broken_get_config
        )
        assert preview_text("hello") == "hello"
