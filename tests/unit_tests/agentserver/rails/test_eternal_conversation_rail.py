from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.eternal_conversation.background_agents import (
    RETRY_CHECKLIST,
    BackgroundAgentRunner,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.coordinator import (
    SessionCoordinator,
    _extractor_evidence,
    _raw_range,
    _validate_extractor,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.evidence import (
    EvidenceWriter,
    resolve_evidence_blobs,
    write_json_atomic,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.memory_cli import (
    DynamicMemoryGateway,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.rail import (
    EternalConversationRail,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.prompts import (
    BUILDER_SYSTEM_PROMPT,
    EXTRACTOR_SYSTEM_PROMPT,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.registry import (
    get_session_coordinator,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.asyncio


async def test_raw_history_is_monotonic_hash_chained_and_mirrored(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path, "session-a")

    first = await writer.append("user-message", {"text": "one"}, task_id="t1")
    second = await writer.append("tool-result", {"value": 2}, task_id="t1")

    assert first["cursor"] == 1
    assert second["cursor"] == 2
    assert second["previous_hash"] == first["hash"]
    raw = _read_jsonl(tmp_path / "raw-history" / "events.jsonl")
    foreground = _read_jsonl(tmp_path / "agent-history" / "foreground" / "conversation.jsonl")
    assert raw == foreground


@pytest.mark.asyncio


async def test_two_writer_instances_share_one_cursor_authority(tmp_path: Path) -> None:
    first = EvidenceWriter(tmp_path, "session-a")
    stale_peer = EvidenceWriter(tmp_path, "session-a")

    one, two = await asyncio.gather(
        first.append("tool-call", {"name": "one"}, task_id="t1"),
        stale_peer.append("tool-result", {"name": "two"}, task_id="t1"),
    )

    rows = _read_jsonl(tmp_path / "raw-history" / "events.jsonl")
    assert sorted((one["cursor"], two["cursor"])) == [1, 2]
    assert [row["cursor"] for row in rows] == [1, 2]
    assert rows[1]["previous_hash"] == rows[0]["hash"]
    recovered = EvidenceWriter(tmp_path, "session-a")
    assert recovered.cursor == 2


@pytest.mark.asyncio


async def test_large_background_history_is_content_addressed_and_reconstructable(
    tmp_path: Path,
) -> None:
    writer = EvidenceWriter(tmp_path, "session-a")
    request = {"frozen_working_memory": "x" * (300 * 1024)}

    await writer.append_agent_history(
        "extractor",
        {"status": "accepted", "system_prompt": "prompt", "request": request},
    )

    row = _read_jsonl(
        tmp_path / "agent-history" / "extractor" / "conversation.jsonl"
    )[0]
    reference = row["request"]
    blob = (
        tmp_path
        / "agent-history"
        / "extractor"
        / reference["$evidence_blob"]
    )
    reconstructed = json.loads(blob.read_text(encoding="utf-8"))
    canonical = json.dumps(
        reconstructed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert reconstructed == request
    assert hashlib.sha256(canonical).hexdigest() == reference["sha256"]
    assert len(canonical) == reference["bytes"]


def test_extractor_evidence_keeps_latest_visible_context_without_repeating_messages() -> None:
    events = [
        {
            "cursor": 1,
            "type": "model-visible-envelope",
            "session_id": "session-a",
            "task_id": "t1",
            "created_at": "now",
            "previous_hash": None,
            "hash": "h1",
            "payload": {
                "messages": ["old context"],
                "tools": ["tool-a"],
                "response": "first answer",
                "status": "succeeded",
                "usage": {"input_tokens": 10},
            },
        },
        {
            "cursor": 2,
            "type": "tool-result",
            "session_id": "session-a",
            "task_id": "t1",
            "created_at": "now",
            "previous_hash": "h1",
            "hash": "h2",
            "payload": {"tool_result": "direct evidence"},
        },
        {
            "cursor": 3,
            "type": "model-visible-envelope",
            "session_id": "session-a",
            "task_id": "t1",
            "created_at": "now",
            "previous_hash": "h2",
            "hash": "h3",
            "payload": {
                "messages": ["final visible context"],
                "tools": ["tool-b"],
                "response": "final answer",
                "status": "succeeded",
                "usage": {"input_tokens": 20},
            },
        },
    ]

    view = _extractor_evidence(events)

    assert view["final_visible_context"]["messages"] == ["final visible context"]
    assert view["final_visible_context"]["tools"] == ["tool-b"]
    assert view["raw_history_manifest"]["event_count"] == 3
    assert view["raw_history_manifest"]["model_visible_envelope_count"] == 2
    assert view["frozen_working_memory"][0]["payload"]["response"] == "first answer"
    assert "messages" not in view["frozen_working_memory"][0]["payload"]
    assert view["frozen_working_memory"][1]["payload"]["tool_result"] == "direct evidence"


def test_extractor_evidence_structurally_bounds_large_protocol_payloads() -> None:
    large = "NimbusGate:" + "x" * 20_000
    events = [
        {
            "cursor": 1,
            "type": "tool-result",
            "hash": "h1",
            "payload": {
                "tool_name": "read_file",
                "status": "succeeded",
                "tool_result": large,
            },
        },
        {
            "cursor": 2,
            "type": "model-visible-envelope",
            "hash": "h2",
            "payload": {
                "messages": ["Keep the exact user term NimbusGate"],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "read_file", "description": large},
                    }
                ],
                "response": large,
                "status": "succeeded",
            },
        },
    ]

    view = _extractor_evidence(events)

    assert events[0]["payload"]["tool_result"] == large
    descriptor = view["frozen_working_memory"][0]["payload"]["tool_result"]
    assert descriptor == {
        "$raw_history_content": True,
        "sha256": hashlib.sha256(large.encode("utf-8")).hexdigest(),
        "utf8_bytes": len(large.encode("utf-8")),
    }
    assert view["frozen_working_memory"][1]["payload"]["response"] == descriptor
    assert view["final_visible_context"]["messages"] == [
        "Keep the exact user term NimbusGate"
    ]
    assert view["final_visible_context"]["tools"] == ["read_file"]


def test_raw_range_batches_only_at_complete_natural_task_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rows: list[dict] = []
    cursor = 0
    for task_number in range(1, 7):
        for kind in ("task-started", "task-finished"):
            cursor += 1
            rows.append({"cursor": cursor, "type": kind, "task_id": f"t{task_number}"})
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    first = _raw_range(path, 1, cursor)
    second = _raw_range(path, int(first[-1]["cursor"]) + 1, cursor)

    assert [row["task_id"] for row in first if row["type"] == "task-finished"] == [
        "t1",
        "t2",
        "t3",
        "t4",
    ]
    assert first[-1]["type"] == "task-finished"
    assert second[-1]["task_id"] == "t6"


def test_extractor_rejects_unsupported_ut_action_before_memory_cli() -> None:
    value = {
        "snapshot": {
            "resident_memory": [],
            "recent_context": [],
            "current_state": [],
            "completed": [],
            "next_actions": [],
            "constraints": [],
        },
        "changed_uts": [
            {
                "action": "update",
                "id": "ut-one",
                "content": "Keep NimbusGate exact.",
                "queries": ["NimbusGate"],
                "must_include": ["NimbusGate"],
            }
        ],
    }

    with pytest.raises(ValueError, match="upsert or retire"):
        _validate_extractor(value)


def test_builder_prompt_cannot_take_over_extractor_semantic_judgment() -> None:
    assert "boundary is structural, not semantic" in BUILDER_SYSTEM_PROMPT
    assert "extraction Agent exclusively owns" in BUILDER_SYSTEM_PROMPT
    assert "Do not reject because a" in BUILDER_SYSTEM_PROMPT
    assert "UT or Snapshot omits a historical item" in BUILDER_SYSTEM_PROMPT


def test_extractor_never_infers_override_from_repetition_or_agent_work() -> None:
    assert "repeating the same contradictory request any number of times" in EXTRACTOR_SYSTEM_PROMPT
    assert "answers, tool edits, passing tests" in EXTRACTOR_SYSTEM_PROMPT
    assert "Direct user messages outrank Agent narration" in EXTRACTOR_SYSTEM_PROMPT


@pytest.mark.asyncio


async def test_raw_history_recovers_state_and_foreground_mirror_after_crash(
    tmp_path: Path,
) -> None:
    writer = EvidenceWriter(tmp_path, "session-a")
    first = await writer.append("user-message", {"text": "one"}, task_id="t1")
    second = await writer.append("task-finished", {"result": "ok"}, task_id="t1")

    # Simulate termination after Raw History fsync but before the two derived
    # writes by rolling both derived files back to the first complete record.
    foreground_path = tmp_path / "agent-history" / "foreground" / "conversation.jsonl"
    foreground_path.write_text(
        json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "state" / "evidence.json").write_text(
        json.dumps({"cursor": 1, "last_hash": first["hash"]}),
        encoding="utf-8",
    )
    (tmp_path / "raw-history" / "search.jsonl").unlink()

    recovered = EvidenceWriter(tmp_path, "session-a")
    assert recovered.cursor == second["cursor"]
    assert _read_jsonl(foreground_path) == _read_jsonl(
        tmp_path / "raw-history" / "events.jsonl"
    )
    search = _read_jsonl(tmp_path / "raw-history" / "search.jsonl")
    assert [row["hash"] for row in search] == [first["hash"], second["hash"]]


@pytest.mark.asyncio


async def test_dynamic_memory_search_unifies_pending_and_built(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path / "feature", "session-a")
    gateway = DynamicMemoryGateway(tmp_path / "feature" / "memory", writer)
    await gateway.ensure_initialized()
    proposal = {
        "base_memory_revision": 0,
        "base_snapshot_revision": 0,
        "from_cursor": 1,
        "to_cursor": 1,
        "snapshot": {"constraints": ["Keep NimbusGate private"]},
        "changed_uts": [
            {
                "action": "upsert",
                "id": "ut-nimbus-gate",
                "memory_id": "memory-ut-nimbus-gate",
                "priority": 90,
                "content": "NimbusGate is the private compatibility environment.",
                "queries": ["NimbusGate"],
                "must_include": ["NimbusGate"],
                "evidence_refs": ["raw-history:cursor-1-1"],
                "source": "session-a",
                "tags": ["alias"],
            }
        ],
        "evidence_refs": ["raw-history:cursor-1-1"],
        "semantic_statement": "The exact alias remains retrievable.",
    }
    await gateway.file_command("publish-pending", proposal, "proposal")

    pending = await gateway.search("NimbusGate")
    assert pending["matches"][0]["id"] == "ut-nimbus-gate"
    assert pending["matches"][0]["build_state"] == "pending"

    batch_path = tmp_path / "batch.json"
    await gateway.call("freeze-pending", "--output", str(batch_path))
    frozen = json.loads(batch_path.read_text(encoding="utf-8"))
    assert "created_at" not in frozen
    assert frozen["frozen_at"] >= frozen["items"][0]["updated_at"]
    await gateway.call("build-pending", "--file", str(batch_path))
    built = await gateway.search("NimbusGate")
    assert built["matches"][0]["build_state"] == "built"


class _FakeModel:
    async def invoke(self, messages, **kwargs):
        system = str(messages[0].content)
        if "memory extraction Agent" in system:
            value = {
                "snapshot": {
                    "resident_memory": ["NimbusGate is private"],
                    "recent_context": [],
                    "current_state": [],
                    "completed": [],
                    "next_actions": [],
                    "constraints": ["Keep NimbusGate private"],
                },
                "changed_uts": [
                    {
                        "action": "upsert",
                        "id": "ut-nimbus-gate",
                        "memory_id": "memory-ut-nimbus-gate",
                        "priority": 90,
                        "content": "NimbusGate is the private compatibility environment.",
                        "queries": ["NimbusGate"],
                        "must_include": ["NimbusGate"],
                        "evidence_refs": ["model-placeholder"],
                        "source": "model-placeholder",
                        "tags": ["alias"],
                    }
                ],
                "semantic_statement": "NimbusGate is preserved under its exact name.",
            }
        else:
            value = {"approved": True, "diagnostics": []}
        return SimpleNamespace(
            content=json.dumps(value),
            usage_metadata=SimpleNamespace(input_tokens=11, output_tokens=7, cache_tokens=3),
        )


class _ControlledModel(_FakeModel):
    def __init__(self, gate: asyncio.Event | None = None, failures: int = 0) -> None:
        self.gate = gate
        self.entered = asyncio.Event()
        self.failures = failures
        self.extractor_calls = 0

    async def invoke(self, messages, **kwargs):
        if "memory extraction Agent" in str(messages[0].content):
            self.extractor_calls += 1
            self.entered.set()
            if self.failures:
                self.failures -= 1
                raise RuntimeError("synthetic extractor outage")
            if self.gate is not None and self.extractor_calls == 1:
                await self.gate.wait()
        return await super().invoke(messages, **kwargs)


class _RejectingBuilderModel(_FakeModel):
    async def invoke(self, messages, **kwargs):
        if "memory build Agent" in str(messages[0].content):
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "approved": False,
                        "diagnostics": ["duplicate IDs with incompatible payloads"],
                    }
                ),
                usage_metadata=None,
            )
        return await super().invoke(messages, **kwargs)


@pytest.mark.asyncio


async def test_background_retry_rebuilds_with_full_structural_checklist(
    tmp_path: Path,
) -> None:
    class RetryModel:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def invoke(self, messages, **kwargs):
            self.prompts.append(str(messages[1].content))
            content = "{}" if len(self.prompts) == 1 else '{"accepted":true}'
            return SimpleNamespace(content=content, usage_metadata=None)

    model = RetryModel()
    runner = BackgroundAgentRunner(
        lambda: model,
        EvidenceWriter(tmp_path, "session-a"),
    )

    def validate(value: dict) -> None:
        if value.get("accepted") is not True:
            raise ValueError("accepted must be true")

    result = await runner.call_json(
        role="builder",
        system_prompt="return JSON",
        request={"batch": 1},
        validate=validate,
    )

    assert result == {"accepted": True}
    assert RETRY_CHECKLIST in model.prompts[1]
    assert "accepted must be true" in model.prompts[1]
    assert 'Here is the exact previous response' in model.prompts[1]
    assert '{}' in model.prompts[1]


def test_extractor_snapshot_limit_error_identifies_exact_item() -> None:
    snapshot = {
        "resident_memory": [],
        "recent_context": [],
        "current_state": [],
        "completed": [],
        "next_actions": [],
        "constraints": ["x" * 304],
    }

    with pytest.raises(
        ValueError,
        match=r"snapshot\.constraints\[0\] has 304 characters; hard limit is 280",
    ):
        _validate_extractor({"snapshot": snapshot, "changed_uts": []})


@pytest.mark.asyncio


async def test_coordinator_runs_two_background_agents_and_publishes_auditable_state(
    tmp_path: Path,
) -> None:
    coordinator = SessionCoordinator(tmp_path, "session-a", lambda: _FakeModel())
    await coordinator.evidence.append("task-started", {"query": "Use NimbusGate"}, task_id="t1")
    finished = await coordinator.evidence.append(
        "task-finished", {"result": "done"}, task_id="t1"
    )

    await coordinator.request_extract(finished["cursor"])
    await coordinator.wait_idle()

    state = await coordinator.memory.projection()
    assert state["covered_through"] == finished["cursor"]
    assert state["snapshot_revision"] == 1
    result = await coordinator.memory.search("NimbusGate")
    assert result["matches"][0]["build_state"] == "built"
    extractor = _read_jsonl(tmp_path / "agent-history" / "extractor" / "conversation.jsonl")
    builder = _read_jsonl(tmp_path / "agent-history" / "builder" / "conversation.jsonl")
    assert extractor[-1]["status"] == "accepted"
    assert builder[-1]["status"] == "accepted"
    assert extractor[-1]["usage"]["input_tokens"] == 11
    assert (tmp_path / "audit" / "source-manifest.json").is_file()


@pytest.mark.asyncio


async def test_new_foreground_events_are_not_lost_while_extractor_is_running(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    model = _ControlledModel(gate)
    coordinator = SessionCoordinator(tmp_path, "session-a", lambda: model)
    await coordinator.evidence.append("task-started", {"query": "first"}, task_id="t1")
    first = await coordinator.evidence.append("task-finished", {"result": "one"}, task_id="t1")
    await coordinator.request_extract(first["cursor"])
    await asyncio.wait_for(model.entered.wait(), timeout=5)

    await coordinator.evidence.append("task-started", {"query": "second"}, task_id="t2")
    second = await coordinator.evidence.append("task-finished", {"result": "two"}, task_id="t2")
    await coordinator.request_extract(second["cursor"])
    assert await coordinator.projection_for_boundary() is None

    gate.set()
    await coordinator.wait_idle()
    projection = await coordinator.memory.projection()
    assert projection["covered_through"] == second["cursor"]
    assert projection["snapshot_revision"] == 2
    assert model.extractor_calls == 2
    eligible = await coordinator.projection_for_boundary()
    assert eligible is not None
    await coordinator.mark_projection_applied(eligible["snapshot_revision"])
    assert await coordinator.projection_for_boundary() is None


@pytest.mark.asyncio


async def test_background_failure_is_audited_and_retried_on_next_task_boundary(
    tmp_path: Path,
) -> None:
    failing = _ControlledModel(failures=6)
    current = {"model": failing}
    coordinator = SessionCoordinator(tmp_path, "session-a", lambda: current["model"])
    await coordinator.evidence.append("task-started", {"query": "remember"}, task_id="t1")
    finished = await coordinator.evidence.append("task-finished", {"result": "done"}, task_id="t1")
    await coordinator.request_extract(finished["cursor"])
    await coordinator.wait_idle()
    extractor = _read_jsonl(tmp_path / "agent-history" / "extractor" / "conversation.jsonl")
    assert [row["status"] for row in extractor[-7:]] == [
        *(["rejected"] * 6),
        "error",
    ]
    failed_state = json.loads(coordinator.state_path.read_text(encoding="utf-8"))
    assert failed_state["extractor_error"]["error_type"] == "RuntimeError"

    current["model"] = _ControlledModel()
    await coordinator.request_extract(finished["cursor"])
    await coordinator.wait_idle()
    projection = await coordinator.memory.projection()
    assert projection["covered_through"] == finished["cursor"]
    recovered_state = json.loads(coordinator.state_path.read_text(encoding="utf-8"))
    assert "extractor_error" not in recovered_state
    assert _read_jsonl(
        tmp_path / "agent-history" / "extractor" / "conversation.jsonl"
    )[-1]["status"] == "accepted"


@pytest.mark.asyncio


async def test_builder_rejection_is_a_durable_fail_fast_state(tmp_path: Path) -> None:
    coordinator = SessionCoordinator(
        tmp_path,
        "session-a",
        lambda: _RejectingBuilderModel(),
    )
    await coordinator.evidence.append(
        "task-started", {"query": "remember NimbusGate"}, task_id="t1"
    )
    finished = await coordinator.evidence.append(
        "task-finished", {"result": "done"}, task_id="t1"
    )

    await coordinator.request_extract(finished["cursor"])
    await coordinator.wait_idle()

    state = json.loads(coordinator.state_path.read_text(encoding="utf-8"))
    assert state["builder_error"]["error_type"] == "RuntimeError"
    assert "duplicate IDs with incompatible payloads" in state["builder_error"]["error"]
    builds = _read_jsonl(tmp_path / "audit" / "builds.jsonl")
    assert builds[-1]["review"]["approved"] is False
    pending = await coordinator.memory.search("NimbusGate")
    assert pending["matches"][0]["build_state"] == "pending"


@pytest.mark.asyncio


async def test_adapter_cleanup_does_not_cancel_session_owned_background_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jiuwenswarm.agents.harness.common.rails.eternal_conversation.rail as rail_module

    monkeypatch.setattr(rail_module, "get_agent_sessions_dir", lambda: tmp_path)
    gate = asyncio.Event()
    model = _ControlledModel(gate)
    rail = EternalConversationRail()
    rail.init(
        SimpleNamespace(
            system_prompt_builder=SystemPromptBuilder(language="cn"),
            ability_manager=_AbilityManager(),
        )
    )
    rail.configure_runtime(
        enabled=True,
        session_id="tui-session",
        request_id="natural-1",
        mode="agent",
        channel="tui",
        project_dir=str(tmp_path),
        model=model,
    )
    context = SimpleNamespace(get_messages=lambda: [], set_messages=lambda _messages: None)
    await rail.before_invoke(SimpleNamespace(context=context, inputs=SimpleNamespace(query="go")))
    await rail.after_invoke(SimpleNamespace(inputs=SimpleNamespace(result="done")))
    await asyncio.wait_for(model.entered.wait(), timeout=5)

    await asyncio.wait_for(rail.close(), timeout=1)
    gate.set()
    await rail.wait_idle()
    coordinator = rail._coordinator
    assert coordinator is not None
    state = await coordinator.memory.projection()
    assert state["covered_through"] > 0
    assert (await coordinator.memory.search("NimbusGate"))["matches"][0][
        "build_state"
    ] == "built"


@pytest.mark.asyncio


async def test_pending_builder_work_resumes_from_durable_state_after_restart(
    tmp_path: Path,
) -> None:
    first = SessionCoordinator(tmp_path, "session-a", lambda: _FakeModel())
    event = await first.evidence.append("task-finished", {"result": "done"}, task_id="t1")
    proposal = {
        "base_memory_revision": 0,
        "base_snapshot_revision": 0,
        "from_cursor": 1,
        "to_cursor": event["cursor"],
        "snapshot": {"constraints": ["Keep NimbusGate private"]},
        "changed_uts": [
            {
                "action": "upsert",
                "id": "ut-nimbus-gate",
                "memory_id": "memory-ut-nimbus-gate",
                "priority": 90,
                "content": "NimbusGate is the private compatibility environment.",
                "queries": ["NimbusGate"],
                "must_include": ["NimbusGate"],
                "evidence_refs": ["raw-history:cursor-1-1"],
                "source": "session-a",
                "tags": ["alias"],
            }
        ],
        "evidence_refs": ["raw-history:cursor-1-1"],
        "semantic_statement": "Preserve the exact alias.",
    }
    await first.memory.file_command("publish-pending", proposal, "restart-proposal")
    write_json_atomic(
        first.state_path,
        {
            "requested_cursor": event["cursor"],
            "builder_error": {
                "at": "2026-08-16T12:56:41+00:00",
                "error_type": "FrameworkError",
                "error": "Insufficient Balance",
            },
        },
    )

    restarted = SessionCoordinator(tmp_path, "session-a", lambda: _FakeModel())
    await restarted.resume_background()
    retry_state = json.loads(restarted.state_path.read_text(encoding="utf-8"))
    assert "builder_error" not in retry_state
    await restarted.wait_idle()
    result = await restarted.memory.search("NimbusGate")
    assert result["matches"][0]["build_state"] == "built"


def test_registry_reuses_coordinator_for_recreated_adapter(tmp_path: Path) -> None:
    first = get_session_coordinator(tmp_path, "session-a", lambda: _FakeModel())
    second = get_session_coordinator(tmp_path, "session-a", lambda: _FakeModel())
    assert second is first


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("true", True), ("off", False), (None, False)],
)


def test_frontend_runtime_flag_is_explicit_and_default_off(value, expected) -> None:
    params = {} if value is None else {"eternal_conversation_enabled": value}
    assert JiuWenSwarmDeepAdapter._resolve_eternal_conversation_enabled(params) is expected


class _AbilityManager:
    def __init__(self) -> None:
        self.cards: dict[str, object] = {}

    def add_ability(self, card, tool):
        self.cards[card.name] = card
        return SimpleNamespace(added=True)

    def remove_ability(self, name):
        self.cards.pop(name, None)


@pytest.mark.asyncio


async def test_rail_is_inert_by_default_then_mounts_prompt_and_tool_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jiuwenswarm.agents.harness.common.rails.eternal_conversation.rail as rail_module

    monkeypatch.setattr(rail_module, "get_agent_sessions_dir", lambda: tmp_path)
    builder = SystemPromptBuilder(language="cn")
    manager = _AbilityManager()
    agent = SimpleNamespace(system_prompt_builder=builder, ability_manager=manager)
    rail = EternalConversationRail()
    rail.init(agent)
    assert manager.cards == {}

    rail.configure_runtime(
        enabled=True,
        session_id="session-a",
        request_id="request-1",
        mode="agent",
        channel="web",
        project_dir=str(tmp_path),
        model=_FakeModel(),
    )
    assert EternalConversationRail.TOOL_NAME in manager.cards
    await rail.before_model_call(SimpleNamespace())
    prompt = builder.build()
    assert "search_long_term_memory" in prompt
    assert "For every suspected conflict" in prompt
    assert "at least two search_long_term_memory calls" in prompt
    assert "even when the Snapshot already states the conflict" in prompt
    assert "repeat that exact original term" in prompt
    assert "final character MUST be ? or ？" in prompt
    assert "Published memory records prior decisions" in prompt

    rail.configure_runtime(
        enabled=False,
        session_id="session-a",
        request_id="request-2",
        mode="agent",
        channel="web",
        project_dir=str(tmp_path),
        model=_FakeModel(),
    )
    assert manager.cards == {}
    assert builder.get_section(EternalConversationRail.SECTION_NAME) is None

    with pytest.raises(RuntimeError, match="multiple Sessions"):
        rail.configure_runtime(
            enabled=True,
            session_id="session-b",
            request_id="request-3",
            mode="agent",
            channel="web",
            project_dir=str(tmp_path),
            model=_FakeModel(),
        )


@pytest.mark.asyncio


async def test_snapshot_replacement_waits_for_real_pre_admission_context_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jiuwenswarm.agents.harness.common.rails.eternal_conversation.rail as rail_module

    monkeypatch.setattr(rail_module, "get_agent_sessions_dir", lambda: tmp_path)
    rail = EternalConversationRail()
    rail.init(
        SimpleNamespace(
            system_prompt_builder=SystemPromptBuilder(language="cn"),
            ability_manager=_AbilityManager(),
        )
    )
    rail.configure_runtime(
        enabled=True,
        session_id="session-a",
        request_id="task-new",
        mode="agent",
        channel="web",
        project_dir=str(tmp_path),
        model=_FakeModel(),
    )
    coordinator = rail._coordinator
    assert coordinator is not None
    old_finished = await coordinator.evidence.append(
        "task-finished", {"result": "old"}, task_id="task-old"
    )
    await coordinator.request_extract(old_finished["cursor"])
    await coordinator.wait_idle()

    # ReactAgent initializes ModelContext only after BEFORE_INVOKE.
    await rail.before_invoke(
        SimpleNamespace(context=None, inputs=SimpleNamespace(query="new work"))
    )
    messages = ["old visible one", "old visible two"]

    def set_messages(value) -> None:
        messages[:] = value

    context = SimpleNamespace(
        get_messages=lambda: list(messages),
        set_messages=set_messages,
    )
    await rail.on_user_message(
        SimpleNamespace(
            context=context,
            inputs=SimpleNamespace(parts=["new work"], source="query"),
        )
    )

    assert messages == []
    harness = json.loads(coordinator.state_path.read_text(encoding="utf-8"))
    assert harness["applied_snapshot_revision"] == 1
    replaced = [
        row
        for row in _read_jsonl(coordinator.evidence.raw_path)
        if row["type"] == "context-replaced"
    ]
    assert replaced[-1]["payload"]["replaced_messages"] == [
        "old visible one",
        "old visible two",
    ]
    assert (
        replaced[-1]["payload"]["safe_boundary"]
        == "before-user-message-admission"
    )


@pytest.mark.asyncio


async def test_permission_resume_stays_inside_one_natural_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jiuwenswarm.agents.harness.common.rails.eternal_conversation.rail as rail_module

    monkeypatch.setattr(rail_module, "get_agent_sessions_dir", lambda: tmp_path)
    agent = SimpleNamespace(
        system_prompt_builder=SystemPromptBuilder(language="cn"),
        ability_manager=_AbilityManager(),
    )
    rail = EternalConversationRail()
    rail.init(agent)
    rail.configure_runtime(
        enabled=True,
        session_id="session-a",
        request_id="natural-1",
        mode="agent",
        channel="web",
        project_dir=str(tmp_path),
        model=_FakeModel(),
    )
    context = SimpleNamespace(get_messages=lambda: [], set_messages=lambda _messages: None)
    await rail.before_invoke(
        SimpleNamespace(context=context, inputs=SimpleNamespace(query="build it"))
    )
    await rail.after_invoke(
        SimpleNamespace(
            inputs=SimpleNamespace(result={"result_type": "interrupt"}),
        )
    )

    rail.configure_runtime(
        enabled=True,
        session_id="session-a",
        request_id="approval-1",
        mode="agent",
        channel="web",
        project_dir=str(tmp_path),
        model=_FakeModel(),
        interaction_resume=True,
    )
    await rail.before_invoke(SimpleNamespace(context=context, inputs=SimpleNamespace(query="")))
    await rail.after_invoke(SimpleNamespace(inputs=SimpleNamespace(result="done")))
    await rail.wait_idle()

    rows = _read_jsonl(
        tmp_path
        / "session-a"
        / "eternal-conversation"
        / "raw-history"
        / "events.jsonl"
    )
    assert [row["type"] for row in rows].count("task-started") == 1
    assert [row["type"] for row in rows].count("task-suspended") == 1
    assert [row["type"] for row in rows].count("task-resumed") == 1
    assert [row["type"] for row in rows].count("task-finished") == 1
    assert {row["task_id"] for row in rows} == {"natural-1"}
