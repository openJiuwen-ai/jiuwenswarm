"""Normalize Evo-Bench validation suites for the single-Harness engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.errors import RsiDatasetInvalid


def normalize_validation_suite(source_path: Path | str) -> dict[str, Any] | None:
    """Return an engine dataset payload, or ``None`` for standard datasets.

    The public GDPVAL suite stores tasks under ``validation`` and identifies
    each task with ``id``. openjiuwen's single-Harness loader instead expects
    a ``cases`` array with ``case_id`` and ``input`` fields.
    """

    source = Path(source_path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RsiDatasetInvalid(f"训练集不是有效 JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("validation"), list):
        return None
    if isinstance(payload.get("cases"), list):
        return None

    tasks = payload["validation"]
    if not tasks:
        raise RsiDatasetInvalid("Evo-Bench 训练集 validation 不能为空")

    cases: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise RsiDatasetInvalid(f"第 {index} 条训练样本必须是对象")
        case_id = str(task.get("id") or "").strip()
        domain = str(task.get("domain") or "").lower()
        source_name = case_id.split("-", 1)[0]
        prompt = str(task.get("prompt") or "")
        if not case_id:
            raise RsiDatasetInvalid(f"第 {index} 条训练样本缺少 id")
        if case_id in seen_case_ids:
            raise RsiDatasetInvalid(f"训练样本 id 重复: {case_id}")
        if domain not in {"general", "office"}:
            raise RsiDatasetInvalid(f"训练样本 {case_id} 仅支持 general/office 领域")
        if source_name not in {"claw", "gdpval", "apex"}:
            raise RsiDatasetInvalid(f"训练样本 {case_id} 不支持来源: {source_name}")
        if not prompt.strip():
            raise RsiDatasetInvalid(f"训练样本 {case_id} 缺少 prompt")
        seen_case_ids.add(case_id)
        cases.append(
            {
                **task,
                "case_id": case_id,
                "task_id": case_id,
                "input": prompt,
                "domain": domain,
                "source": source_name,
                "task_type": str((task.get("metadata") or {}).get("task_type") or domain),
            }
        )

    return {"dataset_id": "evobench_local_no_key_validation", "cases": cases}
