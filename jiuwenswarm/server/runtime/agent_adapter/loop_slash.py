# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""/loop 斜杠命令：服务端文本解析 + 流式适配器。

链路（复用 /goal 的文本解析模式，Gateway/前端零改动）：

    CLI/TUI 输入 "/loop [--verify '命令'] [--max-iterations N] 任务文本"
      └─ 未注册斜杠 → Gateway 不拦截，chat.send 原样透传 AgentServer
          └─ interface_deep._handle_slash_command 调 parse_loop_slash 命中
              └─ 流式分支调 run_loop_stream：await LoopEngine.run()（分钟级，
                 外层靠 AgentServer keepalive 心跳保活）→ 过程事件后置回放
                 → 终局 chat.final 汇总

事件复用既有 chat.* 类型，TUI/Web 前端零改动即可渲染进度与结果。
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator, Callable

from jiuwenswarm.channels.loop_cli.app import LoopEngine, LoopOptions
from jiuwenswarm.common.schema.agent import AgentResponseChunk

LOOP_SLASH_PREFIX = "/loop"

# 可选参数语法：--verify "bash xxx" / --verify 'bash xxx'、--max-iterations N
_LOOP_VERIFY_RE = re.compile(r"""--verify\s+(?:"([^"]+)"|'([^']+)')""")
_LOOP_MAX_IT_RE = re.compile(r"--max-iterations\s+(\d+)")


def parse_loop_slash(query: str) -> dict[str, Any] | None:
    """解析 ``/loop [--verify 命令] [--max-iterations N] 任务文本``。

    返回 ``{"result_type": "loop_stream", ...}``；非 /loop 命令返回 None
    （继续按普通消息处理）。
    """
    text = (query or "").strip()
    # token 边界：仅 "/loop" 或 "/loop ..." 命中，避免 "/loops"/"/loopback"
    # 这类以 /loop 为前缀的普通文本被误触发
    if text == LOOP_SLASH_PREFIX:
        return None
    if not text.startswith(LOOP_SLASH_PREFIX + " "):
        return None
    rest = text[len(LOOP_SLASH_PREFIX):].strip()
    if not rest:
        return None

    verify_cmd = None
    m = _LOOP_VERIFY_RE.search(rest)
    if m:
        verify_cmd = next(g for g in m.groups() if g is not None)
        rest = (rest[:m.start()] + " " + rest[m.end():]).strip()

    max_iterations = 3
    m = _LOOP_MAX_IT_RE.search(rest)
    if m:
        max_iterations = max(1, int(m.group(1)))
        rest = (rest[:m.start()] + " " + rest[m.end():]).strip()

    if not rest:
        return None
    return {
        "result_type": "loop_stream",
        "task": rest,
        "verify_cmd": verify_cmd,
        "max_iterations": max_iterations,
    }


def _text_chunk(rid: str, cid: str, content: str) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=rid, channel_id=cid,
        payload={"event_type": "chat.delta", "content": content},
        is_complete=False,
    )


async def run_loop_stream(
    *, request: Any, log: Callable[..., None] | None = None,
) -> AsyncIterator[AgentResponseChunk]:
    """执行 Loop Engineering 编排并产出 chat.* 事件流。

    两阶段设计（规避外层流生命周期管理的边界情况）：
    1. 启动提示 delta（立即可见，长任务等待期间靠 AgentServer keepalive 保活）
    2. ``await engine.run()`` 整体执行；过程事件先入队缓存，结束后后置回放，
       最后以 chat.delta 输出终局汇总、chat.final 收口（is_complete=True）
    """
    rid = request.request_id
    cid = request.channel_id
    params = request.params if isinstance(request.params, dict) else {}
    query_text = str(params.get("query") or params.get("content") or "")
    parsed = parse_loop_slash(query_text)

    cwd = str(params.get("cwd") or params.get("project_dir") or ".")
    options = LoopOptions(
        task=str(parsed["task"]),
        cwd=cwd,
        project_dir=str(params.get("project_dir") or cwd),
        trusted_dirs=list(params.get("trusted_dirs") or [cwd]),
        verify_cmd=parsed.get("verify_cmd"),
        mode=str(params.get("mode") or "agent.code.normal"),
        max_iterations=int(parsed.get("max_iterations", 3)),
        channel_id=cid or "tui",
    )

    yield _text_chunk(
        rid, cid,
        "[loop] Loop Engineering 编排已启动：rubric 分解 → maker 执行 → "
        "机器验证 → 独立 grader 验收（数分钟，过程事件将在结束时回放）\n")

    # 过程事件缓存：engine 的 log 回调入队，maker 原始事件转文本摘要行
    events: list[str] = []

    def on_log(phase: str, **kw: Any) -> None:
        events.append("[loop·" + phase + "] " + " ".join(
            f"{k}={str(v)[:90]}" for k, v in kw.items()))

    def on_event(chunk: Any) -> None:
        try:
            payload = chunk.payload if chunk is not None else None
            if isinstance(payload, dict):
                ev = str(payload.get("event_type", ""))
                if ev == "chat.tool_call":
                    tc = payload.get("tool_call") or {}
                    events.append(f"[loop·maker_tool] {tc.get('name', '?')}")
                elif ev == "chat.tool_result":
                    events.append(
                        f"[loop·maker_tool_result] {payload.get('tool_name', '?')}")
                elif ev == "chat.error":
                    events.append(f"[loop·maker_error] {payload.get('error')}")
        except Exception:  # noqa: BLE001
            pass

    engine = LoopEngine(options, log=log or on_log, on_event=on_event)
    try:
        report = await engine.run()
    except Exception as exc:  # noqa: BLE001
        yield AgentResponseChunk(
            request_id=rid, channel_id=cid,
            payload={"event_type": "chat.error", "error": f"loop 编排失败: {exc}"},
            is_complete=True,
        )
        return

    # ── 过程事件后置回放（保留完整轨迹）────────────────────────────
    if events:
        yield _text_chunk(rid, cid, "\n".join(events) + "\n\n")

    # ── 终局：汇总报告 ─────────────────────────────────────────────
    rubric_lines = "\n".join(f"- {r}" for r in (report.rubric or []))
    summary = (
        f"Loop Engineering 完成\n\n"
        f"- 循环终态: {report.final}\n"
        f"- 机器验证: {'PASS' if report.verify_pass else 'FAIL'}\n"
        f"- 迭代轮数: {report.iterations}\n"
        f"- 耗时: {report.wall_seconds}s\n"
        f"- maker tokens: {report.maker_tokens}\n"
        f"- 状态文件: {report.state_path}\n\n"
        f"验收 rubric:\n{rubric_lines}\n"
    )
    yield _text_chunk(rid, cid, summary)
    yield AgentResponseChunk(
        request_id=rid, channel_id=cid,
        payload={"event_type": "chat.final", "content": summary},
        is_complete=True,
    )
