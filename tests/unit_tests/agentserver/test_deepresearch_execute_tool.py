"""Regression tests for deterministic DeepResearch orchestration."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import pickle
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails import (
    deepresearch_execution_rail as dr,
)
from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
    DEEPRESEARCH_EXECUTION_ALIAS_KEY,
    DEEPRESEARCH_EXECUTION_STATE_KEY,
    DEEPRESEARCH_INTERACTION_PLACEHOLDER,
    DeepResearchExecutionRail,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch import execution as de
from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt


class _Session:
    def __init__(self):
        self.state: dict[str, object] = {}

    def get_state(self, key):
        return self.state.get(key)

    def update_state(self, values):
        self.state.update(values)


class _Model:
    def __init__(self, content: str):
        self.invoke = AsyncMock(return_value=SimpleNamespace(content=content))


@pytest.fixture(autouse=True)
def _disable_query_scan_by_default(monkeypatch):
    """修复9'：默认把扫库认领打桩为未命中。

    真实扫库会经 _resolve_skill_root 读开发机共享技能目录下的
    checkpointer.db，可能命中与本文件默认 query 相同的真实会话，
    使既有认领失败用例变得机器相关。扫库专项用例在用例内显式
    覆盖本桩（patch.object 后应用即覆盖）。stream action='start'
    的认领兜底入口（_claim_start_snapshot）同理打桩——未打桩的
    用例若放行到真实 stream 实现会真实读库。
    """
    monkeypatch.setattr(
        de, "_scan_resumable_checkpoint_by_query", Mock(return_value=None)
    )
    monkeypatch.setattr(
        dt, "_claim_start_snapshot", AsyncMock(return_value=None)
    )


def test_execution_rail_converts_before_stream_event_rail():
    assert DeepResearchExecutionRail.priority > JiuSwarmStreamEventRail.priority


def _option_payload(*questions: str) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "question_index": index,
                    "options": [
                        {"label": f"{question}：方向A"},
                        {"label": f"{question}：方向B"},
                    ],
                }
                for index, question in enumerate(questions)
            ]
        },
        ensure_ascii=False,
    )


async def _invoke(
    *,
    state=None,
    user_input=None,
    model=None,
    query="研究智能家电竞争格局",
    file_name="智能家电报告",
    requested_report_type=None,
    report_type=None,
    upgrade_from_brief=False,
    deepresearch_followup=None,
    conversation_id="",
    materials_context=None,
    mcp_servers=None,
):
    saved: list[dict] = []
    token = de.bind_deepresearch_execution_context(
        tool_call_id="call-1",
        state=state,
        user_input=user_input,
        model=model,
        save_state=lambda value: saved.append(dict(value)),
        requested_report_type=requested_report_type,
        deepresearch_followup=deepresearch_followup,
        materials_context=materials_context,
        mcp_servers=mcp_servers,
    )
    try:
        result = await de.deepresearch_execute._func(
            query=query,
            file_name=file_name,
            report_type=report_type,
            upgrade_from_brief=upgrade_from_brief,
            conversation_id=conversation_id,
        )
    finally:
        de.reset_deepresearch_execution_context(token)
    return result, saved


@pytest.mark.asyncio
async def test_missing_query_fails_before_sdk_start():
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        result, saved = await _invoke(query="   ")

    assert result["kind"] == "error"
    assert result["error_code"] == "query_missing"
    assert saved[-1]["phase"] == "error"
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_error_preserves_bounded_subprocess_diagnostics():
    outcome = {
        "status": "error",
        "error_code": "terminal_marker_missing",
        "error": "no terminal marker",
        "returncode": 1,
        "stderr_tail": "ModuleNotFoundError: No module named 'aiosqlite'",
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
    ):
        result, saved = await _invoke(query="请生成一份详细的智能家电竞争报告")

    assert result["kind"] == "error"
    assert result["error_code"] == "terminal_marker_missing"
    assert result["returncode"] == 1
    assert result["stderr_tail"] == "ModuleNotFoundError: No module named 'aiosqlite'"
    assert saved[-1]["phase"] == "error"


@pytest.mark.asyncio
async def test_new_query_starts_sdk_directly():
    sdk_usage = {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "llm_call_count": 2,
        "agent_name_token_usage": [],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "workflow_llm_token_usage": sdk_usage,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(query="研究智能家电竞争格局")

    assert result["kind"] == "completed"
    assert result["workflow_llm_token_usage"] == sdk_usage
    assert saved[0]["phase"] == "starting"
    stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_query_persists_request_report_type_before_sdk_start():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        _, saved = await _invoke(requested_report_type="brief")

    assert saved[0]["phase"] == "starting"
    assert saved[0]["requested_report_type"] == "brief"
    assert stream.await_args.kwargs["report_type"] == "brief"


@pytest.mark.asyncio
async def test_execute_rejects_cached_outline_claim_without_checkpoint():
    """修复5：probe 未命中（无可恢复快照）时不得用残留缓存大纲假认领。

    事故场景：图正常完结清快照后，内存大纲缓存残留，旧逻辑仍弹大纲卡，
    用户确认后 resume 无快照可恢复而全新起跑并崩溃。新契约：probe=None
    一律走认领失败 → 全新 id 从头执行。
    """
    outline = {
        "title": "智能家电竞争格局研究",
        "sections": [
            {"id": "1", "title": "市场规模", "is_core_section": True, "description": "整体规模"},
            {"id": "2", "title": "主要玩家"},
        ],
    }
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S1")
    try:
        dt._cache_outline_json(dt._get_route(), "C-OLD", outline)
        with (
            patch.object(
                de, "_probe_resumable_checkpoint", new=Mock(return_value=None)
            ),
            patch.object(
                de,
                "salvage_deepresearch_report",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                de,
                "_call_deepresearch_stream_impl",
                new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
            ) as stream,
        ):
            result, saved = await _invoke(conversation_id="C-OLD")
    finally:
        dt.reset_deepresearch_route(route_token)
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()

    # 不得弹大纲卡：缓存大纲被拒绝，走全新起跑
    assert result["kind"] == "error"
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-OLD"
    assert saved[-1]["phase"] == "error"


@pytest.mark.asyncio
async def test_execute_claims_outline_with_db_confirmed_cache_fallback():
    """修复5边界：db 确认 outline_interaction 可恢复中断但快照缺大纲 JSON
    → 允许缓存兜底补大纲（claim source=cache）。"""
    outline = {
        "title": "智能家电竞争格局研究",
        "sections": [
            {"id": "1", "title": "市场规模", "is_core_section": True, "description": "整体规模"},
        ],
    }
    probe = {"node": "outline_interaction"}
    route_token = dt.push_deepresearch_route("R1", "CH1", "S1")
    try:
        dt._cache_outline_json(dt._get_route(), "C-OLC", outline)
        with (
            patch.object(de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)),
            patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
        ):
            result, saved = await _invoke(conversation_id="C-OLC")
    finally:
        dt.reset_deepresearch_route(route_token)
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()

    assert result["kind"] == "interaction"
    stream.assert_not_awaited()
    final = saved[-1]
    assert final["phase"] == "wait_outline"
    assert final["conversation_id"] == "C-OLC"
    question = result["interaction"]["questions"][0]
    assert "### P1: 市场规模（重点）" in question["preview"]["text"]


@pytest.mark.asyncio
async def test_execute_claim_failure_salvages_existing_report():
    """修复7：认领失败（无可恢复快照）但报告已落盘且主题一致 → 止损交付，
    不重跑研究。"""
    salvage_outcome = {
        "status": "completed",
        "conversation_id": "C-SAL",
        "report_delivered": True,
        "salvaged_report": True,
    }
    with (
        patch.object(
            de, "_probe_resumable_checkpoint", new=Mock(return_value=None)
        ),
        patch.object(
            de,
            "salvage_deepresearch_report",
            new=AsyncMock(return_value=salvage_outcome),
        ) as salvage,
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(conversation_id="C-SAL")

    salvage.assert_awaited_once()
    stream.assert_not_awaited()
    assert result["kind"] == "completed"
    assert "已成功生成并交付" in result["content"]
    assert saved[-1]["phase"] == "completed"
    assert saved[-1]["conversation_id"] == "C-SAL"


@pytest.mark.asyncio
async def test_execute_error_phase_salvages_existing_report():
    """修复7：error 相位死路（无可恢复快照）且主题一致 → 先止损交付报告。"""
    salvage_outcome = {
        "status": "completed",
        "conversation_id": "C-ERRS",
        "report_delivered": True,
        "salvaged_report": True,
    }
    state = {
        "schema_version": 1,
        "phase": "error",
        "revision": 1,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "C-ERRS",
    }
    with (
        patch.object(
            de, "_probe_resumable_checkpoint", new=Mock(return_value=None)
        ),
        patch.object(
            de,
            "salvage_deepresearch_report",
            new=AsyncMock(return_value=salvage_outcome),
        ) as salvage,
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(state=state, conversation_id="C-ERRS")

    salvage.assert_awaited_once()
    stream.assert_not_awaited()
    assert result["kind"] == "completed"
    assert saved[-1]["phase"] == "completed"


@pytest.mark.asyncio
async def test_execute_claim_without_cached_outline_restarts_with_fresh_id():
    """认领失败（缓存无大纲）→ 换全新 conversation_id 从头执行，绝不复用旧 id 发起新跑。"""
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S2")
    try:
        with patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
        ) as stream:
            result, saved = await _invoke(conversation_id="C-GONE")
    finally:
        dt.reset_deepresearch_route(route_token)

    assert result["kind"] == "error"
    assert saved[-1]["phase"] == "error"
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-GONE"


def _encode_checkpoint_blob(value):
    return "__BYTES__:" + base64.b64encode(pickle.dumps(value)).decode("ascii")


_HOST_SESSION = "S-HOST"


@contextmanager
def _host_route(session_id: str = _HOST_SESSION):
    """绑定与快照归属字段配套的宿主 route(probe/scan 认领校验依赖)。"""
    token = dt.push_deepresearch_route("R-OWN", "CH-OWN", session_id)
    try:
        yield
    finally:
        dt.reset_deepresearch_route(token)


def _write_checkpoint_db(
    root,
    conversation_id,
    *,
    node,
    node_status="__interrupt__",
    questions="",
    outline=None,
    exceptions=None,
    extra_pending=None,
    original_query="",
    host_session_id="",
):
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    connection = sqlite3.connect(str(data_dir / "checkpointer.db"))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS kv_store"
            " (key VARCHAR(255), value VARCHAR(4096))"
        )
        pending_node = {
            node: SimpleNamespace(
                status=node_status,
                exception=list(exceptions or []),
            )
        }
        for entry in extra_pending or ():
            pending_node[str(entry["node"])] = SimpleNamespace(
                status=str(entry.get("status") or "__interrupt__"),
                exception=list(entry.get("exceptions") or []),
            )
        graph_state = SimpleNamespace(pending_node=pending_node)
        search_context = {}
        if original_query:
            search_context["original_query"] = original_query
        if questions:
            search_context["questions"] = questions
        if outline is not None:
            search_context["current_outline"] = outline
        global_state: dict = {"search_context": search_context}
        if host_session_id:
            # SDK StartNode 白名单写入的会话 config(归属字段随快照落盘)
            global_state["config"] = {"host_session_id": host_session_id}
        workflow_state = {"global_state": global_state}
        connection.executemany(
            "INSERT INTO kv_store (key, value) VALUES (?, ?)",
            [
                (
                    f"{conversation_id}:workflow-graph:research_workflow:checkpoint_data_value",
                    _encode_checkpoint_blob(graph_state),
                ),
                (
                    f"{conversation_id}:workflow:research_workflow:workflow_state_blobs",
                    _encode_checkpoint_blob(workflow_state),
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_probe_resumable_checkpoint_reads_feedback_stage(tmp_path):
    """停研究问题阶段的会话 → 探测出节点与按段落切分的问题清单。"""
    _write_checkpoint_db(
        tmp_path,
        "C-FB",
        node="feedback_handler",
        questions="1. 重点研究哪些品类？\n第一问的补充限定\n\n2. 覆盖哪些市场？",
        host_session_id=_HOST_SESSION,
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
    ):
        probe = dt._probe_resumable_checkpoint("C-FB")

    assert probe == {
        "node": "feedback_handler",
        "questions": ["1. 重点研究哪些品类？\n第一问的补充限定", "2. 覆盖哪些市场？"],
    }


def test_probe_resumable_checkpoint_reads_outline_stage(tmp_path):
    """停大纲确认阶段的会话 → 探测出节点与工具层大纲 JSON。"""
    outline = SimpleNamespace(
        title="智能家电竞争格局研究",
        thought="先规模后玩家",
        sections=[
            SimpleNamespace(title="市场规模", is_core_section=True),
            SimpleNamespace(title="主要玩家", is_core_section=False),
        ],
    )
    _write_checkpoint_db(
        tmp_path,
        "C-OL",
        node="outline_interaction",
        questions="1. 问题",
        outline=outline,
        host_session_id=_HOST_SESSION,
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
    ):
        probe = dt._probe_resumable_checkpoint("C-OL")

    assert probe == {
        "node": "outline_interaction",
        "questions": ["1. 问题"],
        "outline_json": {
            "title": "智能家电竞争格局研究",
            "thought": "先规模后玩家",
            "sections": [
                {"title": "市场规模", "is_core_section": True},
                {"title": "主要玩家", "is_core_section": False},
            ],
        },
    }


def test_probe_resumable_checkpoint_returns_none_without_resumable_interrupt(tmp_path):
    """非中断状态 / 会话不存在 / db 不存在 → 均探测不到，认领退回新跑。"""
    _write_checkpoint_db(
        tmp_path,
        "C-ERR",
        node="feedback_handler",
        node_status="__error__",
        questions="1. 问题",
    )

    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        assert dt._probe_resumable_checkpoint("C-ERR") is None
        assert dt._probe_resumable_checkpoint("C-UNKNOWN") is None
    (tmp_path / "data" / "checkpointer.db").unlink()
    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        assert dt._probe_resumable_checkpoint("C-ERR") is None
    with patch.object(dt, "_resolve_skill_root", return_value=""):
        assert dt._probe_resumable_checkpoint("C-ERR") is None


def test_probe_resumable_checkpoint_prefers_stop_snapshot_over_stale_interrupt(tmp_path):
    """停止快照优先于残留 interrupt：认领停止会话不再被拉回早期交互重弹卡。"""
    outline = SimpleNamespace(
        title="智能家电竞争格局研究",
        thought="先规模后玩家",
        sections=[
            SimpleNamespace(title="市场规模", is_core_section=True),
        ],
    )
    # 停止前的早期交互残留（stale interrupt）排在 pending_node 前部；
    # 若 interrupt 优先，会把已停止会话错认成大纲中断认领。
    _write_checkpoint_db(
        tmp_path,
        "C-STP",
        node="outline_interaction",
        node_status="__interrupt__",
        questions="1. 重点研究哪些品类？",
        outline=outline,
        host_session_id=_HOST_SESSION,
        extra_pending=[
            {
                "node": "report_writing",
                "status": "__error__",
                "exceptions": [RuntimeError("deepresearch task stopped by user")],
            }
        ],
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
    ):
        probe = dt._probe_resumable_checkpoint("C-STP")

    assert probe == {"node": "report_writing", "stopped": True}


@pytest.mark.asyncio
async def test_execute_claims_feedback_stage_from_checkpoint_probe():
    """checkpointer.db 探测到停研究问题阶段 → 出反馈卡，state 进入 wait_feedback。"""
    probe = {
        "node": "feedback_handler",
        "questions": ["1. 重点研究哪些品类？", "2. 覆盖哪些市场？"],
    }
    model = _Model(_option_payload("重点研究哪些品类？", "覆盖哪些市场？"))
    with (
        patch.object(
            de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)
        ) as probe_mock,
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(model=model, conversation_id="C-FB")

    probe_mock.assert_called_once_with("C-FB")
    stream.assert_not_awaited()
    assert result["kind"] == "interaction"
    assert result["interaction"]["query"] == "请回答以下研究主题澄清问题"
    cards = result["interaction"]["questions"]
    assert [card["question"] for card in cards] == [
        "重点研究哪些品类？",
        "覆盖哪些市场？",
    ]
    assert all(len(card["options"]) == 2 for card in cards)
    final = saved[-1]
    assert final["phase"] == "wait_feedback"
    assert final["conversation_id"] == "C-FB"
    assert final["questions"] == ["重点研究哪些品类？", "覆盖哪些市场？"]


@pytest.mark.asyncio
async def test_execute_claims_outline_stage_from_checkpoint_probe():
    """checkpointer.db 探测到停大纲确认 → 无需内存缓存直接出审阅卡（跨重启认领）。"""
    probe = {
        "node": "outline_interaction",
        "outline_json": {
            "title": "智能家电竞争格局研究",
            "thought": "先规模后玩家",
            "sections": [
                {"title": "市场规模", "is_core_section": True},
                {"title": "主要玩家", "is_core_section": False},
            ],
        },
    }
    with (
        patch.object(de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)),
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(conversation_id="C-OL")

    stream.assert_not_awaited()
    assert result["kind"] == "interaction"
    final = saved[-1]
    assert final["phase"] == "wait_outline"
    assert final["conversation_id"] == "C-OL"
    assert final["outline_presented"] is True
    question = result["interaction"]["questions"][0]
    assert "**研究思路**：先规模后玩家" in question["preview"]["text"]
    assert "### P1: 市场规模（重点）" in question["preview"]["text"]
    assert "### P2: 主要玩家" in question["preview"]["text"]


@pytest.mark.asyncio
async def test_execute_restarts_fresh_when_checkpoint_lacks_questions():
    """db 指向研究问题阶段但取不到问题清单 → 认领失败，换全新 id 重跑。"""
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S3")
    try:
        with (
            patch.object(
                de,
                "_probe_resumable_checkpoint",
                new=Mock(return_value={"node": "feedback_handler"}),
            ),
            patch.object(
                de,
                "_call_deepresearch_stream_impl",
                new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
            ) as stream,
        ):
            result, saved = await _invoke(conversation_id="C-FB2")
    finally:
        dt.reset_deepresearch_route(route_token)

    assert result["kind"] == "error"
    assert saved[-1]["phase"] == "error"
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-FB2"


@pytest.mark.asyncio
async def test_execute_claims_stopped_snapshot_and_resumes_stopped_node():
    """probe 报告停止快照且主题一致 → 不弹交互卡直通 resume node='stopped'。"""
    probe = {"node": "report_writing", "stopped": True}
    completed = {
        "status": "completed",
        "conversation_id": "C-STP",
        "report_delivered": True,
    }
    state = {
        "schema_version": 1,
        "phase": "new",
        "revision": 0,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "C-STP",
        "requested_report_type": "brief",
    }
    with (
        patch.object(
            de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)
        ) as probe_mock,
        patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
        ) as stream,
    ):
        result, saved = await _invoke(
            state=state, query="研究智能家电竞争格局"
        )

    probe_mock.assert_called_once_with("C-STP")
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "stopped"
    assert stream.await_args.kwargs["conversation_id"] == "C-STP"
    assert result["kind"] == "completed"
    assert saved[0]["phase"] == "resuming_stopped"
    assert saved[-1]["phase"] == "completed"


@pytest.mark.asyncio
async def test_execute_stopped_snapshot_with_new_topic_restarts_fresh():
    """停止快照 + 用户换主题 → 不认领旧会话、不复活旧大纲缓存，全新 id 研究新主题。"""
    probe = {"node": "report_writing", "stopped": True}
    outline = {
        "title": "智能家电竞争格局研究",
        "sections": [
            {"id": "1", "title": "市场规模", "is_core_section": True},
        ],
    }
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    state = {
        "schema_version": 1,
        "phase": "new",
        "revision": 0,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "C-STP",
        "requested_report_type": "brief",
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S4")
    try:
        dt._cache_outline_json(dt._get_route(), "C-STP", outline)
        with (
            patch.object(
                de,
                "_probe_resumable_checkpoint",
                new=Mock(return_value=probe),
            ),
            patch.object(
                de,
                "_call_deepresearch_stream_impl",
                new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
            ) as stream,
        ):
            result, saved = await _invoke(
                state=state, query="研究新能源车市场竞争格局"
            )
    finally:
        dt.reset_deepresearch_route(route_token)
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()

    # 不弹旧主题的任何交互卡；换全新 id 且以新主题 start。
    assert result["kind"] == "error"
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    assert stream.await_args.kwargs["query"] == "研究新能源车市场竞争格局"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-STP"


def test_is_stop_requested_exception_recognizes_cancelled_error():
    """SDK 取消路径写入的 __error__+CancelledError 快照按可续跑停止快照识别。"""
    assert dt._is_stop_requested_exception(asyncio.CancelledError()) is True
    assert dt._is_stop_requested_exception(RuntimeError("普通失败")) is False
    assert dt._is_stop_requested_exception("not an exception") is False


def test_probe_resumable_checkpoint_recognizes_cancelled_snapshot(tmp_path):
    """取消落盘的 __error__+CancelledError 快照 → stopped=True，认领后可直通续跑。"""
    _write_checkpoint_db(
        tmp_path,
        "C-CXL",
        node="report_writing",
        node_status="__error__",
        exceptions=[asyncio.CancelledError()],
        host_session_id=_HOST_SESSION,
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
    ):
        probe = dt._probe_resumable_checkpoint("C-CXL")

    assert probe == {"node": "report_writing", "stopped": True}


def test_scan_resumable_checkpoint_by_query_claims_stopped_snapshot(tmp_path):
    """修复9'：LLM 幻觉 cid 时按 query 扫库 → 认领到同主题的停止快照。"""
    _write_checkpoint_db(
        tmp_path,
        "C-REAL",
        node="report_writing",
        node_status="__error__",
        exceptions=[RuntimeError("deepresearch task stopped by user")],
        original_query="研究智能家电竞争格局",
        host_session_id=_HOST_SESSION,
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
    ):
        scan = dt._scan_resumable_checkpoint_by_query("研究智能家电竞争格局")

    assert scan == {
        "conversation_id": "C-REAL",
        "node": "report_writing",
        "stopped": True,
    }


def test_scan_resumable_checkpoint_by_query_claims_interrupt_with_cards(tmp_path):
    """修复9'：扫库命中 interrupt → 附带回弹卡字段（questions/outline_json）。"""
    outline = SimpleNamespace(
        title="智能家电竞争格局研究",
        thought="先规模后玩家",
        sections=[SimpleNamespace(title="市场规模", is_core_section=True)],
    )
    _write_checkpoint_db(
        tmp_path,
        "C-INT",
        node="outline_interaction",
        questions="1. 重点研究哪些品类？",
        outline=outline,
        original_query="研究智能家电竞争格局",
        host_session_id=_HOST_SESSION,
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
    ):
        scan = dt._scan_resumable_checkpoint_by_query("研究智能家电竞争格局")

    assert scan is not None
    assert scan["conversation_id"] == "C-INT"
    assert scan["node"] == "outline_interaction"
    assert scan.get("stopped") is not True
    assert scan["questions"] == ["1. 重点研究哪些品类？"]
    assert scan["outline_json"]["sections"] == [
        {"title": "市场规模", "is_core_section": True}
    ]


def test_scan_resumable_checkpoint_by_query_rejects_topic_change(tmp_path):
    """修复9'：换主题不认领旧会话；original_query 缺失同样不认领。"""
    _write_checkpoint_db(
        tmp_path,
        "C-REAL",
        node="report_writing",
        node_status="__error__",
        exceptions=[asyncio.CancelledError()],
        original_query="研究智能家电竞争格局",
    )
    # 第二个会话快照缺失 original_query（更早版本落盘）
    _write_checkpoint_db(
        tmp_path,
        "C-NOQ",
        node="feedback_handler",
        questions="1. 问题",
    )

    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        assert (
            dt._scan_resumable_checkpoint_by_query("研究新能源车市场竞争格局")
            is None
        )
        assert dt._scan_resumable_checkpoint_by_query("") is None


def test_scan_resumable_checkpoint_by_query_prefers_latest_match(tmp_path):
    """修复9'：rowid 倒序，同主题多快照时认领最新落盘的会话。"""
    for cid in ("C-OLD", "C-NEW"):
        _write_checkpoint_db(
            tmp_path,
            cid,
            node="report_writing",
            node_status="__error__",
            exceptions=[asyncio.CancelledError()],
            original_query="研究智能家电竞争格局",
            host_session_id=_HOST_SESSION,
        )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
    ):
        scan = dt._scan_resumable_checkpoint_by_query("研究智能家电竞争格局")

    assert scan == {
        "conversation_id": "C-NEW",
        "node": "report_writing",
        "stopped": True,
    }


def test_scan_resumable_checkpoint_by_query_ignores_unresumable(tmp_path):
    """修复9'：无可续跑状态的快照（正常完结残留）不参与认领。"""
    _write_checkpoint_db(
        tmp_path,
        "C-DONE",
        node="report_writing",
        node_status="__success__",
        original_query="研究智能家电竞争格局",
    )

    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        assert (
            dt._scan_resumable_checkpoint_by_query("研究智能家电竞争格局") is None
        )


def test_probe_rejects_cross_session_snapshot(tmp_path, caplog):
    """归属校验：快照归属别家宿主会话 → 拒绝认领并记观察哨日志。"""
    _write_checkpoint_db(
        tmp_path,
        "C-FOREIGN",
        node="feedback_handler",
        questions="1. 问题",
        host_session_id="S-OTHER",
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
        caplog.at_level(logging.WARNING),
    ):
        assert dt._probe_resumable_checkpoint("C-FOREIGN") is None

    assert any(
        "ownership rejected" in record.message
        and "reason=ownership_mismatch" in record.message
        and "C-FOREIGN" in record.message
        for record in caplog.records
    )


def test_probe_rejects_legacy_snapshot_without_owner(tmp_path, caplog):
    """归属校验：升级前落盘的旧快照无归属字段 → 一律拒绝（拍板）。"""
    _write_checkpoint_db(
        tmp_path,
        "C-LEGACY",
        node="feedback_handler",
        questions="1. 问题",
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
        caplog.at_level(logging.WARNING),
    ):
        assert dt._probe_resumable_checkpoint("C-LEGACY") is None

    assert any(
        "reason=missing_owner" in record.message for record in caplog.records
    )


def test_probe_rejects_when_requester_unverifiable(tmp_path):
    """归属校验：请求未绑定宿主会话（无 session_id）→ 严格拒绝认领。"""
    _write_checkpoint_db(
        tmp_path,
        "C-ANY",
        node="feedback_handler",
        questions="1. 问题",
        host_session_id=_HOST_SESSION,
    )

    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        assert dt._probe_resumable_checkpoint("C-ANY") is None


def test_scan_skips_foreign_snapshot_and_claims_own(tmp_path, caplog):
    """归属校验：同 query 命中别家会话快照 → 跳过并继续认领本会话快照。

    复现实机事故形态:新会话跑同主题,扫库先撞上别家(或陈旧)快照,
    必须跳过它继续扫,而不是误认领。
    """
    # 先落本会话快照(rowid 较小),再落别家快照(rowid 较大 → 扫库先命中)
    _write_checkpoint_db(
        tmp_path,
        "C-OWN",
        node="report_writing",
        node_status="__error__",
        exceptions=[asyncio.CancelledError()],
        original_query="研究智能家电竞争格局",
        host_session_id=_HOST_SESSION,
    )
    _write_checkpoint_db(
        tmp_path,
        "C-FOREIGN",
        node="report_writing",
        node_status="__error__",
        exceptions=[asyncio.CancelledError()],
        original_query="研究智能家电竞争格局",
        host_session_id="S-OTHER",
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
        caplog.at_level(logging.WARNING),
    ):
        scan = dt._scan_resumable_checkpoint_by_query("研究智能家电竞争格局")

    assert scan is not None
    assert scan["conversation_id"] == "C-OWN"
    assert any(
        "reason=ownership_mismatch" in record.message
        and "C-FOREIGN" in record.message
        for record in caplog.records
    )


def test_scan_returns_none_when_no_candidate_owned(tmp_path, caplog):
    """归属校验：候选全为别家/旧快照 → 不认领任何对象,退回全新起跑。"""
    _write_checkpoint_db(
        tmp_path,
        "C-FOREIGN",
        node="report_writing",
        node_status="__error__",
        exceptions=[asyncio.CancelledError()],
        original_query="研究智能家电竞争格局",
        host_session_id="S-OTHER",
    )
    _write_checkpoint_db(
        tmp_path,
        "C-LEGACY",
        node="report_writing",
        node_status="__error__",
        exceptions=[asyncio.CancelledError()],
        original_query="研究智能家电竞争格局",
    )

    with (
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        _host_route(),
        caplog.at_level(logging.WARNING),
    ):
        assert dt._scan_resumable_checkpoint_by_query("研究智能家电竞争格局") is None

    reasons = {
        "reason=ownership_mismatch",
        "reason=missing_owner",
    }
    logged = {record.message for record in caplog.records}
    assert any(reason in message for message in logged for reason in reasons)


def test_build_request_config_injects_host_session_id_from_route():
    """供给侧:config frame 随 route 携带宿主会话归属标识。"""
    with (
        patch.object(dt, "get_task_env_overlay", return_value=None),
        patch.object(dt, "build_effective_env_overlay", return_value={}),
        patch.object(dt, "_build_deepresearch_config", return_value={}),
        _host_route("S-CFG"),
    ):
        config = dt._build_deepresearch_request_config(interactive_ask=True)

    assert config["DEEPSEARCH_HOST_SESSION_ID"] == "S-CFG"
    assert config["DEEPSEARCH_HITL"] == "true"


def test_build_request_config_omits_host_session_id_without_route():
    """供给侧:未绑定 route 时不注入归属键(行为与旧版 config frame 兼容)。"""
    with (
        patch.object(dt, "get_task_env_overlay", return_value=None),
        patch.object(dt, "build_effective_env_overlay", return_value={}),
        patch.object(dt, "_build_deepresearch_config", return_value={}),
    ):
        config = dt._build_deepresearch_request_config(interactive_ask=False)

    assert "DEEPSEARCH_HOST_SESSION_ID" not in config
    assert config["DEEPSEARCH_HITL"] == "false"


@pytest.mark.asyncio
async def test_execute_hallucinated_cid_claims_stopped_conversation_by_scan(tmp_path):
    """修复9'：LLM 幻觉 cid → probe 落空但扫库按主题命中停止快照，认领直通续跑。"""
    _write_checkpoint_db(
        tmp_path,
        "C-REAL",
        node="report_writing",
        node_status="__error__",
        exceptions=[RuntimeError("deepresearch task stopped by user")],
        original_query="研究智能家电竞争格局",
        host_session_id=_HOST_SESSION,
    )
    completed = {
        "status": "completed",
        "conversation_id": "C-REAL",
        "report_delivered": True,
    }
    state = {
        "schema_version": 1,
        "phase": "new",
        "revision": 0,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "C-HALLUCINATED",
        "requested_report_type": "brief",
    }
    with (
        _host_route(),
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        patch.object(
            de,
            "_scan_resumable_checkpoint_by_query",
            new=dt._scan_resumable_checkpoint_by_query,
        ),
        patch.object(de, "_call_deepresearch_stream_impl",
                     new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False))) as stream,
    ):
        result, saved = await _invoke(
            state=state, query="研究智能家电竞争格局"
        )

    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "stopped"
    assert stream.await_args.kwargs["conversation_id"] == "C-REAL"
    assert result["kind"] == "completed"
    assert saved[0]["phase"] == "resuming_stopped"
    assert saved[0]["conversation_id"] == "C-REAL"


@pytest.mark.asyncio
async def test_execute_hallucinated_cid_scan_miss_starts_fresh(tmp_path):
    """修复9'：扫库也无同主题快照 → 换全新 id 从头执行（不复用幻觉 cid）。"""
    _write_checkpoint_db(
        tmp_path,
        "C-OTHER",
        node="report_writing",
        node_status="__error__",
        exceptions=[asyncio.CancelledError()],
        original_query="研究智能家电竞争格局",
    )
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S9")
    try:
        with (
            patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
            patch.object(de, "_call_deepresearch_stream_impl",
                         new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False))) as stream,
        ):
            result, saved = await _invoke(
                query="研究新能源车市场竞争格局",
                conversation_id="C-HALLUCINATED",
            )
    finally:
        dt.reset_deepresearch_route(route_token)

    assert result["kind"] == "error"
    assert saved[-1]["phase"] == "error"
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-HALLUCINATED"


@pytest.mark.asyncio
async def test_execute_hallucinated_cid_scan_claims_outline_interaction(tmp_path):
    """修复9'：扫库命中 outline_interaction → 主题一致即回弹大纲审阅卡。"""
    outline = SimpleNamespace(
        title="智能家电竞争格局研究",
        thought="先规模后玩家",
        sections=[SimpleNamespace(title="市场规模", is_core_section=True)],
    )
    _write_checkpoint_db(
        tmp_path,
        "C-REAL",
        node="outline_interaction",
        questions="1. 重点研究哪些品类？",
        outline=outline,
        original_query="研究智能家电竞争格局",
        host_session_id=_HOST_SESSION,
    )
    state = {
        "schema_version": 1,
        "phase": "new",
        "revision": 0,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "C-HALLUCINATED",
        "requested_report_type": "brief",
    }
    with (
        _host_route(),
        patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)),
        patch.object(
            de,
            "_scan_resumable_checkpoint_by_query",
            new=dt._scan_resumable_checkpoint_by_query,
        ),
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(
            state=state, query="研究智能家电竞争格局"
        )

    stream.assert_not_awaited()
    assert result["kind"] == "interaction"
    final = saved[-1]
    assert final["phase"] == "wait_outline"
    assert final["conversation_id"] == "C-REAL"
    assert final["outline_presented"] is True


@pytest.mark.asyncio
async def test_execute_hallucinated_cid_scan_salvages_report(tmp_path):
    """修复9'：扫库未命中可续跑快照，但同主题报告已落盘 → 以扫出的 cid 止损交付。

    扫库要求 original_query 严格一致才认领；报告止损场景中快照已被清理，
    无从扫出 cid，salvage 仍以入参 cid 定位（修复7 语义不变）。
    """
    salvage_outcome = {
        "status": "completed",
        "conversation_id": "C-SAL",
        "report_delivered": True,
        "salvaged_report": True,
    }
    with (
        patch.object(dt, "_resolve_skill_root", return_value=""),
        patch.object(
            de,
            "salvage_deepresearch_report",
            new=AsyncMock(return_value=salvage_outcome),
        ) as salvage,
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(conversation_id="C-SAL")

    salvage.assert_awaited_once()
    assert salvage.await_args.kwargs["conversation_id"] == "C-SAL"
    stream.assert_not_awaited()
    assert result["kind"] == "completed"
    assert saved[-1]["phase"] == "completed"
    assert saved[-1]["conversation_id"] == "C-SAL"


@pytest.mark.asyncio
async def test_execute_claim_failure_logs_warning_and_starts_fresh(caplog):
    """历史会话 checkpoint 探测失败 → 记 warning 且换全新 id 从头执行。"""
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    state = {
        "schema_version": 1,
        "phase": "new",
        "revision": 0,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "C-GONE",
        "requested_report_type": "brief",
    }
    with (
        patch.object(
            de, "_probe_resumable_checkpoint", new=Mock(return_value=None)
        ) as probe_mock,
        patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
        ) as stream,
    ):
        with caplog.at_level("WARNING", logger=de.logger.name):
            result, _saved = await _invoke(
                state=state, query="研究智能家电竞争格局"
            )

    probe_mock.assert_called_once_with("C-GONE")
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-GONE"
    assert result["kind"] == "error"
    warnings = [
        record
        for record in caplog.records
        if record.levelno == 30 and "claim failed" in record.getMessage()
    ]
    assert warnings, "认领失败应记录可观测的 warning 日志"
    assert "C-GONE" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_invalid_option_generation_retries_then_keeps_free_text_questions():
    questions = ["重点研究哪些品类？", "覆盖哪些市场？"]
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "\n".join(questions)},
    }
    model = _Model("not-json")

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
    ):
        result, _ = await _invoke(model=model)

    assert [item["question"] for item in result["interaction"]["questions"]] == questions
    assert [item["options"] for item in result["interaction"]["questions"]] == [[], []]
    assert model.invoke.await_count == 2


@pytest.mark.asyncio
async def test_option_generation_timeout_keeps_free_text_questions():
    questions = ["重点研究哪些品类？", "覆盖哪些市场？"]
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "\n".join(questions)},
    }

    async def never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    model = SimpleNamespace(invoke=never_returns)
    with (
        patch.object(
            de,
            "_OPTIONS_GENERATION_TIMEOUT_SECONDS",
            0.01,
        ),
        patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
        ),
    ):
        result, saved = await asyncio.wait_for(_invoke(model=model), timeout=0.2)

    assert result["kind"] == "interaction"
    assert [item["question"] for item in result["interaction"]["questions"]] == questions
    assert [item["options"] for item in result["interaction"]["questions"]] == [[], []]
    assert saved[-1]["phase"] == "wait_feedback"


@pytest.mark.asyncio
async def test_option_generation_preserves_model_defaults_and_completion_budget():
    questions = ["重点研究哪些品类？", "覆盖哪些市场？", "报告用于什么决策？"]
    model = _Model(_option_payload(*questions))
    model.model_config = SimpleNamespace(model_name="glm-5.2")
    context = de.DeepResearchExecutionContext(
        tool_call_id="call-1",
        state=None,
        user_input=None,
        model=model,
        agent_id="jiuwenswarm",
        save_state=lambda _value: None,
    )

    options = await de._generate_options(context, "研究智能家电竞争格局", questions)

    assert all(len(item) == 2 for item in options)
    messages = model.invoke.await_args.args[0]
    kwargs = model.invoke.await_args.kwargs
    assert kwargs["max_tokens"] == 2048
    assert "extra_body" not in kwargs
    prompt = messages[0]["content"]
    assert "description 不超过 50 字" in prompt
    assert "不得输出分析或推理过程" in prompt


def test_option_description_is_truncated_without_rejecting_valid_options():
    payload = json.dumps(
        {
            "items": [
                {
                    "question_index": 0,
                    "options": [
                        {"label": "方向A", "description": "说明" * 30},
                        {"label": "方向B", "description": "简短说明"},
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    parsed = de._parse_option_payload(payload, 1)

    assert parsed is not None
    assert len(parsed[0][0]["description"]) == 50
    assert parsed[0][1]["description"] == "简短说明"


def test_options_llm_is_recorded_in_request_summary_with_distinct_source():
    collector = SimpleNamespace(
        get_accumulator=Mock(return_value=None),
        record_llm=Mock(),
    )
    context = de.DeepResearchExecutionContext(
        tool_call_id="call-1",
        state=None,
        user_input=None,
        model=SimpleNamespace(model_config=SimpleNamespace(model_name="glm-5.2")),
        agent_id="jiuwenswarm",
        save_state=lambda _value: None,
    )
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(input_tokens=12, output_tokens=7)
    )

    with (
        patch(
            "jiuwenswarm.perf.context.get_request_context",
            return_value={"request_id": "request-1"},
        ),
        patch("jiuwenswarm.perf.context.get_react_iteration", return_value=2),
        patch("jiuwenswarm.perf.context.resolve_task_id", return_value=None),
        patch(
            "jiuwenswarm.perf.collector.get_perf_collector",
            return_value=collector,
        ),
    ):
        de._record_options_llm_perf(
            context,
            response=response,
            duration_ms=1234.5,
            status="ok",
        )

    request_id, event = collector.record_llm.call_args.args
    assert request_id == "request-1"
    assert event.model == "glm-5.2"
    assert event.duration_ms == 1234.5
    assert event.input_tokens == 12
    assert event.output_tokens == 7
    assert event.agent_id == "jiuwenswarm"
    assert event.stream_source_id == "deepresearch_options"


def test_options_llm_uses_bound_request_id_when_task_context_is_missing():
    collector = SimpleNamespace(
        get_accumulator=Mock(return_value=None),
        record_llm=Mock(),
    )
    context = de.DeepResearchExecutionContext(
        tool_call_id="call-1",
        state=None,
        user_input=None,
        model=SimpleNamespace(model_config=SimpleNamespace(model_name="glm-5.2")),
        agent_id="jiuwenswarm",
        save_state=lambda _value: None,
        request_id="request-from-rail",
    )

    with (
        patch("jiuwenswarm.perf.context.get_request_context", return_value=None),
        patch("jiuwenswarm.perf.context.get_react_iteration", return_value=0),
        patch("jiuwenswarm.perf.context.resolve_task_id", return_value=None),
        patch(
            "jiuwenswarm.perf.collector.get_perf_collector",
            return_value=collector,
        ),
    ):
        de._record_options_llm_perf(
            context,
            response=SimpleNamespace(),
            duration_ms=500.0,
            status="ok",
        )

    request_id, event = collector.record_llm.call_args.args
    assert request_id == "request-from-rail"
    assert event.stream_source_id == "deepresearch_options"


@pytest.mark.asyncio
async def test_timing_logs_attribute_sdk_and_options_without_business_content():
    query = "请详细研究绝密智能家电主题"
    questions = ["绝密问题甲？", "绝密问题乙？"]
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "\n".join(questions)},
    }
    model = _Model(_option_payload(*questions))

    with (
        patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
        ),
        patch.object(de.logger, "info") as log_info,
    ):
        await _invoke(query=query, model=model)

    messages = "\n".join(
        call.args[0] % call.args[1:]
        for call in log_info.call_args_list
    )
    assert "sdk window" in messages
    assert "action=start" in messages
    assert "status=interrupted" in messages
    assert "option generation completed" in messages
    assert "attempts=1" in messages
    assert "questions=2" in messages
    assert query not in messages
    assert all(question not in messages for question in questions)
    assert "方向A" not in messages


@pytest.mark.asyncio
async def test_missing_sdk_questions_preserve_original_free_text_fallback():
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"prompt": "请说明希望补充的研究方向"},
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
    ):
        result, _ = await _invoke(query="请详细研究智能家电")

    assert result["interaction"]["query"] == "请补充研究方向反馈"
    assert result["interaction"]["questions"] == [
        {
            "header": "研究方向反馈",
            "question": "请说明希望补充的研究方向",
            "multi_select": False,
            "options": [],
        }
    ]


def test_question_split_keeps_every_valid_sdk_question():
    questions = [f"问题{i}？" for i in range(1, 6)]

    assert de._split_questions(
        "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
    ) == questions


def test_execution_tool_is_an_exclusive_batch_barrier():
    assert de.deepresearch_execute.card.parallel_safe is False


@pytest.mark.asyncio
async def test_feedback_answer_resumes_once_and_returns_direct_completion():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "questions": ["重点研究哪些品类？"],
        "requested_report_type": "brief",
        "revision": 3,
    }
    answer = {
        "status": "answered",
        "answers": [
            {
                "question": "重点研究哪些品类？",
                "selected_options": ["空调与冰箱"],
            }
        ],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(
            state=state,
            user_input=answer,
            requested_report_type="professional",
        )

    assert result["kind"] == "completed"
    assert "✅ **深度研究已完成！**" in result["content"]
    assert "智能家电报告已成功生成并交付" in result["content"]
    assert saved[0]["phase"] == "resuming_feedback"
    assert saved[-1]["phase"] == "completed"
    assert stream.await_count == 1
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "feedback_handler"
    assert stream.await_args.kwargs["report_type"] == "brief"
    assert "空调与冰箱" in stream.await_args.kwargs["feedback"]


@pytest.mark.asyncio
async def test_legacy_feedback_state_resumes_with_native_auto_report_type():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "questions": ["继续吗？"],
        "revision": 3,
    }
    answer = {
        "status": "answered",
        "answers": [{"question": "继续吗？", "selected_options": ["继续"]}],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        await _invoke(
            state=state,
            user_input=answer,
            requested_report_type="brief",
        )

    assert stream.await_args.kwargs["report_type"] is None


@pytest.mark.asyncio
async def test_completion_warns_when_html_style_falls_back():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "html_style_status": "fallback",
        "html_style_phase": "invoke_llm",
        "html_style_reason_code": "llm_call_failed",
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, saved = await _invoke(query="研究智能家电竞争格局")

    assert result["kind"] == "completed"
    assert result["html_style_status"] == "fallback"
    assert result["html_style_phase"] == "invoke_llm"
    assert result["html_style_reason_code"] == "llm_call_failed"
    assert (
        "HTML 已交付内置基础视觉模板，但 AI 生成的增强样式未应用"
        in result["content"]
    )
    assert saved[-1]["html_style_status"] == "fallback"
    assert saved[-1]["html_style_phase"] == "invoke_llm"
    assert saved[-1]["html_style_reason_code"] == "llm_call_failed"


@pytest.mark.asyncio
async def test_terminal_result_preserves_all_sdk_timing_windows():
    previous_window = {
        "action": "start",
        "node": "",
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "timing": {
            "schema_version": 2,
            "runner_total_ms": 120,
            "runner_bootstrap_ms": 20,
            "sdk_execution_ms": 100,
            "sdk_node_spans": [],
        },
        "skill_execution_ms": 130,
    }
    state = {
        "schema_version": 1,
        "phase": "wait_outline",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "outline_presented": True,
        "timing_windows": [previous_window],
        "revision": 5,
    }
    answer = {
        "status": "answered",
        "answers": [{"selected_options": ["确认大纲，继续研究"]}],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "timing": {
            "schema_version": 2,
            "runner_total_ms": 340,
            "runner_bootstrap_ms": 40,
            "sdk_execution_ms": 300,
            "sdk_first_node_ms": 6,
            "sdk_node_spans": [],
        },
        "skill_execution_ms": 350,
        "report_delivery_ms": 10,
    }

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, saved = await _invoke(state=state, user_input=answer)

    assert result["kind"] == "completed"
    assert result["timing"]["sdk_execution_ms"] == 300
    assert result["skill_execution_ms"] == 350
    assert result["report_delivery_ms"] == 10
    assert [window["action"] for window in result["timing_windows"]] == [
        "start",
        "resume",
    ]
    assert result["timing_windows"][1]["node"] == "outline_interaction"
    assert saved[-1]["timing_windows"] == result["timing_windows"]


@pytest.mark.asyncio
async def test_partial_feedback_marks_unanswered_sdk_question_without_finishing():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "questions": ["是否结束研究？", "重点研究哪些品类？"],
        "revision": 3,
    }
    answer = {
        "status": "answered",
        "answers": [
            {
                "question": "重点研究哪些品类？",
                "selected_options": ["空调与冰箱"],
            }
        ],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        await _invoke(state=state, user_input=answer)

    feedback = json.loads(stream.await_args.kwargs["feedback"])["feedback"]
    assert "问题1: 是否结束研究？\n回答: 未回答" in feedback
    assert "问题2: 重点研究哪些品类？\n回答: 空调与冰箱" in feedback
    assert feedback != "finish"


@pytest.mark.asyncio
async def test_outline_is_presented_once_then_confirmed_without_main_agent():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "questions": ["重点研究哪些品类？"],
        "requested_report_type": "professional",
        "revision": 3,
    }
    outline = "## 页面规划\n\n### P1: 市场格局"
    interrupted = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "outline_interaction",
        "marker": {"outline": outline},
    }
    feedback_answer = {
        "status": "answered",
        "answers": [{"selected_options": ["空调与冰箱"]}],
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(interrupted, ensure_ascii=False)),
    ):
        interaction, _ = await _invoke(state=state, user_input=feedback_answer)

    assert interaction["kind"] == "interaction"
    assert interaction["state"]["phase"] == "wait_outline"
    assert interaction["state"]["outline_sections"] == ["市场格局"]
    assert interaction["interaction"]["questions"][0]["preview"]["text"] == outline

    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    outline_answer = {
        "status": "answered",
        "answers": [{"selected_options": ["确认大纲，继续研究"]}],
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(
            state=interaction["state"],
            user_input=outline_answer,
            requested_report_type="brief",
        )

    assert result["kind"] == "completed"
    assert "1. 市场格局" in result["content"]
    assert saved[0]["phase"] == "resuming_outline"
    assert stream.await_args.kwargs["node"] == "outline_interaction"
    assert stream.await_args.kwargs["report_type"] == "professional"
    assert "accepted" in stream.await_args.kwargs["feedback"]


@pytest.mark.asyncio
async def test_inflight_replay_fails_closed_without_duplicate_sdk_call():
    state = {
        "schema_version": 1,
        "phase": "resuming_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "revision": 4,
    }
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        result, _ = await _invoke(state=state, user_input={"status": "answered"})

    assert result["kind"] == "error"
    assert result["error_code"] == "execution_uncertain"
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_feedback_stops_without_resuming_sdk():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "questions": ["继续吗？"],
        "revision": 3,
    }
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        result, saved = await _invoke(
            state=state,
            user_input={"status": "cancelled", "answers": []},
        )

    assert result["kind"] == "cancelled"
    assert saved[-1]["phase"] == "cancelled"
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_during_sdk_window_translates_to_stopped_without_error_overwrite():
    """修复点3.1回归：取消落在 SDK 窗口时只落 stopped 终态。

    旧的双守卫串联结构中，守卫#1 的转译结果（无 status 字段）会被
    守卫#2 内的 _handle_outcome 误判为无效 outcome，在同一 revision
    上追加 error 覆盖写（生产日志 phase=new rev=4 与 phase=error rev=4
    同毫秒双写的根因）。单守卫重构后 saved 序列为 resuming_feedback →
    new，绝无 error 帧。
    """
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "questions": ["继续吗？"],
        "revision": 3,
    }
    answer = {"status": "answered", "answers": [{"selected_options": ["是"]}]}

    async def _raise_cancelled(**kwargs):
        raise asyncio.CancelledError()

    with patch.object(de, "_call_deepresearch_stream_impl", new=_raise_cancelled):
        result, saved = await _invoke(state=state, user_input=answer)

    assert result["kind"] == "stopped"
    assert [frame["phase"] for frame in saved] == ["resuming_feedback", "new"]
    assert saved[-1]["conversation_id"] == "conversation-1"
    # 修复11：stopped 文案为条件式恢复口径——禁止模型在用户停止后
    # 自主重启（旧邀请式"如需继续，请再次调用"是生产事故诱因）。
    # content 不含 conversation_id（与 rail 改写口径对齐，cid 不泄露
    # 给模型；认领靠 query + 归属守卫，cid 功能载体是 state）。
    assert "请勿" in result["content"]
    assert "明确要求继续" in result["content"]
    assert "如需继续，请再次调用" not in result["content"]
    assert "conversation-1" not in result["content"]


@pytest.mark.asyncio
async def test_cancelled_during_option_generation_translates_to_stopped():
    """修复点3.1：取消落在 _handle_outcome 内的选项生成段也被同一守卫覆盖。"""
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "questions": ["继续吗？"],
        "revision": 3,
    }
    answer = {"status": "answered", "answers": [{"selected_options": ["是"]}]}
    interrupted = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "1. 继续吗？"},
    }
    model = _Model(_option_payload("继续吗？"))
    model.invoke = AsyncMock(side_effect=asyncio.CancelledError())
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(interrupted, ensure_ascii=False)),
    ):
        result, saved = await _invoke(state=state, user_input=answer, model=model)

    assert result["kind"] == "stopped"
    assert [frame["phase"] for frame in saved] == ["resuming_feedback", "new"]


@pytest.mark.asyncio
async def test_sdk_cancelled_outcome_translates_to_stopped_terminal():
    """修复点3.2：SDK 竞态 cancelled outcome（取消晚于子进程优雅退出）统一 stopped 口径。"""
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "questions": ["继续吗？"],
        "revision": 3,
    }
    answer = {"status": "answered", "answers": [{"selected_options": ["是"]}]}
    cancelled_outcome = {
        "status": "cancelled",
        "conversation_id": "conversation-1",
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(cancelled_outcome, ensure_ascii=False)),
    ):
        result, saved = await _invoke(state=state, user_input=answer)

    assert result["kind"] == "stopped"
    # content 不含 conversation_id（修复11 口径对齐，cid 不泄露给模型）
    assert "conversation-1" not in result["content"]
    assert saved[-1]["phase"] == "new"
    assert saved[-1]["conversation_id"] == "conversation-1"


@pytest.mark.asyncio
async def test_error_phase_with_stopped_snapshot_claims_and_resumes():
    """修复点3.2：error 相位下存有停止快照时认领续跑而非判死。"""
    state = {
        "schema_version": 1,
        "phase": "error",
        "revision": 4,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "requested_report_type": "brief",
    }
    probe = {"node": "report_writing", "stopped": True}
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with (
        patch.object(de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)),
        patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
        ) as stream,
    ):
        result, saved = await _invoke(state=state)

    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "stopped"
    assert stream.await_args.kwargs["conversation_id"] == "conversation-1"
    assert result["kind"] == "completed"
    assert saved[0]["phase"] == "resuming_stopped"
    assert saved[-1]["phase"] == "completed"


@pytest.mark.asyncio
async def test_error_phase_without_stopped_snapshot_reports_finished():
    """修复点3.2：error 相位无停止快照（真实失败）→ 判死并引导重新发起。"""
    state = {
        "schema_version": 1,
        "phase": "error",
        "revision": 4,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
    }
    with (
        patch.object(de, "_probe_resumable_checkpoint", new=Mock(return_value=None)),
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, _ = await _invoke(state=state)

    assert result["kind"] == "error"
    assert "请重新发起" in result["content"]
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_phase_stays_terminal_even_with_stopped_snapshot():
    """修复点3.2（选1）：cancelled 是显式放弃的死终态，有停止快照也不认领。"""
    state = {
        "schema_version": 1,
        "phase": "cancelled",
        "revision": 4,
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
    }
    probe = {"node": "report_writing", "stopped": True}
    with (
        patch.object(de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)),
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(state=state)

    assert result["kind"] == "cancelled"
    assert "已经结束" in result["content"]
    stream.assert_not_awaited()
    assert saved == []


def _rail_ctx(
    *,
    result,
    session=None,
    resume_input=None,
    resume_key=None,
    tool_call_id="call-1",
    arguments=None,
):
    session = session or _Session()
    tool_call = SimpleNamespace(
        id=tool_call_id,
        name="deepresearch_execute",
        arguments=arguments
        if arguments is not None
        else {"query": "q", "file_name": "r"},
    )
    forced: list[dict] = []
    extra = {}
    if resume_input is not None:
        from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

        interactive_input = InteractiveInput()
        interactive_input.update(resume_key or tool_call_id, resume_input)
        extra[RESUME_USER_INPUT_KEY] = interactive_input
    return SimpleNamespace(
        session=session,
        agent=None,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="deepresearch_execute",
            tool_args=tool_call.arguments,
            tool_result=result,
        ),
        extra=extra,
        exception=None,
        request_force_finish=forced.append,
        force_finish_requests=forced,
    )


@pytest.mark.asyncio
async def test_execution_rail_turns_interaction_result_into_native_interrupt():
    state = {"schema_version": 1, "phase": "wait_feedback", "revision": 1}
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "interaction",
        "interaction": {
            "query": "请回答以下研究主题澄清问题",
            "return_json": True,
            "questions": [
                {
                    "header": "研究方向反馈",
                    "question": "重点研究哪些品类？",
                    "options": [{"label": "方向A"}, {"label": "方向B"}],
                }
            ],
        },
        "state": state,
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_result, ToolInterruptException)
    assert ctx.inputs.tool_result.request.questions == result["interaction"]["questions"]
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY]["call-1"] == state
    # 修复点1a：占位 JSON 不再裸入 LLM 历史，改为面向模型的 ToolMessage；
    # tool_call_id 保持原始调用 id（与 assistant 消息中的 tool_calls 对齐，
    # 而非 interaction 别名 id），内部 phase/占位结构不泄露。
    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-1"
    assert "结构化问答" in ctx.inputs.tool_msg.content
    assert "wait_feedback" not in ctx.inputs.tool_msg.content
    assert "schema_version" not in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_execution_rail_force_finishes_terminal_result_without_next_llm():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == [
        {"output": "研究报告已生成并交付。", "result_type": "answer"}
    ]
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    # 完成态回写：tool_msg 为完成摘要短文案（非原始 payload JSON）
    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-1"
    assert ctx.inputs.tool_msg.content == "研究报告已生成并交付。"


@pytest.mark.asyncio
async def test_execution_rail_writes_back_completion_summary_to_llm_history():
    """完成态回写：completed 的 tool_msg 改写为完成摘要短文案。

    旁路自恢复（答复交互卡后的 interrupt continuation）不重入 LLM 主循环，
    本次执行的 tool_msg 是完成态进入 LLM 历史并随快照落盘的唯一通道；
    不改写时历史中该 tool_call_id 下最后一条 tool 消息停留在暂停占位
    文案，后续轮模型误判任务未完成（实测事故：任务已完成，模型答
    "还在第一阶段暂停等待用户答复"）。tool_call_id 必须用原始调用 id
    （与 assistant 消息中的 tool_calls 对齐）；内部 state/token usage
    不泄露；tool_result 保持原 dict 供 StreamEventRail 推送前端
    chat.tool_result 帧，与 tool_msg 解耦。
    """
    state = {
        "schema_version": 1,
        "phase": "completed",
        "query": "深度研究AI芯片行业",
        "conversation_id": "conversation-1",
        "revision": 6,
    }
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "✅ **深度研究已完成！**\n\nai_chip_report已成功生成并交付。",
        "state": state,
        "workflow_llm_token_usage": {
            "input_tokens": 795008,
            "output_tokens": 38569,
        },
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-1"
    assert ctx.inputs.tool_msg.content == result["content"]
    # 内部 state / token usage 不泄露进 LLM 历史
    assert "conversation-1" not in ctx.inputs.tool_msg.content
    assert "schema_version" not in ctx.inputs.tool_msg.content
    assert "revision" not in ctx.inputs.tool_msg.content
    assert "795008" not in ctx.inputs.tool_msg.content
    # tool_result 保持原始 dict（前端交付通道，与 tool_msg 解耦）
    assert ctx.inputs.tool_result is result
    # 完成态照常清理 states/aliases 并 force finish
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert ctx.force_finish_requests == [
        {"output": result["content"], "result_type": "answer"}
    ]


@pytest.mark.asyncio
async def test_execution_rail_completed_without_content_uses_fallback_notice():
    """content 缺失时回写兜底文案，完成态仍对后续轮模型可见。"""
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.content == "DeepResearch 任务已完成，报告已交付。"
    # content 为空时维持现状：不 force finish
    assert ctx.force_finish_requests == []


@pytest.mark.asyncio
async def test_execution_rail_does_not_write_back_completion_for_cancelled():
    """取消路径不回写完成态（kind=cancelled 不触发 completed 改写），
    避免把取消/停止的续跑终态伪装成完成。"""
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "cancelled",
        "content": "DeepResearch 任务已取消。",
        "state": {"schema_version": 1, "phase": "cancelled", "revision": 3},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_msg is None
    assert ctx.force_finish_requests == [
        {"output": "DeepResearch 任务已取消。", "result_type": "answer"}
    ]


@pytest.mark.asyncio
async def test_execution_rail_rewrites_stopped_result_to_block_auto_restart():
    """修复11：stopped 结果 JSON 不再裸入 LLM 历史，改写为禁止自动重启口径。

    旧实现 stopped 分支直接 return，完整 JSON（含内部 state/revision 与
    "请再次调用本工具"邀请文案）裸入 LLM 历史，模型自主重启了已被用户
    停止的研究。改写后：tool_msg 为禁止口径且不含 conversation_id（切断
    模型拿 cid 直通重启的最短路径）；tool_result 保持原 dict（stopped 非
    interaction，不转 TIE）；不 force finish；states 保留停止快照供下一
    次调用凭 probe/扫库认领。
    """
    state = {
        "schema_version": 1,
        "phase": "new",
        "query": "研究智能家电竞争格局",
        "conversation_id": "conversation-1",
        "revision": 4,
    }
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "stopped",
        "content": (
            "DeepResearch 任务已被用户停止（conversation_id=conversation-1）。"
            "如需继续，请再次调用 deepresearch_execute 并传入"
            " conversation_id=conversation-1，将从停止处恢复。"
        ),
        "state": state,
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-1"
    assert "已被用户停止" in ctx.inputs.tool_msg.content
    assert "请勿" in ctx.inputs.tool_msg.content
    # conversation_id 与内部 state 字段不泄露进 LLM 历史
    assert "conversation-1" not in ctx.inputs.tool_msg.content
    assert "schema_version" not in ctx.inputs.tool_msg.content
    assert "revision" not in ctx.inputs.tool_msg.content
    # tool_result 保持原始 dict，不转 ToolInterruptException
    assert ctx.inputs.tool_result is result
    # 不 force finish——外层请求正被用户停止，无需再产出回答
    assert ctx.force_finish_requests == []
    # 停止快照 state 保留，供下一次调用认领直通续跑
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY]["call-1"] == state


def _resumed_session() -> _Session:
    """暂停后恢复现场：aliases 已含 interaction 克隆件 id → 原始 id 映射。"""
    session = _Session()
    session.update_state({
        DEEPRESEARCH_EXECUTION_ALIAS_KEY: {"call-1_interaction_1": "call-1"}
    })
    return session


@pytest.mark.asyncio
async def test_execution_rail_completed_after_resume_uses_original_tool_call_id():
    """resume 完成态回写的 tool_msg 必须挂原始 tool_call_id（事故回归）。

    handle_resume 深拷贝 TIE 克隆件恢复执行，ctx.inputs.tool_call.id 是
    interaction 克隆件 id（{workflow_id}_interaction_N）；若 tool_msg 沿用该
    id，与 assistant 消息 tool_calls 的原始 id 不匹配，追问轮被
    StreamEventRail 孤儿清理删除（orphan_removed），历史停留在暂停占位
    ——实测事故：任务已完成，模型答"还在第一阶段暂停等待用户答复"。
    """
    session = _resumed_session()
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "✅ **深度研究已完成！**\n\nai_chip_report已成功生成并交付。",
        "state": {
            "schema_version": 1,
            "phase": "completed",
            "query": "深度研究AI芯片行业",
            "conversation_id": "conversation-1",
            "revision": 6,
        },
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=result,
        session=session,
        tool_call_id="call-1_interaction_1",
    )

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-1"
    assert ctx.inputs.tool_msg.content == result["content"]
    # 完成态照常清理 states/aliases（按 workflow_id 收敛）
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {}


@pytest.mark.asyncio
async def test_execution_rail_stopped_after_resume_uses_original_tool_call_id():
    """resume 后停止的 tool_msg 同样挂原始 id（与 completed 同构修复）。"""
    session = _resumed_session()
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "stopped",
        "state": {
            "schema_version": 1,
            "phase": "new",
            "query": "深度研究AI芯片行业",
            "conversation_id": "conversation-1",
            "revision": 2,
        },
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=result,
        session=session,
        tool_call_id="call-1_interaction_1",
    )

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-1"
    assert "已被用户停止" in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_execution_rail_bridge_call_keeps_own_tool_call_id():
    """bridge 场景例外：本次调用 id 本身就是历史 id，不得换成旧 workflow id。

    模型换新 tool_call_id 重试被桥接旧 state 时，aliases 里有新 id → 旧
    workflow_id 的映射，但历史 assistant tool_calls 挂的是本次新 id——
    id 修正须按 interaction 后缀模式判断，而非"是否在 aliases 中"。
    """
    session = _Session()
    session.update_state({
        DEEPRESEARCH_EXECUTION_ALIAS_KEY: {"call-new": "call-old"},
        DEEPRESEARCH_EXECUTION_STATE_KEY: {
            "call-old": {
                "schema_version": 1,
                "phase": "starting",
                "query": "q",
                "file_name": "r",
                "conversation_id": "conversation-1",
                "revision": 0,
            }
        },
    })
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 1},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session, tool_call_id="call-new")

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-new"
    # states/aliases 仍按 workflow_id（call-old）清理
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {}


class _ModelContext:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self):
        return list(self.messages)

    def pop_messages(self, size):
        popped = self.messages[:size]
        self.messages = self.messages[size:]
        return popped

    async def add_messages(self, message):
        self.messages.append(message)


def _deepresearch_history_ctx(tool_messages, tool_call_id="call_00c9"):
    rail_ctx = SimpleNamespace(
        context=_ModelContext([
            UserMessage(content="深度研究AI芯片行业"),
            AssistantMessage(
                content="",
                tool_calls=[{
                    "type": "function",
                    "id": tool_call_id,
                    "function": {
                        "name": "deepresearch_execute",
                        "arguments": "{\"query\":\"深度研究AI芯片行业\"}",
                    },
                }],
            ),
            *tool_messages,
        ]),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )
    return rail_ctx


@pytest.mark.asyncio
async def test_fix_incomplete_tool_context_replaces_deepresearch_placeholder_with_completion():
    """占位注册白名单后，"占位 + completed 同 id"收敛为占位被真实结果替换。

    事故链（修复前）：deepresearch 占位文案不在白名单 → 占位被当普通
    消息保留，completed 同 id 第二条被 duplicate 规则删除 → 历史停留在
    "等待答复" → 模型误判任务未完成。注册后：占位被识别为 placeholder，
    real_tool_messages_by_id 命中 completed → 原位替换、后续重复删除。
    """
    tool_call_id = "call_00c9"
    rail = JiuSwarmStreamEventRail()
    ctx = _deepresearch_history_ctx([
        ToolMessage(
            content=DEEPRESEARCH_INTERACTION_PLACEHOLDER,
            tool_call_id=tool_call_id,
        ),
        ToolMessage(
            content="✅ **深度研究已完成！**\n\nai_chip_report已成功生成并交付。",
            tool_call_id=tool_call_id,
        ),
    ])

    await rail._fix_incomplete_tool_context(ctx)

    tool_msgs = [m for m in ctx.context.get_messages() if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == tool_call_id
    assert "已完成" in tool_msgs[0].content
    assert "等待用户答复" not in tool_msgs[0].content


@pytest.mark.asyncio
async def test_fix_incomplete_tool_context_keeps_deepresearch_placeholder_while_pending():
    """暂停期间（无真实结果）：占位原样保留，模型能看到"等待用户答复"。"""
    tool_call_id = "call_00c9"
    rail = JiuSwarmStreamEventRail()
    ctx = _deepresearch_history_ctx([
        ToolMessage(
            content=DEEPRESEARCH_INTERACTION_PLACEHOLDER,
            tool_call_id=tool_call_id,
        ),
    ])

    await rail._fix_incomplete_tool_context(ctx)

    tool_msgs = [m for m in ctx.context.get_messages() if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == DEEPRESEARCH_INTERACTION_PLACEHOLDER


def test_deepresearch_placeholder_registered_in_interrupt_templates():
    """占位文案白名单识别：仅 deepresearch_execute 名下命中。"""
    rail = JiuSwarmStreamEventRail()
    assert rail._is_legacy_tool_interrupt_placeholder_text(
        DEEPRESEARCH_INTERACTION_PLACEHOLDER, "deepresearch_execute"
    )
    # 其他工具名下不识别（防止文案碰撞扩散到无关工具）
    assert not rail._is_legacy_tool_interrupt_placeholder_text(
        DEEPRESEARCH_INTERACTION_PLACEHOLDER, "other_tool"
    )


@pytest.mark.asyncio
async def test_execution_rail_binds_request_id_for_nested_options_llm():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "extract_session_id_from_callback",
            return_value="session-1",
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "get_request_context",
            return_value={"request_id": "request-from-registry"},
        ),
    ):
        await rail.before_tool_call(ctx)

    assert de._execution_context.get().request_id == "request-from-registry"
    await rail.after_tool_call(ctx)
    assert de._execution_context.get() is None


@pytest.mark.asyncio
async def test_same_outer_tool_call_survives_multiple_native_interrupts():
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)

    sdk_questions = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "1. 重点研究哪些品类？"},
    }

    first = _rail_ctx(result=None, session=session)
    await rail.before_tool_call(first)
    start_sdk = AsyncMock(return_value=json.dumps(sdk_questions, ensure_ascii=False))
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=start_sdk,
    ):
        first.inputs.tool_result = await de.deepresearch_execute._func(
            query="研究智能家电竞争格局",
            file_name="智能家电报告",
        )
    await rail.after_tool_call(first)
    assert isinstance(first.inputs.tool_result, ToolInterruptException)
    first_interaction_id = first.inputs.tool_result.tool_call.id
    assert first_interaction_id != "call-1"
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY]["call-1"]["phase"] == (
        "wait_feedback"
    )
    assert first.inputs.tool_result.request.questions[0]["question"] == (
        "重点研究哪些品类？"
    )

    feedback_answer = {
        "status": "answered",
        "answers": [
            {
                "question": "重点研究哪些品类？",
                "selected_options": ["空调与冰箱"],
            }
        ],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    second = _rail_ctx(
        result=None,
        session=session,
        resume_input=feedback_answer,
        tool_call_id=first_interaction_id,
    )
    await rail.before_tool_call(second)
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        second.inputs.tool_result = await de.deepresearch_execute._func(
            query="研究智能家电竞争格局",
            file_name="智能家电报告",
        )
    await rail.after_tool_call(second)

    assert second.force_finish_requests[0]["result_type"] == "answer"
    assert "✅ **深度研究已完成！**" in second.force_finish_requests[0]["output"]
    # 事故回归：resume 完成态 tool_msg 必须挂原始调用 id（本次 ctx 携带
    # 的是 TIE 克隆件 interaction id），否则追问轮被孤儿清理删除，历史
    # 停留在暂停占位，模型误判任务未完成
    assert isinstance(second.inputs.tool_msg, ToolMessage)
    assert second.inputs.tool_msg.tool_call_id == "call-1"
    assert "已完成" in second.inputs.tool_msg.content
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {}


def _stale_session(**state_overrides):
    """硬中断现场：in-flight state 遗留在旧 tool_call_id 下。"""
    state = {
        "schema_version": 1,
        "phase": "starting",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "requested_report_type": "brief",
        "revision": 0,
    }
    state.update(state_overrides)
    session = _Session()
    session.update_state({DEEPRESEARCH_EXECUTION_STATE_KEY: {"call-old": state}})
    return session


_STOPPED_PROBE = {"node": "researcher", "stopped": True}


@pytest.mark.asyncio
async def test_bridge_recovers_stale_inflight_state_and_resumes():
    """场景①：state miss + 同 query + 探针停止快照 → 桥接并直通续跑。"""
    session = _stale_session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "q", "file_name": "r"},
    )
    stopped_probe = {"node": "researcher", "stopped": True}
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with (
        patch.object(dr, "_probe_resumable_checkpoint", return_value=stopped_probe),
        patch.object(de, "_probe_resumable_checkpoint", return_value=stopped_probe),
    ):
        await rail.before_tool_call(ctx)
        assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {
            "call-new": "call-old"
        }
        with patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
        ) as stream:
            ctx.inputs.tool_result = await de.deepresearch_execute._func(
                query="q",
                file_name="r",
            )
        await rail.after_tool_call(ctx)

    assert stream.await_count == 1
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "stopped"
    assert stream.await_args.kwargs["conversation_id"] == "conversation-1"


@pytest.mark.asyncio
async def test_bridge_rejects_when_query_switches_topic():
    """场景②：用户换研究主题 → 不桥，全新会话重跑。"""
    session = _stale_session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "完全不同的新主题", "file_name": "r"},
    )
    with (
        patch.object(dr, "_probe_resumable_checkpoint", return_value=_STOPPED_PROBE),
        patch.object(de, "_probe_resumable_checkpoint", return_value=None),
    ):
        await rail.before_tool_call(ctx)

    assert session.state.get(DEEPRESEARCH_EXECUTION_ALIAS_KEY, {}) == {}
    completed = {"status": "completed", "report_delivered": True}
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        await de.deepresearch_execute._func(query="完全不同的新主题", file_name="r")

    assert stream.await_args.kwargs["action"] == "start"
    assert stream.await_args.kwargs["conversation_id"] != "conversation-1"


@pytest.mark.asyncio
async def test_bridge_rejects_multiple_candidate_states():
    """场景③：两条同主题候选 → 唯一性守卫拒绝，不桥。"""
    state = {
        "schema_version": 1,
        "phase": "starting",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "revision": 0,
    }
    other = dict(state, conversation_id="conversation-2")
    session = _Session()
    session.update_state(
        {
            DEEPRESEARCH_EXECUTION_STATE_KEY: {
                "call-old-a": state,
                "call-old-b": other,
            }
        }
    )
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "q", "file_name": "r"},
    )
    with patch.object(dr, "_probe_resumable_checkpoint", return_value=_STOPPED_PROBE):
        await rail.before_tool_call(ctx)

    assert session.state.get(DEEPRESEARCH_EXECUTION_ALIAS_KEY, {}) == {}


@pytest.mark.asyncio
async def test_bridge_rejects_when_explicit_cid_points_elsewhere():
    """场景④：显式传入不同的 conversation_id → 不抢别人的会话。"""
    session = _stale_session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={
            "query": "q",
            "file_name": "r",
            "conversation_id": "conversation-other",
        },
    )
    with patch.object(dr, "_probe_resumable_checkpoint", return_value=_STOPPED_PROBE):
        await rail.before_tool_call(ctx)

    assert session.state.get(DEEPRESEARCH_EXECUTION_ALIAS_KEY, {}) == {}


@pytest.mark.asyncio
async def test_bridge_rejects_live_inflight_state_without_stopped_snapshot():
    """场景⑤（探针门）：in-flight state 无停止快照（子进程还活着）→ 不桥。"""
    session = _stale_session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "q", "file_name": "r"},
    )
    live_probe = {"node": "feedback_handler", "questions": ["继续吗？"]}
    with patch.object(dr, "_probe_resumable_checkpoint", return_value=live_probe):
        await rail.before_tool_call(ctx)

    assert session.state.get(DEEPRESEARCH_EXECUTION_ALIAS_KEY, {}) == {}


@pytest.mark.asyncio
async def test_bridge_from_new_phase_state_cleans_alias_on_terminal():
    """场景⑥：new 相位（免探针门）桥接，终态后 state 与 alias 清理干净。"""
    session = _stale_session(phase="new")
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "q", "file_name": "r"},
    )
    await rail.before_tool_call(ctx)
    assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {"call-new": "call-old"}

    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    ctx.inputs.tool_result = result
    await rail.after_tool_call(ctx)

    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {}


@pytest.mark.asyncio
async def test_bridge_from_error_phase_state_resumes_stopped():
    """场景⑦（修复点3.2）：error 相位 + 停止快照 → 桥接并直通续跑。"""
    session = _stale_session(phase="error", revision=4)
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "q", "file_name": "r"},
    )
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with (
        patch.object(dr, "_probe_resumable_checkpoint", return_value=_STOPPED_PROBE),
        patch.object(de, "_probe_resumable_checkpoint", return_value=_STOPPED_PROBE),
    ):
        await rail.before_tool_call(ctx)
        assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {
            "call-new": "call-old"
        }
        with patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
        ) as stream:
            ctx.inputs.tool_result = await de.deepresearch_execute._func(
                query="q",
                file_name="r",
            )
        await rail.after_tool_call(ctx)

    assert stream.await_count == 1
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "stopped"
    assert stream.await_args.kwargs["conversation_id"] == "conversation-1"


@pytest.mark.asyncio
async def test_bridge_rejects_error_phase_state_without_stopped_snapshot():
    """场景⑧（探针门）：error 相位无停止快照（子进程失败退出）→ 不桥。"""
    session = _stale_session(phase="error", revision=4)
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "q", "file_name": "r"},
    )
    with patch.object(dr, "_probe_resumable_checkpoint", return_value=None):
        await rail.before_tool_call(ctx)

    assert session.state.get(DEEPRESEARCH_EXECUTION_ALIAS_KEY, {}) == {}


@pytest.mark.asyncio
async def test_bridge_rejects_cancelled_phase_state():
    """场景⑨（选1）：cancelled 是显式放弃的死终态，不入桥候选。"""
    session = _stale_session(phase="cancelled", revision=4)
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        tool_call_id="call-new",
        arguments={"query": "q", "file_name": "r"},
    )
    with patch.object(dr, "_probe_resumable_checkpoint", return_value=_STOPPED_PROBE):
        await rail.before_tool_call(ctx)

    assert session.state.get(DEEPRESEARCH_EXECUTION_ALIAS_KEY, {}) == {}


def test_resume_input_falls_back_to_only_entry_on_key_mismatch():
    """作答键与恢复键不一致但作答唯一 → 兜底取之；多条目时仍按键取。"""
    from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    answer = {"status": "answered"}
    interactive_input = InteractiveInput()
    interactive_input.update("call_a02da078", answer)
    ctx = SimpleNamespace(extra={RESUME_USER_INPUT_KEY: interactive_input})

    # 键断裂（作答挂在历史卡片 id 下）：唯一条目兜底取值。
    assert (
        dr._resume_input(ctx, "call_6f57dea2_interaction_1") == answer
    )
    # 键匹配：正常取值。
    assert dr._resume_input(ctx, "call_a02da078") == answer

    # 多条目时无法安全兜底：仍返回 None，不得张冠李戴。
    interactive_input.update("call_other_card", {"status": "answered"})
    assert dr._resume_input(ctx, "call_6f57dea2_interaction_1") is None


@pytest.mark.asyncio
async def test_rail_resumes_outline_with_answer_keyed_by_stale_card_id():
    """作答挂在历史卡片 id 下（键断裂）→ rail 兜底取唯一作答续跑大纲确认。"""
    outline_confirmed = {
        "status": "answered",
        "answers": [
            {
                "question": "研究大纲是否确认？",
                "selected_options": ["确认大纲，继续研究"],
            }
        ],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    session = _Session()
    session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] = {
        "call-1": {
            "schema_version": 1,
            "phase": "wait_outline",
            "revision": 1,
            "query": "研究智能家电竞争格局",
            "file_name": "智能家电报告",
            "conversation_id": "conversation-1",
            "requested_report_type": "brief",
            "outline_presented": True,
        }
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(
        result=None,
        session=session,
        resume_input=outline_confirmed,
        resume_key="call_a02da078",
        tool_call_id="call-1",
    )
    await rail.before_tool_call(ctx)
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        ctx.inputs.tool_result = await de.deepresearch_execute._func(
            query="研究智能家电竞争格局",
            file_name="智能家电报告",
        )
    await rail.after_tool_call(ctx)

    # 兜底取到作答 → 大纲确认通过，resume outline_interaction 并完成。
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "outline_interaction"
    assert ctx.force_finish_requests[0]["result_type"] == "answer"


@pytest.mark.asyncio
async def test_start_passes_upgrade_from_brief_to_stream_and_not_state():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(
            report_type="professional",
            upgrade_from_brief=True,
        )

    assert result["kind"] == "completed"
    assert stream.await_args.kwargs["report_type"] == "professional"
    assert stream.await_args.kwargs["upgrade_from_brief"] is True
    assert saved[0]["requested_report_type"] == "professional"
    # start 一次性语义：不入 state（resume 路径不透传）
    assert "upgrade_from_brief" not in saved[0]


@pytest.mark.asyncio
async def test_upgrade_from_brief_defaults_to_false_without_flag():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        await _invoke()

    assert stream.await_args.kwargs["upgrade_from_brief"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [True, False])
async def test_new_upgrade_does_not_claim_stopped_conversation(structured):
    _seed_upgrade_record(metadata=_UPGRADE_TEST_METADATA)
    completed = {"status": "completed", "report_delivered": True}
    followup = {
        "v": 1, "action": "upgrade_latest_brief", "source_report_id": "brief-conv-1",
    }
    try:
        with (
            patch.object(de, "_probe_resumable_checkpoint") as probe,
            patch.object(de, "_scan_resumable_checkpoint_by_query") as scan,
            patch.object(de, "_call_deepresearch_stream_impl",
                         new=AsyncMock(return_value=json.dumps(completed))) as stream,
        ):
            result, _ = await _invoke(
                conversation_id="old-stopped-conversation",
                deepresearch_followup=followup if structured else None,
                upgrade_from_brief=not structured,
            )
        assert result["kind"] == "completed"
        assert stream.await_args.kwargs["action"] == "start"
        assert stream.await_args.kwargs["upgrade_from_brief"] is True
        assert stream.await_args.kwargs["conversation_id"] != "old-stopped-conversation"
        probe.assert_not_called()
        scan.assert_not_called()
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [True, False])
async def test_upgrade_rail_does_not_bridge_to_stale_workflow(structured):
    ctx = _rail_ctx(
        result=None, session=_stale_session(), tool_call_id="call-new",
        arguments={"query": "q", "upgrade_from_brief": not structured},
    )
    followup = {"v": 1, "action": "upgrade_latest_brief", "source_report_id": "brief-1"}
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    with (
        patch.object(dr, "get_request_context", return_value={
            "deepresearch_followup": followup if structured else None,
        }),
        patch.object(dr, "_bridge_stale_workflow") as bridge,
        patch.object(dr, "bind_deepresearch_execution_context") as bind,
    ):
        await rail.before_tool_call(ctx)
    bridge.assert_not_called()
    assert bind.call_args.kwargs["tool_call_id"] == "call-new"
    assert bind.call_args.kwargs["state"] is None


_UPGRADE_TEST_METADATA = {
    "brief_outline": [{"title": "第一章"}],
    "research_intent": "原始意图",
    "language": "zh",
}


def _seed_upgrade_record(
    *,
    report_id: str = "brief-conv-1",
    original_query: str = "原始研究查询",
    original_file_name: str = "原报告.md",
    metadata: dict | None = None,
    available: bool = True,
    report_path: str = "",
) -> None:
    """按 execution.py 实际解析口径（默认路由）种入 brief 来源记录。"""
    dt._BRIEF_UPGRADE_RECORDS[dt._outline_cache_key(dt._get_route())] = {
        "report_id": report_id,
        "original_query": original_query,
        "original_file_name": original_file_name,
        "metadata": metadata,
        "upgrade_available": available,
        "report_path": report_path,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["upgrade_latest_brief", "refresh_latest_brief", "manual_upgrade"])
async def test_fresh_followup_binds_current_mcp_sources_without_persisting_credentials(tmp_path, action):
    report = tmp_path / "brief.html"
    report.write_text("<h1>原报告</h1><p>已有研究结论</p>", encoding="utf-8")
    source = {
        "server_name": "search", "server_url": "https://example.org/mcp",
        "transport_type": "streamable_http", "type": "search", "timeout": 30.0,
        "headers": {"Authorization": "request-secret"},
    }
    _seed_upgrade_record(metadata=_UPGRADE_TEST_METADATA, report_path=str(report))
    followup = None if action == "manual_upgrade" else {
        "v": 1, "action": action, "source_report_id": "brief-conv-1",
    }
    try:
        with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(
            return_value=json.dumps({"status": "completed", "report_delivered": True})
        )) as stream:
            result, saved = await _invoke(
                query="补充研究", report_type="professional", mcp_servers=[source],
                materials_context={"scope": "followup-session"},
                upgrade_from_brief=action == "manual_upgrade", deepresearch_followup=followup,
            )
        assert result["kind"] == "completed"
        assert stream.await_args.kwargs["_mcp_servers"] == [source]
        assert saved[-1]["mcp_server_names"] == ["search"]
        assert "request-secret" not in repr(saved)
        if action == "refresh_latest_brief":
            assert stream.await_args.kwargs["report_type"] == "brief"
            assert stream.await_args.kwargs["_brief_refresh_original_query"] == "原始研究查询"
        else:
            assert stream.await_args.kwargs["upgrade_from_brief"] is True
            assert stream.await_args.kwargs["_upgrade_brief_metadata"] == _UPGRADE_TEST_METADATA
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
async def test_refresh_binds_report_body_and_forces_new_brief(tmp_path):
    report = tmp_path / "original.html"
    report.write_text("<h1>原报告</h1><p>证据不足的结论</p>", encoding="utf-8")
    _seed_upgrade_record(metadata=_UPGRADE_TEST_METADATA, report_path=str(report))
    try:
        with (
            patch.object(de, "_probe_resumable_checkpoint") as probe,
            patch.object(de, "_scan_resumable_checkpoint_by_query") as scan,
            patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(
                return_value=json.dumps({"status": "completed", "report_delivered": True})
            )) as stream,
        ):
            result, saved = await _invoke(
                query="补充关键数据和参考来源", file_name="模型选择的文件名",
                report_type="professional", upgrade_from_brief=True,
                conversation_id="old-stopped-conversation",
                deepresearch_followup={
                    "v": 1, "action": "refresh_latest_brief",
                    "source_report_id": "brief-conv-1",
                },
            )
        assert result["kind"] == "completed"
        kwargs = stream.await_args.kwargs
        assert kwargs["report_type"] == "brief"
        assert kwargs["upgrade_from_brief"] is False
        assert kwargs["action"] == "start"
        assert kwargs["conversation_id"] != "old-stopped-conversation"
        assert "原始研究查询" in kwargs["query"]
        assert "证据不足的结论" in kwargs["query"]
        assert "补充关键数据和参考来源" in kwargs["query"]
        assert kwargs["file_name"].startswith("原报告（补充版-")
        assert kwargs["_brief_refresh_original_query"] == "原始研究查询"
        assert saved[0]["source_brief_report_id"] == "brief-conv-1"
        assert saved[0]["requested_report_type"] == "brief"
        assert report.read_text(encoding="utf-8") == "<h1>原报告</h1><p>证据不足的结论</p>"
        probe.assert_not_called()
        scan.assert_not_called()
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id,report_path,code", [
    ("another-report", "", "brief_refresh_target_changed"),
    ("brief-conv-1", "", "brief_refresh_unavailable"),
])
async def test_refresh_fails_closed_without_matching_readable_source(source_id, report_path, code):
    _seed_upgrade_record(metadata=_UPGRADE_TEST_METADATA, report_path=report_path)
    try:
        with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
            result, _ = await _invoke(deepresearch_followup={
                "v": 1, "action": "refresh_latest_brief", "source_report_id": source_id,
            })
        assert result["kind"] == "error"
        assert result["error_code"] == code
        stream.assert_not_awaited()
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
async def test_refresh_resume_keeps_original_task_and_brief_type():
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(
        return_value=json.dumps({"status": "completed", "report_delivered": True})
    )) as stream:
        result, _ = await _invoke(
            state={
                "phase": "wait_feedback", "query": "绑定后的补充查询",
                "file_name": "原报告（补充版-1）", "conversation_id": "refresh-conv",
                "requested_report_type": "brief", "brief_refresh_original_query": "原始研究查询",
                "questions": ["需要补充什么？"], "revision": 1,
            },
            user_input={"status": "answered", "answers": [{
                "question": "需要补充什么？", "selected_options": ["参考来源"],
            }]}, report_type="professional",
        )
    assert result["kind"] == "completed"
    assert stream.await_args.kwargs["report_type"] == "brief"
    assert stream.await_args.kwargs["_brief_refresh_original_query"] == "原始研究查询"


@pytest.mark.asyncio
async def test_strict_followup_binds_latest_record_snapshot():
    """strict 上下文：query/文件名/metadata 同源绑定，强制 professional。"""
    completed = {
        "status": "completed",
        "conversation_id": "conversation-2",
        "report_delivered": True,
    }
    _seed_upgrade_record(
        report_id="brief-conv-1",
        original_query="原始研究查询",
        original_file_name="原报告.md",
        metadata=_UPGRADE_TEST_METADATA,
    )
    try:
        with patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
        ) as stream:
            result, saved = await _invoke(
                query="模型自选的错 query",
                file_name="模型自选.md",
                report_type="brief",
                upgrade_from_brief=False,
                deepresearch_followup={
                    "v": 1,
                    "action": "upgrade_latest_brief",
                    "source_report_id": "brief-conv-1",
                },
            )

        assert result["kind"] == "completed"
        kwargs = stream.await_args.kwargs
        assert kwargs["query"] == "原始研究查询"
        assert kwargs["file_name"] == "原报告完整版"
        assert kwargs["report_type"] == "professional"
        assert kwargs["upgrade_from_brief"] is True
        assert kwargs["_upgrade_brief_metadata"] == _UPGRADE_TEST_METADATA
        assert saved[0]["query"] == "原始研究查询"
        assert saved[0]["requested_report_type"] == "professional"
        assert saved[0]["source_brief_report_id"] == "brief-conv-1"
        assert result["state"]["source_brief_report_id"] == "brief-conv-1"
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
async def test_strict_followup_target_changed_errors_without_sdk_start():
    """来源与最新记录不一致 → 明确失败，不静默降级、不启动 SDK。"""
    _seed_upgrade_record(report_id="brief-B", metadata=_UPGRADE_TEST_METADATA)
    try:
        with patch.object(
            de, "_call_deepresearch_stream_impl", new=AsyncMock()
        ) as stream:
            result, saved = await _invoke(
                deepresearch_followup={
                    "v": 1,
                    "action": "upgrade_latest_brief",
                    "source_report_id": "brief-C",
                },
            )

        assert result["kind"] == "error"
        assert result["error_code"] == "brief_upgrade_target_changed"
        assert saved[-1]["phase"] == "error"
        stream.assert_not_awaited()
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
async def test_strict_followup_unavailable_record_errors_without_sdk_start():
    """记录缺失或不可用（缓存失败登记）→ 明确失败，不启动 SDK。"""
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        result, saved = await _invoke(
            deepresearch_followup={
                "v": 1,
                "action": "upgrade_latest_brief",
                "source_report_id": "brief-conv-1",
            },
        )
    assert result["kind"] == "error"
    assert result["error_code"] == "brief_upgrade_unavailable"
    assert saved[-1]["phase"] == "error"
    stream.assert_not_awaited()

    _seed_upgrade_record(report_id="brief-conv-1", metadata=None, available=False)
    try:
        with patch.object(
            de, "_call_deepresearch_stream_impl", new=AsyncMock()
        ) as stream:
            result2, _ = await _invoke(
                deepresearch_followup={
                    "v": 1,
                    "action": "upgrade_latest_brief",
                    "source_report_id": "brief-conv-1",
                },
            )
        assert result2["kind"] == "error"
        assert result2["error_code"] == "brief_upgrade_unavailable"
        stream.assert_not_awaited()
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
async def test_manual_upgrade_overrides_query_from_record_snapshot():
    """手动 upgrade_from_brief：同源覆盖模型自选 query/文件名（修混源缺陷）。"""
    completed = {
        "status": "completed",
        "conversation_id": "conversation-2",
        "report_delivered": True,
    }
    _seed_upgrade_record(
        report_id="brief-conv-1",
        original_query="原始研究查询",
        original_file_name="原报告.md",
        metadata=_UPGRADE_TEST_METADATA,
    )
    try:
        with patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
        ) as stream:
            _, saved = await _invoke(
                query="模型自选的错 query",
                file_name="模型自选.md",
                report_type="professional",
                upgrade_from_brief=True,
            )

        kwargs = stream.await_args.kwargs
        assert kwargs["query"] == "原始研究查询"
        assert kwargs["file_name"] == "原报告完整版"
        assert kwargs["_upgrade_brief_metadata"] == _UPGRADE_TEST_METADATA
        assert saved[0]["source_brief_report_id"] == "brief-conv-1"
    finally:
        dt._BRIEF_UPGRADE_RECORDS.clear()


@pytest.mark.asyncio
async def test_manual_upgrade_without_record_keeps_model_query():
    """无可用记录：保持 v2 降级语义——模型 query 保留、无快照、不报错。"""
    completed = {
        "status": "completed",
        "conversation_id": "conversation-2",
        "report_delivered": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(
            query="模型自选 query",
            report_type="professional",
            upgrade_from_brief=True,
        )

    assert result["kind"] == "completed"
    kwargs = stream.await_args.kwargs
    assert kwargs["query"] == "模型自选 query"
    assert kwargs["upgrade_from_brief"] is True
    assert kwargs["_upgrade_brief_metadata"] is None
    assert "source_brief_report_id" not in saved[0]


@pytest.mark.asyncio
async def test_completed_result_carries_validated_brief_report():
    """有效 brief_report 小描述随完成结果透传；无效时静默省略。"""
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "brief_report": {
            "v": 1,
            "report_id": "conversation-1",
            "report_type": "brief",
            "report_delivered": True,
            "title": "智能家电报告",
            "upgrade_available": True,
        },
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, _ = await _invoke(requested_report_type="brief")

    assert result["brief_report"] == completed["brief_report"]

    invalid = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "brief_report": {"v": 1, "report_type": "brief"},  # 缺 report_id
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(invalid, ensure_ascii=False)),
    ):
        result2, _ = await _invoke(requested_report_type="brief")

    assert result2["kind"] == "completed"
    assert "brief_report" not in result2


@pytest.mark.asyncio
async def test_followup_ignored_for_existing_workflow_state():
    """已有工作流状态（resume 轮）忽略 followup，按原恢复语义执行。"""
    state = {
        "schema_version": 1,
        "phase": "wait_outline",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "outline_presented": True,
        "outline_sections": ["用户画像"],
        "requested_report_type": "professional",
        "revision": 3,
    }
    answer = {
        "status": "answered",
        "answers": [{"selected_options": ["确认大纲，继续研究"]}],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, _ = await _invoke(
            state=state,
            user_input=answer,
            deepresearch_followup={
                "v": 1,
                "action": "upgrade_latest_brief",
                "source_report_id": "brief-conv-1",
            },
        )

    assert result["kind"] == "completed"
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "outline_interaction"
    assert "source_brief_report_id" not in result["state"]


@pytest.mark.asyncio
async def test_completed_brief_with_upgrade_available_appends_question():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "brief_upgrade_available": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, _ = await _invoke(requested_report_type="brief")

    assert result["kind"] == "completed"
    assert "请回复「生成完整报告」" in result["content"]
    assert "精简版报告" in result["content"]


@pytest.mark.asyncio
async def test_completed_without_upgrade_available_has_no_question():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, _ = await _invoke(requested_report_type="brief")

    assert result["kind"] == "completed"
    assert "生成完整报告" not in result["content"]


def test_completion_content_question_gates():
    brief_state = {"file_name": "智能家电报告", "requested_report_type": "brief"}
    professional_state = {
        "file_name": "智能家电报告",
        "requested_report_type": "professional",
    }

    # brief + 缓存成功 → 追问
    assert "请回复「生成完整报告」" in de._completion_content(
        brief_state, brief_upgrade_available=True
    )
    # brief + 缓存缺失 → 不追问（不承诺"沿用大纲"）
    assert "生成完整报告" not in de._completion_content(
        brief_state, brief_upgrade_available=False
    )
    # professional → 永不追问
    assert "生成完整报告" not in de._completion_content(
        professional_state, brief_upgrade_available=True
    )
    # 兼容旧调用（无新参数）
    assert "生成完整报告" not in de._completion_content(brief_state)


@pytest.mark.asyncio
async def test_completed_applies_edited_outline_sections_to_summary_and_state():
    """卡上大纲 A → 用户编辑 → SDK 生效大纲 C：完成摘要与完成 state 均使用 C。"""
    state = {
        "schema_version": 1,
        "phase": "wait_outline",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "outline_presented": True,
        "outline_sections": ["用户画像", "行为习惯", "潜在需求"],
        "requested_report_type": "professional",
        "revision": 5,
    }
    answer = {
        "status": "answered",
        "answers": [{"selected_options": ["确认大纲，继续研究"]}],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "outline_sections": ["用户画像", "行为习惯", "潜在需求与来源"],
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, saved = await _invoke(state=state, user_input=answer)

    assert result["kind"] == "completed"
    # 「报告概览」列出 SDK 最后生效章节（C），不再是卡上旧版（A）
    assert "3. 潜在需求与来源" in result["content"]
    # 持久化与返回的 state 同步为同一份章节
    assert saved[-1]["outline_sections"] == ["用户画像", "行为习惯", "潜在需求与来源"]
    assert result["state"]["outline_sections"] == [
        "用户画像",
        "行为习惯",
        "潜在需求与来源",
    ]


@pytest.mark.asyncio
async def test_completed_keeps_outline_sections_when_outcome_field_invalid():
    """回传字段缺失或无效（非列表/空列表/空标题/非字符串）：保持卡上原章节。"""
    state = {
        "schema_version": 1,
        "phase": "wait_outline",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "outline_presented": True,
        "outline_sections": ["用户画像", "行为习惯", "潜在需求"],
        "requested_report_type": "professional",
        "revision": 5,
    }
    answer = {
        "status": "answered",
        "answers": [{"selected_options": ["确认大纲，继续研究"]}],
    }
    for invalid_field in ("not-a-list", [], ["章节A", ""], ["章节A", 3]):
        completed = {
            "status": "completed",
            "conversation_id": "conversation-1",
            "report_delivered": True,
            "outline_sections": invalid_field,
        }
        with patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
        ):
            result, saved = await _invoke(state=dict(state), user_input=answer)

        assert result["kind"] == "completed"
        assert "3. 潜在需求与来源" not in result["content"]
        assert "3. 潜在需求" in result["content"]
        assert saved[-1]["outline_sections"] == ["用户画像", "行为习惯", "潜在需求"]

    # 字段整体缺失（直接确认、resume 未重新生成大纲）同样保持原值
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, saved = await _invoke(state=dict(state), user_input=answer)

    assert saved[-1]["outline_sections"] == ["用户画像", "行为习惯", "潜在需求"]
    assert "3. 潜在需求" in result["content"]


@pytest.mark.asyncio
async def test_completed_ignores_outline_sections_without_presented_card():
    """无大纲卡的完成（brief 等场景）：回传字段被忽略，完成文案不新增「报告概览」。"""
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "outline_sections": ["章节A", "章节B"],
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, saved = await _invoke(requested_report_type="brief")

    assert result["kind"] == "completed"
    assert "报告概览" not in result["content"]
    assert "outline_sections" not in saved[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"status": "skipped", "answers": []},
    {"status": "answered", "answers": [{"question_index": 0, "selected_options": [], "custom_input": "聚焦技术"}]}])
async def test_material_snapshot_survives_clarification_and_outline(tmp_path, response):
    from jiuwenswarm.agents.harness.common.tools.deepresearch import materials as m
    source = tmp_path / "research.txt"
    source.write_text("青鹭项目的试验数据是73.42%。")
    material_context = {"original_query": "参考附件研究青鹭项目", "files": [{"path": str(source), "name": source.name}],
                        "scope": "session-A", "output_dir": str(tmp_path)}
    interrupted = {"status": "interrupted", "node_id": "feedback_handler", "marker": {"questions": "研究范围？"}}
    outline = {"status": "interrupted", "node_id": "outline_interaction", "marker": {"outline": "# 研究报告\n## 结论"}}
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(side_effect=[interrupted, outline,
            {"status": "completed", "report_delivered": True}])) as stream:
        first, _ = await _invoke(materials_context=material_context)
        assert first["kind"] == "interaction"
        reference = first["state"]["materials_reference"]
        original_query = first["state"]["query"]
        assert m.MATERIALS_POLICY in original_query
        assert material_context["original_query"] in original_query
        source.unlink()  # resumes must not re-read input
        with patch.object(m, "prepare_materials", new=AsyncMock(side_effect=AssertionError("re-prepared"))):
            second, _ = await _invoke(state=first["state"], user_input=response,
                                      materials_context={**material_context, "original_query": "new https://bad.invalid"})
            assert second["kind"] == "interaction"
            third, _ = await _invoke(state=second["state"], user_input={"status": "skipped", "answers": []})
        assert third["kind"] == "completed"
        assert all(call.kwargs["_materials_reference"] == reference for call in stream.await_args_list)
        assert third["state"]["query"] == original_query
        replay, _ = await _invoke(state=third["state"], materials_context=material_context)
        assert replay["kind"] == "completed"
        assert stream.await_count == 3


@pytest.mark.asyncio
async def test_material_preparation_cancel_and_duplicate_start(tmp_path):
    from jiuwenswarm.agents.harness.common.tools.deepresearch import materials as m
    entered = asyncio.Event()
    async def pending(*args):
        entered.set()
        await asyncio.Event().wait()
    context = {"original_query": "附件研究", "files": [], "scope": "A", "output_dir": str(tmp_path)}
    with patch.object(m, "prepare_materials", new=pending), patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        first = asyncio.create_task(_invoke(materials_context=context))
        await entered.wait()
        duplicate, _ = await _invoke(materials_context=context)
        assert duplicate["error_code"] == "execution_uncertain"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_material_sessions_are_isolated(tmp_path):
    async def run(scope):
        path = tmp_path / (scope + ".txt")
        path.write_text(scope)
        context = {"original_query": "参考附件", "files": [{"path": str(path)}], "scope": scope, "output_dir": str(tmp_path)}
        return await _invoke(materials_context=context)
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(return_value={"status": "completed", "report_delivered": True})):
        (a, _), (b, _) = await asyncio.gather(run("A"), run("B"))
    assert a["state"]["materials_reference"]["path"] != b["state"]["materials_reference"]["path"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [
    "runner_missing", "runtime_python_invalid", "runtime_python_missing", "search_config_missing",
])
async def test_unavailable_engine_returns_to_model_instead_of_force_finish(error_code):
    """The actual execute -> Rail path must allow another model turn."""
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=None, session=session)
    await rail.before_tool_call(ctx)
    try:
        with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(return_value=json.dumps({
            "status": "error", "error_code": error_code, "error": "engine unavailable",
        }))) as stream:
            ctx.inputs.tool_result = await de.deepresearch_execute._func(
                query="深入研究苹果的发展", file_name="苹果发展研究",
            )
        await rail.after_tool_call(ctx)
    finally:
        # after_tool_call must also release the bound execution context.
        assert de._execution_context.get() is None
    assert ctx.force_finish_requests == []
    result = ctx.inputs.tool_result
    assert result["kind"] == "error" and result["error_code"] == error_code
    assert result["fallback"]["mode"] == "model_research"
    assert result["fallback"]["restart_from"] == "明确问题与范围"
    assert result["fallback"]["query"] == "深入研究苹果的发展"
    assert result.get("report_delivered") is not True
    assert "Markdown" in result["content"]
    assert stream.await_args.kwargs["action"] == "start"
    # Replaying the failed call returns the same handoff without starting again.
    replay = _rail_ctx(result=None, session=session)
    await rail.before_tool_call(replay)
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as repeated:
        replay.inputs.tool_result = await de.deepresearch_execute._func(query="q", file_name="r")
    await rail.after_tool_call(replay)
    repeated.assert_not_awaited()
    assert replay.force_finish_requests == []
    assert replay.inputs.tool_result["fallback"] == result["fallback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [
    "execution_uncertain", "terminal_marker_missing", "report_delivery_failed",
    "materials_preparation_failed", "outcome_invalid", "child_config_invalid",
])
async def test_other_failures_remain_terminal_without_model_handoff(error_code):
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=None)
    await rail.before_tool_call(ctx)
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(return_value=json.dumps({
        "status": "error", "error_code": error_code, "error": "failed",
    }))):
        ctx.inputs.tool_result = await de.deepresearch_execute._func(query="研究主题", file_name="r")
    await rail.after_tool_call(ctx)
    assert "fallback" not in ctx.inputs.tool_result
    assert len(ctx.force_finish_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['url', 'document', 'snapshot'])
async def test_material_preparation_failure_hands_off_and_replays(tmp_path, failure):
    from jiuwenswarm.agents.harness.common.tools.deepresearch import materials as m

    source = tmp_path / 'broken.docx'
    source.write_bytes(b'not a valid docx archive')
    original = ('必须使用 https://[bad/path 作为核心材料研究主题' if failure == 'url'
                else '必须使用附件作为核心材料研究主题')
    material_context = {'original_query': original, 'scope': 'material-handoff',
                        'output_dir': str(tmp_path), 'files': [] if failure == 'url' else [
                            {'path': str(source), 'name': source.name}]}
    rail = DeepResearchExecutionRail(model_provider=lambda: None,
                                     materials_context_provider=lambda: material_context)
    session = _Session()
    ctx = _rail_ctx(result=None, session=session)
    await rail.before_tool_call(ctx)
    with patch.object(de, '_call_deepresearch_stream_impl', new=AsyncMock()) as stream:
        if failure == 'snapshot':
            with patch.object(m, 'prepare_materials', new=AsyncMock(side_effect=OSError('/private/internal/path'))):
                ctx.inputs.tool_result = await de.deepresearch_execute._func(query='model-rewritten query')
        else:
            ctx.inputs.tool_result = await de.deepresearch_execute._func(query='model-rewritten query')
    await rail.after_tool_call(ctx)
    result = ctx.inputs.tool_result
    assert result['error_code'] == 'materials_preparation_failed'
    assert result['fallback']['mode'] == 'model_research'
    assert result['fallback']['query'] == original
    assert '不得绕过' in result['content']
    assert '/private/internal/path' not in str(result)
    assert ctx.force_finish_requests == []
    stream.assert_not_awaited()
    replay = _rail_ctx(result=None, session=session)
    await rail.before_tool_call(replay)
    with patch.object(m, 'prepare_materials', new=AsyncMock()) as prepared:
        replay.inputs.tool_result = await de.deepresearch_execute._func(query='new query')
    await rail.after_tool_call(replay)
    prepared.assert_not_awaited()
    assert replay.force_finish_requests == []
    assert replay.inputs.tool_result['fallback'] == result['fallback']


@pytest.mark.asyncio
async def test_offline_material_policy_does_not_handoff(tmp_path):
    context = {'original_query': '只根据附件，禁止联网', 'files': [],
               'scope': 'offline', 'output_dir': str(tmp_path)}
    with patch.object(de, '_call_deepresearch_stream_impl', new=AsyncMock()) as stream:
        result, _ = await _invoke(materials_context=context)
    assert result['kind'] == 'error'
    assert 'fallback' not in result
    stream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('denial', ['file_permission', 'private_url', 'private_dns'])
async def test_material_access_denial_does_not_handoff(tmp_path, monkeypatch, denial):
    files = [{'path': str(tmp_path / 'protected.txt'), 'name': 'protected.txt', 'read_allowed': False}]
    query = '必须使用附件为核心材料研究主题'
    if denial != 'file_permission':
        files = []
        query = ('必须使用 http://127.0.0.1/source 为核心材料研究主题' if denial == 'private_url'
                 else '必须使用 https://private.example.org/source 为核心材料研究主题')
    if denial == 'private_dns':
        async def private_dns(*args, **kwargs):
            return [(2, 1, 6, '', ('10.0.0.1', 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', private_dns)
    context = {'original_query': query, 'files': files, 'scope': 'denied', 'output_dir': str(tmp_path)}
    with patch.object(de, '_call_deepresearch_stream_impl', new=AsyncMock()) as stream:
        result, _ = await _invoke(materials_context=context)
    assert 'fallback' not in result
    assert '安全限制拒绝' in result['content']
    stream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['document', 'snapshot'])
async def test_optional_denial_with_core_parse_failure_does_not_handoff(tmp_path, monkeypatch, failure):
    source = tmp_path / 'broken.docx'
    source.write_bytes(b'not a valid docx archive')
    if failure == 'snapshot':
        from jiuwenswarm.agents.harness.common.tools.deepresearch import materials as m
        monkeypatch.setattr(m, 'extract_attachment', AsyncMock(return_value='readable content'))
        create_snapshot = Mock(side_effect=OSError('disk full'))
        monkeypatch.setattr(m.tempfile, 'NamedTemporaryFile', create_snapshot)
    context = {'original_query': '必须使用 broken.docx 作为核心材料研究主题。另参考 http://127.0.0.1/source',
               'files': [{'path': str(source), 'name': source.name}],
               'scope': 'mixed-denied', 'output_dir': str(tmp_path)}
    with patch.object(de, '_call_deepresearch_stream_impl', new=AsyncMock()) as stream:
        result, _ = await _invoke(materials_context=context)
    if failure == 'snapshot':
        create_snapshot.assert_called_once()
    assert result['kind'] == 'error'
    assert result['error_code'] == 'materials_preparation_failed'
    assert 'fallback' not in result
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_engine_on_resume_does_not_start_second_research():
    state = {"schema_version": 1, "phase": "wait_feedback", "revision": 1,
             "query": "研究主题", "conversation_id": "existing"}
    result, _ = await _resume_unavailable_engine(state)
    assert "fallback" not in result
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)
    await rail.after_tool_call(ctx)
    assert len(ctx.force_finish_requests) == 1


async def _resume_unavailable_engine(state):
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(return_value=json.dumps({
        "status": "error", "error_code": "runner_missing", "error": "runner missing",
    }))) as stream:
        result, saved = await _invoke(state=state, user_input={"status": "answered", "answers": [
            {"question": "范围", "selected_options": ["全部"]},
        ]})
    assert stream.await_args.kwargs["action"] == "resume"
    return result, saved


@pytest.mark.asyncio
async def test_stopped_material_research_resumes_with_original_query_and_snapshot():
    from jiuwenswarm.agents.harness.common.tools.deepresearch.materials import MATERIALS_POLICY

    original = "参考附件研究青鹭项目"
    reference = {"path": "saved-materials.json", "sha256": "saved-digest"}
    state = {"schema_version": 1, "phase": "new", "revision": 1,
             "query": MATERIALS_POLICY + "\n" + original, "original_query": original,
             "conversation_id": "stopped-materials", "materials_prepared": True,
             "materials_reference": reference}
    with patch.object(de, "_probe_resumable_checkpoint", return_value={"stopped": True, "node": "info_collector"}), \
            patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(return_value={
                "status": "completed", "report_delivered": True})) as stream:
        result, _ = await _invoke(state=state, query=original)
    assert result["kind"] == "completed"
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "stopped"
    assert stream.await_args.kwargs["conversation_id"] == "stopped-materials"
    assert stream.await_args.kwargs["_materials_reference"] == reference
