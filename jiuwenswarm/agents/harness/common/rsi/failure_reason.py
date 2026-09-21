"""Safe, bounded task failure details for the existing task-get API."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import mask_sensitive


def with_failure_reason(task: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if task.status != "FAILED":
        return payload
    reason = str(payload.get("failure_reason") or "").strip()
    prefix = "LLM evaluator failed; inspect "
    detail = reason
    if reason.startswith(prefix) and task.run_dir:
        try:
            root = Path(task.run_dir).resolve()
            path = Path(reason[len(prefix):]).resolve()
            # Never follow an error message to a different task or arbitrary file.
            if path.is_relative_to(root) and path.name == "error.json":
                with path.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) <= 65536:
                    error = json.loads(raw)
                    if isinstance(error, dict) and isinstance(error.get("message"), str):
                        detail = error["message"]
        except (OSError, ValueError, RuntimeError):
            pass  # Old or moved tasks still expose their persisted failure reason.
    if "429" in detail and ("RateLimitError" in detail or "rate limit" in detail.lower()):
        limit = re.search(r"Current limit:\s*(\d+)", detail)
        if "max_parallel_requests" in detail:
            quota = f"（上限 {limit.group(1)} 个并发请求）" if limit else ""
            detail = f"模型服务并发限流：HTTP 429{quota}。请降低评测并发，或等待其他请求结束后重试。"
        else:
            detail = "模型服务限流：HTTP 429。请检查服务配额、降低请求频率或稍后重试。"
        if reason.startswith(prefix):
            detail = "LLM Judge 评测失败。" + detail
    payload["failure_reason"] = mask_sensitive(detail)[:4000] or "任务执行失败，未记录具体原因。"
    return payload
