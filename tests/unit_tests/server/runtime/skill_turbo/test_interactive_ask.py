# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guided mode must be readable from SkillTurbo inputs / resume params."""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.interactive_ask import (
    apply_interactive_ask_to_inputs,
    extract_interactive_ask,
    resolve_interactive_ask_from_inputs,
    resolve_resume_interactive_ask,
)


class TestExtractInteractiveAsk:
    def test_params_opt_in(self):
        assert extract_interactive_ask({"interactive_ask": True}) is True

    def test_missing_is_off(self):
        assert extract_interactive_ask(None, {"session_id": "x"}) is False

    def test_does_not_infer_from_supports_user_interaction(self):
        assert (
            extract_interactive_ask({"supports_user_interaction": True}) is False
        )


class TestResolveInteractiveAskFromInputs:
    def test_reads_metadata_flag(self):
        assert resolve_interactive_ask_from_inputs(
            {"metadata": {"interactive_ask": True}}
        ) is True

    def test_reads_top_level_camel_case(self):
        assert resolve_interactive_ask_from_inputs({"interactiveAsk": True}) is True

    def test_false_when_declared_off(self):
        assert resolve_interactive_ask_from_inputs(
            {"metadata": {"interactive_ask": False}}
        ) is False

    def test_false_when_absent(self):
        assert resolve_interactive_ask_from_inputs({"metadata": {"session_id": "x"}}) is False
        assert resolve_interactive_ask_from_inputs(None) is False


class TestApplyInteractiveAskToInputs:
    def test_stamps_metadata_when_param_present(self):
        inputs = {"metadata": {"session_id": "s1"}}
        merged = apply_interactive_ask_to_inputs(inputs, True)
        assert merged["metadata"]["interactive_ask"] is True
        assert merged["metadata"]["session_id"] == "s1"
        assert "interactive_ask" not in inputs["metadata"]

    def test_omitted_resume_param_forces_off(self):
        inputs = {"metadata": {"interactive_ask": True, "session_id": "s1"}}
        merged = apply_interactive_ask_to_inputs(inputs, None)
        assert merged["metadata"]["interactive_ask"] is False
        assert inputs["metadata"]["interactive_ask"] is True

    def test_resume_overlay_keeps_saved_slots(self):
        saved = {
            "topic": "南京旅游",
            "metadata": {"interactive_ask": True, "session_id": "s1"},
        }
        merged = apply_interactive_ask_to_inputs(saved, True)
        assert merged["topic"] == "南京旅游"
        assert merged["metadata"]["interactive_ask"] is True


class TestResolveResumeInteractiveAsk:
    def test_inbound_explicit_value_wins(self):
        """前端显式传 False 时，即使 saved 为 True 也以入站为准。"""
        saved = {"metadata": {"interactive_ask": True}}
        assert resolve_resume_interactive_ask(False, saved) is False

    def test_inbound_none_restores_from_saved_true(self):
        """answers payload 不带 interactive_ask 时，从中断点恢复 True。"""
        saved = {"metadata": {"interactive_ask": True, "session_id": "s1"}}
        assert resolve_resume_interactive_ask(None, saved) is True

    def test_inbound_none_restores_from_saved_false(self):
        """中断前就是 False，resume 仍是 False（不凭空变 True）。"""
        saved = {"metadata": {"interactive_ask": False}}
        assert resolve_resume_interactive_ask(None, saved) is False

    def test_inbound_none_and_saved_absent_returns_false(self):
        """saved 也没声明时落成 False（apply 后保持 False）。"""
        assert resolve_resume_interactive_ask(None, {"metadata": {}}) is False
        assert resolve_resume_interactive_ask(None, None) is False

    def test_inbound_none_restores_from_top_level_camel(self):
        saved = {"interactiveAsk": True}
        assert resolve_resume_interactive_ask(None, saved) is True

    def test_end_to_end_resume_restores_guided_mode(self):
        """端到端：raw=None + saved.interactive_ask=True → resume_inputs 仍为 True。"""
        saved = {"topic": "x", "metadata": {"interactive_ask": True, "session_id": "s1"}}
        raw = resolve_resume_interactive_ask(None, saved)
        merged = apply_interactive_ask_to_inputs(saved, raw)
        assert merged["metadata"]["interactive_ask"] is True
        assert merged["topic"] == "x"

    def test_end_to_end_resume_omitted_stays_off(self):
        """端到端：raw=None + saved 无声明 → resume_inputs 落成 False。"""
        saved = {"metadata": {"session_id": "s1"}}
        raw = resolve_resume_interactive_ask(None, saved)
        merged = apply_interactive_ask_to_inputs(saved, raw)
        assert merged["metadata"]["interactive_ask"] is False
