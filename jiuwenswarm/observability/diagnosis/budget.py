# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""证据包落盘与过期清理（设计 §4.3）。

v2 起证据不再裁剪/脱敏后塞进 prompt——Agent 自行 ``read_file`` 证据包，按需
grep 日志查证（见 ``prompts.build_agent_prompt``）。本模块只负责全量包落盘
（供报告头部回指复查）与过期产物清理。证据含原始对话内容，与 session history
同本机信任级，不单独脱敏；上传日志的脱敏在 ``evidence.collect_log_excerpts``。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .models import DiagnosisEvidence

logger = logging.getLogger(__name__)


def persist_full_evidence(
    evidence: DiagnosisEvidence,
    diagnosis_dir: Path,
) -> str:
    """全量证据包落盘 JSON，返回路径供报告头部回指。"""
    session_dir = diagnosis_dir / evidence.session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = session_dir / f"evidence-{ts}.json"
    payload = {
        "session_id": evidence.session_id,
        "trace_id": evidence.trace_id,
        "trigger": evidence.trigger,
        "user_note": evidence.user_note,
        "window": list(evidence.window),
        "span_summaries": evidence.span_summaries,
        "failure_spans": evidence.failure_spans,
        "llm_io": evidence.llm_io,
        "llm_rounds": evidence.llm_rounds,
        "context_commits": evidence.context_commits,
        "compaction_events": evidence.compaction_events,
        "usage_snapshot": evidence.usage_snapshot,
        "log_excerpts": evidence.log_excerpts,
        "offline": evidence.offline,
        "persisted_at": ts,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    return str(path)


def cleanup_expired_evidence(diagnosis_dir: Path, keep_days: int) -> int:
    """清理过期诊断产物（evidence JSON / report markdown / uploads）。

    随 run_diagnosis 每次运行顺带触发，无需独立后台循环。
    返回清理的文件数。失败不抛（清理是 best-effort）。
    """
    if not diagnosis_dir.exists() or keep_days <= 0:
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        for entry in diagnosis_dir.rglob("*"):
            if not entry.is_file():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        # 清理空 session 目录
        for child in list(diagnosis_dir.iterdir()):
            if child.is_dir():
                try:
                    next(child.iterdir(), None) is None and child.rmdir()
                except OSError:
                    continue
    except OSError:
        pass
    return removed
