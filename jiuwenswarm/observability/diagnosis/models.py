# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""诊断证据包数据模型（设计 §4.3）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DiagnosisTrigger(str, Enum):
    """触发场景，对应设计 §4.1。

    ``error``: trace 内存在 has_error span 或 chat.error 事件
    ``interrupt``: run 未正常收尾 / forced_close
    ``unexpected``: 无错误信号，以 user_note + 最后 1-2 轮为分析对象
    """

    ERROR = "error"
    INTERRUPT = "interrupt"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class FailureWindow:
    """失败窗口：[start_unix_nano, end_unix_nano]。

    以最早的错误 span 为锚，取 [锚点所在 turn 的起点, run 结束]；
    无错误时取最后 1-2 个 turn。窗口内 span 全量进入证据，窗口外只留摘要。
    """

    start_unix_nano: int
    end_unix_nano: int
    anchor_span_id: str | None = None
    """触发窗口定位的锚点 span（最早的错误 span），unexpected 场景为 None。"""

    in_window: tuple[str, ...] = field(default_factory=tuple)
    """窗口内 span_id 集合（用于投影时判定 in/out）。"""


@dataclass
class DiagnosisEvidence:
    """送 LLM 前（裁剪 + 脱敏后）的证据包。

    全量包（裁剪前）落盘 ``~/.jiuwenswarm/.trace/diagnosis/<session>-<ts>.json``
    供复查；本对象是裁剪后的版本。
    """

    session_id: str
    trace_id: str
    trigger: str
    user_note: str | None
    window: tuple[int, int]
    span_summaries: list[dict]
    """全量 span 摘要（名称/时间/状态/subject），窗口内外都含。"""

    failure_spans: list[dict]
    """窗口内错误 span 的投影（含工具参数/结果/失败原因/异常栈）。"""

    llm_io: list[dict]
    """窗口内 llm.call 的输入输出投影（messages/usage/TTFT）。"""

    context_commits: list[dict]
    """关键 LLM 调用的 context_window_commit（模型实际看到了什么）。"""

    compaction_events: list[dict]
    """压缩事件（"结果不符预期"场景的重点证据）。"""

    usage_snapshot: dict
    """各 subject token 消耗。"""

    log_excerpts: list[dict]
    """4.2 的日志片段（带来源标注：文件/行号/匹配键/来源类型）。"""

    llm_rounds: list[dict] = field(default_factory=list)
    """LLM 调用轮次（全局 trace）：每轮 = 一次 llm.call / llm.reasoning span。
    字段：round_index / span_id / request_id / start_unix_nano / end_unix_nano /
    has_error / error_messages / error_message / content_preview（每条消息 [:300] 截断）。
    完整内容（每轮 input/output 全文、span 细节）落盘在 evidence_path 对应的 JSON，
    供 LLM 按需 read_file 检索，不在摘要里展开。"""

    offline: bool = False
    """离线纯日志模式（无 trace）：window 无意义，日志即全部证据。"""

    evidence_path: str | None = None
    """全量证据包落盘路径（供报告头部回指复查）。"""
