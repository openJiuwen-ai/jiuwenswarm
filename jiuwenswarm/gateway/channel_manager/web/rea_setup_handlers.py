# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web RPC handlers for the two-phase REA setup flow."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

from jiuwenswarm.common.rea_capability import build_capability_cards
from jiuwenswarm.common.rea_mcp_probe import probe_rea_mcp_inventory
from jiuwenswarm.common.rea_setup import (
    InstallCancelled,
    REA_MCP_NAME,
    configure_component,
    enhanced_component_plan,
    install_enhanced_components,
    install_rea_base,
    mark_rea_mcp_state,
    rea_mcp_record,
    scan_rea_setup,
)

_JOBS: dict[str, dict[str, Any]] = {}
_JOB_TASKS: set[asyncio.Task[Any]] = set()
_JOB_CANCEL_EVENTS: dict[str, threading.Event] = {}
_JOB_LOCK = threading.Lock()

_BASE_STEPS: tuple[tuple[str, str, int, int], ...] = (
    ("node_npx", "检查兼容的 Node.js 与 npx", 1, 5),
    ("rea_package", "下载或核对固定版本 rea-agents", 3, 180),
    ("write_config", "写入 Workswarm 的 REA MCP 配置", 1, 5),
    ("verify_mcp", "启动临时 MCP 并校验 tools/list 与 binary_session", 3, 120),
    ("final_scan", "验证基础能力并生成增强计划", 2, 30),
)

_COMPONENT_TIME_ESTIMATES: dict[str, dict[str, tuple[int, int]]] = {
    "java": {
        "planning": (1, 3),
        "connect": (1, 15),
        "download": (30, 900),
        "verify": (2, 20),
        "install": (5, 90),
        "verified": (3, 30),
    },
    "ghidra": {
        "planning": (1, 3),
        "connect": (1, 15),
        "download": (120, 2400),
        "verify": (5, 30),
        "install": (10, 180),
        "verified": (10, 60),
    },
    "jadx": {
        "planning": (1, 3),
        "connect": (1, 15),
        "download": (10, 300),
        "verify": (1, 15),
        "install": (2, 20),
        "verified": (3, 30),
    },
    "adb": {
        "planning": (1, 3),
        "connect": (1, 15),
        "download": (5, 180),
        "verify": (1, 15),
        "install": (2, 30),
        "verified": (2, 30),
    },
    "apktool": {
        "planning": (1, 3),
        "connect": (1, 15),
        "download": (5, 240),
        "verify": (1, 15),
        "install": (2, 30),
        "verified": (2, 30),
    },
    "pcap": {
        "planning": (1, 3),
        "connect": (1, 15),
        "download": (30, 900),
        "verify": (2, 20),
        "install": (10, 180),
        "verified": (3, 30),
    },
    "hopper": {
        "planning": (1, 3),
        "connect": (1, 15),
        "download": (30, 900),
        "verify": (2, 20),
        "install": (10, 180),
        "verified": (5, 60),
    },
}


def _base_steps() -> list[dict[str, Any]]:
    return _steps_from_specs(_BASE_STEPS)


def _steps_from_specs(
    specs: list[tuple[str, str, int, int]] | tuple[tuple[str, str, int, int], ...],
) -> list[dict[str, Any]]:
    return [
        {
            "id": step_id,
            "label": label,
            "status": "pending",
            "expected_seconds_min": minimum,
            "expected_seconds_max": maximum,
            "started_at": None,
            "finished_at": None,
            "detail": None,
        }
        for step_id, label, minimum, maximum in specs
    ]


def _component_phase_specs(
    component_id: str,
    label: str,
    install_mode: str,
) -> list[tuple[str, str, int, int]]:
    estimates = _COMPONENT_TIME_ESTIMATES.get(
        component_id,
        {
            "planning": (1, 3),
            "connect": (1, 30),
            "download": (10, 600),
            "verify": (2, 30),
            "install": (5, 180),
            "verified": (3, 60),
        },
    )
    if install_mode == "package_manager":
        phases = ("planning", "connect", "install", "verified")
    elif install_mode in {"managed", "rea_setup", "python_venv", "python_tool"}:
        phases = ("planning", "connect", "download", "verify", "install", "verified")
    else:
        phases = ("planning", "verified")
    phase_labels = {
        "planning": "检查本地版本与缓存",
        "connect": "连接下载源或安装器",
        "download": "下载并显示速度与剩余时间",
        "verify": "校验文件哈希",
        "install": "安装文件并写入环境",
        "verified": "使用 REA 原生探针验证能力",
    }
    return [
        (
            f"component_{component_id}_{phase}",
            f"{label}：{phase_labels[phase]}",
            estimates[phase][0],
            estimates[phase][1],
        )
        for phase in phases
    ]


def _enhanced_steps(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[tuple[str, str, int, int]] = [
        ("enhanced_plan", "读取当前平台的增强能力清单", 1, 5)
    ]
    for item in plan:
        if not item.get("auto_install"):
            continue
        specs.extend(
            _component_phase_specs(
                str(item.get("id") or ""),
                str(item.get("label") or item.get("id") or ""),
                str(item.get("install_mode") or ""),
            )
        )
    specs.append(("enhanced_scan", "刷新最终能力状态", 2, 30))
    return _steps_from_specs(specs)


def _job_estimate(steps: list[dict[str, Any]]) -> tuple[int, int]:
    return (
        sum(int(step["expected_seconds_min"]) for step in steps),
        sum(int(step["expected_seconds_max"]) for step in steps),
    )


def _transition_steps(
    job: dict[str, Any],
    stage: str,
    message: str,
    details: dict[str, Any] | None,
) -> None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return
    now = time.time()
    aliases = {
        "resolving_node": "node_npx",
        "preparing_rea": "rea_package",
        "writing_config": "write_config",
        "verifying_mcp": "verify_mcp",
        "managed_plan": "enhanced_plan",
        "scanning": "enhanced_scan" if job.get("kind") == "enhanced" else "final_scan",
    }
    step_id = aliases.get(stage, stage)
    component_id = ""
    component_phase = ""
    if stage.startswith("component_"):
        parts = stage.split("_", 2)
        if len(parts) == 3:
            component_id = parts[1]
            component_phase = parts[2]

    if component_id and component_phase in {"cached", "complete"}:
        component_steps = [
            step
            for step in steps
            if str(step.get("id") or "").startswith(f"component_{component_id}_")
        ]
        for step in component_steps:
            if step.get("status") in {"pending", "running"}:
                phase = str(step.get("id") or "").rsplit("_", 1)[-1]
                step["status"] = (
                    "skipped"
                    if component_phase == "cached"
                    and phase in {"connect", "download", "verify"}
                    else "succeeded"
                )
            step["finished_at"] = now
        job["current_step_id"] = None
        return

    if component_id and component_phase in {"failed", "skipped"}:
        target = next(
            (
                step
                for step in steps
                if step.get("id") == f"component_{component_id}_{component_phase}"
            ),
            None,
        )
        if target is None:
            target = next(
                (
                    step
                    for step in steps
                    if step.get("id") == f"component_{component_id}_verified"
                ),
                None,
            )
        if target is not None:
            target["status"] = "failed" if component_phase == "failed" else "skipped"
            target["detail"] = message
            target["finished_at"] = now
            job["current_step_id"] = str(target["id"])
        return

    if component_id and component_phase == "verified":
        step_id = f"component_{component_id}_verified"
    elif component_id and component_phase == "planning":
        step_id = f"component_{component_id}_planning"

    target = next((step for step in steps if step.get("id") == step_id), None)
    if target is None and component_id:
        step_id = f"component_{component_id}_{component_phase}"
        target = next((step for step in steps if step.get("id") == step_id), None)
    if target is not None and component_id:
        target_index = steps.index(target)
        for step in steps[:target_index]:
            if (
                str(step.get("id") or "").startswith(f"component_{component_id}_")
                and step.get("status") in {"pending", "running"}
            ):
                step["status"] = "succeeded"
                step["finished_at"] = now
    for step in steps:
        if step is not target and step.get("status") == "running":
            step["status"] = "succeeded"
            step["finished_at"] = now
    if target is not None:
        if target.get("status") == "pending":
            target["status"] = "running"
            target["started_at"] = now
        target["detail"] = message
        job["current_step_id"] = step_id


def _update_job(job_id: str, **changes: Any) -> None:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(changes)
        job["updated_at"] = time.time()


def _update_progress(
    job_id: str,
    stage: str,
    percent: float,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        _transition_steps(job, stage, message, details)
        component_result = (details or {}).get("component_result")
        if isinstance(component_result, dict):
            component_id = str(component_result.get("id") or "").strip()
            if component_id:
                component_results = job.setdefault("component_results", {})
                if isinstance(component_results, dict):
                    component_results[component_id] = dict(component_result)
        next_percent = max(
            float(job.get("percent") or 0),
            max(1.0, min(99.0, float(percent))),
        )
        job.update(
            {
                "stage": stage,
                "percent": next_percent,
                "message": message,
                "detail": details or {},
                "updated_at": time.time(),
            }
        )


def _finish_steps(job_id: str, *, ok: bool) -> None:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        steps = job.get("steps") if job else None
        if not isinstance(steps, list):
            return
        now = time.time()
        for step in steps:
            status = step.get("status")
            if status == "running":
                step["status"] = "succeeded" if ok else "failed"
                step["finished_at"] = now
            elif status == "pending":
                step["status"] = "skipped"
                step["finished_at"] = now
        if isinstance(job, dict):
            job["current_step_id"] = None
            job["updated_at"] = now


def _job_payload(job_id: str) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else {}


def _create_job(
    *,
    kind: str,
    steps: list[dict[str, Any]],
    plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = time.time()
    with _JOB_LOCK:
        for old_id, old in list(_JOBS.items()):
            if now - float(old.get("updated_at") or now) > 3600:
                _JOBS.pop(old_id, None)
                _JOB_CANCEL_EVENTS.pop(old_id, None)
        active = next(
            (
                item
                for item in _JOBS.values()
                if item.get("kind") == kind
                and item.get("status") in {"queued", "running"}
            ),
            None,
        )
        if active is not None:
            return dict(active)
        job_id = uuid.uuid4().hex
        minimum, maximum = _job_estimate(steps)
        _JOB_CANCEL_EVENTS[job_id] = threading.Event()
        _JOBS[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "stage": "queued",
            "percent": 0,
            "message": "任务已创建",
            "started_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "steps": steps,
            "current_step_id": None,
            "detail": {},
            "component_results": {},
            "plan": plan or [],
            "estimated_seconds_min": minimum,
            "estimated_seconds_max": maximum,
        }
        return dict(_JOBS[job_id])


async def _verify_rea_mcp(workspace: str | None = None) -> dict[str, Any]:
    """Verify the registered REA MCP through a temporary stdio connection."""

    record = rea_mcp_record(workspace)
    if not record:
        return {"ok": False, "tool_count": 0, "error": "REA MCP record is missing"}
    try:
        inventory = await asyncio.wait_for(
            asyncio.to_thread(probe_rea_mcp_inventory, record),
            timeout=120,
        )
        tools = inventory["tools"]
        if not tools:
            return {"ok": False, "tool_count": 0, "error": "REA MCP returned no tools"}
        mark_rea_mcp_state("connected", workspace, tool_count=len(tools))
        return {
            "ok": True,
            "tool_count": len(tools),
            "tools": [str(tool.get("name") or tool.get("id") or "") for tool in tools[:8]],
            "inventory": inventory,
            "inventory_verified": inventory.get("inventory_verified") is True,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tool_count": 0, "error": str(exc)}


async def _attach_capability_cards(
    scan: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    inventory = verification.get("inventory")
    if not isinstance(inventory, dict):
        scan["capability_cards"] = []
        scan["rea_inventory_diagnostics"] = {
            "error": verification.get("error") or "REA inventory is unavailable",
            "inventory_contract_ok": False,
        }
        return scan
    cards, diagnostics = build_capability_cards(
        scan,
        inventory["binary_session"],
        inventory["tools"],
    )
    scan["capability_cards"] = cards
    scan["rea_inventory_diagnostics"] = diagnostics
    return scan


async def _run_base_job(job_id: str) -> None:
    cancel_event = _JOB_CANCEL_EVENTS[job_id]
    try:
        _update_job(
            job_id,
            status="running",
            stage="starting",
            percent=1,
            message="正在安装基础能力；不会自动安装增强能力",
        )

        def progress(
            stage: str,
            percent: float,
            message: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            if cancel_event.is_set():
                raise InstallCancelled("安装已取消")
            _update_progress(job_id, stage, percent, message, details)

        installed = await asyncio.to_thread(
            install_rea_base,
            progress_callback=progress,
            cancel_check=cancel_event.is_set,
        )
        _update_progress(
            job_id,
            "verify_mcp",
            88,
            "启动临时 REA MCP 并校验 tools/list 与 binary_session",
            {"phase": "starting_mcp"},
        )
        verification = await _verify_rea_mcp()
        if cancel_event.is_set():
            raise InstallCancelled("安装已取消")
        _update_progress(
            job_id,
            "final_scan",
            96,
            "MCP 工具合同已校验，正在刷新基础能力",
            {"phase": "scanning", "mcp_tool_count": verification["tool_count"]},
        )
        refreshed = await asyncio.to_thread(scan_rea_setup)
        refreshed = await _attach_capability_cards(refreshed, verification)
        base_ready = bool(verification["ok"] and refreshed.get("ready"))
        _finish_steps(job_id, ok=base_ready)
        _update_job(
            job_id,
            status="succeeded" if base_ready else "failed",
            stage="completed" if base_ready else "failed",
            percent=100,
            message="基础能力已安装并通过 MCP 验证"
            if base_ready
            else "基础文件已处理，但 REA MCP 验证未通过",
            result={
                "kind": "base",
                "base_ready": base_ready,
                "mcp_verified": verification["ok"],
                "mcp_tool_count": verification["tool_count"],
                "mcp_tools": verification.get("tools", []),
                "tool_inventory_verified": verification.get(
                    "inventory_verified",
                    False,
                ),
                "verification_error": verification.get("error"),
                "enhanced_install_results": [],
                "capability_cards": refreshed.get("capability_cards", []),
                "rea_inventory_diagnostics": refreshed.get(
                    "rea_inventory_diagnostics",
                    {},
                ),
                "scan": refreshed,
                "previous_ready": installed.get("ready", False),
            },
            error=None if base_ready else verification.get("error"),
        )
    except InstallCancelled:
        _finish_steps(job_id, ok=False)
        _update_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            percent=100,
            message="基础安装已取消",
            error=None,
            result={"kind": "base", "base_ready": False, "cancelled": True},
        )
    except Exception as exc:  # noqa: BLE001
        _finish_steps(job_id, ok=False)
        _update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message=str(exc),
            error=str(exc),
            result={"kind": "base", "base_ready": False},
        )


async def _run_enhanced_job(job_id: str, selected_ids: list[str] | None) -> None:
    cancel_event = _JOB_CANCEL_EVENTS[job_id]
    try:
        _update_job(
            job_id,
            status="running",
            stage="starting",
            percent=1,
            message="正在安装增强能力；手动配置项不会阻塞自动安装",
        )

        def progress(
            stage: str,
            percent: float,
            message: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            if cancel_event.is_set():
                raise InstallCancelled("安装已取消")
            _update_progress(job_id, stage, percent, message, details)

        installed = await asyncio.to_thread(
            install_enhanced_components,
            selected_ids=selected_ids,
            progress_callback=progress,
            cancel_check=cancel_event.is_set,
        )
        if cancel_event.is_set():
            raise InstallCancelled("安装已取消")
        _update_progress(
            job_id,
            "verifying_mcp",
            97,
            "增强环境已写入，正在重新验证 REA MCP",
            {"phase": "starting_mcp"},
        )
        verification = await _verify_rea_mcp()
        refreshed = await asyncio.to_thread(scan_rea_setup)
        refreshed = await _attach_capability_cards(refreshed, verification)
        summary = dict(installed.get("summary") or {})
        completed = bool(verification["ok"])
        failed_count = int(summary.get("failed") or 0)
        needs_action = int(summary.get("needs_action") or 0)
        unsupported = int(summary.get("unsupported") or 0)
        _finish_steps(job_id, ok=completed)
        outcome = (
            "failed"
            if not completed
            else "partial"
            if failed_count or needs_action or unsupported
            else "complete"
        )
        _update_job(
            job_id,
            status="succeeded" if completed else "failed",
            stage="completed" if completed else "failed",
            percent=100,
            message=(
                "增强能力处理完成；部分能力需要手动配置或当前平台不支持"
                if completed and outcome == "partial"
                else "增强能力已安装并验证"
                if completed
                else "增强环境已写入，但 REA MCP 重新验证失败"
            ),
            result={
                "kind": "enhanced",
                "outcome": outcome,
                "mcp_verified": verification["ok"],
                "mcp_tool_count": verification["tool_count"],
                "mcp_tools": verification.get("tools", []),
                "verification_error": verification.get("error"),
                "enhanced_install_results": installed.get("results", []),
                "enhanced_plan": installed.get("plan", []),
                "summary": summary,
                "capability_cards": refreshed.get("capability_cards", []),
                "rea_inventory_diagnostics": refreshed.get(
                    "rea_inventory_diagnostics",
                    {},
                ),
                "tool_inventory_verified": verification.get(
                    "inventory_verified",
                    False,
                ),
                "scan": refreshed,
            },
            error=None if completed else verification.get("error"),
        )
    except InstallCancelled:
        _finish_steps(job_id, ok=False)
        _update_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            percent=100,
            message="增强能力安装已取消",
            error=None,
            result={"kind": "enhanced", "outcome": "cancelled", "cancelled": True},
        )
    except Exception as exc:  # noqa: BLE001
        _finish_steps(job_id, ok=False)
        _update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message=str(exc),
            error=str(exc),
            result={"kind": "enhanced", "outcome": "failed"},
        )


def register_rea_setup_handlers(channel: Any) -> None:
    """Register status, base install, enhanced install, and assisted setup RPCs."""

    async def _rea_status_get(ws, req_id, params, session_id, user_id=None):
        del session_id, user_id
        try:
            scope = str((params or {}).get("scope") or "all").strip().lower()
            if scope not in {"all", "base", "enhanced"}:
                scope = "all"
            include_enhanced = scope != "base"
            payload = await asyncio.to_thread(
                scan_rea_setup,
                include_enhanced=include_enhanced,
            )
            if payload.get("ready"):
                verification = await _verify_rea_mcp()
                if include_enhanced:
                    payload = await _attach_capability_cards(payload, verification)
                else:
                    payload["capability_cards"] = []
                    payload["rea_inventory_diagnostics"] = {
                        "mcp_tool_count": verification["tool_count"],
                        "mcp_verified": verification["ok"],
                        "error": verification.get("error"),
                    }
            else:
                payload["capability_cards"] = []
                payload["rea_inventory_diagnostics"] = {
                    "error": "REA base environment is not ready",
                }
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as exc:  # noqa: BLE001
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="REA_STATUS_FAILED",
            )

    async def _start_job(
        ws,
        req_id,
        *,
        kind: str,
        runner,
        steps: list[dict[str, Any]],
        plan: list[dict[str, Any]] | None = None,
    ) -> None:
        payload = _create_job(kind=kind, steps=steps, plan=plan)
        if payload["status"] == "queued":
            task = asyncio.create_task(runner(payload["job_id"]))
            _JOB_TASKS.add(task)
            task.add_done_callback(_JOB_TASKS.discard)
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _rea_base_install(ws, req_id, params, session_id, user_id=None):
        del params, session_id, user_id
        await _start_job(
            ws,
            req_id,
            kind="base",
            runner=_run_base_job,
            steps=_base_steps(),
        )

    async def _rea_enhanced_install(ws, req_id, params, session_id, user_id=None):
        del session_id, user_id
        raw_ids = (params or {}).get("components")
        if raw_ids is None:
            selected_ids = None
        elif isinstance(raw_ids, list):
            selected_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        else:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="components must be an array",
                code="BAD_REQUEST",
            )
            return
        try:
            plan = await asyncio.to_thread(enhanced_component_plan, selected_ids)
            await _start_job(
                ws,
                req_id,
                kind="enhanced",
                runner=lambda job_id: _run_enhanced_job(job_id, selected_ids),
                steps=_enhanced_steps(plan),
                plan=plan,
            )
        except Exception as exc:  # noqa: BLE001
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="REA_ENHANCED_PLAN_FAILED",
            )

    async def _rea_setup_progress(ws, req_id, params, session_id, user_id=None):
        del session_id, user_id
        job_id = str((params or {}).get("job_id") or "").strip()
        payload = _job_payload(job_id)
        if not payload:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="REA install job not found",
                code="REA_SETUP_JOB_NOT_FOUND",
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _rea_component_configure(ws, req_id, params, session_id, user_id=None):
        del session_id, user_id
        component_id = str((params or {}).get("id") or "").strip()
        values = (params or {}).get("values")
        if not component_id or not isinstance(values, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="id and values are required",
                code="BAD_REQUEST",
            )
            return
        try:
            await asyncio.to_thread(configure_component, component_id, values)
            verification = await _verify_rea_mcp()
            scan = await asyncio.to_thread(scan_rea_setup)
            scan = await _attach_capability_cards(scan, verification)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={
                    "configured": True,
                    "mcp_verified": verification["ok"],
                    "verification_error": verification.get("error"),
                    "scan": scan,
                },
            )
        except Exception as exc:  # noqa: BLE001
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="REA_COMPONENT_CONFIGURE_FAILED",
            )

    async def _rea_setup_cancel(ws, req_id, params, session_id, user_id=None):
        del session_id, user_id
        job_id = str((params or {}).get("job_id") or "").strip()
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
            cancel_event = _JOB_CANCEL_EVENTS.get(job_id)
            if job is None or cancel_event is None:
                payload = None
            else:
                cancel_event.set()
                job["message"] = "正在取消安装"
                job["updated_at"] = time.time()
                payload = dict(job)
        if payload is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="REA install job not found",
                code="REA_SETUP_JOB_NOT_FOUND",
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    channel.register_method("rea.status.get", _rea_status_get)
    channel.register_method("rea.base.install", _rea_base_install)
    channel.register_method("rea.enhanced.install", _rea_enhanced_install)
    channel.register_method("rea.setup.install", _rea_base_install)
    channel.register_method("rea.setup.progress", _rea_setup_progress)
    channel.register_method("rea.setup.cancel", _rea_setup_cancel)
    channel.register_method("rea.component.configure", _rea_component_configure)


__all__ = ["register_rea_setup_handlers"]
