"""Shared Core answer, summary protocol and voice receipt presentation."""

import re
from typing import Any

from .tasks.prompts import build_brief_prompt

MAX_REALTIME_BRIEF_CHARS = 180


def full_answer_language_rule() -> str:
    """Language for the visible Core Agent answer; same source as the spoken receipt."""
    from jiuwenswarm.extensions.video_duplex.backend import settings
    if settings.tool_language() == "en":
        return (
            "The complete answer before the markers must be written in English. "
            "Do not write that answer in Chinese. "
        )
    return "标记之前的完整答案必须使用简体中文。"


def core_agent_brief_protocol(nonce: str = "") -> str:
    from . import settings, joyai_provider
    begin, end = "[[JIUWEN_BRIEF_BEGIN]]", "[[JIUWEN_BRIEF_END]]"
    language_rule = joyai_provider.response_language_instruction(settings.tool_language())
    return (
        f"\n\n{full_answer_language_rule()}\n{language_rule}"
        + build_brief_prompt(begin, end, language_rule)
    )


def _result_kind(question: str, answer: str, tools_used: list[str]) -> str:
    combined_tools = " ".join(tools_used).casefold()
    combined_text = f"{question}\n{answer}".casefold()
    if re.search(r"search|fetch|browser|网页|搜索", combined_tools):
        return "research"
    if "```" in answer or re.search(
        r"\b(?:python|typescript|javascript|java|c\+\+|sql)\b|代码|程序|函数",
        combined_text,
    ):
        return "code"
    if re.search(r"计算|求解|等于|方程|积分|概率|\d+\s*[-+*/^]\s*\d+", combined_text):
        return "calculation"
    if re.search(r"file|glob|grep|document|pdf|文件|文档", combined_tools):
        return "file"
    if re.search(r"bash|powershell|write|edit|computer|执行|写入", combined_tools):
        return "action"
    return "generic"


def _safe_brief(value: str) -> str:
    brief = value.strip()
    if not brief or len(brief) > MAX_REALTIME_BRIEF_CHARS:
        return ""
    if "\n" in brief or "\r" in brief:
        return ""
    if re.search(r"https?://|www\.|```|`[^`]+`|\[\[JIUWEN_", brief, re.IGNORECASE):
        return ""
    if brief.startswith(("{", "[")) or re.search(
        r'"(?:status|result|answer|code)"\s*:', brief
    ):
        return ""
    return brief


def present_core_agent_result(
    raw_answer: str,
    *,
    nonce: str = "",
    question: str,
    tools_used: list[str] | None = None,
) -> dict[str, Any]:
    """Split Core Agent output into authoritative UI content and a safe spoken brief."""
    # Match only the current Core answer, never a marker identity from history.
    # Accept older tagged answers while existing model sessions are still warm.
    # A final answer may omit the closing marker; the same brief validation applies.
    pattern = re.compile(
        r"(?:\r?\n)*\[\[JIUWEN_BRIEF_BEGIN(?::[^\]\r\n]+)?\]\]\s*(.*?)\s*"
        r"(?:\[\[JIUWEN_BRIEF_END(?::[^\]\r\n]+)?\]\]|$)(?:\r?\n)*", re.DOTALL,
    )
    matches = list(pattern.finditer(raw_answer))
    match = matches[-1] if matches else None
    display_result = pattern.sub("\n", raw_answer).strip()
    if not display_result:
        display_result = raw_answer.strip()
    kind = _result_kind(question, display_result, tools_used or [])
    model_brief = _safe_brief(match.group(1)) if match else ""
    if model_brief:
        summary, source = model_brief, "core_agent"
    else:
        # Keep Core's actual outcome when its optional spoken summary is absent
        # or malformed. Never substitute a generic success claim.
        summary, source = display_result, "derived"
    return {
        "display_result": display_result,
        "realtime_brief": {
            "status": "completed",
            "result_kind": kind,
            "summary": summary,
            "displayed_in_ui": True,
            "response_mode": "brief",
            "source": source,
        },
    }


def receipt_payload(result: dict, *, question: str = "") -> dict:
    """Project native replies and stored task results onto one receipt contract."""
    content = str(result.get("display_result") or result.get("answer") or result.get("result") or "").strip()
    brief = result.get("realtime_brief")
    if brief and str(brief.get("summary") or "").strip():
        presented = {"display_result": content, "realtime_brief": brief}
    elif content:
        presented = present_core_agent_result(content, question=question)
    else:
        presented = {"display_result": "", "realtime_brief": None}
    return {"result": presented["display_result"], **presented}
