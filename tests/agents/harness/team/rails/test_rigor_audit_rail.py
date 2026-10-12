# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for RigorAuditRail — the deterministic manuscript-rigor scanner.

The rail carries no model call, so every assertion here is exact: a reported
value either is arithmetically infeasible or it is not. That is the point of
the layer. It is the floor beneath the semantic reviewers, and a floor whose
firing needs a model to adjudicate would not be a floor.
"""

import pytest

from jiuwenswarm.agents.harness.team.rails.rigor_audit_rail import (
    RigorAuditRail,
    RigorFinding,
    _grim_infeasible,
    audit_text,
)


def codes(text: str) -> set[str]:
    return {f.code for f in audit_text(text)}


class TestGrim:
    """A mean of n integer responses must land on the 1/n grid."""

    def test_infeasible_mean_is_flagged(self):
        assert _grim_infeasible("3.17", 20) is True

    def test_feasible_mean_passes(self):
        assert _grim_infeasible("3.15", 20) is False   # 63/20
        assert _grim_infeasible("2.50", 4) is False    # 10/4

    def test_large_n_admits_two_decimals(self):
        assert _grim_infeasible("3.17", 1000) is False

    def test_out_of_range_n_is_not_judged(self):
        assert _grim_infeasible("3.17", 0) is False
        assert _grim_infeasible("3.17", 5000) is False

    def test_fires_through_the_public_entry_point(self):
        assert "D10_stat_feasibility" in codes("The mean = 3.17 across conditions (n = 20).")


class TestDeterministicChecks:
    def test_clean_text_yields_nothing(self):
        assert audit_text(
            "We report 85.2% accuracy on the held-out split (n = 200), p < 0.01, "
            "95% CI [0.81, 0.89]."
        ) == []

    def test_p_value_of_exactly_zero(self):
        assert "D11_pvalue_exact_zero" in codes("The contrast was significant (p = 0.000).")

    def test_percentage_above_one_hundred(self):
        assert "D9_percent_out_of_range" in codes("Recall improved to 118.4% of baseline.")

    def test_collapsed_dispersion_needs_two_cells(self):
        one = "Group A scored 4.20 (SD = 0.00)."
        two = one + " Group B scored 3.80 (SD = 0.00)."
        assert "D2_missing_variance" not in codes(one)
        assert "D2_missing_variance" in codes(two)

    def test_inverted_interval(self):
        assert "D20_ci_inverted" in codes("The effect was large, 95% CI [0.71, 0.55].")

    def test_zero_width_interval(self):
        assert "D27_ci_zero_width" in codes("The estimate was tight, 95% CI [0.40, 0.40].")

    def test_placeholder_left_in_the_draft(self):
        assert "D34_submission_completeness" in codes("Accuracy improved by TODO points.")
        assert "D34_submission_completeness" in codes("Written by Author B and colleagues.")

    def test_rounding_grid_needs_four_values(self):
        three = "Scores were 1.00, 2.00 and 3.00."
        four = three + " A fourth condition gave 4.00."
        assert "D1_result_number_grid" not in codes(three)
        assert "D1_result_number_grid" in codes(four)

    def test_findings_render_without_raising(self):
        for finding in audit_text("p = 0.000 and the interval is [0.90, 0.10]."):
            rendered = finding.render()
            assert isinstance(rendered, str) and finding.code in rendered

    def test_audit_text_is_pure(self):
        """Same input, same output, and the first call cannot affect the second."""
        text = "mean = 3.17 (n = 20), p = 0.000"
        assert audit_text(text) == audit_text(text)


class TestRailContract:
    def test_priority_does_not_collide_with_governance_rail(self):
        from jiuwenswarm.agents.harness.team.rails.governance_review_rail import GovernanceReviewRail
        assert RigorAuditRail.SECTION_PRIORITY != GovernanceReviewRail.SECTION_PRIORITY

    def test_findings_start_empty(self):
        assert RigorAuditRail().findings == []

    def test_rail_adds_no_model_call(self):
        """The audit layer must cost zero tokens; that is what makes it a floor."""
        import inspect
        src = inspect.getsource(
            __import__(
                "jiuwenswarm.agents.harness.team.rails.rigor_audit_rail",
                fromlist=["rigor_audit_rail"],
            )
        )
        for forbidden in ("chat(", "completion(", "invoke_model", "acompletion"):
            assert forbidden not in src, f"rail must stay deterministic: {forbidden}"

    def test_finding_is_immutable(self):
        f = RigorFinding("D1", "msg", "evidence")
        with pytest.raises(Exception):
            f.code = "D2"

    def test_rail_is_actually_mounted_on_team_members(self):
        """Exporting the class is not enough -- it has to reach the rail chain.

        This is the regression that matters most: a rail that is importable but
        never mounted looks fine in review and does nothing at runtime.
        """
        from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
            build_member_rails,
        )

        names = [type(r).__name__ for r in build_member_rails()]
        assert "RigorAuditRail" in names
        # Same regression for process governance: a rail class that exists but
        # is not in this chain never runs.
        assert "GovernanceReviewRail" in names

    def test_mounting_can_be_disabled_by_config(self):
        from jiuwenswarm.common.config import get_rigor_audit_enabled

        assert get_rigor_audit_enabled(None) is True
        assert get_rigor_audit_enabled({}) is True
        assert get_rigor_audit_enabled({"rigor_audit": {"enabled": False}}) is False
        assert get_rigor_audit_enabled({"rigor_audit": {"enabled": True}}) is True



class TestResponseIsActuallyRead:
    """The rail must read the response from where the framework puts it.

    AFTER_MODEL_CALL carries a ModelCallInputs on ctx.inputs, and the assistant
    text is ctx.inputs.response. A rail reading ctx.response gets None, audits
    an empty string, reports nothing -- and looks identical in the log to a rail
    that examined a clean draft. That silence is why this is tested directly.
    """

    @staticmethod
    def _ctx(response):
        from openjiuwen.core.single_agent.rail.base import (
            AgentCallbackContext,
            ModelCallInputs,
        )

        return AgentCallbackContext(agent=None, inputs=ModelCallInputs(response=response))

    def test_reads_text_from_inputs_response(self):
        from jiuwenswarm.agents.harness.team.rails.response_text import response_text

        class Resp:
            content = "mean 3.47 with n=10"

        assert response_text(self._ctx(Resp())) == "mean 3.47 with n=10"

    def test_reads_multimodal_content_blocks(self):
        from jiuwenswarm.agents.harness.team.rails.response_text import response_text

        class Resp:
            content = [{"type": "text", "text": "p = 0.000"},
                       {"type": "image", "source": "..."}]

        assert response_text(self._ctx(Resp())) == "p = 0.000"

    def test_missing_response_yields_empty_string(self):
        from jiuwenswarm.agents.harness.team.rails.response_text import response_text

        assert response_text(self._ctx(None)) == ""
        assert response_text(None) == ""

    @pytest.mark.asyncio
    async def test_rail_records_a_finding_from_a_real_context(self):
        """End to end: a flawed draft on ctx.inputs.response must produce a finding."""
        rail = RigorAuditRail()

        class Resp:
            content = "We observed 130% improvement (p = 0.000)."

        await rail.after_model_call(self._ctx(Resp()))
        codes = {f.code for f in rail.findings}
        assert "D9_percent_out_of_range" in codes
        assert "D11_pvalue_exact_zero" in codes


class TestFindingsReachTheNextCall:
    """The writing agent sees the findings of its last response on its next call."""

    class _Builder:
        def __init__(self):
            self.sections = {}

        def add_section(self, section):
            self.sections[section.name] = section

    @staticmethod
    def _response(text):
        class Resp:
            content = text

        return TestResponseIsActuallyRead._ctx(Resp())

    @pytest.mark.asyncio
    async def test_next_prompt_carries_only_the_latest_findings(self):
        rail = RigorAuditRail(language="en")
        rail.system_prompt_builder = builder = self._Builder()

        def section():
            return builder.sections[RigorAuditRail.SECTION_NAME].content["en"]

        await rail.after_model_call(self._response("We observed 130% improvement."))
        await rail.before_model_call(None)
        assert "D9_percent_out_of_range" in section()

        await rail.after_model_call(self._response("Accuracy rose to 71.3%."))
        await rail.before_model_call(None)
        assert "D9_percent_out_of_range" not in section()
        assert "Rigor Self-Check" in section()


class TestMountedInEveryMode:
    """One assembly point is not the assembly point.

    Rails reach a running agent through three builders: team members go through
    build_member_rails(), agent mode through the deep adapter's
    _build_agent_rails(), and code mode through the code adapter's own
    _build_agent_rails(). Wiring only one of them leaves the others with no rail
    at all, and the gap is invisible until something actually runs.
    """

    def test_team_member_path_mounts_it(self):
        from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
            build_member_rails,
        )

        names = [type(r).__name__ for r in build_member_rails()]
        assert "RigorAuditRail" in names
        assert "GovernanceReviewRail" in names

    def test_single_agent_path_registers_a_builder(self):
        import inspect

        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        assert hasattr(JiuWenSwarmDeepAdapter, "_build_rigor_audit_rail")
        assert hasattr(JiuWenSwarmDeepAdapter, "_build_governance_review_rail")
        src = inspect.getsource(JiuWenSwarmDeepAdapter._build_agent_rails)
        assert "_rigor_audit_rail" in src, (
            "the builder exists but is not in the rail list, so agent mode still "
            "runs without the floor"
        )
        assert "_governance_review_rail" in src, (
            "process governance is mounted for team members but not for the "
            "single agent the CLI starts by default"
        )

    def test_single_agent_builder_returns_a_rail(self):
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        rail = JiuWenSwarmDeepAdapter._build_rigor_audit_rail()
        governance = JiuWenSwarmDeepAdapter._build_governance_review_rail()
        assert governance is not None
        assert type(governance).__name__ == "GovernanceReviewRail"
        assert rail is not None and type(rail).__name__ == "RigorAuditRail"

    def test_single_agent_builder_honours_the_config_opt_out(self, monkeypatch):
        # Reading the switch from None always answers "enabled", which silently
        # ignored `rigor_audit.enabled: false` in config.yaml.
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        monkeypatch.delenv("RIGOR_AUDIT", raising=False)
        off = {"rigor_audit": {"enabled": False}}
        assert JiuWenSwarmDeepAdapter._build_rigor_audit_rail(off) is None
        assert JiuWenSwarmDeepAdapter._build_rigor_audit_rail({}) is not None

    def test_code_mode_mounts_both_rails(self):
        import inspect

        from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
            JiuwenSwarmCodeAdapter,
        )

        src = inspect.getsource(JiuwenSwarmCodeAdapter._build_agent_rails)
        assert "_rigor_audit_rail" in src
        assert "_governance_review_rail" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
