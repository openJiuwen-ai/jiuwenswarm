# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the scientific-verification rail provider (research gates).

The provider mounts a deterministic, model-free integrity gate on every swarm
team member output: bracketed claims-ledger tokens (``[C-NNN]``) are stripped
from prose, remaining ledger references must map to approved claims, and
placeholder citation markers are flagged. ``full`` strictness additionally
pushes a steering message and requests one more ReAct iteration; ``autopilot``
only logs. The rail never raises and never touches the network or an LLM.

Sections:
  A. config — the ``react.research_gates`` getters and spec emission,
  B. mounting — factory behavior against ``SwarmBuildContext``,
  C. verification — the deterministic checks and the model-call hook,
  D. robustness — corruption / malformed-input degradation.

Mounting is decided at spec time (``config_specs`` emits the ``RailSpec``
only when the feature is enabled and bakes strictness into params), so the
factory tests below exercise params + build-context environment only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
)

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import (
    _research_gate_rail_specs,
    build_member_capability_specs,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import research_gate_rails
from jiuwenswarm.agents.swarm.providers.research_gate_rails import (
    RESEARCH_GATE,
    ScientificVerificationRail,
    _resolve_claims_path,
    approved_claim_ids,
    build_research_gate_rail,
    find_ledger_id_references,
    find_placeholder_markers,
    load_claims_ledger,
    strip_ledger_id_tokens,
    verify_scientific_text,
)
from jiuwenswarm.common.config import (
    get_research_gates_enabled,
    get_research_gates_strictness,
)


def _gates_config(enabled: bool = True, strictness: str = "autopilot") -> dict:
    return {"react": {"research_gates": {"enabled": enabled, "strictness": strictness}}}


def _claims_yaml(entries: list[dict]) -> str:
    return yaml.safe_dump({"claims": entries})


# ---------------------------------------------------------------------------
# A. config
# ---------------------------------------------------------------------------


def test_research_gates_disabled_by_default() -> None:
    assert get_research_gates_enabled({}) is False
    assert get_research_gates_enabled(None) is False
    assert get_research_gates_enabled({"react": {}}) is False
    assert get_research_gates_enabled({"react": {"research_gates": {}}}) is False


def test_research_gates_enabled_when_switch_is_true() -> None:
    assert get_research_gates_enabled(_gates_config(enabled=True)) is True


@pytest.mark.parametrize("bad_value", ["true", "yes", 1, ["x"]])
def test_research_gates_enabled_requires_literal_true(bad_value: Any) -> None:
    config = {"react": {"research_gates": {"enabled": bad_value}}}
    assert get_research_gates_enabled(config) is False


def test_research_gates_strictness_defaults_to_autopilot() -> None:
    assert get_research_gates_strictness({}) == "autopilot"
    assert get_research_gates_strictness(None) == "autopilot"
    assert get_research_gates_strictness({"react": {"research_gates": {}}}) == "autopilot"


@pytest.mark.parametrize("raw", ["full", " FULL ", "Full"])
def test_research_gates_strictness_normalized(raw: str) -> None:
    assert get_research_gates_strictness(_gates_config(strictness=raw)) == "full"


@pytest.mark.parametrize("raw", ["strict", "off", "", None, 7])
def test_research_gates_strictness_invalid_values_coerced(raw: Any) -> None:
    assert get_research_gates_strictness(_gates_config(strictness=raw)) == "autopilot"


def test_spec_emission_gated_and_registry_reexport() -> None:
    # Registry wiring: config_specs references the provider by symbol, so a
    # rename/missing re-export breaks here rather than at team assembly.
    assert registry.RESEARCH_GATE == RESEARCH_GATE == "swarm.research_gate"
    assert "RESEARCH_GATE" in registry.__all__

    # Disabled (or absent) config emits no spec at all — the factory is never
    # consulted, which is the no-behavior-change guarantee for existing users.
    assert _research_gate_rail_specs({}) == []
    assert _research_gate_rail_specs({"react": {"research_gates": {"enabled": False}}}) == []

    # Enabled config emits exactly one spec with the strictness baked into
    # params (the swarm assembly doctrine: params, not ctx.config, at build).
    specs = _research_gate_rail_specs(_gates_config(enabled=True, strictness="full"))
    assert len(specs) == 1
    assert specs[0].type == registry.RESEARCH_GATE
    assert specs[0].params == {"strictness": "full"}


# ---------------------------------------------------------------------------
# B. mounting
# ---------------------------------------------------------------------------


def test_factory_mounts_rail_when_team_workspace_root_present(tmp_path: Path) -> None:
    ctx = SwarmBuildContext(session_id="session-1", team_ws_root=str(tmp_path))

    rails = build_research_gate_rail({"strictness": "FULL"}, ctx)

    assert len(rails) == 1
    assert isinstance(rails[0], ScientificVerificationRail)
    assert rails[0].strictness == "full"


def test_factory_mounts_nothing_without_team_workspace_root() -> None:
    ctx = SwarmBuildContext(session_id="session-1", team_ws_root=None)

    assert build_research_gate_rail({}, ctx) == []


def test_factory_defaults_to_autopilot_when_strictness_param_empty(tmp_path: Path) -> None:
    ctx = SwarmBuildContext(session_id="session-1", team_ws_root=str(tmp_path))

    rails = build_research_gate_rail({}, ctx)

    assert len(rails) == 1
    assert rails[0].strictness == "autopilot"


def test_factory_loads_approved_claims_from_team_workspace(tmp_path: Path) -> None:
    entries = [
        {"id": "C-001", "status": "supported"},
        {"id": "C-002", "status": "frozen"},
        {"id": "C-003", "status": "proposed"},
        {"id": "C-004"},
    ]
    # The ledger resolves at the workspace root first...
    root_ws = tmp_path / "root_ws"
    root_ws.mkdir()
    (root_ws / "claims.yml").write_text(_claims_yaml(entries), encoding="utf-8")
    # ...then falls back to the nested workspace/ directory layout.
    nested_ws = tmp_path / "nested_ws"
    (nested_ws / "workspace").mkdir(parents=True)
    (nested_ws / "workspace" / "claims.yml").write_text(_claims_yaml(entries), encoding="utf-8")

    for team_ws_root in (str(root_ws), str(nested_ws)):
        ctx = SwarmBuildContext(session_id="session-1", team_ws_root=team_ws_root)
        rails = build_research_gate_rail({}, ctx)
        assert len(rails) == 1
        assert rails[0].approved_ids == {"C-001", "C-002"}


def test_resolve_claims_path_rejects_escape_attempts(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "secret.yml"
    outside.write_text("id: C-999\nstatus: supported\n", encoding="utf-8")

    assert _resolve_claims_path(str(root), "../secret.yml") is None
    assert _resolve_claims_path(str(root), str(outside)) is None
    # Prefix-collision sibling directories are not inside the root either.
    sibling = tmp_path / "ws_evil"
    sibling.mkdir()
    (sibling / "claims.yml").write_text("x", encoding="utf-8")
    assert _resolve_claims_path(str(root), "../ws_evil/claims.yml") is None
    # In-root resolution is unchanged.
    (root / "claims.yml").write_text("x", encoding="utf-8")
    assert _resolve_claims_path(str(root), "claims.yml") == str(root / "claims.yml")


def test_factory_ignores_traversal_claims_file(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "claims.yml").write_text(
        _claims_yaml([{"id": "C-001", "status": "supported"}]), encoding="utf-8"
    )
    root_ws = tmp_path / "root_ws"
    root_ws.mkdir()
    ctx = SwarmBuildContext(session_id="session-1", team_ws_root=str(root_ws))

    rails = build_research_gate_rail({"claims_file": "../outside/claims.yml"}, ctx)

    assert len(rails) == 1
    assert rails[0].approved_ids == set()


@pytest.mark.parametrize("mode", ["team", "code.team"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_capability_specs_mount_research_gate_for_every_member(mode: str, role: str) -> None:
    rails_on, _ = build_member_capability_specs(
        _gates_config(enabled=True, strictness="full"), mode, role
    )
    research_specs = [spec for spec in rails_on if spec.type == registry.RESEARCH_GATE]
    assert len(research_specs) == 1
    assert research_specs[0].params == {"strictness": "full"}

    rails_off, _ = build_member_capability_specs({}, mode, role)
    assert registry.RESEARCH_GATE not in [spec.type for spec in rails_off]


# ---------------------------------------------------------------------------
# C. verification
# ---------------------------------------------------------------------------


def test_strip_ledger_id_tokens_removes_bracketed_tokens() -> None:
    cleaned, stripped = strip_ledger_id_tokens("We prove [C-001] and again [C-001]. See C-001.")

    # Bracketed bookkeeping tokens vanish (no spacing inserted, matching the
    # deterministic repair in the research workflow); bare references stay.
    assert cleaned == "We prove  and again . See C-001."
    assert stripped == 2


def test_find_ledger_id_references_returns_sorted_unique() -> None:
    assert find_ledger_id_references("b C-002 a C-001 c C-002") == ["C-001", "C-002"]
    assert find_ledger_id_references("") == []


def test_verify_scientific_text_checks_run_on_cleaned_text() -> None:
    report = verify_scientific_text(
        "Claim [C-002] rests on C-001 and C-003.",
        {"C-001"},
    )

    assert report["cleaned_text"] == "Claim  rests on C-001 and C-003."
    assert report["stripped_ids"] == 1
    # C-002 was bracketed bookkeeping (stripped, not a citation); C-001 is
    # approved; C-003 is a bare reference outside the approved set.
    assert report["unmapped_ids"] == ["C-003"]
    assert report["placeholder_markers"] == []


def test_find_placeholder_markers_flags_every_marker_shape() -> None:
    text = "TODO fix [?] the <ref> tag [citation needed] and FIXME later"
    assert set(find_placeholder_markers(text)) == {
        "TODO",
        "[?]",
        "<ref>",
        "[citation needed]",
        "FIXME",
    }
    assert find_placeholder_markers("clean text") == []


def _hook_context(response: Any) -> AgentCallbackContext:
    ctx = AgentCallbackContext(agent=SimpleNamespace())
    ctx.inputs = ModelCallInputs(response=response)
    ctx.bind_steering_queue(asyncio.Queue())
    return ctx


@pytest.mark.asyncio
async def test_rail_strips_in_place_and_passes_clean_output() -> None:
    response = SimpleNamespace(content="Result [C-001] holds; see C-001.")
    rail = ScientificVerificationRail(strictness="autopilot", approved_ids={"C-001"})
    ctx = _hook_context(response)

    await rail.after_model_call(ctx)

    assert response.content == "Result  holds; see C-001."
    # No violations remain: no steering pushed, no extra iteration, in either
    # strictness profile.
    assert ctx.drain_steering() == []
    assert ctx.consume_model_continue_request() is False

    clean = SimpleNamespace(content="All good, see C-001.")
    full_ctx = _hook_context(clean)
    full_rail = ScientificVerificationRail(strictness="full", approved_ids={"C-001"})
    await full_rail.after_model_call(full_ctx)
    assert full_ctx.drain_steering() == []
    assert full_ctx.consume_model_continue_request() is False


@pytest.mark.asyncio
async def test_rail_full_mode_steers_and_requests_one_more_iteration() -> None:
    response = SimpleNamespace(content="This uses C-999 and [citation needed] here.")
    rail = ScientificVerificationRail(strictness="full", approved_ids=set())
    ctx = _hook_context(response)

    await rail.after_model_call(ctx)

    assert ctx.consume_model_continue_request() is True
    steering = ctx.drain_steering()
    assert len(steering) == 1
    assert "C-999" in steering[0]
    assert "citation needed" in steering[0]


# ---------------------------------------------------------------------------
# D. robustness
# ---------------------------------------------------------------------------


def test_factory_returns_empty_when_input_resolution_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(params: dict, ctx: SwarmBuildContext) -> Any:
        raise RuntimeError("resolution failed")

    monkeypatch.setattr(research_gate_rails.ResearchGateInput, "resolve", _boom)
    ctx = SwarmBuildContext(session_id="session-1", team_ws_root=str(tmp_path))

    assert build_research_gate_rail({}, ctx) == []


def test_load_claims_ledger_defends_against_bad_files_and_shapes(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.yml"
    corrupt.write_text("{{{ not yaml", encoding="utf-8")
    assert load_claims_ledger(str(corrupt)) == []
    assert load_claims_ledger(str(tmp_path / "missing.yml")) == []

    as_list = tmp_path / "list.yml"
    as_list.write_text(
        yaml.safe_dump([{"id": "C-001", "status": "supported"}, "junk", {"id": "C-002"}]),
        encoding="utf-8",
    )
    assert load_claims_ledger(str(as_list)) == [
        {"id": "C-001", "status": "supported"},
        {"id": "C-002"},
    ]

    as_entries = tmp_path / "entries.yml"
    as_entries.write_text(
        yaml.safe_dump({"entries": [{"id": "C-009", "status": "frozen"}]}), encoding="utf-8"
    )
    assert approved_claim_ids(load_claims_ledger(str(as_entries))) == {"C-009"}

    scalar = tmp_path / "scalar.yml"
    scalar.write_text("just a string", encoding="utf-8")
    assert load_claims_ledger(str(scalar)) == []


def test_factory_mounts_with_empty_approved_set_on_corrupt_ledger(tmp_path: Path) -> None:
    (tmp_path / "claims.yml").write_text("{{{ not yaml", encoding="utf-8")
    ctx = SwarmBuildContext(session_id="session-1", team_ws_root=str(tmp_path))

    rails = build_research_gate_rail({}, ctx)

    # A corrupt ledger degrades to "no approved claims" — every ledger
    # reference is then unmapped — instead of failing team assembly.
    assert len(rails) == 1
    assert rails[0].approved_ids == set()


@pytest.mark.asyncio
async def test_rail_hook_never_raises_on_malformed_inputs() -> None:
    rail = ScientificVerificationRail(strictness="full", approved_ids={"C-001"})

    untyped = AgentCallbackContext(agent=SimpleNamespace())
    untyped.inputs = {"not": "ModelCallInputs"}
    none_response = AgentCallbackContext(agent=SimpleNamespace())
    none_response.inputs = ModelCallInputs(response=None)
    no_content = AgentCallbackContext(agent=SimpleNamespace())
    no_content.inputs = ModelCallInputs(response=SimpleNamespace())
    non_string = AgentCallbackContext(agent=SimpleNamespace())
    non_string.inputs = ModelCallInputs(response=SimpleNamespace(content=["x"]))
    empty = AgentCallbackContext(agent=SimpleNamespace())
    empty.inputs = ModelCallInputs(response=SimpleNamespace(content=""))

    for ctx in (untyped, none_response, no_content, non_string, empty):
        await rail.after_model_call(ctx)  # must not raise
