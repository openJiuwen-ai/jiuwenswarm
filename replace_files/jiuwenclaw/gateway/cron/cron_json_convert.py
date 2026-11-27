#!/usr/bin/env python3
"""
CLI 工具：把 cron JSON 在「扁平结构」与「OpenClaw 嵌套结构」之间互转。

扁平结构（内部 CronJobStore 使用）示例：
{
  "version": 1,
  "jobs": [
    {
      "id": "...",
      "name": "...",
      "enabled": true,
      "cron_expr": "0 9 * * *",
      "timezone": "Asia/Shanghai",
      "wake_offset_seconds": 60,
      "description": "...",
      "targets": "web"
    }
  ]
}

OpenClaw 嵌套结构示例（schedule/payload/delivery）：
{
  "version": 1,
  "jobs": [
    {
      "id": "...",
      "name": "...",
      "enabled": true,
      "schedule": { "kind": "cron", "expr": "...", "tz": "...", "staggerMs": 0 },
      "payload": { "kind": "systemEvent", "text": "..." },
      "delivery": { "mode": "announce", "channel": "webchat" },
      "wakeMode": "now"
    }
  ]
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from datetime import datetime

from jiuwenclaw.gateway.cron.cron_expr import iso_to_seven_field_cron


_EXTERNAL_TO_INTERNAL_CHANNELS: dict[str, str] = {
    # OpenClaw 侧叫 webchat；内部 cron 以 web 表示
    "webchat": "web",
}

_INTERNAL_TO_EXTERNAL_CHANNELS: dict[str, str] = {
    # 反向保持文件尽量使用OpenClaw 名称
    "web": "webchat",
}


def _read_json(path: Path) -> dict[str, Any]:
    # utf-8-sig 自动跳过 BOM
    raw = path.read_text(encoding="utf-8-sig")
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("Top-level JSON must be an object")
    if "version" not in data:
        data["version"] = 1
    if "jobs" not in data or not isinstance(data["jobs"], list):
        data["jobs"] = []
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")


def convert_cron_job_dict_to_flat(data: dict[str, Any]) -> dict[str, Any]:
    """
    把 OpenClaw 嵌套 job（schedule/payload/delivery）转成内部 CronJob 所需的扁平字段。

    若输入本身就是扁平结构（包含 cron_expr + timezone），则原样返回。
    """

    if not isinstance(data, dict):
        return {}

    cron_expr = str(data.get("cron_expr") or "").strip()
    timezone = str(data.get("timezone") or "").strip()
    if cron_expr and timezone:
        return data

    sched = data.get("schedule")
    if not isinstance(sched, dict):
        return data

    kind = str(sched.get("kind") or "").strip().lower()

    expr = ""
    tz = ""
    if kind == "cron":
        expr = str(sched.get("expr") or "").strip()
        tz = str(sched.get("tz") or "").strip()
    elif kind == "at":
        # one-shot：使用 croniter 的 7 段表达式固定到指定年份
        at_raw = str(sched.get("at") or "").strip()
        if not at_raw:
            return data
        tz = str(sched.get("tz") or "").strip() or "UTC"
        try:
            expr = iso_to_seven_field_cron(at_raw, timezone=tz)
        except Exception:  # noqa: BLE001
            return data
    else:
        return data

    # 你的偏好：不管 OpenClaw 里 wakeMode 是什么，转换到 jiuwenclaw 时都统一写成 60s
    wake_offset_seconds = 60

    desc = str(data.get("description") or data.get("name") or "")
    payload = data.get("payload")
    if isinstance(payload, dict) and str(payload.get("kind") or "") == "systemEvent":
        pt = str(payload.get("text") or "").strip()
        if pt:
            desc = pt

    targets = ""
    delivery = data.get("delivery")
    if isinstance(delivery, dict):
        targets = str(delivery.get("channel") or "").strip()
    if not targets:
        targets = str(data.get("targets") or "").strip()

    # 显式通道别名：OpenClaw(webchat) -> 内部(web)
    targets = _EXTERNAL_TO_INTERNAL_CHANNELS.get(targets, targets)

    created_at: float | None = None
    updated_at: float | None = None
    ca = data.get("created_at")
    ua = data.get("updated_at")
    if isinstance(ca, (int, float)):
        created_at = float(ca)
    elif "createdAtMs" in data:
        cam = data.get("createdAtMs")
        if isinstance(cam, (int, float)):
            created_at = float(cam) / 1000.0

    if isinstance(ua, (int, float)):
        updated_at = float(ua)
    elif "updatedAtMs" in data:
        uam = data.get("updatedAtMs")
        if isinstance(uam, (int, float)):
            updated_at = float(uam) / 1000.0

    return {
        "id": str(data.get("id") or "").strip(),
        "name": str(data.get("name") or "").strip(),
        "enabled": bool(data.get("enabled", False)),
        "cron_expr": expr,
        "timezone": tz,
        "wake_offset_seconds": wake_offset_seconds,
        "description": desc,
        "targets": targets,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def convert_flat_cron_job_to_openclaw_dict(flat_job: dict[str, Any]) -> dict[str, Any]:
    """
    将内部扁平 CronJob 字段写回到 OpenClaw 嵌套 job 格式。
    """

    channel = str(flat_job.get("targets") or "").strip()
    external_channel = _INTERNAL_TO_EXTERNAL_CHANNELS.get(channel, channel)

    # 你的偏好：不管 jiuwenclaw 里 wake_offset_seconds 是多少，转换到 OpenClaw 时都统一写成 now
    wake_mode = "now"

    created_at = flat_job.get("created_at")
    updated_at = flat_job.get("updated_at")
    created_at_ms = (
        int(created_at * 1000) if isinstance(created_at, (int, float)) else None
    )
    updated_at_ms = (
        int(updated_at * 1000) if isinstance(updated_at, (int, float)) else None
    )

    desc = str(flat_job.get("description") or "")

    out: dict[str, Any] = {
        "id": str(flat_job.get("id") or "").strip(),
        "name": str(flat_job.get("name") or "").strip(),
        "enabled": bool(flat_job.get("enabled", False)),
        "description": desc,
        "schedule": {
            "kind": "cron",
            "expr": str(flat_job.get("cron_expr") or "").strip(),
            "tz": str(flat_job.get("timezone") or "").strip(),
            "staggerMs": 0,
        },
        "sessionTarget": "main",
        "wakeMode": wake_mode,
        "payload": {
            "kind": "systemEvent",
            "text": desc,
        },
        "deleteAfterRun": False,
        "delivery": {
            "mode": "announce",
            "channel": external_channel,
        },
    }
    if created_at_ms is not None:
        out["createdAtMs"] = created_at_ms
    if updated_at_ms is not None:
        out["updatedAtMs"] = updated_at_ms
    return out


def convert_file(input_path: Path, output_path: Path, to_format: str) -> None:
    jobs_in = _read_json(input_path).get("jobs") or []

    out_jobs: list[dict[str, Any]] = []
    for job in jobs_in:
        if not isinstance(job, dict):
            continue
        if to_format == "jiuwenclaw":
            flat_job = convert_cron_job_dict_to_flat(job)
            out_jobs.append(flat_job)
        elif to_format == "openclaw":
            # 可能输入就是扁平，也可能是嵌套；统一先转成内部结构再写回 openclaw
            flat_job = convert_cron_job_dict_to_flat(job)
            out_jobs.append(convert_flat_cron_job_to_openclaw_dict(flat_job))
        else:
            raise ValueError("Unknown to_format")

    _write_json(output_path, {"version": 1, "jobs": out_jobs})


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Convert cron job JSON formats")
    parser.add_argument("--input", required=True, help="Input cron JSON file path")
    parser.add_argument("--output", required=True, help="Output cron JSON file path")
    parser.add_argument(
        "--to",
        choices=["jiuwenclaw", "openclaw"],
        required=True,
        help="Target format: jiuwenclaw (internal) or openclaw (nested)",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    convert_file(input_path, output_path, args.to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

