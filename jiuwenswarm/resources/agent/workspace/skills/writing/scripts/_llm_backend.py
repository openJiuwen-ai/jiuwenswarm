# -*- coding: utf-8 -*-
"""
_llm_backend.py — JiuwenBackend：把 facade.agent() / facade.human() 转发到真实 LLM 客户端

为什么需要这个类？
    openjiuwen/agent_teams/workflow/engine/runner.py:149 写死
    ``backend=backend or MockBackend()``，所以 ``run_workflow()`` 不传 backend 时
    **默认走 MockBackend**（返回 ``[mock:label] generated text {n}`` 假字符串），
    **完全不会读 .env 里的 API key**。

调用链（修复后）：
    scripts/main.py
        → run_workflow(..., backend=JiuwenBackend())
        → facade.agent() 在 runtime 里 → backend.run(prompt, opts, schema_json)
        → JiuwenBackend.run()
            → JiuwenSwarmChatClient.complete_json_async()
              （走用户级 ~/.jiuwenswarm/config/.env 凭证 + json_repair）
        → 返回 AgentResult 给 facade

    facade.human() 走 stateful session：
        → backend.open_session(kind="human", instructions=..., opts=...) 返 sid
        → backend.send_turn(sid, prompt, opts, schema_json, history=...)   返 AgentResult
        → backend.close_session(sid) 释放

设计要点（与 planning/scripts/_llm_backend.py 同构，不重复）：
    * 继承 ``AgentBackend``，重写 ``run()``（唯一抽象方法）+ session 四件套。
    * session 之间无状态共享，每个 ``send_turn`` 当成独立的单次 LLM 调起
      （no-human-review 模式下 human() 也走 LLM，绕开真人等）。
    * ``__init__`` 延迟 import ``jiuwenswarm.symphony.llm``：让 ``import scripts._llm_backend``
      不会因 .env 缺失而炸；实例化时才要求凭证。
    * token 数按 ``MockBackend._result`` 公式粗算，保证 ``bind_budget`` 后的 ledger 不卡 0。
    * 支持 ``schema_json`` 非 None 路径：把 text json.loads 成 dict 放 ``structured``
      字段，给 facade 拿去 Pydantic 校验。失败时回退 ``text`` 路径。
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from openjiuwen.agent_teams.workflow.engine.backends import AgentBackend, AgentResult


class JiuwenBackend(AgentBackend):
    """AgentBackend：转发到 ``JiuwenSwarmChatClient``（读取用户级配置凭证）。"""

    #: 框架白名单：登记 backend 接受的额外 opts 键。当前只用引擎自带
    #: ``label / phase / schema / model / timeout``，无需扩展。
    KNOWN_OPTIONS: frozenset[str] = frozenset()

    def __init__(self) -> None:
        super().__init__()
        # 延迟 import：避免 ``import scripts._llm_backend`` 在 .env 缺失时炸。
        # 实例化（main.py 里 ``JiuwenBackend()``）时如果 .env 没配会清晰抛错。
        from jiuwenswarm.symphony.llm import LLMConfig, create_llm_client
        self._client = create_llm_client(LLMConfig.from_default_model())
        # 会话表：sid → {"kind": str, "opts": dict}。每次 send_turn 合并
        # session 存下的 opts + 调用时 opts（后者优先），相当于"会话级默认 opts"。
        self._sessions: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 共享：单次 LLM 调起 + AgentResult 包装
    # ------------------------------------------------------------------

    async def _call_llm(
        self, prompt: str, opts: dict, schema_json: dict | None
    ) -> AgentResult:
        """``run()`` 和 ``send_turn()`` 共用的 LLM 调起 + 账本 + JSON 解析逻辑。"""
        label = opts.get("label") or "agent"
        # persona 已在 prompt 头部。schema 路径额外给模型一条不可歧义的 JSON
        # 指令；否则模型会把检查点问题当成普通咨询，返回 Markdown/列表，facade
        # 无法交给 Pydantic 校验。
        system_prompt = ""
        if schema_json is not None:
            system_prompt = (
                "Return exactly one valid JSON object matching the requested schema. "
                "Do not use Markdown fences, prose, or arrays at the top level."
            )
        text = await self._client.complete_json_async(
            system_prompt=system_prompt,
            user_content=prompt,
            error_context=f"JiuwenBackend[{label}]",
        )
        # 与 MockBackend._result 公式一致：(prompt + text) 字符数 / 4
        tokens = (len(prompt) + len(text)) // 4
        self.budget.add(tokens)

        if schema_json is not None:
            structured: Any = None
            try:
                structured = json.loads(text)
            except (ValueError, TypeError):
                # LLM 偶尔会返回非 JSON（例如自然语言解释）；让 facade 报错提示
                structured = None
            if structured is not None:
                return AgentResult(structured=structured, tokens=tokens)

        return AgentResult(text=text, tokens=tokens)

    # ------------------------------------------------------------------
    # 单次 agent() 入口
    # ------------------------------------------------------------------

    async def run(
        self, prompt: str, opts: dict, schema_json: dict | None
    ) -> AgentResult:
        """单次 agent() 调用的实现（facade.agent() 走这里）。"""
        return await self._call_llm(prompt, opts, schema_json)

    # ------------------------------------------------------------------
    # Stateful sessions（facade.human() / facade.agent_session() 走这里）
    # ------------------------------------------------------------------

    async def open_session(
        self, *, kind: str, instructions: str | None, opts: dict
    ) -> str:
        sid = f"jiuwen-sess-{len(self._sessions) + 1}"
        self._sessions[sid] = {"kind": kind, "opts": dict(opts or {})}
        return sid

    async def send_turn(
        self,
        session_id: str,
        prompt: str,
        opts: dict,
        schema_json: dict | None,
        *,
        history: Sequence[dict] = (),
        correlation_id: str | None = None,
    ) -> AgentResult:
        # 合并 session 存下的 opts + 调用时 opts（调用时优先）
        sess = self._sessions.get(session_id, {})
        merged_opts = {**sess.get("opts", {}), **(opts or {})}
        # session 间无状态：每 turn 独立 LLM 调起（no-human-review 模式下
        # 真人不会答，只能 LLM 兜底；正常 human 模式由 facade 把 turn 派发到
        # 真人通道，引擎拿到响应后可能不再 send_turn）。
        return await self._call_llm(prompt, merged_opts, schema_json)

    async def close_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def aclose(self) -> None:
        self._sessions.clear()
