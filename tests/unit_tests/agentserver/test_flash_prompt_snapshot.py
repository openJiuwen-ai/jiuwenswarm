# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""M0 S1: deterministic Flash prompt snapshots and section/band hashes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from jiuwenswarm.agents.harness.flash.prompt.snapshot import (
    FLASH_PROMPT_SNAPSHOT_SCHEMA_VERSION,
    build_prompt_snapshot,
    write_prompt_snapshot,
)


@dataclass
class _Section:
    name: str
    priority: int
    content: dict[str, str]


def _sections() -> list[_Section]:
    return [
        _Section("技能总则", 700, {"cn": "技能规则", "en": "skill rules"}),
        _Section("身份", 0, {"cn": "身份", "en": "identity"}),
        _Section("时间事实", 1020, {"cn": "今天", "en": "today"}),
        _Section("安全总纲", 100, {"cn": "安全", "en": "safety"}),
    ]


def test_snapshot_is_deterministic_and_language_specific() -> None:
    first = build_prompt_snapshot(_sections(), language="zh")
    second = build_prompt_snapshot(list(reversed(_sections())), language="zh")
    english = build_prompt_snapshot(_sections(), language="en")

    assert first.schema_version == FLASH_PROMPT_SNAPSHOT_SCHEMA_VERSION
    assert first.language == "cn"
    assert first.full_hash == second.full_hash
    assert first.static_hash == second.static_hash
    assert first.band_hashes == second.band_hashes
    assert first.full_hash != english.full_hash
    assert [section.priority for section in first.sections] == [0, 100, 700, 1020]


def test_section_and_band_hashes_cover_static_and_dynamic_sections() -> None:
    snapshot = build_prompt_snapshot(_sections(), language="cn")
    hashes = {name: digest for _priority, name, digest in snapshot.section_hashes}

    assert set(hashes) == {"身份", "安全总纲", "技能总则", "时间事实"}
    # 前缀长度/哈希按 SDK 拼接规则（段间 "\n\n"）计算，而非各段长度之和。
    static_texts = [
        section.text for section in snapshot.sections if section.priority < 800
    ]
    assert snapshot.prefix_chars == len("\n\n".join(static_texts))
    assert snapshot.prefix_hash
    assert "identity" in snapshot.band_hashes
    assert "security" in snapshot.band_hashes
    assert "skills" in snapshot.band_hashes
    assert "facts" in snapshot.band_hashes


def test_static_hash_excludes_dynamic_region() -> None:
    sections = _sections()
    static = build_prompt_snapshot(
        [section for section in sections if section.priority < 800],
        language="cn",
    )
    with_dynamic = build_prompt_snapshot(sections, language="cn")

    assert static.static_hash == with_dynamic.static_hash
    assert static.full_hash != with_dynamic.full_hash


def test_prefix_hash_skips_empty_sections() -> None:
    """空段/纯空白段不参与前缀拼接，对齐 SDK 的 part.strip() 过滤。"""
    sections = [
        _Section("A", 0, {"cn": "A", "en": "A"}),
        _Section("空", 10, {"cn": "", "en": ""}),
        _Section("空格", 20, {"cn": " ", "en": " "}),
        _Section("B", 30, {"cn": "B", "en": "B"}),
    ]
    snapshot = build_prompt_snapshot(sections, language="cn")
    assert snapshot.prefix_chars == len("A\n\nB")
    assert snapshot.prefix_hash == hashlib.sha256("A\n\nB".encode("utf-8")).hexdigest()


def test_section_text_does_not_fallback_when_language_empty() -> None:
    """单语言空正文（{"cn": "", "en": "X"}）不跨语言回退，中文渲染跳过该段。"""
    sections = [
        _Section("A", 0, {"cn": "A", "en": "A"}),
        _Section("空cn", 10, {"cn": "", "en": "X"}),
        _Section("B", 20, {"cn": "B", "en": "B"}),
    ]
    snapshot = build_prompt_snapshot(sections, language="cn")
    assert snapshot.prefix_chars == len("A\n\nB")
    assert snapshot.prefix_hash == hashlib.sha256("A\n\nB".encode("utf-8")).hexdigest()


def test_unknown_priority_is_visible_and_unbanded() -> None:
    snapshot = build_prompt_snapshot(
        [_Section("遗留段", 42, {"cn": "旧规则", "en": "legacy"})],
        language="cn",
    )

    section = snapshot.sections[0]
    assert section.registered is False
    # 未登记段号（priority 42）不在 FLASH_SECTION_REGISTRY，resolve_carrier 默认归 attachment。
    assert section.carrier == "attachment"
    assert section.band == "unbanded"


def test_snapshot_writes_deterministic_json_and_text(tmp_path) -> None:
    snapshot = build_prompt_snapshot(_sections(), language="cn")
    json_path, text_path = write_prompt_snapshot(snapshot, tmp_path)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == FLASH_PROMPT_SNAPSHOT_SCHEMA_VERSION
    assert payload["language"] == "cn"
    assert payload["full_hash"] == snapshot.full_hash
    assert payload["sections"][0]["priority"] == 0
    assert "full_hash=" in text_path.read_text(encoding="utf-8")


def test_snapshot_stem_rejects_path_segments(tmp_path) -> None:
    snapshot = build_prompt_snapshot(_sections(), language="cn")

    try:
        write_prompt_snapshot(snapshot, tmp_path, stem="../escape")
    except ValueError as exc:
        assert "path separators" in str(exc)
    else:
        raise AssertionError("unsafe snapshot stem must be rejected")
