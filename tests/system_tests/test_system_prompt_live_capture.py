#!/usr/bin/env python3
"""System test: live capture the final system prompt sent to the LLM.

Walks the full runtime path:
    create_instance() → process_message_impl() → Runner.run_agent()
    → rails before_model_call → LLM_INPUT callback captures messages

Reads real ~/.jiuwenswarm/config/config.yaml for model configuration.
Outputs:
    tests/system_tests/output/system_prompt_live_capture.json
    tests/system_tests/output/system_prompt_live_capture.txt
"""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import LLMCallEvents

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod

OUTPUT_DIR = Path(__file__).parent / "output"
logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        return str(value)


def _extract_system_messages(messages: list[dict[str, Any]]) -> list[str]:
    results: list[str] = []
    for item in messages:
        if item.get("role") != "system":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            results.append(content)
        else:
            results.append(json.dumps(content, ensure_ascii=False, indent=2))
    return results


def _sync_prompt_workspace_templates(language: str) -> None:
    """Refresh prompt-facing workspace assets in temporary HOME only."""
    home_dir = Path.home()
    if not str(home_dir).startswith("/tmp/"):
        return

    workspace_dir = home_dir / ".jiuwenswarm" / "agent" / "workspace"
    if not workspace_dir.exists():
        return

    template_root = (
        Path(__file__).resolve().parents[2]
        / "jiuwenswarm"
        / "resources"
        / "agent"
        / "workspace"
    )
    suffix = "ZH" if language == "zh" else "EN"
    file_map = [
        (f"AGENT_{suffix}.md", "AGENT.md"),
        (f"SOUL_{suffix}.md", "SOUL.md"),
        (f"HEARTBEAT_{suffix}.md", "HEARTBEAT.md"),
        (f"IDENTITY_{suffix}.md", "IDENTITY.md"),
        (f"memory/MEMORY_{suffix}.md", "memory/MEMORY.md"),
    ]

    for src_rel, dst_rel in file_map:
        src_path = template_root / src_rel
        dst_path = workspace_dir / dst_rel
        if not src_path.exists():
            continue
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)

    skills_src = template_root / "skills"
    skills_dst = workspace_dir / "skills"
    if skills_src.exists():
        shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)

    skills_state_src = template_root.parent.parent / "skills_state.json"
    if skills_state_src.exists():
        skills_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skills_state_src, skills_dst / "skills_state.json")


class PromptCapture:
    """Callback handler that captures LLM_INPUT events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def on_llm_input(
        self,
        *,
        model_name: str | None = None,
        model_provider: Any = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: Any = None,
        top_p: Any = None,
        max_tokens: Any = None,
    ) -> None:
        payload_messages = _json_safe(messages or [])
        self.events.append(
            {
                "model_name": model_name,
                "model_provider": str(model_provider) if model_provider is not None else None,
                "messages": payload_messages,
                "system_messages": _extract_system_messages(payload_messages),
                "tools": _json_safe(tools or []),
                "tool_count": len(tools or []),
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
            }
        )


@pytest.fixture()
async def prompt_capture():
    """Register LLM_INPUT callback, yield capture, then clean up."""
    capture = PromptCapture()
    callback = capture.on_llm_input
    Runner.callback_framework.register_sync(
        LLMCallEvents.LLM_INPUT,
        callback,
        namespace="system_test_prompt_capture",
        priority=1000,
    )
    yield capture
    await Runner.callback_framework.unregister(LLMCallEvents.LLM_INPUT, callback)


def _write_outputs(result: dict[str, Any], *, stem: str = "system_prompt_live_capture") -> None:
    """Write JSON and plain-text output files."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    json_path = OUTPUT_DIR / f"{stem}.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    txt_path = OUTPUT_DIR / f"{stem}.txt"
    lines: list[str] = []
    events = result.get("captured_events") or []
    for i, event in enumerate(events, 1):
        lines.append(f"{'=' * 80}")
        lines.append(f"Model Call #{i}")
        lines.append(f"Model: {event.get('model_name')}")
        lines.append(f"Provider: {event.get('model_provider')}")
        lines.append(f"Tool count: {event.get('tool_count', 0)}")
        lines.append(f"{'=' * 80}")
        lines.append("")
        for j, sys_msg in enumerate(event.get("system_messages") or [], 1):
            lines.append(f"--- System Message #{j} ---")
            lines.append(sys_msg)
            lines.append("")
    txt_path.write_text("\n".join(lines), encoding="utf-8")


async def _run_live_capture(
    prompt_capture: PromptCapture,
    *,
    mode: str,
    skill_mode: str,
    output_stem: str,
) -> tuple[dict[str, Any], str]:
    """Live-capture the full system prompt through the real runtime path."""
    config_base = deepcopy(get_config())
    config_base.setdefault("react", {})["skill_mode"] = skill_mode

    query = "只回复 PONG。不要调用任何工具。不要解释。"
    lang = config_base.get("preferred_language", "zh")
    _sync_prompt_workspace_templates(lang)
    channel = "web"
    session_id = f"{channel}_prompt_capture_{uuid.uuid4().hex[:8]}"
    request_id = f"prompt-capture-{uuid.uuid4().hex[:8]}"

    request = AgentRequest(
        request_id=request_id,
        channel_id=channel,
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": query, "mode": mode, "files": {}},
        is_stream=False,
        metadata={"source": f"system_test_live_capture_{skill_mode}"},
    )

    inputs = {
        "conversation_id": session_id,
        "query": build_user_prompt(query, files={}, channel=channel, language=lang),
        "channel": channel,
        "language": lang,
    }

    result: dict[str, Any] = {
        "request": {
            "request_id": request_id,
            "session_id": session_id,
            "channel_id": channel,
            "mode": mode,
            "lang": lang,
            "query": query,
            "skill_mode": skill_mode,
        },
        "captured_events": [],
        "response": None,
        "error": None,
    }

    async def _noop_checkpoint():
        pass

    async def _noop_load_user_rails(self_):
        pass

    with mock.patch.object(
        JiuWenSwarmDeepAdapter, "set_checkpoint", staticmethod(_noop_checkpoint)
    ), mock.patch.object(
        JiuWenSwarmDeepAdapter, "load_user_rails", _noop_load_user_rails
    ), mock.patch.dict(
        "os.environ", {"BROWSER_RUNTIME_MCP_ENABLED": "true"}
    ), mock.patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config",
        return_value=config_base,
    ):
        adapter = JiuWenSwarmDeepAdapter()
        try:
            await adapter.create_instance()
            response = await adapter.process_message_impl(request, inputs)
            result["response"] = {
                "ok": response.ok,
                "payload": _json_safe(response.payload),
            }
        except Exception as exc:
            result["error"] = repr(exc)

    result["captured_events"] = prompt_capture.events
    _write_outputs(result, stem=output_stem)

    assert result["error"] is None, f"运行时出错: {result['error']}"
    assert len(prompt_capture.events) >= 1, "未捕获到任何 LLM_INPUT 事件"
    first_event = prompt_capture.events[0]
    assert len(first_event["system_messages"]) > 0, "system messages 为空"
    first_system_prompt = "\n\n".join(first_event["system_messages"])
    return result, first_system_prompt


def _assert_common_prompt_structure(first_system_prompt: str) -> None:
    assert "# 安全原则" in first_system_prompt
    assert "# 可用工具" in first_system_prompt
    assert "- bash: 执行 Shell 命令" in first_system_prompt
    assert "- code: 执行 Python 或 JavaScript 代码" in first_system_prompt
    assert "## bash 使用原则" in first_system_prompt
    assert "## task_tool 使用原则" in first_system_prompt
    assert "## task_tool 用于创建临时子代理" not in first_system_prompt
    assert "- bash / code:" not in first_system_prompt
    assert '- "code_agent":' in first_system_prompt
    assert '- "research_agent":' in first_system_prompt
    assert '- "browser_agent":' in first_system_prompt
    assert "# 技能" in first_system_prompt
    assert "# 持久化存储体系" in first_system_prompt
    assert "# 消息说明" in first_system_prompt
    assert "# 工作空间" in first_system_prompt
    assert "# 项目上下文" in first_system_prompt
    assert "使用 todo 工具（todo_create、todo_modify、todo_list）" in first_system_prompt
    assert "# 上下文压缩" in first_system_prompt
    assert "# 当前日期与时间" in first_system_prompt
    assert "# 运行时" in first_system_prompt


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM_TESTS", "").lower() not in ("1", "true", "yes"),
    reason="需要真实大模型，设置 RUN_LIVE_LLM_TESTS=1 手动运行",
)
async def test_live_capture_system_prompt(prompt_capture: PromptCapture):
    """Live-capture the full system prompt for explicit auto_list mode."""
    result, first_system_prompt = await _run_live_capture(
        prompt_capture,
        mode="agent.plan",
        skill_mode="auto_list",
        output_stem="system_prompt_live_capture",
    )

    _assert_common_prompt_structure(first_system_prompt)
    assert result["request"]["mode"] == "agent.plan"
    assert result["request"]["skill_mode"] == "auto_list"
    assert "- list_skill: 列出可用技能" in first_system_prompt
    assert "需要时先调用 list_skill 查看可用技能" in first_system_prompt

    logger.info("捕获到 %d 个 model call 事件", len(prompt_capture.events))
    logger.info(
        "第一个事件的 system message 长度: %d 字符",
        sum(len(m) for m in prompt_capture.events[0]["system_messages"]),
    )
    logger.info("输出文件: %s", OUTPUT_DIR / "system_prompt_live_capture.txt")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM_TESTS", "").lower() not in ("1", "true", "yes"),
    reason="需要真实大模型，设置 RUN_LIVE_LLM_TESTS=1 手动运行",
)
async def test_live_capture_system_prompt_skill_mode_all(prompt_capture: PromptCapture):
    """Live-capture the full system prompt for explicit all mode."""
    result, first_system_prompt = await _run_live_capture(
        prompt_capture,
        mode="agent.plan",
        skill_mode="all",
        output_stem="system_prompt_live_capture_skill_mode_all",
    )

    _assert_common_prompt_structure(first_system_prompt)
    assert result["request"]["mode"] == "agent.plan"
    assert result["request"]["skill_mode"] == "all"
    assert "- list_skill: 列出可用技能" not in first_system_prompt
    assert "需要时先调用 list_skill 查看可用技能" not in first_system_prompt
    assert "执行前先用 read_file 阅读相关 SKILL.md。" in first_system_prompt
    assert "可用技能：" in first_system_prompt
    assert re.search(r"\n\d+\.\s+.+?: .+", first_system_prompt)
    assert "Path: " in first_system_prompt

    logger.info("捕获到 %d 个 model call 事件", len(prompt_capture.events))
    logger.info(
        "第一个事件的 system message 长度: %d 字符",
        sum(len(m) for m in prompt_capture.events[0]["system_messages"]),
    )
    logger.info(
        "输出文件: %s",
        OUTPUT_DIR / "system_prompt_live_capture_skill_mode_all.txt",
    )
