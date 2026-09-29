# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""诊断 Agent 任务书模板（设计 v2 §2.4/2.5）。

分析阶段是 AgentServer 上的完整 Agent 流程：本模块产出"任务书"——
摘要层（时间线/失败窗口/错误 span 清单/history 尾部）+ 全量证据包落盘路径
+ 查证指令。Agent 首轮自行 read_file 证据包，按需 grep 日志 / 读源码锁定
证据后输出 Markdown 报告。不再把证据包塞进 prompt。
"""

from __future__ import annotations

from typing import Any

_AGENT_TASK_PROMPT = """你是 JiuwenSwarm 的故障诊断专家。请对下述会话的一次执行失败做根因分析定位，输出结构化 Markdown 诊断报告。

## 输入材料

1. [执行摘要]（见下方）——LLM 调用轮次（全局 trace）、错误信号清单、对话历史尾部。
2. [全量证据包]：`{evidence_path}`（JSON 文件）。请先 read_file 读取它。

## 证据结构（证据包 JSON 顶层字段）
- `llm_rounds`：本轮诊断的核心视图。每轮 = 一次 LLM 调用（llm.call / llm.reasoning span），
    含 round_index / span_id / request_id / start-end(纳秒) / has_error / error_messages /
    content_preview（每条消息 [:300] 截断的紧凑预览）。
- 其余字段（span_summaries / failure_spans / context_commits / compaction_events / log_excerpts）含义不变。
- 范围：{window}。完整内容（每轮 input/output 全文、span 细节）均在证据包中，按需 read_file 检索，不要凭预览下结论。

## 工作方式（必须遵守）

你有完整的工具能力，不要只依据摘要下结论：
1. 先 read_file 证据包全文，按 llm_rounds 建立轮次与全局 trace 认知；
2. 对每条根因假设，主动查证——用 grep/read_file 检索日志（日志目录 `{log_dir}`）、读取相关源码（你的工作目录就是被诊断项目的代码仓）、回到证据包核对 span 细节；
3. 证据锁定后才写报告：每条根因必须有具体证据支撑（span_id + status、带行号的日志原文、或源码 path:line），查证不到的标明"未验证"；
4. 只读取证，不修改任何文件。

## 证据约束

A. 全局 trace：证据包已含所选范围的全部 LLM 调用轮次，不要自行按时间窗裁剪；范围外历史日志不得作为根因证据（离线模式按日志自身时间戳判断）。
B. 同一任务若多轮出错（rounds 中多个 has_error=True），每一轮的 error_messages 都要逐一分析定位，不得遗漏任何一轮。
C. 区分"证据支持的事实"与"推断"——不得把"可能看到了 xxx"当作事实写进根因证据。
D. 禁止编造 span_id、行号、文件路径；每个 code_location 都应是你亲自读过的位置。

## 输出格式（Markdown，直接输出，不要 JSON 代码块）

### 时间线（按轮次）
- **Round 1** [T+Xs] has_error=... 一句话

### 根因假设
1. **[confidence / kind]** 根因一句话描述
   - 证据：span xxx status=yyy / 日志 `file:line` 原文 / 源码 `path:line in func`
   - 代码位置：`path:line in func`（行为类根因标 N/A）
   - 查证过程：你实际执行的检索/读取动作

### 模型实际看到
基于 context_commits 还原模型在关键决策点实际收到的上下文……

### 建议措施
1. [对应根因#1] 具体可执行的修复/排查动作

{title_line}
"""


def build_agent_prompt(
    *,
    evidence_path: str,
    log_dir: str,
    window: str,
    summary_blocks: dict[str, str] | None = None,
    user_note: str | None = None,
    session_id: str | None = None,
) -> str:
    """组装诊断 Agent 任务书。

    Args:
        evidence_path: 全量证据包落盘路径（Agent 首轮 read_file 的目标）。
        log_dir: 本地日志目录（grep 目标；离线上传日志时为其落盘目录）。
        window: 失败时间窗描述（"start ~ end" 或离线占位说明）。
        summary_blocks: 摘要层各块（timeline / failure_spans / history 等），
            键为块标题、值为 Markdown 文本，按插入顺序拼接。
        user_note: 用户问题描述（可空）。
        session_id: 被诊断会话 id（标题 fallback 用）。
    """
    parts: list[str] = []
    note_text = user_note.strip() if isinstance(user_note, str) else ""
    if note_text:
        note_short = note_text.splitlines()[0][:40]
        title_line = f"报告标题输出为：## 关于「{note_short}」的诊断报告"
    else:
        sid = (session_id or "")[-12:]
        title_line = f"报告标题输出为：## 诊断报告（{sid or '本次会话'}）"

    prompt = _AGENT_TASK_PROMPT.format(
        evidence_path=evidence_path,
        log_dir=log_dir,
        window=window,
        title_line=title_line,
    )
    parts.append(prompt)

    if note_text:
        parts.append(f"\n## 用户补充描述\n\n{note_text}\n")

    if summary_blocks:
        parts.append("\n## 执行摘要\n")
        for title, body in summary_blocks.items():
            if body and body.strip():
                parts.append(f"\n### {title}\n\n{body}\n")

    return "".join(parts)


def build_timeline_summary(records: list[Any], max_items: int = 40, session_start_nano: int = 0) -> str:
    """trace 时间线摘要（record 级）：每 record 一行（名称/时间/状态/错误标记）。

    rel 以会话起点为基准（修复旧版把绝对 epoch 秒当相对秒的 bug）。
    record 为 ``evidence.SpanRecord`` 视图；这里 duck-typing 解耦，避免
    prompts 层 import evidence 层。
    """
    lines: list[str] = []
    for record in records[:max_items]:
        start = getattr(record, "start_time_unix_nano", 0) or 0
        rel = (start - session_start_nano) / 1e9 if start and session_start_nano else 0.0
        request_id = getattr(record, "request_id", None) or "-"
        error_flag = " ❌has_error" if getattr(record, "has_error", False) else ""
        lifecycle = getattr(record, "lifecycle", "") or ""
        span_count = len(getattr(record, "spans", []) or [])
        lines.append(
            f"- T+{rel:.2f}s request={request_id} spans={span_count}"
            f" lifecycle={lifecycle}{error_flag}"
        )
    if len(records) > max_items:
        lines.append(f"…（共 {len(records)} 条 record，仅列前 {max_items} 条）")
    return "\n".join(lines)


def build_llm_rounds_summary(
    llm_rounds: list[dict[str, Any]],
    evidence_path: str,
    session_start_nano: int = 0,
    max_preview_chars: int = 80,
) -> str:
    """LLM 调用轮次摘要（全局 trace 视图，对应决策 1/2/3）。

    每轮：序号 / request / 绝对时间 + 相对 T+ / 错误信号；末尾指向全量证据文件。
    """
    if not llm_rounds:
        return (
            "（所选范围内未发现 llm.call / llm.reasoning span）\n"
            f"完整证据信息仍在文件 `{evidence_path}`，可用 read_file 工具检索。"
        )
    lines: list[str] = []
    for r in llm_rounds:
        start = r.get("start_unix_nano") or 0
        end = r.get("end_unix_nano") or 0
        rel_s = (start - session_start_nano) / 1e9 if start and session_start_nano else 0.0
        rel_e = (end - session_start_nano) / 1e9 if end and session_start_nano else 0.0
        if r.get("has_error"):
            err = r.get("error_message") or ""
            err = err[:200] + "…" if len(err) > 200 else err
            flag = f"has_error=True | error: {err}"
        else:
            flag = "has_error=False"
        preview = r.get("content_preview") or {}
        ptext = _round_preview_text(preview, max_preview_chars)
        lines.append(
            f"- Round {r.get('round_index')} | request={r.get('request_id')} | "
            f"{_fmt_dt(start)} (T+{rel_s:.2f}s) ~ {_fmt_dt(end)} (T+{rel_e:.2f}s) | {flag}\n"
            f"    preview: {ptext}"
        )
    lines.append(
        f"\n完整的证据信息（含每轮 input/output 全文、span 详情）在文件 `{evidence_path}`，"
        f"可用 read_file 工具按需检索。"
    )
    return "\n".join(lines)


def _round_preview_text(preview: dict, max_chars: int) -> str:
    parts: list[str] = []
    for m in (preview.get("messages") or [])[:4]:
        role = m.get("role", "?")
        content = (m.get("content") or "")[:max_chars]
        parts.append(f"{role}:{content}")
    if preview.get("reasoning"):
        parts.append("reasoning:" + preview["reasoning"][:max_chars])
    if preview.get("output"):
        parts.append("out:" + preview["output"][:max_chars])
    for t in (preview.get("tool_outputs") or [])[:4]:
        tool = t.get("tool", "?")
        out = (t.get("output") or "")[:max_chars]
        parts.append(f"tool[{tool}]:{out}")
    return " | ".join(parts) if parts else "(无内容预览)"


def _fmt_dt(nano: int) -> str:
    """纳秒时间戳 → 可读绝对时间（修复旧版日期错乱）。"""
    if not nano:
        return "-"
    import datetime

    return datetime.datetime.fromtimestamp(nano / 1e9).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def build_history_summary(
    history_records: list[dict[str, Any]],
    max_records: int = 20,
) -> str:
    """被诊断会话 history 尾部摘要（"模型实际看到/用户到底要什么"的第一手材料）。

    history 记录结构宽松（不同版本字段有差），按 role/content 常见字段尽力
    投影，认不出的结构原样截断 JSON。
    """
    if not history_records:
        return "（无对话历史）"
    lines: list[str] = []
    for item in history_records[-max_records:]:
        role = str(
            item.get("role")
            or item.get("type")
            or item.get("speaker")
            or "?"
        )
        content = item.get("content") or item.get("text") or item.get("output")
        if not isinstance(content, str):
            content = str(content)
        content = content.strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + "…"
        lines.append(f"- [{role}] {content}")
    if len(history_records) > max_records:
        lines.insert(0, f"…（共 {len(history_records)} 条，仅列尾部 {max_records} 条）")
    return "\n".join(lines)
