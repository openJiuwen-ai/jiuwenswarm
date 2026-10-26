# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CronRunState ↔ Ephemeral bytes。"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from jiuwenswarm.gateway.cron.models import CronRunState

RUNS_HASH = "runs"


def run_field_key(service_id: str, agent_id: str, run_id: str) -> str:
    sid = str(service_id or "default").strip() or "default"
    aid = str(agent_id or "default").strip() or "default"
    return f"{sid}:{aid}:{run_id}"


def cron_run_to_bytes(state: CronRunState) -> bytes:
    return json.dumps(asdict(state), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def cron_run_from_bytes(raw: bytes) -> CronRunState:
    data: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return CronRunState(
        run_id=str(data["run_id"]),
        job_id=str(data["job_id"]),
        wake_at_iso=str(data["wake_at_iso"]),
        push_at_iso=str(data["push_at_iso"]),
        status=str(data.get("status") or "pending"),
        placeholder_sent=bool(data.get("placeholder_sent", False)),
        pushed_final=bool(data.get("pushed_final", False)),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        result_text=data.get("result_text"),
        error=data.get("error"),
        job_name=data.get("job_name"),
        targets=data.get("targets"),
        session_id=data.get("session_id"),
        chat_type=data.get("chat_type"),
        timezone=data.get("timezone"),
        exec_channel_id=data.get("exec_channel_id"),
        exec_session_id=data.get("exec_session_id"),
        manual_trigger=bool(data.get("manual_trigger", False)),
    )


__all__ = [
    "RUNS_HASH",
    "cron_run_from_bytes",
    "cron_run_to_bytes",
    "run_field_key",
]
