# -*- coding: utf-8 -*-
"""_experiment_output.py — 模块三终态产物的解读（CLI 与 agent 两条执行路径共用）。

模块三无论是被确定性 CLI（`experiment/scripts/main.py run`）还是被 experiment-agent
（SDK 直连）驱动，最终都在 **run_dir** 下落同一份 `outputs/experiment-module-output.json`。
所以「怎么判定这一阶段成没成」只该有一份实现——就是这里。

注意 run_dir 不是 stage3 根目录：`execution_config.run_dir` 决定它（paper-gen 投影层
统一写成 `<stage3_dir>/run` 的相对形式），outputs/ 挂在它下面。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUTPUT_NAME = "experiment-module-output.json"
RUN_DIR_NAME = "run"
REQUEST_NAME = "request.json"

# 模块三 status → paper-gen stage status。
# REPLAN 不是崩溃：模块三结构化地把球踢回模块二，理由在 planning_feedback 里，
# 端到端流程仍然前进了，所以算 partial 而不是 error。
_STATUS_MAP: dict[str, str] = {
    "PASS": "complete",
    "COMPLETED": "complete",
    "PARTIAL": "partial",
    "REPLAN": "partial",
    "FAILED": "error",
}


def output_path(run_dir: Path) -> Path:
    return run_dir / "outputs" / OUTPUT_NAME


def _recorded_run_dir(stage3_dir: Path) -> str | None:
    """Return the run directory recorded by the stage-three request."""
    path = stage3_dir / REQUEST_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    execution = payload.get("execution_config") if isinstance(payload, dict) else None
    recorded = execution.get("run_dir") if isinstance(execution, dict) else None
    return recorded.strip() if isinstance(recorded, str) and recorded.strip() else None


def resolve_stage3_run_dir(
    stage3_dir: str | Path,
    base_dir: str | Path | None = None,
) -> Path:
    """Resolve a stage directory to the run root that owns ``outputs/``.

    New stage-three runs use ``stage3/runs/<run_id>`` and record that path in
    ``request.json``.  The recorded path is authoritative even before the
    output file exists; requiring an existing terminal output here made error
    reporting and interrupted-run recovery impossible.  If the request is
    absent, exactly one completed content-addressed run may be discovered;
    otherwise the legacy ``stage3/run`` convention is returned.

    The stage directory itself is deliberately never treated as a run root.
    Accepting ``stage3/outputs`` can silently feed a stale result from an older
    layout into the writing stage.
    """
    stage3 = Path(stage3_dir).resolve()
    recorded = _recorded_run_dir(stage3)
    if recorded:
        recorded_path = Path(recorded)
        if recorded_path.is_absolute():
            return recorded_path.resolve()
        if base_dir is not None:
            return (Path(base_dir) / recorded_path).resolve()
        if recorded_path.name == RUN_DIR_NAME:
            return (stage3 / RUN_DIR_NAME).resolve()
        if recorded_path.parent.name == "runs" and recorded_path.name:
            return (stage3 / "runs" / recorded_path.name).resolve()
        raise ValueError(
            f"{stage3 / REQUEST_NAME} records custom relative run_dir={recorded!r}; "
            "base_dir is required to resolve it safely"
        )

    runs_dir = stage3 / "runs"
    valid: list[Path] = []
    if runs_dir.is_dir():
        valid = sorted(
            (path.resolve() for path in runs_dir.iterdir()
             if path.is_dir() and output_path(path).is_file()),
            key=lambda path: path.as_posix(),
        )
    if len(valid) == 1:
        return valid[0]
    if len(valid) > 1:
        rendered = ", ".join(str(path) for path in valid)
        raise ValueError(f"stage3 run directory is ambiguous: {rendered}")

    return (stage3 / RUN_DIR_NAME).resolve()


def read_experiment_output(run_dir: Path) -> tuple[dict | None, str | None]:
    """读 run_dir 下的模块三终态 JSON。返回 (payload, 读取失败说明)。"""
    path = output_path(run_dir)
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"{path} 解析失败: {exc}"
    return (payload if isinstance(payload, dict) else None), (
        None if isinstance(payload, dict) else f"{path} 不是 JSON 对象"
    )


def parse_json_object(text: str) -> dict | None:
    """从 `--print-output` 的 stdout 里抠出 JSON 对象（前面可能混着日志行）。"""
    if not text or "{" not in text:
        return None
    try:
        payload = json.loads(text[text.index("{"):])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def describe_planning_feedback(feedback: Any) -> list[str]:
    """把模块三 REPLAN 的 planning_feedback 摊成可读行。

    不摊开的话 REPLAN 只会留下一个 `rc=2`，完全看不出是"数据没下载"还是"契约不合"。
    blockers 带 `[data]`/`[compute]` 这类模块二路由类别前缀，直接可用于定位。
    """
    if not isinstance(feedback, dict):
        return []
    out: list[str] = []
    reason = feedback.get("reason")
    if reason:
        out.append(f"REPLAN reason: {reason}")
    for blocker in feedback.get("blockers", [])[:8]:
        out.append(f"REPLAN blocker: {blocker}")
    for suggestion in feedback.get("suggested_changes", [])[:5]:
        out.append(f"REPLAN suggested: {suggestion}")
    return out


def interpret_payload(payload: dict) -> tuple[str, list[str], list[str]]:
    """模块三终态 payload → (stage status, errors, warnings)。"""
    status = _STATUS_MAP.get(str(payload.get("status", "")).upper(), "error")
    errors = [str(e) for e in payload.get("errors", [])[:10]]
    warnings = [str(w) for w in payload.get("warnings", [])[:10]]
    warnings.extend(describe_planning_feedback(payload.get("planning_feedback")))
    return status, errors, warnings


def requires_replan(payload: dict | None) -> bool:
    """Return the module-three routing decision without parsing display text."""
    return isinstance(payload, dict) and str(payload.get("status") or "").upper() == "REPLAN"


def planning_feedback(payload: dict | None) -> dict[str, Any] | None:
    """Return structured REPLAN feedback only when module three requested it."""
    feedback = payload.get("planning_feedback") if requires_replan(payload) else None
    return feedback if isinstance(feedback, dict) else None


def collect_artifacts(run_dir: Path, limit: int = 20) -> list[str]:
    if not run_dir.is_dir():
        return []
    return [str(p) for p in run_dir.rglob("*.json") if p.is_file()][:limit]


STATE_NAME = "agent-state.json"   # 模块三 agent_coordinator.STATE_PATH = "outputs/agent-state.json"


def state_path(run_dir: Path) -> Path:
    return run_dir / "outputs" / STATE_NAME


def describe_stage(run_dir: Path) -> str:
    """当前跑到哪个阶段（超时/中断/心跳时用来说明进展）。

    读模块三自己的 `outputs/agent-state.json`（`current_stage` 是它的推进锚点，
    值形如 INITIALIZED / DATA_READY / ... / REPLAN）。
    """
    path = state_path(run_dir)
    if not path.is_file():
        return "stage=unknown（模块三尚未写 agent-state.json）"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return f"stage=unreadable（{exc}）"
    if not isinstance(payload, dict):
        return "stage=unreadable（agent-state.json 不是对象）"
    stage = payload.get("current_stage") or "unknown"
    return f"stage={stage} run_id={payload.get('run_id')}"
