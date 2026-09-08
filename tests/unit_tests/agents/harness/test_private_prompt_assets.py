"""Contract tests for the optional Xiaoyi private prompt-asset bridge."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.prompt import private_assets


def test_oss_build_uses_no_private_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XIAOYI_PROMPT_ASSETS", raising=False)
    monkeypatch.setattr(private_assets, "_xiaoyi_prompt_assets", None)

    assert private_assets.load_mode_sections("office") is None


def test_private_sections_preserve_asset_order_and_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "schema_version": 1,
        "modes": {
            "office": [
                {"id": "identity", "priority": 10, "content": {"en": "private"}},
                {"id": "task_execution", "priority": 31, "content": {"en": "rules"}},
            ]
        },
        "shared": {},
    }
    monkeypatch.setattr(
        private_assets,
        "_xiaoyi_prompt_assets",
        SimpleNamespace(load_prompt_assets=lambda: payload),
    )

    sections = private_assets.load_mode_sections("office")

    assert sections is not None
    assert [(section.name, section.priority) for section in sections] == [
        ("identity", 10),
        ("task_execution", 31),
    ]


def test_required_release_fails_closed_when_assets_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XIAOYI_PROMPT_ASSETS", "required")
    monkeypatch.setattr(private_assets, "_xiaoyi_prompt_assets", None)

    with pytest.raises(private_assets.PrivatePromptAssetsError, match="required"):
        private_assets.load_mode_sections("office")
