# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Optional voice receipt policy must not break ordinary runtime prompts."""

import builtins
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail


_POLICY_MODULE = "jiuwenswarm.extensions.video_duplex.backend.session_receipts"


@pytest.mark.parametrize("with_builder", [True, False])
def test_missing_voice_policy_clears_stale_section(monkeypatch, with_builder):
    original_import = builtins.__import__

    def import_without_policy(name, *args, **kwargs):
        if name == _POLICY_MODULE:
            raise ModuleNotFoundError(name, name=name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_policy)
    rail = RuntimePromptRail()
    builder = Mock() if with_builder else None
    rail.system_prompt_builder = builder
    rail._apply_voice_summary_policy(SimpleNamespace())
    if builder is not None:
        builder.remove_section.assert_called_once_with("video_duplex.voice_receipt")


def test_voice_policy_internal_import_failure_is_not_hidden(monkeypatch):
    original_import = builtins.__import__

    def import_broken_policy(name, *args, **kwargs):
        if name == _POLICY_MODULE:
            raise ModuleNotFoundError("missing dependency", name="receipt_dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_broken_policy)
    with pytest.raises(ModuleNotFoundError, match="missing dependency"):
        RuntimePromptRail()._apply_voice_summary_policy(SimpleNamespace())


def test_installed_voice_policy_receives_runtime_context(monkeypatch):
    original_import = builtins.__import__
    policy = Mock()

    def import_with_policy(name, *args, **kwargs):
        if name == _POLICY_MODULE:
            return SimpleNamespace(apply_summary_policy=policy)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_policy)
    rail = RuntimePromptRail()
    rail.system_prompt_builder = Mock()
    ctx = SimpleNamespace()
    rail._apply_voice_summary_policy(ctx)
    policy.assert_called_once_with(rail.system_prompt_builder, rail._session_id, ctx)
