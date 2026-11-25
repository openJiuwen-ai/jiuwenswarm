# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tool-text i18n: normalize + catalog + t()."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.i18n import (
    normalize_preferred_language,
    t,
    try_normalize_preferred_language,
)
from jiuwenswarm.common.i18n import translate as translate_mod
from jiuwenswarm.enterprise.config import preferred_language as pref


_CATALOG_DIR = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm"
    / "common"
    / "i18n"
    / "catalog"
)
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    translate_mod.reset_catalog_cache_for_tests()
    yield
    translate_mod.reset_catalog_cache_for_tests()


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "en"),
        ("", "en"),
        ("  ", "en"),
        ("fr", "en"),
        ("invalid", "en"),
        ("zh", "zh"),
        ("ZH", "zh"),
        ("cn", "zh"),
        ("zh-CN", "zh"),
        ("zh_cn", "zh"),
        ("en", "en"),
        ("EN", "en"),
        ("en-US", "en"),
        ("en_us", "en"),
    ],
)
def test_normalize_preferred_language(raw, expected):
    assert normalize_preferred_language(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("fr", None),
        ("zh", "zh"),
        ("cn", "zh"),
        ("zh-CN", "zh"),
        ("en-US", "en"),
    ],
)
def test_try_normalize_preferred_language(raw, expected):
    assert try_normalize_preferred_language(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "fr", "zh", "cn", "zh-CN", "en", "en-US"],
)
def test_preferred_language_normalize_matches_shared(raw):
    assert pref._normalize(raw) == try_normalize_preferred_language(raw)


def test_catalog_keys_and_placeholders_match():
    en = yaml.safe_load((_CATALOG_DIR / "en.yaml").read_text(encoding="utf-8"))
    zh = yaml.safe_load((_CATALOG_DIR / "zh.yaml").read_text(encoding="utf-8"))
    assert set(en) == set(zh)
    for key in en:
        en_params = set(_PLACEHOLDER_RE.findall(en[key]))
        zh_params = set(_PLACEHOLDER_RE.findall(zh[key]))
        assert en_params == zh_params, key


def test_t_literal_ids_exist_in_both_catalogs():
    en = yaml.safe_load((_CATALOG_DIR / "en.yaml").read_text(encoding="utf-8"))
    zh = yaml.safe_load((_CATALOG_DIR / "zh.yaml").read_text(encoding="utf-8"))
    for message_id in (
        "tools.send_file.none",
        "tools.send_file.sent",
        "tools.memory.group_write_forbidden",
        "tools.multi_session.empty",
        "tools.cron.proactive_cannot_delete",
    ):
        assert message_id in en
        assert message_id in zh
        assert t(message_id, language="en") == en[message_id]
        assert t(message_id, language="zh") == zh[message_id]


def test_t_missing_param_keeps_placeholder():
    text = t("tools.send_file.submit_failed", language="en")
    assert "{error}" in text


def test_t_param_braces_are_literal():
    text = t(
        "tools.send_file.submit_failed",
        language="en",
        error="boom {nested}",
    )
    assert "boom {nested}" in text
    assert "{error}" not in text


def test_t_missing_zh_falls_back_to_en(monkeypatch):
    catalogs = {
        "en": {"tools.send_file.none": "No files to send"},
        "zh": {},
    }
    monkeypatch.setattr(translate_mod, "_CATALOGS", catalogs)
    assert t("tools.send_file.none", language="zh") == "No files to send"


def test_t_missing_both_returns_id(monkeypatch):
    monkeypatch.setattr(translate_mod, "_CATALOGS", {"en": {}, "zh": {}})
    assert t("tools.unknown.id", language="en") == "tools.unknown.id"


def test_t_without_language_defaults_to_en():
    text = t("tools.send_file.none")
    assert text == "No files to send"
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in text)


def test_t_with_resolved_preferred_language_follows_memo(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(pref, "_yaml_or_en", lambda: "en")
    with pref.bind_preferred_language_memo(user_id="alice", language="zh"):
        text = t(
            "tools.send_file.none",
            language=pref.resolve_preferred_language(),
        )
    assert "没有可发送的文件" in text


def test_send_file_en_has_no_chinese():
    text = t("tools.send_file.all_missing", language="en")
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in text)
    assert "none of the files exist" in text.lower()


def test_send_file_zh_matches_design_template():
    assert t("tools.send_file.duplicate_skipped", language="zh") == (
        "文件已在本次会话发送过，跳过重复投递："
    )
