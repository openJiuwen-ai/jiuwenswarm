# -*- coding: utf-8 -*-
"""
_llm_backend.py — JiuwenBackend：把 facade.agent() / facade.human() 转发到真实 LLM 客户端

为什么需要这个类？
    openjiuwen/agent_teams/workflow/engine/runner.py:149 写死
    ``backend=backend or MockBackend()``，所以 ``run_workflow()`` 不传 backend 时
    **默认走 MockBackend**（返回 ``[mock:label] generated text {n}`` 假字符串），
    **完全不会读 .env 里的 API key**。这是 planning skill 2026-08-27 22:30
    真打 LLM 才暴露的坑。

调用链（修复后）：
    scripts/main.py
        → run_workflow(..., backend=JiuwenBackend())
        → facade.agent() 在 runtime 里 → backend.run(prompt, opts, schema_json)
        → JiuwenBackend.run()
            → JiuwenSwarmChatClient.complete_json_async()   (走 .env 凭证 + json_repair)
        → 返回 AgentResult 给 facade

    facade.human()（planning skill 4 个检查点 + no-human-review 模式下仍触发）
    走 stateful session：
        → backend.open_session(kind="human", instructions=..., opts=...) 返 sid
        → backend.send_turn(sid, prompt, opts, schema_json, history=...)   返 AgentResult
        → backend.close_session(sid) 释放

设计要点：
    * 继承 ``AgentBackend``，重写 ``run()``（唯一抽象方法）+ session 四件套。
    * session 之间无状态共享，每个 ``send_turn`` 当成独立的单次 LLM 调起
      （no-human-review 模式下 human() 也走 LLM，绕开真人等）。
    * ``__init__`` 延迟 import ``jiuwenswarm.symphony.llm``：让 ``import scripts._llm_backend``
      不会因 .env 缺失而炸；实例化时才要求凭证。
    * token 数按 ``MockBackend._result`` 公式粗算（见 .venv/.../backends/mock.py:237,239），
      保证 ``bind_budget`` 后的 ledger 不卡 0。**真实 token** 走 jiuwenswarm 内部
      ``_TOKEN_USAGE_TRACKER``（``get_llm_token_usage_summary()``），是另一份账。
    * 支持 ``schema_json`` 非 None 路径：把 text json.loads 成 dict 放 ``structured``
      字段，给 facade 拿去 Pydantic 校验。失败时回退 ``text`` 路径，让 facade 报
      JSON 解析错。当前 ``_subagent.py`` 走 ``schema=None``，本路径暂未实战。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Sequence

from openjiuwen.agent_teams.workflow.engine.backends import AgentBackend, AgentResult

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.llm_backend")


class JiuwenBackend(AgentBackend):
    """AgentBackend 实现：转发到 ``JiuwenSwarmChatClient``（自动读 .env 凭证）。"""

    #: 框架白名单：登记 backend 接受的额外 opts 键。当前只用引擎自带
    #: ``label / phase / schema / model / timeout``，无需扩展。
    KNOWN_OPTIONS: frozenset[str] = frozenset()

    def __init__(self) -> None:
        super().__init__()
        # 延迟 import：避免 ``import scripts._llm_backend`` 在 .env 缺失时炸。
        # 实例化（main.py 里 ``JiuwenBackend()``）时如果 .env 没配会清晰抛错。
        from jiuwenswarm.symphony.llm import LLMConfig, create_llm_client
        self._client = create_llm_client(LLMConfig.from_default_model())
        self._member_counter = 0
        # 会话表：sid → {"kind": str, "opts": dict}。每次 send_turn 合并
        # session 存下的 opts + 调用时 opts（后者优先），相当于"会话级默认 opts"。
        self._sessions: dict[str, dict] = {}

    async def ensure_member_name(self, *, kind: str, opts: dict) -> str:
        """Reserve a stable member name required by newer workflow engines."""
        self._member_counter += 1
        label = str((opts or {}).get("label") or kind or "agent")
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)
        return f"planning-{safe_label[:48]}-{self._member_counter}"

    # ------------------------------------------------------------------
    # 共享：单次 LLM 调起 + AgentResult 包装
    # ------------------------------------------------------------------

    async def _call_llm(
        self, prompt: str, opts: dict, schema_json: dict | None
    ) -> AgentResult:
        """``run()`` 和 ``send_turn()`` 共用的 LLM 调起 + 账本 + JSON 解析逻辑。"""
        label = opts.get("label") or "agent"
        # persona 已在 prompt 头部，system_prompt 留空避免重复指令。
        # 双重 timeout 兜底(2026-09-08 hang 修复)：
        #   1) timeout=300 透传到 openjiuwen OpenAIModelClient.invoke()，覆盖 yaml
        #      model_client_config.timeout: 360 的 client-level timeout，更精确。
        #   2) asyncio.timeout(300) 是 Python 层硬兜底，即使 openjiuwen 共享 httpx
        #      连接池的 keep-alive 死连接被复用(实测 0.7.0 共享池 keepalive_expiry=60s,
        #      max_keepalive_connections=20)，也能强制 300s 抛 TimeoutError，不会
        #      复现 9.7 paper-gen 端到端 planning round 1 method-designer hang 12+ 分钟。
        # 实测最大 47.8s(7 月 9 日 Layer A retry)留 6x 余量；yaml 360s 做最后防线。
        try:
            async with asyncio.timeout(300):
                text = await self._client.complete_json_async(
                    system_prompt="",
                    user_content=prompt,
                    timeout=300,
                    error_context=f"JiuwenBackend[{label}]",
                )
        except (asyncio.TimeoutError, TimeoutError):
            log.warning(
                f"[JiuwenBackend:{label}] LLM call 超时 300s,"
                f" prompt={len(prompt)} chars, raise to retry/fail-fast"
            )
            raise
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
        self,
        *,
        kind: str,
        instructions: str | None,
        opts: dict,
        fork_data: dict | None = None,
        member_name: str | None = None,
    ) -> str:
        sid = f"jiuwen-sess-{len(self._sessions) + 1}"
        # kind 记在 session 表里；send_turn 看到 kind=="human" 走真人通道。
        self._sessions[sid] = {
            "kind": kind,
            "opts": dict(opts or {}),
            "member_name": member_name,
            "fork_data": fork_data,
        }
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
        # 真人 session 走阻塞 stdin 通道（不调 LLM、不写 budget、不强制 JSON）
        if sess.get("kind") == "human":
            text = self._prompt_human(prompt)
            tokens = len(text) // 4  # 与 _call_llm 公式对齐
            return AgentResult(text=text, tokens=tokens)
        # session 间无状态：每 turn 独立 LLM 调起（no-human-review 模式下
        # 真人不会答，只能 LLM 兜底；正常 human 模式由 facade 把 turn 派发到
        # 真人通道，引擎拿到响应后可能不再 send_turn）。
        return await self._call_llm(prompt, merged_opts, schema_json)

    async def close_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def aclose(self) -> None:
        self._sessions.clear()

    # ------------------------------------------------------------------
    # 真人通道（仅供本 backend 内部 send_turn 调；UI 层应通过 facade.human() 走）
    # ------------------------------------------------------------------

    def _prompt_human(self, prompt: str) -> str:
        """阻塞 stdin 拿真人输入（raw str，不强制 JSON、不调 LLM、不写 budget）。

        异常处理：
            EOFError（stdin 关闭）：返空串；上层 facade 看到 text="" 通常会 fallback
            KeyboardInterrupt（Ctrl+C）：抛出去让 CLI exit code 正常。

        Args:
            prompt: facade 拼好的人审 prompt（含检查点摘要 + 关注点）。

        Returns:
            用户输入的 raw 字符串（去首尾空白）。
        """
        # 把 prompt 单独打到 stderr，让用户先看检查点摘要
        print(prompt, file=__import__("sys").stderr)
        try:
            raw = input("> ")
        except EOFError:
            return ""
        return raw.strip()
