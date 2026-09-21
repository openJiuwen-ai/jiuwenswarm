# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trace 问题自动分析定位 · 证据收集与分析骨架。

本包是诊断功能的心脏，纯后端、不依赖 Gateway/LLM 调用链路，可直接单测。
编排顺序（对应设计 §4.1-4.3）：
    signal detect → failure window → trace evidence projection →
    log evidence (辅助) → budget trim → sanitize → DiagnosisEvidence

主证据来自 trajectory store 的 ``raw_json``（与日志级别无关）；
日志仅作辅助证据，抓 span 覆盖不到的进程级异常（Traceback / ERROR）。
"""

from __future__ import annotations

from .models import DiagnosisEvidence, DiagnosisTrigger, FailureWindow
from .evidence import (
    DiagnosisContext,
    collect_evidence,
    detect_trigger,
    locate_failure_window,
)

__all__ = [
    "DiagnosisEvidence",
    "DiagnosisTrigger",
    "FailureWindow",
    "DiagnosisContext",
    "collect_evidence",
    "detect_trigger",
    "locate_failure_window",
]
