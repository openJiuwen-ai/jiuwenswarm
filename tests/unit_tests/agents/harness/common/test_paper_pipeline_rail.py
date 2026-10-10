"""Integration tests for PaperEvidenceRail on the pinned agent-core: the rail is mounted on the agents
the real pipeline builds (``ManagerAgent._create_agent`` / ``ReportingAgent._build_paper_agent``),
those agents are driven by a scripted model (no paid call), and DONE is gated by the same evidence
check the host acceptance uses."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")

from openjiuwen.core.foundation.llm import (  # noqa: E402
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk  # noqa: E402

from jiuwenswarm.agents.harness.common.paper_pipeline import (  # noqa: E402
    evidence,
    evidence_rail,
    revision_state,
    runner,
)
from test_paper_pipeline_evidence import (  # noqa: E402  (sibling test module: shared evidence fixtures)
    _audit,
    _cell,
    _pattern,
    _revision,
    _revision_execution,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(evidence, "_CONFIG", {"missing_primary_rule": None, "delivery_policy": None})
    monkeypatch.setattr(revision_state, "_ACTIVE", None)
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common import workspace

    root = workspace.project_root()
    yield
    evidence_rail.uninstall_paper_evidence_rail()
    workspace.set_project_root(root)  # ManagerAgent sets the process-wide project root


# --------------------------------------------------------------------------- scripted model
class _Scripted:
    """Stands in for the runtime ``Model``: returns the given assistant messages in order."""

    def __init__(self, responses: list[AssistantMessage]):
        self.responses = list(responses)
        self.calls: list = []
        self.model_client_config = ModelClientConfig(client_provider="OpenAI", api_key="test", api_base="http://mock",
                                                     verify_ssl=False)
        self.model_config = ModelRequestConfig(model_name="mock-model")

    async def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0) if self.responses else AssistantMessage(content="ok")

    async def stream(self, messages, **kwargs):
        reply = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(content=reply.content, tool_calls=reply.tool_calls,
                                    usage_metadata=reply.usage_metadata)

    def seen(self) -> str:
        return json.dumps([str(m) for call in self.calls for m in (call if isinstance(call, list) else [call])],
                          ensure_ascii=False)


def _decide(signal: str, call_id: str, **extra) -> AssistantMessage:
    args = {"signal": signal, "rationale": f"{signal} now", **extra}
    return AssistantMessage(content="", tool_calls=[ToolCall(id=call_id, type="function",
                                                             name="submit_manager_decision",
                                                             arguments=json.dumps(args))],
                            usage_metadata=UsageMetadata(model_name="mock-model", finish_reason="tool_calls"))


def _manager_round(run_dir: Path, responses: list[AssistantMessage]):
    """One manager decision on the agent the real ManagerAgent builds."""
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.session.agent import Session
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.extensions.tools.submit_manager_decision import (
        SubmitManagerDecisionTool,
    )
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.manager.agent import ManagerAgent

    model = _Scripted(responses)
    manager = ManagerAgent({"manager": {"max_iterations": 4}}, model=model, project_root_path=run_dir)
    tool = SubmitManagerDecisionTool()
    tool.reset(session_id="s1", request_id="r1")
    agent = manager._create_agent(run_id="r1", submit_tool=tool)

    async def go():
        payload = {"query": "MODE: manage\nCall submit_manager_decision exactly once.", "conversation_id": "s1"}
        session = Session(session_id="s1", card=getattr(agent, "card", None))
        await session.pre_run(inputs=payload)
        try:
            await Runner.run_agent(agent, payload, session=session)
        finally:
            await session.post_run()

    asyncio.run(go())
    return tool.get_submission(session_id="s1", request_id="r1"), model, agent


def _run(tmp_path: Path, cells: dict) -> Path:
    """A run directory the rail can find (manager state + results with evidence)."""
    exp = tmp_path / "experiments" / "r1"
    (exp / "manager").mkdir(parents=True)
    (exp / "manager" / "state.json").write_text("{}", encoding="utf-8")
    _audit(exp / "results", cells)
    return exp / "results"


GOOD = {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))}


def _rails(agent) -> list:
    return agent.find_rails_by_type((evidence_rail.PaperEvidenceRail,))


# --------------------------------------------------------------------------- 1. off by default
def test_rail_off_leaves_the_pipeline_unchanged(tmp_path: Path):
    _run(tmp_path, {"proposed_T1": _cell(_pattern(40), budget=625), "baseline_T1": _cell(_pattern(30), budget=650)})
    decision, model, agent = _manager_round(tmp_path, [_decide("DONE", "c1")])
    assert decision is not None and decision.signal == "DONE"  # no rail: DONE goes through as before
    assert not _rails(agent) and len(model.calls) >= 1


# --------------------------------------------------------------------------- 2. valid evidence
def test_valid_evidence_lets_done_through_and_mounts_once(tmp_path: Path):
    _run(tmp_path, GOOD)
    evidence_rail.install_paper_evidence_rail(tmp_path)
    evidence_rail.install_paper_evidence_rail(tmp_path)  # repeated install: one wrapper, one rail
    decision, _, agent = _manager_round(tmp_path, [_decide("DONE", "c1")])
    assert decision is not None and decision.signal == "DONE"
    assert len(_rails(agent)) == 1
    log = (evidence.evidence_dir(tmp_path / "experiments" / "r1" / "results") / evidence_rail.LOG_FILE).read_text(
        encoding="utf-8")
    assert '"event": "done_requested", "accepted": true' in log


# --------------------------------------------------------------------------- 3. primary comparison missing
def test_done_without_a_verified_primary_comparison_is_rejected_with_tasks(tmp_path: Path):
    _run(tmp_path, {"proposed_T1": _cell(_pattern(40), budget=625), "baseline_T1": _cell(_pattern(30), budget=650)})
    evidence_rail.install_paper_evidence_rail(tmp_path)
    decision, model, _ = _manager_round(tmp_path, [
        _decide("DONE", "c1"),
        _decide("BLOCKED", "c2", blocked_reason="primary comparison cannot be verified"),
    ])
    assert decision is not None and decision.signal == "BLOCKED"  # the DONE call never reached the tool
    seen = model.seen()
    assert "DONE rejected by the host evidence check" in seen and "PRIMARY_COMPARISON_UNVERIFIED" in seen
    assert "recorded budgets differ" in seen


# --------------------------------------------------------------------------- 4. results changed
def test_done_after_results_changed_is_rejected(tmp_path: Path):
    results = _run(tmp_path, GOOD)
    evidence_rail.install_paper_evidence_rail(tmp_path)
    path = results / "baseline_T1.metrics.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"answer_em": 0.5', '"answer_em": 0.1'), encoding="utf-8")
    decision, model, _ = _manager_round(tmp_path, [_decide("DONE", "c1"), _decide("BLOCKED", "c2",
                                                                                   blocked_reason="results changed")])
    assert decision.signal == "BLOCKED" and "RESULT_CHANGED" in model.seen()


# --------------------------------------------------------------------------- 5. interrupted revision
def test_revision_protection_holds_for_a_fresh_service_after_restart(tmp_path: Path):
    state, results = _revision(tmp_path)
    (results.parent / "manager").mkdir()
    (results.parent / "manager" / "state.json").write_text("{}", encoding="utf-8")
    _revision_execution(tmp_path, state, results, {"abl_a_T1": None, "abl_b_T1": _cell(_pattern(35))})
    _revision_execution(tmp_path, state, results, {"abl_b_T1": _cell(_pattern(36))})
    revision_state.install_execution_guard(None)  # "process restart": nothing in memory
    service = evidence_rail.EvidenceService(tmp_path)  # a new service reads the persisted revision
    result = service.check()
    assert not result["ok"] and "REVISION_CELL_DROPPED" in {b["code"] for b in result["blocking"]}  # no retirement record
    evidence_rail.install_paper_evidence_rail(tmp_path)
    decision, model, _ = _manager_round(tmp_path, [_decide("DONE", "c1"), _decide("BLOCKED", "c2",
                                                                                   blocked_reason="cell A failed")])
    assert decision.signal == "BLOCKED" and "abl_a_T1" in model.seen()


def test_runner_reinstalls_the_rail_on_resume_from_run_settings(tmp_path: Path, monkeypatch):
    from jiuwenswarm.agents.harness.common.paper_pipeline import code_agent_budget, research_protocol

    monkeypatch.setattr(code_agent_budget, "install_code_agent_iteration_fix", lambda n: 80)
    monkeypatch.setattr(research_protocol, "install_research_protocol", lambda: [])
    installed = []
    monkeypatch.setattr(evidence_rail, "install_paper_evidence_rail", lambda run_dir: installed.append(run_dir) or [])

    async def nothing(opts):
        return None

    monkeypatch.setattr(runner, "fresh_run", nothing)
    monkeypatch.setattr(runner, "resume_run", nothing)
    asyncio.run(runner.run(runner.PaperRunOptions(run_dir=tmp_path, topic="t"), resume=False))
    assert installed == []  # off by default
    asyncio.run(runner.run(runner.PaperRunOptions(run_dir=tmp_path, topic="t", evidence_rail=True), resume=False))
    asyncio.run(runner.run(runner.PaperRunOptions(run_dir=tmp_path, topic="t"), resume=True))  # flag omitted
    assert len(installed) == 2
    with pytest.raises(runner.PaperRunError, match="needs the rigor protocol"):
        asyncio.run(runner.run(runner.PaperRunOptions(run_dir=tmp_path, topic="t", rigor=False), resume=True))


# --------------------------------------------------------------------------- 6. negative result
def test_a_negative_result_with_valid_evidence_is_delivered_and_briefed(tmp_path: Path):
    _run(tmp_path, {"proposed_T1": _cell(_pattern(20)), "baseline_T1": _cell(_pattern(45))})
    evidence_rail.install_paper_evidence_rail(tmp_path)
    decision, _, _ = _manager_round(tmp_path, [_decide("DONE", "c1")])
    assert decision.signal == "DONE"
    brief = evidence.writing_brief(evidence_rail.EvidenceService(tmp_path).check())
    assert "outcome `a_worse`" in brief


# --------------------------------------------------------------------------- writing-start point
def test_reporting_agent_receives_the_evidence_manifest_before_writing(tmp_path: Path):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.session.agent import Session
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness import create_deep_agent

    _run(tmp_path, {"proposed_T1": _cell(_pattern(40), budget=625), "baseline_T1": _cell(_pattern(30), budget=650)})
    model = _Scripted([AssistantMessage(content="draft written")])
    agent = create_deep_agent(model=model, card=AgentCard(name="reporting", description="writer"),
                              system_prompt="write the paper", rails=[], enable_task_loop=False, max_iterations=2,
                              cwd=str(tmp_path), project_root=str(tmp_path), auto_create_workspace=False)
    assert evidence_rail.mount(agent, evidence_rail.EvidenceService(tmp_path), "reporting")
    assert not evidence_rail.mount(agent, evidence_rail.EvidenceService(tmp_path), "reporting")  # once per agent

    async def go():
        payload = {"query": "Write the paper.", "conversation_id": "w1"}
        session = Session(session_id="w1", card=agent.card)
        await session.pre_run(inputs=payload)
        try:
            await Runner.run_agent(agent, payload, session=session)
        finally:
            await session.post_run()

    asyncio.run(go())
    seen = model.seen()
    assert "Host evidence manifest" in seen and "NOT verified" in seen and "Write the paper." in seen


def test_install_wraps_the_builders_the_pipeline_uses(tmp_path: Path, monkeypatch):
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import ReportingAgent

    built = []

    class _Agent:
        def __init__(self):
            self.rails = []

        def add_rail(self, rail):
            self.rails.append(rail)

        def find_rails_by_type(self, types):
            return [r for r in self.rails if isinstance(r, types)]

    monkeypatch.setattr(ReportingAgent, "_build_paper_agent", lambda self, *, run_id: built.append(_Agent()) or built[-1])
    patched = evidence_rail.install_paper_evidence_rail(tmp_path)
    assert patched == ["ManagerAgent._create_agent", "ReportingAgent._build_paper_agent"]
    agent = ReportingAgent._build_paper_agent(SimpleNamespace(), run_id="r1")
    assert [r.role for r in agent.rails] == ["reporting"]
    evidence_rail.uninstall_paper_evidence_rail()
    assert not ReportingAgent._build_paper_agent(SimpleNamespace(), run_id="r1").rails


def test_a_check_that_cannot_run_rejects_done(tmp_path: Path):
    result = evidence_rail.EvidenceService(tmp_path / "nowhere").check()
    assert not result["ok"] and result["blocking"][0]["code"] == "NO_RESULTS"
    assert "DONE rejected" in evidence_rail.rejection_text(result)
