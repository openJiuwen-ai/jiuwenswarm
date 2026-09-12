# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""deepresearch_stream tool 的 chunk → RelayClawEventType frame 路由。

纯函数,无副作用:输入一条 deepsearch chunk(JSON dict)+ RouterState,
输出 0~N 个 send_push payload(event_type + task_id + task_content + ...)。
调用方(deepresearch_stream tool)负责把 payload 包成 msg 后 send_push。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 节点中文显示名。与 DeepResearchTaskManager.NODE_DISPLAY_INFO 保持一致
# (直接 import 类属性会触发 manager 模块重初始化,这里显式复制常量)。
NODE_DISPLAY_INFO: dict[str, tuple[str, str]] = {
    "intent_recognition": ("意图识别", "分析研究需求"),
    "question_generator": ("问题生成", "规划搜索问题"),
    "info_collector": ("信息收集", "检索网页信息"),
    "understanding_analyzer": ("理解分析", "综合分析检索结果"),
    "outline": ("大纲生成", "规划报告章节结构"),
    "brief_outline": ("精简大纲", "规划精简报告结构"),
    "brief_info_collector": ("报告级资料检索", "检索并评估全报告证据"),
    "brief_evidence_reviewer": ("证据审阅", "检查证据覆盖和缺口"),
    "brief_sub_reporter": ("精简章节撰写", "并行生成精简报告章节"),
    "brief_reporter": ("精简报告整合", "生成核心摘要"),
    "brief_mermaid_generator": ("图表生成", "生成精简报告图表"),
    "brief_source_tracer": ("溯源校验", "核查精简报告引用"),
    "brief_html_reporter": ("精简报告排版", "生成精简版 HTML 成品"),
    "plan_reasoning": ("规划调研", "为当前章节制定分步信息采集计划"),
    "collector_query_generation": ("生成检索词", "为当前章节生成搜索查询"),
    "collector_info_retrieval": ("资料检索", "并行检索网页和资料"),
    "collector_supervisor": ("采集评估", "判断当前资料是否充分"),
    "collector_summary": ("资料汇总", "整理本轮采集结果"),
    "sub_reporter": ("章节撰写", "生成章节内容"),
    "editor_team": ("报告撰写", "生成报告内容"),
    "reporter": ("报告整合", "整合最终报告"),
    "source_tracer": ("溯源校验", "核查报告事实陈述"),
    "source_tracer_infer": ("溯源推理", "推理校验事实链路"),
}

# 低价值节点,不推进度(与 manager _SKIP_PROGRESS_NODES 一致)
_SKIP_NODES = {"start", "end", "framework", "entry", "outline_interaction"}

_SECTION_PROCESS_NODES = {
    "plan_reasoning",
    "collector_query_generation",
    "collector_info_retrieval",
    "collector_supervisor",
    "collector_summary",
    "sub_reporter",
}
# Brief mode performs report-wide collection/review outside the per-section
# process branch.  Their ordinary ``content`` carries the evidence detail that
# users expect in the expanded reasoning view, not final report prose.
_BRIEF_PROCESS_CONTENT_NODES = {
    "brief_info_collector",
    "brief_evidence_reviewer",
}
BRIEF_FINAL_REPORT_NODES = frozenset({
    "brief_reporter",
    "brief_mermaid_generator",
    "brief_source_tracer",
    "brief_html_reporter",
})
FINAL_REPORT_NODES = frozenset({
    "reporter",
    "vlm_chart_generator",
    "source_tracer",
    "source_tracer_infer",
    *BRIEF_FINAL_REPORT_NODES,
})

_SUMMARY_RESPONSE_EVENT = "summary_response"
_SECTION_SUCCESS_MARKER = "SUCCESS"
_CONTROL_PROCESS_VALUES = {_SECTION_SUCCESS_MARKER, "ALL END", "SECTION END"}
_QUESTION_NODES = {"question_generator", "generate_questions"}
_MARKDOWN_ESCAPE_RE = re.compile(r"""([!"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])""")
_SAFE_HTTP_URL_RE = re.compile(r"https?://[^\x00-\x20\x7f<>]+", re.IGNORECASE)

ROUTER_INVALID_ERROR = "deepresearch_router_invalid"
ROUTER_LIMIT_ERROR = "deepresearch_router_limit_exceeded"
MAX_SECTION_COUNT = 256
MAX_STATE_ENTRIES = 256
MAX_IDENTIFIER_CHARS = 1024
MAX_CHUNK_TEXT_CHARS = 1_048_576
MAX_TERMINAL_RESULT_TEXT_CHARS = 16 * 1024 * 1024
MAX_ACCUMULATED_TEXT_CHARS = 1_048_576
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 16_384

DEEPRESEARCH_STAGES: tuple[str, ...] = (
    "研究主题澄清",
    "大纲生成",
    "并行调研与章节撰写",
    "报告交付",
)

_NODE_STAGE: dict[str, int] = {
    "intent_recognition": 1,
    "question_generator": 1,
    "generate_questions": 1,
    "feedback_handler": 1,
    "outline": 2,
    "outline_interaction": 2,
    "brief_outline": 2,
    "editor_team": 3,
    "reporter": 3,
    "vlm_chart_generator": 3,
    "source_tracer": 3,
    "source_tracer_infer": 3,
    "brief_info_collector": 3,
    "brief_evidence_reviewer": 3,
    "brief_sub_reporter": 3,
    **dict.fromkeys(FINAL_REPORT_NODES, 3),
}


@dataclass
class RouterState:
    """路由器跨 chunk 状态:追踪每个节点的 start/done + 累积 report + 捕获中断信息。"""

    active_nodes: dict[str, dict] = field(default_factory=dict)
    # report 累积(仅 reporter;report 不在 marker key 列表,必须累积)
    report_parts: list[str] = field(default_factory=list)
    # outline_interaction 的 interrupt marker 可能不带大纲正文。
    outline_parts: list[str] = field(default_factory=list)
    # 中断 chunk 捕获(interrupt chunk 先于 interrupted marker 到达)
    interrupt_node_id: str = ""
    interrupt_raw_prompt: str = ""
    interrupt_conversation_id: str = ""
    section_titles: dict[str, str] = field(default_factory=dict)
    authoritative_section_indices: set[str] = field(default_factory=set)
    started_section_indices: set[str] = field(default_factory=set)
    completed_section_indices: set[str] = field(default_factory=set)
    expected_section_total: int = 0
    final_report_started: bool = False
    final_report_completed: bool = False
    pending_final_report_frames: list[dict] = field(default_factory=list)
    question_parts: dict[str, list[str]] = field(default_factory=dict)
    question_order: list[str] = field(default_factory=list)
    current_stage: int = 0
    stages_completed: bool = False
    _accumulated_text_chars: int = field(init=False, repr=False)
    _pending_final_report_text_chars: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.authoritative_section_indices.update(self.section_titles)
        (
            self._accumulated_text_chars,
            self._pending_final_report_text_chars,
        ) = _initial_state_text_sizes(self)


def _limited_text(value: Any) -> str:
    text = _as_text(value)
    if len(text) > MAX_CHUNK_TEXT_CHARS:
        raise ValueError(ROUTER_LIMIT_ERROR)
    return text


def _validate_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_IDENTIFIER_CHARS:
        raise ValueError(ROUTER_LIMIT_ERROR)
    return text


def _validate_json_shape(
    value: Any, *, max_text_chars: int = MAX_CHUNK_TEXT_CHARS
) -> int:
    """Bound container traversal without recursion or copying caller data."""
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    nodes = 0
    text_chars = 0
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active.discard(id(current))
            continue
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError(ROUTER_LIMIT_ERROR)
        if isinstance(current, str):
            if len(current) > max_text_chars:
                raise ValueError(ROUTER_LIMIT_ERROR)
            text_chars += len(current)
            if text_chars > max_text_chars:
                raise ValueError(ROUTER_LIMIT_ERROR)
            continue
        if not isinstance(current, (dict, list, tuple)):
            continue
        identity = id(current)
        if identity in active:
            raise ValueError(ROUTER_LIMIT_ERROR)
        active.add(identity)
        stack.append((current, depth, True))
        if isinstance(current, dict):
            children: list[Any] = []
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ValueError(ROUTER_INVALID_ERROR)
                if len(key) > MAX_IDENTIFIER_CHARS:
                    raise ValueError(ROUTER_LIMIT_ERROR)
                children.append(item)
        else:
            children = list(current)
        for item in reversed(children):
            stack.append((item, depth + 1, False))
    return text_chars


def _add_bounded_text_size(total: int, value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError(ROUTER_INVALID_ERROR)
    size = len(value)
    if size > MAX_CHUNK_TEXT_CHARS or total > MAX_ACCUMULATED_TEXT_CHARS - size:
        raise ValueError(ROUTER_LIMIT_ERROR)
    return total + size


def _initial_state_text_sizes(state: RouterState) -> tuple[int, int]:
    """Validate caller-provided text state once and derive trusted counters."""
    text_part_limit = MAX_ACCUMULATED_TEXT_CHARS
    invalid_state_shape = (
        not isinstance(state.report_parts, list)
        or not isinstance(state.outline_parts, list)
        or not isinstance(state.question_parts, dict)
        or not isinstance(state.pending_final_report_frames, list)
        or len(state.report_parts) > text_part_limit
        or len(state.outline_parts) > text_part_limit
        or len(state.question_parts) > MAX_STATE_ENTRIES
        or len(state.pending_final_report_frames) > MAX_STATE_ENTRIES
    )
    if invalid_state_shape:
        raise ValueError(ROUTER_LIMIT_ERROR)

    total = 0
    for parts in (state.report_parts, state.outline_parts):
        for part in parts:
            total = _add_bounded_text_size(total, part)

    question_part_count = 0
    for message_id, parts in state.question_parts.items():
        _validate_identifier(message_id)
        if not isinstance(parts, list):
            raise ValueError(ROUTER_INVALID_ERROR)
        question_part_count += len(parts)
        if question_part_count > text_part_limit:
            raise ValueError(ROUTER_LIMIT_ERROR)
        for part in parts:
            total = _add_bounded_text_size(total, part)

    pending = 0
    for frame in state.pending_final_report_frames:
        if not isinstance(frame, dict):
            raise ValueError(ROUTER_INVALID_ERROR)
        content = frame.get("content", "")
        if not isinstance(content, str):
            raise ValueError(ROUTER_INVALID_ERROR)
        pending = _add_bounded_text_size(pending, content)
        total = _add_bounded_text_size(total, content)
    return total, pending


def _ensure_text_capacity(state: RouterState, size: int) -> None:
    accumulated_text_chars = getattr(state, "_accumulated_text_chars")
    if size < 0 or accumulated_text_chars > MAX_ACCUMULATED_TEXT_CHARS - size:
        raise ValueError(ROUTER_LIMIT_ERROR)


def _append_text_part(state: RouterState, parts: list[str], text: str) -> None:
    size = len(text)
    _ensure_text_capacity(state, size)
    parts.append(text)
    state._accumulated_text_chars += size


def _pending_frames_text_size(frames: list[dict]) -> int:
    total = 0
    for frame in frames:
        content = frame.get("content", "")
        if not isinstance(content, str):
            raise ValueError(ROUTER_INVALID_ERROR)
        total = _add_bounded_text_size(total, content)
    return total


def _extend_pending_final_report_frames(
    state: RouterState,
    frames: list[dict],
) -> None:
    if len(state.pending_final_report_frames) + len(frames) > MAX_STATE_ENTRIES:
        raise ValueError(ROUTER_LIMIT_ERROR)
    text_size = _pending_frames_text_size(frames)
    _ensure_text_capacity(state, text_size)
    state.pending_final_report_frames.extend(frames)
    state._pending_final_report_text_chars += text_size
    state._accumulated_text_chars += text_size


def _node_frames_for_chunk(
    state: RouterState,
    chunk: dict,
    *,
    key: str,
    stage: int,
    agent: str,
    display: tuple[str, str],
    event: str,
    content: Any,
) -> tuple[list[dict], bool, bool]:
    """Build node frames without mutating state so persistence can preflight."""
    node_state = state.active_nodes.get(key)
    starts_node = node_state is None
    completes_node = event == "done" and (
        node_state is None or not node_state["done"]
    )
    frames: list[dict] = []
    if starts_node and event != "done":
        frames.append(_node_reasoning(stage, agent, display, f"{display[0]}开始\n"))
    if agent in _BRIEF_PROCESS_CONTENT_NODES:
        process_parts = _raw_process_parts(chunk, content, agent=agent)
    else:
        reasoning = _chunk_reasoning_content(chunk, content)
        process_parts = [_as_text(reasoning)] if reasoning else []
    for process_content in process_parts:
        frames.append(_node_reasoning(stage, agent, display, process_content))
    if completes_node:
        frames.append(_node_reasoning(stage, agent, display, f"{display[0]}完成\n"))
    return frames, starts_node, completes_node


def _validate_router_input(
    chunk: dict,
    state: RouterState,
    *,
    terminal_result_content: bool = False,
) -> None:
    if not isinstance(chunk, dict):
        raise ValueError(ROUTER_INVALID_ERROR)
    for key in ("agent", "section_idx", "message_id", "conversation_id"):
        _validate_identifier(chunk.get(key, ""))
    section_title = chunk.get("section_title")
    if section_title is not None:
        _limited_text(section_title)
    incoming_text_chars = 0
    for key in ("content", "reasoning_content"):
        value = chunk.get(key)
        if value is not None and not (
            terminal_result_content and key == "content"
        ):
            incoming_text_chars += _validate_json_shape(value)
    if len(state.active_nodes) > MAX_STATE_ENTRIES:
        raise ValueError(ROUTER_LIMIT_ERROR)
    section_entries = set(state.section_titles)
    section_entries.update(state.authoritative_section_indices)
    section_entries.update(state.started_section_indices)
    section_entries.update(state.completed_section_indices)
    if len(section_entries) > MAX_SECTION_COUNT:
        raise ValueError(ROUTER_LIMIT_ERROR)
    if len(state.question_parts) > MAX_STATE_ENTRIES:
        raise ValueError(ROUTER_LIMIT_ERROR)
    if len(state.pending_final_report_frames) > MAX_STATE_ENTRIES:
        raise ValueError(ROUTER_LIMIT_ERROR)
    _ensure_text_capacity(state, incoming_text_chars)


def _node_key(agent: str, section_idx: str) -> str:
    return f"{section_idx}:{agent}" if section_idx and section_idx != "0" else agent


def _task_frame(event_type: str, agent: str, section_idx: str, display: tuple[str, str]) -> dict:
    task_id = f"dr_{section_idx}_{agent}" if section_idx != "0" else f"dr_{agent}"
    task_content = display[0] + (f" - {display[1]}" if display[1] else "")
    return {
        "event_type": event_type,
        "task_id": task_id,
        "task_content": task_content,
        "node_name": agent,
        "section_idx": section_idx,
        "display_name": display[0],
        "description": display[1],
    }


def _stage_child_reasoning(stage: int, agent: str, display: tuple[str, str], content: str) -> dict:
    task_content = display[0] + (f" - {display[1]}" if display[1] else "")
    return {
        "event_type": "chat.reasoning",
        "task_id": f"deepresearch_stage_{stage}",
        "task_content": task_content,
        "stream_source_id": f"dr_{agent}",
        "content": content,
    }


def _stage_snapshot_frames(state: RouterState) -> list[dict]:
    tasks = []
    for index, title in enumerate(DEEPRESEARCH_STAGES, start=1):
        if state.stages_completed or index < state.current_stage:
            status = "completed"
        elif index == state.current_stage:
            status = "in_progress"
        else:
            status = "pending"
        tasks.append({
            "task_id": f"deepresearch_stage_{index}",
            "task_content": title,
            "status": status,
        })

    completed = len(DEEPRESEARCH_STAGES) if state.stages_completed else state.current_stage - 1
    in_progress = 0 if state.stages_completed else 1
    return [
        {
            "event_type": "task.update",
            "tasks": tasks,
            "total_tasks": len(DEEPRESEARCH_STAGES),
            "completed_tasks": completed,
            "in_progress_tasks": in_progress,
            "pending_tasks": len(DEEPRESEARCH_STAGES) - completed - in_progress,
        }
    ]


def advance_stage(state: RouterState, stage: int, *, complete: bool = False) -> list[dict]:
    """Advance monotonically and emit every missing Stage-facing snapshot."""
    if complete:
        if state.stages_completed:
            return []
        state.current_stage = len(DEEPRESEARCH_STAGES)
        state.stages_completed = True
        return _stage_snapshot_frames(state)

    if state.stages_completed or stage <= state.current_stage:
        return []
    if stage < 1 or stage > len(DEEPRESEARCH_STAGES):
        raise ValueError(f"invalid deepresearch stage: {stage}")

    frames: list[dict] = []
    for next_stage in range(state.current_stage + 1, stage + 1):
        state.current_stage = next_stage
        frames.extend(_stage_snapshot_frames(state))
    return frames


def _as_text(val) -> str:
    """marker 透传字段转文本:str 直用;dict/list(JSON)转字符串供 agent 按 (1) §Stage3 规则解析。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(val)


def _as_json_object(val: Any) -> dict[str, Any] | None:
    if isinstance(val, dict):
        return val
    if not isinstance(val, str):
        return None
    try:
        parsed = json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _text_field(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _escape_markdown_text(value: str) -> str:
    """Keep untrusted display text inert inside the frontend Markdown renderer."""
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", value)


def _format_outline_markdown(data: dict[str, Any]) -> str | None:
    title = _text_field(data.get("title"))
    thought = _text_field(data.get("thought"))
    raw_sections = data.get("sections")
    if not title or not isinstance(raw_sections, list):
        return None

    sections: list[tuple[dict[str, Any], str]] = []
    for section in raw_sections:
        if not isinstance(section, dict):
            continue
        section_title = _text_field(section.get("title"))
        if section_title:
            sections.append((section, section_title))
    if not sections:
        return None

    blocks: list[str] = []
    blocks.append(f"### {_escape_markdown_text(title)}")
    if thought:
        blocks.append(f"规划思路：{_escape_markdown_text(thought)}")

    section_lines: list[str] = []
    for section_number, (section, section_title) in enumerate(sections, start=1):
        core_suffix = "（重点）" if section.get("is_core_section") is True else ""
        safe_section_title = _escape_markdown_text(section_title)
        section_lines.append(f"{section_number}. **{safe_section_title}{core_suffix}**")
        description = _text_field(section.get("description"))
        if description:
            section_lines.append(f"   {_escape_markdown_text(description)}")
    blocks.append("\n".join(section_lines))
    return "\n\n".join(blocks)


def _format_outline_card_markdown(data: dict[str, Any]) -> str | None:
    """Format outline JSON as a card-style markdown with ## 页面规划 and ### P{N}: entries.

    The （重点） suffix becomes part of the display title after the colon.
    """
    title = _text_field(data.get("title"))
    thought = _text_field(data.get("thought"))
    raw_sections = data.get("sections")
    if not title or not isinstance(raw_sections, list):
        return None

    sections: list[tuple[dict[str, Any], str]] = []
    for section in raw_sections:
        if not isinstance(section, dict):
            continue
        section_title = _text_field(section.get("title"))
        if section_title:
            sections.append((section, section_title))
    if not sections:
        return None

    blocks: list[str] = []
    blocks.append(f"# 大纲：{_escape_markdown_text(title)}")
    if thought:
        blocks.append(f"**研究思路**：{_escape_markdown_text(thought)}")

    section_lines: list[str] = ["## 页面规划"]
    for section_number, (section, section_title) in enumerate(sections, start=1):
        core_suffix = "（重点）" if section.get("is_core_section") is True else ""
        safe_section_title = _escape_markdown_text(section_title)
        section_lines.append(f"### P{section_number}: {safe_section_title}{core_suffix}")
    blocks.append("\n".join(section_lines))
    return "\n\n".join(blocks)


def _format_plan_markdown(data: dict[str, Any]) -> str | None:
    title = _text_field(data.get("title"))
    thought = _text_field(data.get("thought"))
    raw_steps = data.get("steps")
    if not title or (raw_steps is not None and not isinstance(raw_steps, list)):
        return None

    steps: list[tuple[dict[str, Any], str]] = []
    for step in raw_steps or []:
        if not isinstance(step, dict):
            continue
        step_title = _text_field(step.get("title"))
        if step_title:
            steps.append((step, step_title))
    completed = data.get("is_research_completed")
    if not steps and not thought and not isinstance(completed, bool):
        return None

    blocks: list[str] = []
    blocks.append(f"#### 调研计划：{_escape_markdown_text(title)}")
    if thought:
        blocks.append(f"调研思路：{_escape_markdown_text(thought)}")
    if isinstance(completed, bool):
        blocks.append("状态：资料已充分，准备撰写" if completed else "状态：继续调研")

    step_lines: list[str] = []
    for step_number, (step, step_title) in enumerate(steps, start=1):
        safe_step_title = _escape_markdown_text(step_title)
        step_lines.append(f"{step_number}. **{safe_step_title}**")
        description = _text_field(step.get("description"))
        if description:
            step_lines.append(f"   {_escape_markdown_text(description)}")
    if step_lines:
        blocks.append("\n".join(step_lines))
    return "\n\n".join(blocks)


def _format_retrieved_source_markdown(data: dict[str, Any]) -> str | None:
    title = _text_field(data.get("title"))
    url = _text_field(data.get("url"))
    raw_query = data.get("query")
    if not title or not url or not isinstance(raw_query, str):
        return None
    query = raw_query.strip()

    safe_title = _escape_markdown_text(title)
    safe_link_url = url if _SAFE_HTTP_URL_RE.fullmatch(url) else ""
    if safe_link_url:
        source = f"[{safe_title}](<{safe_link_url}>)"
    else:
        source = safe_title
    blocks = [f"发现资料：{source}"]
    if not safe_link_url:
        blocks.append(f"链接：{_escape_markdown_text(url)}")
    if query:
        blocks.append(f"检索词：{_escape_markdown_text(query)}")
    return "\n\n".join(blocks)


def _format_process_content(agent: str, value: Any) -> str:
    data = _as_json_object(value)
    if data is None:
        return _as_text(value)

    formatted: str | None = None
    if agent in {"outline", "brief_outline"}:
        formatted = _format_outline_markdown(data)
    elif agent == "plan_reasoning":
        formatted = _format_plan_markdown(data)
    elif agent == "collector_info_retrieval":
        formatted = _format_retrieved_source_markdown(data)
    return formatted if formatted is not None else _as_text(value)


def collected_questions(state: RouterState) -> str:
    """Return question-generator message fragments in first-seen message order."""
    return "".join(
        "".join(state.question_parts[message_id])
        for message_id in state.question_order
    ).strip()


def _remember_question_chunk(state: RouterState, chunk: dict, content: Any) -> None:
    if (
        str(chunk.get("agent", "")).strip() not in _QUESTION_NODES
        or str(chunk.get("message_type", "")).strip() != "message_chunk"
    ):
        return
    text = _as_text(content)
    if not text:
        return
    message_id = str(chunk.get("message_id", "")).strip() or "__default__"
    if message_id not in state.question_parts:
        if len(state.question_parts) >= MAX_STATE_ENTRIES:
            raise ValueError(ROUTER_LIMIT_ERROR)
        _ensure_text_capacity(state, len(text))
        state.question_parts[message_id] = [text]
        state.question_order.append(message_id)
        state._accumulated_text_chars += len(text)
        return
    _append_text_part(state, state.question_parts[message_id], text)


def _chunk_reasoning_content(chunk: dict, content: Any) -> Any:
    reasoning = chunk.get("reasoning_content")
    if reasoning or not isinstance(content, str):
        return reasoning
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return reasoning
    return parsed.get("reasoning_content") if isinstance(parsed, dict) else reasoning


def _raw_process_parts(
    chunk: dict,
    content: Any,
    *,
    agent: str,
    include_content: bool = True,
) -> list[str]:
    """Return process bodies with node-aware display formatting and lossless fallback."""
    parts: list[str] = []
    values = [_chunk_reasoning_content(chunk, content)]
    if include_content:
        values.append(content)
    for value in values:
        text = _format_process_content(agent, value)
        if not text or not text.strip() or text.strip() in _CONTROL_PROCESS_VALUES:
            continue
        if text not in parts:
            parts.append(text)
    return parts


def _remember_section_title(state: RouterState, section_idx: str, section_title: Any) -> str:
    section_title = _as_text(section_title).strip()
    if section_title and section_idx not in state.authoritative_section_indices:
        state.section_titles[section_idx] = section_title
    return state.section_titles.get(section_idx, "")


def _positive_int(value: Any, *, maximum: int | None = None) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    if maximum is not None and parsed > maximum:
        raise ValueError(ROUTER_LIMIT_ERROR)
    return parsed


def _section_reasoning(state: RouterState, chunk: dict, content: str) -> dict:
    section_idx = str(chunk.get("section_idx", "0")).strip() or "0"
    section_title = _remember_section_title(state, section_idx, chunk.get("section_title"))
    payload = {
        "event_type": "chat.reasoning",
        "task_id": "deepresearch_stage_3",
        "task_content": section_title or f"章节 {section_idx}",
        "stream_source_id": f"deepresearch_section_{section_idx}",
        "content": content,
    }
    task_index = _positive_int(section_idx, maximum=MAX_SECTION_COUNT)
    if task_index is not None:
        payload["task_index"] = task_index
    total_tasks = _positive_int(
        chunk.get("section_total"), maximum=MAX_SECTION_COUNT
    )
    if total_tasks is not None:
        payload["total_tasks"] = total_tasks
    return payload


def _section_boundary(state: RouterState, chunk: dict, event_type: str) -> dict:
    payload = _section_reasoning(state, chunk, "")
    payload["event_type"] = event_type
    payload.pop("content")
    return payload


def _remember_expected_sections(state: RouterState, chunk: dict) -> None:
    total = _positive_int(
        chunk.get("section_total"), maximum=MAX_SECTION_COUNT
    )
    if total is not None:
        section_idx = _positive_int(
            chunk.get("section_idx"), maximum=MAX_SECTION_COUNT
        )
        if section_idx is not None and section_idx > total:
            raise ValueError(ROUTER_INVALID_ERROR)
        state.expected_section_total = max(
            state.expected_section_total,
            total,
            len(state.authoritative_section_indices),
        )


def _all_sections_completed(state: RouterState) -> bool:
    total = max(
        state.expected_section_total,
        len(state.authoritative_section_indices),
    )
    if total > MAX_SECTION_COUNT:
        raise ValueError(ROUTER_LIMIT_ERROR)
    expected = set(state.authoritative_section_indices)
    if total:
        expected.update(str(index) for index in range(1, total + 1))
    return bool(expected) and expected.issubset(state.completed_section_indices)


def _can_start_final_report(state: RouterState, agent: str) -> bool:
    """Brief is report-wide; only Professional waits for per-section completion."""
    return agent in BRIEF_FINAL_REPORT_NODES or _all_sections_completed(state)


def _is_successful_workflow_end(chunk: dict, agent: str, event: str) -> bool:
    """Recognize the SDK EndNode result emitted after the workflow has finished."""
    if (
        agent != "end"
        or event != _SUMMARY_RESPONSE_EVENT
        or str(chunk.get("section_idx", "0")).strip() not in {"", "0"}
    ):
        return False
    content = chunk.get("content")
    _validate_json_shape(
        content,
        max_text_chars=MAX_TERMINAL_RESULT_TEXT_CHARS,
    )
    result = _as_json_object(content)
    if result is not None:
        _validate_json_shape(
            result,
            max_text_chars=MAX_TERMINAL_RESULT_TEXT_CHARS,
        )
    return bool(
        result
        and isinstance(result.get("response_content"), str)
        and result["response_content"].strip()
        and not result.get("exception_info")
    )


def _final_report_boundary(event_type: str) -> dict:
    return {
        "event_type": event_type,
        "task_id": "deepresearch_stage_4",
        "task_content": "最终报告处理",
        "stream_source_id": "deepresearch_final_report",
    }


def start_final_report_processing(state: RouterState) -> list[dict]:
    if state.final_report_started:
        return []
    state.final_report_started = True
    frames = advance_stage(state, 4)
    frames.append(_final_report_boundary("task.start"))
    frames.extend(state.pending_final_report_frames)
    state.pending_final_report_frames.clear()
    pending_text_chars = getattr(state, "_pending_final_report_text_chars")
    accumulated_text_chars = getattr(state, "_accumulated_text_chars")
    setattr(state, "_accumulated_text_chars", accumulated_text_chars - pending_text_chars)
    setattr(state, "_pending_final_report_text_chars", 0)
    return frames


def complete_final_report_processing(state: RouterState) -> list[dict]:
    if state.final_report_completed or not state.final_report_started:
        return []
    state.final_report_completed = True
    return [_final_report_boundary("task.complete")]


def _node_reasoning(
    stage: int,
    agent: str,
    display: tuple[str, str],
    content: str,
) -> dict:
    if agent not in FINAL_REPORT_NODES:
        return _stage_child_reasoning(stage, agent, display, content)
    return {
        "event_type": "chat.reasoning",
        "task_id": "deepresearch_stage_4",
        "task_content": "最终报告处理",
        "stream_source_id": "deepresearch_final_report",
        "content": content,
    }


def is_outline_status_placeholder(val: Any) -> bool:
    """Return whether SDK content is only the outline interaction status prompt."""
    text = _as_text(val).strip()
    return bool(re.fullmatch(r"round\s+\d+\s*:\s*waiting for user feedback\.?", text, re.IGNORECASE))


def build_interrupt_prompt(node_id: str, state: RouterState, marker: dict, query: str) -> str:
    """中断时按 node 拼交互内容进 outcome.prompt。

    marker 字段优先；outline/report 在 SDK marker 缺少正文时使用路由器累积内容。
    """
    if node_id == "feedback_handler":
        ctx = (
            _as_text(marker.get("prompt"))
            or _as_text(marker.get("content"))
            or _as_text(marker.get("questions"))
            or query
        )
        raw = _as_text(marker.get("prompt")) or state.interrupt_raw_prompt or ""
    elif node_id == "outline_interaction":
        # (1) §Stage3(b) 读 marker.content(JSON 解析为 OutlineContent),其次 marker.outline
        marker_content = _as_text(marker.get("content"))
        if is_outline_status_placeholder(marker_content):
            marker_content = ""
        ctx = (
            marker_content
            or _as_text(marker.get("outline"))
            or "".join(state.outline_parts)
            or query
        )
        raw = state.interrupt_raw_prompt or ""
        if is_outline_status_placeholder(raw):
            raw = ""
    elif node_id == "user_feedback_processor":
        rpt = "".join(state.report_parts)  # 报告不在 marker,必须累积
        ctx = rpt[:6000] + ("…\n(完整报告见最终产物)" if len(rpt) > 6000 else "")
        raw = state.interrupt_raw_prompt or ""
    else:
        ctx, raw = "", state.interrupt_raw_prompt or ""
    if ctx.strip():
        return f"{ctx.strip()}\n\n{raw.strip()}".strip() if raw.strip() else ctx.strip()
    return raw or "请输入反馈"


def route_chunk(chunk: dict, state: RouterState) -> list[dict]:
    """把一条 deepsearch chunk 路由成 0~N 个 send_push payload。

    职责:① 累积 report 和 outline,为不含正文的 interrupt marker 提供兜底;
         ② 捕获 interrupt chunk 的 node_id/raw_prompt 进 state(interrupt chunk 先于
            interrupted marker 到达,marker 到达时 tool 调 build_interrupt_prompt);
         ③ outline 正文和并行章节未经压缩的原始过程写入 chat.reasoning。
    status marker(__deepsearch_status__)不在此处理(由 tool 主循环识别);interrupted marker
    本体由 tool 主循环捕获后整块传给 build_interrupt_prompt(marker 参数)。
    """
    if not isinstance(chunk, dict):
        raise ValueError(ROUTER_INVALID_ERROR)
    agent = _validate_identifier(chunk.get("agent", ""))
    event = str(chunk.get("event", "")).strip()
    section_idx = _validate_identifier(chunk.get("section_idx", "0")) or "0"
    # The EndNode embeds the complete final result in ``content`` (1+ MiB).
    # It is a workflow boundary, not process text for the frontend, so keep it
    # under the terminal 16 MiB protocol bound without applying the 1 MiB
    # process-display accumulator limit — even when the result carries
    # non-fatal exception_info (the SDK then emits event=error).  Delivery is
    # still gated by _is_successful_workflow_end below.
    is_end_node = (
        agent == "end"
        and section_idx in {"", "0"}
    )
    successful_workflow_end = _is_successful_workflow_end(chunk, agent, event)

    _validate_router_input(
        chunk,
        state,
        terminal_result_content=is_end_node,
    )
    frames: list[dict] = []
    if "__deepsearch_status__" in chunk:
        return frames
    if successful_workflow_end:
        return start_final_report_processing(state)

    content = chunk.get("content", "")
    message_type = str(chunk.get("message_type", "")).strip()
    target_stage = 3 if agent in _SECTION_PROCESS_NODES and section_idx != "0" else _NODE_STAGE.get(agent)
    key = _node_key(agent, section_idx)
    display = NODE_DISPLAY_INFO.get(agent)
    active_node_limit_reached = (
        display
        and agent not in _SKIP_NODES
        and key not in state.active_nodes
        and len(state.active_nodes) >= MAX_STATE_ENTRIES
    )
    if active_node_limit_reached:
        raise ValueError(ROUTER_LIMIT_ERROR)

    preflight_node_frames: tuple[list[dict], bool, bool] | None = None
    must_defer_final_report = (
        display
        and target_stage in {1, 2, 3}
        and agent in FINAL_REPORT_NODES
        and not state.final_report_started
        and not _can_start_final_report(state, agent)
    )
    if must_defer_final_report:
        preflight_node_frames = _node_frames_for_chunk(
            state,
            chunk,
            key=key,
            stage=target_stage,
            agent=agent,
            display=display,
            event=event,
            content=content,
        )
        pending_size = _pending_frames_text_size(preflight_node_frames[0])
        report_size = (
            len(content)
            if agent == "reporter" and isinstance(content, str) and content
            else 0
        )
        _ensure_text_capacity(state, report_size + pending_size)
        if (
            len(state.pending_final_report_frames)
            + len(preflight_node_frames[0])
            > MAX_STATE_ENTRIES
        ):
            raise ValueError(ROUTER_LIMIT_ERROR)

    _remember_question_chunk(state, chunk, content)
    if target_stage is not None:
        frames.extend(advance_stage(state, target_stage))

    # 中断 chunk:捕获 raw_prompt + node_id,不转发(interrupted marker 到达时拼 prompt)
    if message_type == "interrupt" or str(chunk.get("event")) == "waiting_user_input":
        state.interrupt_node_id = agent
        state.interrupt_raw_prompt = _limited_text(content)
        cid = chunk.get("conversation_id", "")
        if cid:
            state.interrupt_conversation_id = cid
        return frames

    # 用户输入结束:大纲已根据修改重新生成,自动确认继续研究
    if str(chunk.get("event")) == "user_input_ended":
        frames.append({
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_2",
            "stream_source_id": "dr_outline",
            "content": "大纲已根据您的修改重新生成，自动确认继续研究",
        })
        return frames

    if not agent or agent in _SKIP_NODES:
        return frames

    if not display:
        return frames  # 未知节点不推(避免噪声)

    if agent in _SECTION_PROCESS_NODES and section_idx != "0":
        _remember_expected_sections(state, chunk)
        start_final_report = False
        process_parts = _raw_process_parts(
            chunk,
            content,
            agent=agent,
            include_content=agent != "sub_reporter",
        )
        _remember_section_title(state, section_idx, chunk.get("section_title"))
        if len(state.section_titles) > MAX_SECTION_COUNT:
            raise ValueError(ROUTER_LIMIT_ERROR)
        if section_idx not in state.started_section_indices:
            state.started_section_indices.add(section_idx)
            frames.append(_section_boundary(state, chunk, "task.start"))
        node_state = state.active_nodes.get(key)
        if node_state is None:
            node_state = {
                "started": True,
                "done": False,
                "agent_name": agent,
                "section_idx": section_idx,
            }
            state.active_nodes[key] = node_state
            if event != "done":
                frames.append(_section_reasoning(state, chunk, f"{display[0]}开始\n"))

        if event == "done" and not node_state["done"]:
            node_state["done"] = True
            frames.append(_section_reasoning(state, chunk, f"{display[0]}完成\n"))

        section_succeeded = (
            agent == "sub_reporter"
            and event == _SUMMARY_RESPONSE_EVENT
            and _text_field(content) == _SECTION_SUCCESS_MARKER
        )
        if section_succeeded and section_idx not in state.completed_section_indices:
            state.completed_section_indices.add(section_idx)
            frames.append(_section_boundary(state, chunk, "task.complete"))
            if _all_sections_completed(state):
                start_final_report = True

        for process_content in process_parts:
            frames.append(_section_reasoning(state, chunk, process_content))
        if start_final_report:
            frames.extend(start_final_report_processing(state))
        return frames

    # 大纲正文只读展示在思考过程；其他非并行节点维持 reasoning_content 通用透传。
    if agent in {"outline", "brief_outline"}:
        if isinstance(content, str) and content:
            _append_text_part(state, state.outline_parts, content)
        task_content = display[0] + (f" - {display[1]}" if display[1] else "")
        for process_content in _raw_process_parts(chunk, content, agent=agent):
            frames.append({
                "event_type": "chat.reasoning",
                "task_id": "deepresearch_stage_2",
                "task_content": task_content,
                "stream_source_id": "dr_outline",
                "content": process_content,
            })
        return frames

    if target_stage in {1, 2, 3}:
        if agent in FINAL_REPORT_NODES and _can_start_final_report(state, agent):
            frames.extend(start_final_report_processing(state))
        node_frames, starts_node, completes_node = (
            preflight_node_frames
            or _node_frames_for_chunk(
                state,
                chunk,
                key=key,
                stage=target_stage,
                agent=agent,
                display=display,
                event=event,
                content=content,
            )
        )
        if agent == "reporter" and isinstance(content, str) and content:
            _append_text_part(state, state.report_parts, content)
        node_state = state.active_nodes.get(key)
        if starts_node:
            node_state = {
                "started": True,
                "done": False,
                "agent_name": agent,
                "section_idx": section_idx,
            }
            state.active_nodes[key] = node_state
        if completes_node:
            node_state["done"] = True
        if agent in FINAL_REPORT_NODES and not state.final_report_started:
            _extend_pending_final_report_frames(state, node_frames)
        else:
            frames.extend(node_frames)
        if agent == "brief_sub_reporter" and completes_node:
            frames.extend(start_final_report_processing(state))
        return frames

    reasoning = _chunk_reasoning_content(chunk, content)
    if reasoning:
        frames.append({"event_type": "chat.reasoning", "content": _as_text(reasoning)})

    # 首次见 → task.start(交互节点也发边界气泡,让用户看到"大纲生成中"等)
    if key not in state.active_nodes:
        state.active_nodes[key] = {
            "started": True,
            "done": False,
            "agent_name": agent,
            "section_idx": section_idx,
        }
        frames.append(_task_frame("task.start", agent, section_idx, display))

    # event=done → task.complete
    if event == "done":
        node_state = state.active_nodes.get(key)
        if node_state and node_state["started"] and not node_state["done"]:
            node_state["done"] = True
            frames.append(_task_frame("task.complete", agent, section_idx, display))

    return frames
