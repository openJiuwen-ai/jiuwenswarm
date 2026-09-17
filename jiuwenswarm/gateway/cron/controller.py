from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, List
from zoneinfo import ZoneInfo

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.gateway.cron.cron_expr import normalize_cron_expr
from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DESCRIPTION_MAX_LENGTH,
    CRON_JOB_NAME_MAX_LENGTH,
    CronTargetChannel,
    cron_job_metadata,
    cron_job_modes_for_tools,
    is_valid_target_channel_id,
    normalize_cron_job_mode,
    normalize_target_channel_id,
    validate_cron_model,
)
from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService, _cron_next_push_dt
from jiuwenswarm.gateway.cron.store import CronJobStore


class CronController:
    """High-level cron API used by WebChannel handlers. Singleton."""

    _instance: ClassVar[CronController | None] = None

    def __init__(self, *, store: CronJobStore, scheduler: CronSchedulerService) -> None:
        self._store = store
        self._scheduler = scheduler
        self._target_channel: CronTargetChannel | None = None

    def set_target_channel(self, channel: CronTargetChannel) -> None:
        self._target_channel = channel

    @classmethod
    def get_instance(
        cls,
        *,
        store: CronJobStore | None = None,
        scheduler: CronSchedulerService | None = None,
    ) -> CronController:
        """Return the singleton instance.

        On first call, store and scheduler are required to create the instance.
        On subsequent calls, both can be omitted to get the existing instance.

        Args:
            store: Required only on first call.
            scheduler: Required only on first call.

        Returns:
            The singleton CronController.

        Raises:
            RuntimeError: If instance not yet initialized and store/scheduler not provided.
        """
        if cls._instance is not None:
            return cls._instance
        if store is None or scheduler is None:
            raise RuntimeError(
                "CronController not initialized. Call get_instance(store=..., scheduler=...) first."
            )
        cls._instance = cls(store=store, scheduler=scheduler)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton. For testing only."""
        cls._instance = None

    @staticmethod
    def _validate_schedule(*, cron_expr: str, timezone: str) -> None:
        tz = ZoneInfo(timezone)
        base = datetime.now(tz=tz)
        _ = _cron_next_push_dt(cron_expr, base)

    _DESCRIPTION_TIME_KEYWORDS = ("每天", "每周", "每月", "上午", "下午", "早上", "晚上", "凌晨")

    def _normalize_targets(self, raw: Any) -> str:
        """将 targets 规范为 CronTargetChannel 枚举值。"""
        raw_s = str(raw or "").strip()
        if self._target_channel is None and not raw_s:
            raise ValueError("targets is required when target_channel is not set")
        if not raw_s:
            return normalize_target_channel_id(self._target_channel.value)
        if not is_valid_target_channel_id(raw_s):
            raise ValueError(
                "targets must be one of tui/web/feishu/dingtalk/whatsapp/wecom/xiaoyi/wechat"
                " or feishu_enterprise:<app_id>"
            )
        return normalize_target_channel_id(raw_s)

    @classmethod
    def _normalize_description(cls, description: str, name: str) -> str:
        """若 description 含时间/频率用语且 name 为纯任务，则只保留任务内容（用 name）。"""
        description = (description or "").strip()
        name = (name or "").strip()
        if not name:
            return description
        if not any(kw in description for kw in cls._DESCRIPTION_TIME_KEYWORDS):
            return description
        if name in description or description.endswith(name):
            return name
        return description

    @staticmethod
    def _routing_session_id(targets: str, raw: Any) -> str | None:
        """Accept session_id for all channels; feishu_enterprise requires SessionMap format."""
        targets_s = str(targets or "").strip()
        raw_s = str(raw or "").strip() if isinstance(raw, str) else ""
        if not raw_s:
            return None
        if targets_s.startswith("feishu_enterprise:"):
            if "::" not in raw_s:
                return None
            parts = raw_s.split("::")
            if len(parts) < 3 or parts[0] != "feishu":
                return None
            return raw_s
        return raw_s

    async def list_jobs(self) -> list[dict[str, Any]]:
        jobs = await self._store.list_jobs()
        return [j.to_dict() for j in jobs]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = await self._store.get_job(job_id)
        return job.to_dict() if job else None

    @staticmethod
    def job_metadata() -> dict[str, Any]:
        return cron_job_metadata()

    async def create_job(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        cron_expr = normalize_cron_expr(str(params.get("cron_expr") or "").strip())
        timezone = str(params.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
        enabled = bool(params.get("enabled", True))
        description = str(params.get("description") or "")
        wake_offset_seconds = params.get("wake_offset_seconds", None)
        raw_targets = params.get("targets")
        mode = params.get("mode")
        if mode is not None and str(mode).strip():
            mode = normalize_cron_job_mode(mode)
        else:
            mode = None
        model_name = validate_cron_model(params.get("model_name"))

        targets = self._normalize_targets(raw_targets)

        self._validate_schedule(cron_expr=cron_expr, timezone=timezone)
        description = self._normalize_description(description, name)

        routing_sid = self._routing_session_id(targets, params.get("session_id"))
        chat_type = params.get("chat_type")
        delete_after_run = params.get("delete_after_run")
        timeout_seconds = params.get("timeout_seconds")
        # work_mode 解析(严格校验:非法值由 resolve_request_work_mode 返回 BAD_REQUEST);
        # 默认值按 controller 目标通道推断(tui→code,web/未设置→work)
        from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode
        default_channel = (
            self._target_channel.value if self._target_channel is not None else "web"
        )
        work_mode, mode_err = resolve_request_work_mode(params, default_channel)
        if mode_err is not None:
            raise ValueError(f"invalid work_mode: {params.get('work_mode')!r}")
        # project_id / project_dir → project_id 解析(设计文档 §6.1 + work_mode 隔离):
        # 优先接受显式 project_id(修改计划 §5 链路 A,与 CronTools 保持一致):
        # 1. 默认项目 ID(default/default_code)→ 直接使用,按 project_id 映射 work_mode
        # 2. 真实 project_id → 校验存在且未隐藏,从 Project 记录注入精确 work_mode
        # 3. 无显式 project_id → 按 (work_mode, project_dir) 解析可见项目,
        #    匹配不到(含命中隐藏项目 / 无命中)归默认项目
        # 非绝对路径抛 ValueError → BAD_REQUEST
        from jiuwenswarm.server.runtime.session.project_store import resolve_cron_project_binding

        raw_project_id = str(params.get("project_id") or "").strip()
        project_dir_raw = params.get("project_dir")
        project_dir_val = (
            str(project_dir_raw).strip()
            if isinstance(project_dir_raw, str) and project_dir_raw.strip()
            else ""
        )
        binding = resolve_cron_project_binding(raw_project_id, project_dir_val, work_mode)
        if binding.error is not None:
            raise ValueError(binding.error)
        resolved_project_id = binding.project_id
        work_mode = binding.work_mode
        app_id = str(params.get("app_id") or "").strip()
        # user_id：web 端创建定时任务时由 handler 注入 params（见 _cron_job_create），
        # 执行时透传给 faas 的 X-Session-Context。agent 内部创建的 cron 无 user_id 即存空串。
        user_id = str(params.get("user_id") or "").strip()
        job = await self._store.create_job(
            job_id=str(params.get("id") or "").strip() or None,
            name=name,
            cron_expr=cron_expr,
            timezone=timezone,
            enabled=enabled,
            wake_offset_seconds=int(wake_offset_seconds) if wake_offset_seconds is not None else None,
            description=description,
            targets=targets,
            session_id=routing_sid,
            chat_type=chat_type,
            mode=mode,
            delete_after_run=delete_after_run,
            timeout_seconds=timeout_seconds,
            project_id=resolved_project_id,
            model_name=model_name,
            app_id=app_id,
            work_mode=work_mode,
            user_id=user_id,
        )
        await self._scheduler.reload()
        return job.to_dict()

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        patch = dict(patch or {})
        if "mode" in patch:
            patch["mode"] = normalize_cron_job_mode(patch.get("mode"))
        if "model_name" in patch:
            patch["model_name"] = validate_cron_model(patch.get("model_name"))
        if "targets" in patch:
            patch["targets"] = self._normalize_targets(patch["targets"])
        existing = await self._store.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")
        if "cron_expr" in patch:
            patch["cron_expr"] = normalize_cron_expr(str(patch["cron_expr"]).strip())
        if "cron_expr" in patch or "timezone" in patch:
            cron_expr = str(patch.get("cron_expr") or existing.cron_expr).strip()
            timezone = str(patch.get("timezone") or existing.timezone).strip()
            self._validate_schedule(cron_expr=cron_expr, timezone=timezone)
        if "description" in patch:
            name = str(patch.get("name") or existing.name or "").strip()
            patch["description"] = self._normalize_description(str(patch.get("description") or ""), name)

        # work_mode / project_id / project_dir 重解析(共享 helper):
        # 与 cron_tools.py update_job 共用同一 ``resolve_cron_job_patch``,
        # 确保 Web RPC 与 AgentTool 两条链路逻辑一致。
        from jiuwenswarm.server.runtime.session.project_store import resolve_cron_job_patch
        resolve_cron_job_patch(
            patch,
            existing_work_mode=existing.work_mode or "",
            channel_id="web",
        )

        final_targets = str(patch.get("targets") or existing.targets).strip()
        if "session_id" in patch:
            patch["session_id"] = self._routing_session_id(
                final_targets, patch.get("session_id")
            )
        elif "targets" in patch:
            patch["session_id"] = self._routing_session_id(
                final_targets, existing.session_id
            )

        job = await self._store.update_job(job_id, patch)
        await self._scheduler.reload()
        return job.to_dict()

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        deleted = await self._store.delete_job(job_id, force=force)
        if deleted:
            await self._scheduler.reload()
        return deleted

    async def toggle_job(self, job_id: str, enabled: bool) -> dict[str, Any]:
        job = await self._store.update_job(job_id, {"enabled": bool(enabled)})
        await self._scheduler.reload()
        return job.to_dict()

    async def preview_job(self, job_id: str, count: int = 5) -> list[dict[str, Any]]:
        job = await self._store.get_job(job_id)
        if job is None:
            raise KeyError("job not found")
        count = max(1, min(int(count), 50))

        tz = ZoneInfo(job.timezone)
        base = datetime.now(tz=tz)
        out: list[dict[str, Any]] = []
        push_dt = base
        for _ in range(count):
            try:
                push_dt = _cron_next_push_dt(job.cron_expr, push_dt)
            except Exception as exc:  # noqa: BLE001
                _msg = str(exc)
                if "CroniterBadDateError" in _msg or "failed to find next date" in _msg:
                    break
                raise
            if out and push_dt.isoformat() == out[-1]["push_at"]:
                break
            wake_dt = push_dt - timedelta(seconds=max(0, int(job.wake_offset_seconds or 0)))
            out.append({"wake_at": wake_dt.isoformat(), "push_at": push_dt.isoformat()})
        return out

    async def run_now(self, job_id: str) -> str:
        run_id = await self._scheduler.trigger_run_now(job_id)
        return run_id

    async def run_now_info(self, job_id: str) -> dict[str, str]:
        return await self._scheduler.trigger_run_now_info(job_id)

    async def _create_job_tool(
        self,
        name: str,
        cron_expr: str,
        timezone: str,
        description: str,
        targets: str = "",
        enabled: bool = True,
        wake_offset_seconds: int | None = None,
        mode: str | None = None,
        timeout_seconds: int | None = None,
        model_name: str | None = None,
        project_dir: str | None = None,
        project_id: str | None = None,
        work_mode: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "cron_expr": cron_expr,
            "timezone": timezone,
            "targets": targets,
            "enabled": enabled,
            "description": description,
        }
        if wake_offset_seconds is not None:
            params["wake_offset_seconds"] = wake_offset_seconds
        if mode is not None and str(mode).strip():
            params["mode"] = mode
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        if model_name is not None and str(model_name).strip():
            params["model_name"] = validate_cron_model(model_name)
        if project_dir is not None:
            params["project_dir"] = str(project_dir).strip()
        if project_id is not None and str(project_id).strip():
            params["project_id"] = str(project_id).strip()
        if work_mode is not None and str(work_mode).strip():
            params["work_mode"] = str(work_mode).strip()
        return await self.create_job(params)

    async def _update_job_tool(
        self, job_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.update_job(job_id, patch)

    async def _preview_job_tool(
        self, job_id: str, count: int = 5
    ) -> list[dict[str, Any]]:
        return await self.preview_job(job_id, count)

    def get_tools(self) -> List[Tool]:
        """Return cron job tools for registration in the openJiuwen Runner.
        Tools to be returned:
            list_jobs
            get_job
            create_job
            update_job
            delete_job
            toggle_job
            preview_job

        Usage:
            toolkit = CronController(xxxxxx)
            tools = toolkit.get_tools()
            Runner.resource_mgr.add_tool(tools)
            for t in tools:
                agent.ability_manager.add(t.card)

        Returns:
            List of Tool instances (LocalFunction) ready for Runner/agent registration.
        """

        def make_tool(
            name: str,
            description: str,
            input_params: dict,
            func,
        ) -> Tool:
            card = ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="cron_list_jobs",
                description=(
                    "List all cron jobs. Returns a list of job objects with"
                    " id, name, cron_expr, timezone, enabled, etc."
                ),
                input_params={"type": "object", "properties": {}},
                func=self.list_jobs,
            ),
            make_tool(
                name="cron_get_job",
                description="Get a single cron job by id. Returns job details or None if not found.",
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "The job id to look up",
                        }
                    },
                    "required": ["job_id"],
                },
                func=self.get_job,
            ),
            make_tool(
                name="cron_create_job",
                description=(
                    "Create a scheduled cron job.\n"
                    "cron_expr:\n"
                    "- Recurring (5 fields): minute hour day month day-of-week.\n"
                    "  Example: daily 9:00 = '0 9 * * *', every Monday 9:00 = '0 9 * * 1'.\n"
                    "- Relative time (e.g. \"in X minutes\"): take now in the given timezone, "
                    "compute run_at = now + X minutes, then encode run_at as 7-field cron "
                    "with a fixed year (minute hour day month day-of-week second year). "
                    "Example: run_at (Mar 19, 2026 10:07:00 local) -> '0 7 10 19 3 * 2026'.\n"
                    "- One-shot (runs only once): must use 7 fields with a fixed year: "
                    "minute hour day month day-of-week second year. "
                    "Example: 2026-03-28 17:00 (local) -> '0 17 28 3 * 0 2026'.\n"
                    "Warning: if you use a 5-field expression with fixed day/month "
                    "but year semantics implicitly '*', it will repeat every year; "
                    "for a real one-shot, use the 7-field form with a fixed year.\n"
                    "description should contain task content only (no time/frequency). "
                    "timezone defaults to Asia/Shanghai."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": f"Job name (max {CRON_JOB_NAME_MAX_LENGTH} characters).",
                        },
                        "cron_expr": {
                            "type": "string",
                            "description": (
                                "Cron expression. "
                                "Recurring jobs use 5 fields: minute hour dom month day-of-week. "
                                "One-shot jobs must use 7 fields: minute hour dom month "
                                "day-of-week second year (fixed year). "
                                "For relative time, treat it as one-shot: compute run_at = now + X minutes, "
                                "then encode it as a 7-field expression with a fixed year. "
                                "Example: 2026-03-28 17:00 (local) -> '0 17 28 3 * 0 2026'."
                            ),
                        },
                        "timezone": {
                            "type": "string",
                            "description": "Time zone (IANA), e.g. Asia/Shanghai",
                            "default": "Asia/Shanghai",
                        },
                        "targets": {
                            "type": "string",
                            "enum": [e.value for e in CronTargetChannel],
                            "description": (
                                "Delivery channel: tui, web, feishu, dingtalk, "
                                "whatsapp, wecom, xiaoyi, wechat. "
                                "If omitted, use the current request source channel."
                            ),
                        },
                        "enabled": {
                            "type": "boolean",
                            "description": "Whether the job is enabled",
                            "default": True,
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Task payload text sent to the assistant at run time. "
                                "Do not include time or frequency. "
                                f"Max {CRON_JOB_DESCRIPTION_MAX_LENGTH} characters."
                            ),
                        },
                        "wake_offset_seconds": {
                            "type": "integer",
                            "description": "Seconds to wake before push. Default 0",
                            "default": 0,
                        },
                        "mode": {
                            "type": "string",
                            "enum": cron_job_modes_for_tools(),
                            "description": (
                                "Agent runtime mode when the job runs. "
                                "Default agent. Use team for multi-agent team execution."
                            ),
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                "Execution timeout in seconds (60-259200). "
                                "Default 600 for normal modes and 1200 for team modes."
                            ),
                        },
                        "model_name": {
                            "type": "string",
                            "description": (
                                "Model name or alias to use when the job runs. "
                                "If omitted, uses the AgentServer default model."
                            ),
                        },
                        "project_dir": {
                            "type": "string",
                            "description": (
                                "Absolute path to the project directory this job belongs to. "
                                "If omitted, uses the current session's project."
                            ),
                        },
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Explicit project id (takes priority over project_dir). "
                                "Omit to resolve from project_dir + work_mode."
                            ),
                        },
                        "work_mode": {
                            "type": "string",
                            "enum": ["code", "work"],
                            "description": (
                                "Working mode of the target project (code/work). "
                                "Defaults to current channel default (tui->code, web->work). "
                                "Only used when project_id is not provided; ignored if project_id "
                                "is provided (work_mode inherited from the project)."
                            ),
                        },
                    },
                    "required": ["name", "cron_expr", "timezone", "description"],
                },
                func=self._create_job_tool,
            ),
            make_tool(
                name="cron_update_job",
                description=(
                    "Update an existing cron job. Pass job_id and a patch dict with fields to update "
                    "(name, enabled, cron_expr, timezone, description, wake_offset_seconds, "
                    "targets, mode, model_name, project_dir, project_id). "
                    f"name max {CRON_JOB_NAME_MAX_LENGTH} characters, "
                    f"description max {CRON_JOB_DESCRIPTION_MAX_LENGTH} characters."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "Job id to update"},
                        "patch": {
                            "type": "object",
                            "description": (
                                "Fields to update (name, enabled, cron_expr, timezone, "
                                "description, wake_offset_seconds, targets, mode, model_name, "
                                "project_dir, project_id). work_mode is not accepted as an "
                                "independent patch field; to change work_mode, patch project_id "
                                "or project_dir + work_mode (work_mode only disambiguates the "
                                "target project when resolving project_dir)."
                            ),
                            "properties": {
                                "targets": {
                                    "type": "string",
                                    "enum": [e.value for e in CronTargetChannel],
                                    "description": (
                                        "推送频道：web/tui/feishu/dingtalk/whatsapp/wecom/xiaoyi/wechat"
                                    ),
                                },
                                "mode": {
                                    "type": "string",
                                    "enum": cron_job_modes_for_tools(),
                                    "description": "Agent runtime mode (agent, team, ...)",
                                },
                                "model_name": {
                                    "type": "string",
                                    "description": "Model to use when the job runs. \
                                        Set to empty string to reset to default.",
                                },
                                "project_dir": {
                                    "type": "string",
                                    "description": "Absolute path to the project directory. \
                                        Set to empty string for default project.",
                                },
                                "project_id": {
                                    "type": "string",
                                    "description": (
                                        "Directly patch the project_id (takes priority over "
                                        "project_dir). work_mode is re-injected from the "
                                        "project record."
                                    ),
                                },
                                "work_mode": {
                                    "type": "string",
                                    "enum": ["code", "work"],
                                    "description": (
                                        "Disambiguates target project when patching "
                                        "project_dir. Not a standalone patchable field."
                                    ),
                                },
                            },
                        },
                    },
                    "required": ["job_id", "patch"],
                },
                func=self._update_job_tool,
            ),
            make_tool(
                name="cron_delete_job",
                description="Delete a cron job by id. Returns True if deleted, False if not found.",
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "Job id to delete"},
                    },
                    "required": ["job_id"],
                },
                func=self.delete_job,
            ),
            make_tool(
                name="cron_toggle_job",
                description="Enable or disable a cron job. Pass job_id and enabled (true/false).",
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "Job id"},
                        "enabled": {
                            "type": "boolean",
                            "description": "Whether to enable the job",
                        },
                    },
                    "required": ["job_id", "enabled"],
                },
                func=self.toggle_job,
            ),
            make_tool(
                name="cron_preview_job",
                description=(
                    "Preview next N scheduled run times for a job. "
                    "Returns list of {wake_at, push_at} timestamps."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "Job id"},
                        "count": {
                            "type": "integer",
                            "description": "Number of runs to preview (1-50, default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["job_id"],
                },
                func=self._preview_job_tool,
            ),
        ]
