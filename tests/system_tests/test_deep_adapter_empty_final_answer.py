"""empty_final_answer 标记的生产端（adapter）打标测试。

对应 cron unary 空文本分流（gateway/cron/scheduler.py _run_unary_cron_job）：
调度层消费 empty_final_answer 标记，本文件验证 adapter 生产端只在
「正向完成证据（chunk>0）+ 无正文 + 无错误」时打标——零 chunk 静默死亡
不打标（维持上层降级失败），dict 形错误事件不被静默丢弃。

harness 模式取自 tests/system_tests/test_a2x_client_runtime.py
（Fake DeepAgent + patch 组），同为 integration/system 层——真 create_instance
带全局副作用（cron 资源注册等），与 unit 层同进程混跑会交叉污染，故按仓库
惯例归入本层。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.system]


def _make_config() -> dict:
    return {
        "preferred_language": "zh",
        "react": {
            "agent_name": "main_agent",
            "workspace_dir": "/tmp/empty-final-answer-test-workspace",
            "enable_task_loop": True,
            "max_iterations": 3,
        },
        "permissions": {"enabled": True},
        "models": {
            "default": {
                "model_client_config": {
                    "api_key": "system-test-key",
                    "api_base": "http://fake.local/v1",
                }
            }
        },
    }


def _make_request() -> tuple[AgentRequest, dict]:
    query = "整理一份今日待办清单建议"
    channel = "web"
    language = "zh"
    request = AgentRequest(
        request_id="empty-final-answer-test-request",
        channel_id=channel,
        session_id="web_empty_final_answer_test",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": query, "mode": "agent.plan", "files": {}},
        is_stream=False,
        metadata={"source": "unit_test"},
    )
    built = build_user_prompt(query, files={}, channel=channel, language=language)
    inputs = {
        "conversation_id": request.session_id,
        "query": built.interaction_prefix + built.user_query + built.statusline_directive,
        "channel": channel,
        "language": language,
    }
    return request, inputs


def _mock_create_model(self, config: dict) -> MagicMock:
    """与 a2x harness 同款：真 ModelClientConfig 以过 _has_valid_model_config。"""
    fake_mcc = ModelClientConfig(
        client_provider="OpenAI",
        api_key="system-test-key",
        api_base="http://fake.local/v1",
    )
    fake_model = MagicMock()
    fake_model.model_client_config = fake_mcc
    fake_model.model_config = MagicMock()
    self._model = fake_model
    return fake_model


class _FakeAbilityManager:
    def __init__(self) -> None:
        self.cards: list = []

    def list(self) -> list:
        return list(self.cards)

    def add(self, card) -> None:
        self.cards.append(card)

    def remove(self, name: str) -> None:
        self.cards = [c for c in self.cards if getattr(c, "name", "") != name]


def _make_fake_agent(chunks: list) -> SimpleNamespace:
    class _FakeInteractionStream:
        def __init__(self, items: list) -> None:
            self._items = items

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for item in self._items:
                yield item

        async def close(self, *, abort_active_round: bool = False) -> None:
            return None

    return SimpleNamespace(
        card=SimpleNamespace(id="jiuwenswarm", name="main_agent"),
        ensure_initialized=AsyncMock(),
        start=AsyncMock(),
        attach_output=AsyncMock(return_value=_FakeInteractionStream(chunks)),
        send_input=AsyncMock(),
        goal_manager=None,
        ability_manager=_FakeAbilityManager(),
    )


async def _run_impl(chunks: list):
    """创建 adapter 并跑一轮非流式 chat.send，返回 AgentResponse。"""
    agent = _make_fake_agent(chunks)
    request, inputs = _make_request()

    with (
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "set_checkpoint", AsyncMock()),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter, "_refresh_multimodal_configs", return_value=None
        ),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_model", _mock_create_model),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_get_tool_cards", AsyncMock(return_value=[])),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_build_agent_rails", return_value=[]),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_sys_operation", return_value=MagicMock()),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter, "_build_configured_subagents", return_value=(None, False)
        ),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_update_runtime_config", AsyncMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "load_user_rails", AsyncMock()),
        patch.object(interface_module, "get_config", return_value=_make_config()),
        patch.object(interface_module, "init_permission_engine", return_value=None),
        patch.object(interface_module, "create_deep_agent", return_value=agent),
        patch.dict("os.environ", {"API_KEY": "system-test-key"}),
    ):
        adapter = JiuWenSwarmDeepAdapter()
        await adapter.create_instance()
        return await adapter.process_message_impl(request, inputs)


class TestEmptyFinalAnswerEmission:
    """adapter 生产端打标：只在「chunk>0 + 无正文 + 无错误」时带标记。"""

    async def test_typed_chunk_with_empty_content_marks_legitimate_empty(self):
        """有 chunk 产出（工具轮 llm_output 空正文）但无汇总文本 → 打标（合法空）。"""
        resp = await _run_impl([SimpleNamespace(type="llm_output", payload={"content": ""})])
        assert resp.ok is True
        assert resp.payload.get("content") == ""
        assert resp.payload.get("empty_final_answer") is True

    async def test_zero_chunks_does_not_mark(self):
        """零 chunk（静默死亡）→ 不打标，维持上层降级失败语义（771f6cec0）。"""
        resp = await _run_impl([])
        assert resp.ok is True
        assert resp.payload.get("content") == ""
        assert "empty_final_answer" not in resp.payload

    async def test_dict_form_error_is_not_dropped(self):
        """dict 形错误事件（无 type 属性的 chunk）须进 error_text → ok=False，不得打标。"""
        resp = await _run_impl([{"result_type": "error", "output": "金价查询服务不可用"}])
        assert resp.ok is False
        assert "金价查询服务不可用" in str(resp.payload.get("error", ""))
        assert "empty_final_answer" not in resp.payload
