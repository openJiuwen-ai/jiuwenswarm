# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EvidenceRail inside a real ``create_deep_agent`` loop with a scripted model client.

The lifecycle tests in ``test_evidence_rail.py`` drive every hook with one shared
``AgentCallbackContext``.  A real DeepAgent does not: ``before_invoke`` /
``after_invoke`` receive the DeepAgent context, while model and tool hooks receive
contexts created by the inner ReAct loop with their own ``extra`` dict.  These
tests pin the behaviour that matters to a user of the rail in that real loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    BaseModelClient,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.evidence_rail import (
    EVIDENCE_ITEMS_KEY,
    EvidenceItem,
    EvidenceRail,
    EvidenceRailConfig,
)

TOOL_NAME = "lookup_value"
EVIDENCE = EvidenceItem(
    evidence_id="ev-1",
    source="fixture:ledger#1",
    content="The looked-up value is 42.",
)


class _ScriptedClient(BaseModelClient):
    __client_name__ = "evidence-rail-test"

    def __init__(self, responses: list[AssistantMessage]) -> None:
        super().__init__(
            model_config=ModelRequestConfig(model_name="evidence-rail-test"),
            model_client_config=ModelClientConfig(
                client_provider="OpenAI",
                api_key="test",
                api_base="http://test.invalid",
                verify_ssl=False,
            ),
        )
        self.responses, self.index = responses, 0
        self.system_prompts: list[str] = []

    async def invoke(
        self, messages: Any, *_args: Any, **_kwargs: Any
    ) -> AssistantMessage:
        self.system_prompts.append(
            "\n".join(
                str(getattr(message, "content", ""))
                for message in messages
                if getattr(message, "role", None) == "system"
            )
        )
        result = self.responses[self.index]
        self.index += 1
        return result

    async def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        if False:
            yield None

    async def generate_image(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def generate_speech(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def generate_video(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError


class _RuntimeModel:
    def __init__(self, client: _ScriptedClient) -> None:
        self.client = client
        self.model_client_config, self.model_config = (
            client.model_client_config,
            client.model_config,
        )

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return await self.client.invoke(*args, **kwargs)


class _EvidenceInjector(DeepAgentRail):
    """Hands evidence to EvidenceRail through the documented ``ctx.extra`` key."""

    priority = 90

    async def before_invoke(self, ctx) -> None:
        ctx.extra[EVIDENCE_ITEMS_KEY] = [EVIDENCE.model_dump()]


def _tool_call() -> AssistantMessage:
    return AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id="call-1",
                type="function",
                name=TOOL_NAME,
                arguments='{"key": "value"}',
            )
        ],
        usage_metadata=UsageMetadata(model_name="evidence-rail-test"),
    )


def _answer(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=text, usage_metadata=UsageMetadata(model_name="evidence-rail-test")
    )


def _lookup_tool(
    calls: list[dict[str, Any]], *, fail_first_with: type[Exception] | None = None
) -> LocalFunction:
    def lookup(key: str) -> dict[str, Any]:
        calls.append({"key": key})
        if fail_first_with is not None and len(calls) == 1:
            raise fail_first_with("injected failure")
        return {"key": key, "value": 42}

    return LocalFunction(
        card=ToolCard(
            id=TOOL_NAME,
            name=TOOL_NAME,
            description="Look up a value.",
            input_params={"type": "object", "properties": {"key": {"type": "string"}}},
        ),
        func=lookup,
    )


async def _run(
    rails: list[Any],
    client: _ScriptedClient,
    tools: list[LocalFunction] | None = None,
) -> dict[str, Any]:
    agent = create_deep_agent(
        model=cast(Any, _RuntimeModel(client)),
        tools=tools or [],
        rails=rails,
        enable_read_image_multimodal=False,
        auto_create_workspace=False,
        enable_task_loop=False,
        max_iterations=4,
    )
    return await agent.invoke(
        {"query": "What is the value?", "conversation_id": "evidence-rail-loop"}
    )


def _only_run_dir(root: Path) -> Path:
    runs = [path for path in root.iterdir() if path.is_dir()]
    assert len(runs) == 1, runs
    return runs[0]


def _manifest(root: Path) -> dict[str, Any]:
    return json.loads(
        (_only_run_dir(root) / "run_manifest.json").read_text(encoding="utf-8")
    )


def _jsonl(root: Path, name: str) -> list[dict[str, Any]]:
    path = _only_run_dir(root) / name
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


async def test_real_loop_projects_evidence_and_records_tool_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "receipts"
    client = _ScriptedClient([_tool_call(), _answer("The value is 42 [ev-1].")])
    calls: list[dict[str, Any]] = []
    rail = EvidenceRail(
        EvidenceRailConfig(artifact_root=str(root), evidence_items=[EVIDENCE])
    )

    result = await _run([rail], client, [_lookup_tool(calls)])

    assert result["output"] == "The value is 42 [ev-1]."
    assert "evidencerail" not in result
    assert client.index == 2
    assert calls == [{"key": "value"}]
    assert all(
        "# Evidence context" in prompt and "ev-1" in prompt
        for prompt in client.system_prompts
    )
    manifest = _manifest(root)
    assert manifest["status"] == "completed"
    assert manifest["terminal_reason_code"] is None
    assert manifest["evidence_ids"] == ["ev-1"]
    assert len(manifest["context_digests"]) == 2
    receipts = _jsonl(root, "tool_receipts.jsonl")
    assert [(item["tool_name"], item["status"]) for item in receipts] == [
        (TOOL_NAME, "succeeded")
    ]
    assert manifest["tool_receipt_ids"] == [receipts[0]["receipt_id"]]
    assert len(_jsonl(root, "context_digests.jsonl")) == 2


async def test_real_loop_default_config_blocks_with_evidence_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "receipts"
    client = _ScriptedClient([_answer("must never be produced")])
    rail = EvidenceRail(EvidenceRailConfig(artifact_root=str(root)))

    result = await _run([rail], client)

    assert client.index == 0
    assert result["evidencerail"] == {
        "status": "blocked",
        "reason_code": "EVIDENCE_MISSING",
    }
    manifest = _manifest(root)
    assert manifest["status"] == "blocked"
    assert manifest["terminal_reason_code"] == "EVIDENCE_MISSING"


async def test_real_loop_accepts_evidence_from_ctx_extra(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    client = _ScriptedClient([_answer("The value is 42 [ev-1].")])
    rail = EvidenceRail(EvidenceRailConfig(artifact_root=str(root)))

    result = await _run([_EvidenceInjector(), rail], client)

    assert result["output"] == "The value is 42 [ev-1]."
    assert client.index == 1
    assert "ev-1" in client.system_prompts[0]
    manifest = _manifest(root)
    assert manifest["status"] == "completed"
    assert manifest["evidence_ids"] == ["ev-1"]


async def test_real_loop_retries_one_retryable_tool_failure(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    client = _ScriptedClient([_tool_call(), _answer("The value is 42 [ev-1].")])
    calls: list[dict[str, Any]] = []
    rail = EvidenceRail(
        EvidenceRailConfig(artifact_root=str(root), evidence_items=[EVIDENCE])
    )

    result = await _run(
        [rail], client, [_lookup_tool(calls, fail_first_with=TimeoutError)]
    )

    assert result["output"] == "The value is 42 [ev-1]."
    assert len(calls) == 2
    receipts = _jsonl(root, "tool_receipts.jsonl")
    assert [
        (item["status"], item["reason_code"], item["retry_index"]) for item in receipts
    ] == [
        ("retry_requested", "TOOL_TIMEOUT", 0),
        ("succeeded", None, 1),
    ]
    manifest = _manifest(root)
    assert manifest["status"] == "completed"
    assert manifest["recovery_count"] == 1


async def test_real_loop_blocks_non_retryable_tool_failure(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    client = _ScriptedClient([_tool_call(), _answer("must never be produced")])
    calls: list[dict[str, Any]] = []
    rail = EvidenceRail(
        EvidenceRailConfig(artifact_root=str(root), evidence_items=[EVIDENCE])
    )

    result = await _run(
        [rail], client, [_lookup_tool(calls, fail_first_with=PermissionError)]
    )

    assert result["evidencerail"]["reason_code"] == "TOOL_PERMISSION_DENIED"
    assert client.index == 1
    assert len(calls) == 1
    receipts = _jsonl(root, "tool_receipts.jsonl")
    assert [(item["status"], item["reason_code"]) for item in receipts] == [
        ("failed", "TOOL_PERMISSION_DENIED")
    ]
    manifest = _manifest(root)
    assert manifest["status"] == "blocked"
    assert manifest["terminal_reason_code"] == "TOOL_PERMISSION_DENIED"
