# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""新 Heartbeat 任务数据模型 — 绑定当前会话的自动续跑触发器.

与旧探活(HealthCheck/Probe)严格区分:
- 旧 ``gateway/heartbeat/heartbeat.py`` 是 ``HEARTBEAT.md`` 驱动的全局周期探活,本质是健康检查,
  将整体迁移到 ``gateway/health_check/``。
- 本模块定义的是"回到原 ``channel_id + session_id`` 继续上下文"的心跳任务,
  按 schedule 投递 follow-up prompt,使 Agent 回到同一线程继续处理。

字段命名原则:与 Cron 已有字段语义一致的必须同名同义(``id/name/enabled/created_at/updated_at/
timezone``);Heartbeat 独有语义才新增字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cron_schedule import validate_cron_expression

# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

# kind 固定值,便于未来 Automation 统一抽象。
HEARTBEAT_KIND: str = "heartbeat"

# status 状态机五态。
STATUS_SCHEDULED: str = "scheduled"
STATUS_RUNNING: str = "running"
STATUS_COMPLETED: str = "completed"
STATUS_EXPIRED: str = "expired"
STATUS_DISABLED: str = "disabled"

HEARTBEAT_STATUSES: tuple[str, ...] = (
    STATUS_SCHEDULED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_DISABLED,
)

# 终态:这些状态下 enabled 必须为 false、next_run_at 必须为 None。
HEARTBEAT_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_EXPIRED, STATUS_DISABLED}
)

# metadata.source 枚举。
SOURCE_AGENT_TOOL: str = "agent_tool"
SOURCE_WEB_RPC: str = "web_rpc"
SOURCE_TUI_RPC: str = "tui_rpc"
SOURCE_SCHEDULE_RECOVERY: str = "schedule_recovery"

HEARTBEAT_SOURCES: tuple[str, ...] = (
    SOURCE_AGENT_TOOL,
    SOURCE_WEB_RPC,
    SOURCE_TUI_RPC,
    SOURCE_SCHEDULE_RECOVERY,
)

# schedule.type 三类。
SCHEDULE_INTERVAL: str = "interval"
SCHEDULE_CRON: str = "cron"
SCHEDULE_ONCE: str = "once"

# datetime supports Unix seconds through year 9999. Larger values are almost
# certainly millisecond timestamps and cannot represent a useful schedule.
MAX_UNIX_TIMESTAMP_SECONDS: float = 253_402_300_799.0

HEARTBEAT_SCHEDULE_TYPES: tuple[str, ...] = (SCHEDULE_INTERVAL, SCHEDULE_CRON, SCHEDULE_ONCE)

# 并发策略。
CONCURRENCY_SKIP: str = "skip"
CONCURRENCY_QUEUE: str = "queue"
CONCURRENCY_REPLACE: str = "replace"

HEARTBEAT_CONCURRENCY_POLICIES: tuple[str, ...] = (
    CONCURRENCY_SKIP,
    CONCURRENCY_QUEUE,
    CONCURRENCY_REPLACE,
)

# 会话删除后处理策略。
SESSION_DELETED_DISABLE: str = "disable"
SESSION_DELETED_COMPLETED: str = "completed"

HEARTBEAT_SESSION_DELETED_POLICIES: tuple[str, ...] = (
    SESSION_DELETED_DISABLE,
    SESSION_DELETED_COMPLETED,
)

# 默认值(可被 config 覆盖)。
DEFAULT_TIMEZONE: str = "Asia/Shanghai"
DEFAULT_MAX_RUNS: int = 12
DEFAULT_CONCURRENCY_POLICY: str = CONCURRENCY_SKIP
DEFAULT_SESSION_DELETED_POLICY: str = SESSION_DELETED_DISABLE
MIN_INTERVAL_SECONDS: int = 60

# 名称最大长度,对齐 Cron 的 CRON_JOB_NAME_MAX_LENGTH。
HEARTBEAT_NAME_MAX_LENGTH: int = 64
# prompt 最大长度,对齐 Cron 的 description 上限。
HEARTBEAT_PROMPT_MAX_LENGTH: int = 2000

# ID 前缀。
HEARTBEAT_ID_PREFIX: str = "hb_"


def _validate_timezone(raw: str, *, default: str = DEFAULT_TIMEZONE) -> str:
    """校验 IANA 时区,空值回退默认。"""
    value = str(raw or "").strip()
    if not value:
        return default
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid timezone {value!r}: {exc}") from exc
    return value


def validate_metadata_source(raw: Any) -> str:
    """校验 metadata.source 枚举,缺失或非法抛 ValueError(由 controller 在创建/更新时调用).

    scheduler 消费时不抛异常,而是记录 warning 后兜底 ``schedule_recovery``。
    """
    if raw is None:
        raise ValueError("metadata.source is required")
    value = str(raw).strip()
    if value not in HEARTBEAT_SOURCES:
        raise ValueError(
            f"invalid metadata.source {raw!r}. Valid: {', '.join(HEARTBEAT_SOURCES)}"
        )
    return value


def _persisted_bool(
    data: dict[str, Any], key: str, *, default: bool | None
) -> bool | None:
    """Read a persisted boolean without coercing strings or numbers."""
    value = data.get(key, default)
    if value is None and default is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatSchedule:
    """心跳调度配置:支持 interval / cron / once."""

    type: str
    # interval 模式使用,>=60。
    interval_seconds: int | None = None
    # cron 模式使用,支持 5 字段 crontab 或普通 Cron 任务使用的 7 字段格式。
    cron_expr: str | None = None
    # cron 模式时区,默认 Asia/Shanghai。
    timezone: str | None = None
    # once 模式使用,Unix 时间戳。
    run_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.type == SCHEDULE_INTERVAL:
            d["interval_seconds"] = int(self.interval_seconds) if self.interval_seconds is not None else None
        elif self.type == SCHEDULE_CRON:
            d["cron_expr"] = self.cron_expr or ""
            d["timezone"] = self.timezone or DEFAULT_TIMEZONE
        elif self.type == SCHEDULE_ONCE:
            d["run_at"] = float(self.run_at) if self.run_at is not None else None
        return d

    @staticmethod
    def from_dict(data: dict[str, Any], *, default_timezone: str = DEFAULT_TIMEZONE) -> "HeartbeatSchedule":
        if not isinstance(data, dict):
            raise ValueError("schedule must be object")
        stype = str(data.get("type") or "").strip()
        if stype not in HEARTBEAT_SCHEDULE_TYPES:
            raise ValueError(
                f"invalid schedule.type {stype!r}. Valid: {', '.join(HEARTBEAT_SCHEDULE_TYPES)}"
            )

        if stype == SCHEDULE_INTERVAL:
            raw_iv = data.get("interval_seconds", None)
            try:
                interval_seconds = int(raw_iv) if raw_iv is not None else None
            except Exception as exc:  # noqa: BLE001
                raise ValueError("schedule.interval_seconds must be int") from exc
            if interval_seconds is None:
                raise ValueError("schedule.interval_seconds is required for interval type")
            if interval_seconds < MIN_INTERVAL_SECONDS:
                raise ValueError(
                    f"schedule.interval_seconds must be at least {MIN_INTERVAL_SECONDS}"
                )
            return HeartbeatSchedule(
                type=SCHEDULE_INTERVAL,
                interval_seconds=interval_seconds,
            )

        if stype == SCHEDULE_CRON:
            cron_expr = str(data.get("cron_expr") or "").strip()
            if not cron_expr:
                raise ValueError("schedule.cron_expr is required for cron type")
            tz = _validate_timezone(
                str(data.get("timezone") or "").strip() or default_timezone,
                default=default_timezone,
            )
            # 与普通 Cron 任务保持一致，支持 5 字段和 7 字段，并保留原表达式。
            validate_cron_expression(cron_expr, timezone=tz)
            return HeartbeatSchedule(
                type=SCHEDULE_CRON,
                cron_expr=cron_expr,
                timezone=tz,
            )

        # once
        raw_run_at = data.get("run_at", None)
        try:
            run_at = float(raw_run_at) if raw_run_at is not None else None
        except Exception as exc:  # noqa: BLE001
            raise ValueError("schedule.run_at must be number") from exc
        if run_at is None:
            raise ValueError("schedule.run_at is required for once type")
        return HeartbeatSchedule(type=SCHEDULE_ONCE, run_at=run_at)

    def validate(self) -> None:
        """二次校验(已被 from_dict 覆盖,供已构造对象复检)。"""
        HeartbeatSchedule.from_dict(self.to_dict())


# ---------------------------------------------------------------------------
# RunState — 轻量运行状态
# ---------------------------------------------------------------------------


# last_run_status 取值。
RUN_SUCCEEDED: str = "succeeded"
RUN_FAILED: str = "failed"
RUN_SKIPPED: str = "skipped"
RUN_CANCELLED: str = "cancelled"

HEARTBEAT_RUN_STATUSES: tuple[str, ...] = (RUN_SUCCEEDED, RUN_FAILED, RUN_SKIPPED, RUN_CANCELLED)


@dataclass
class HeartbeatRunState:
    """心跳任务的轻量运行状态(持久化在 job 内,非独立文件)。"""

    current_run_id: str | None = None
    current_run_started_at: float | None = None
    last_run_status: str | None = None
    last_error: str | None = None
    last_cancel_status: str | None = None
    last_cancel_error: str | None = None
    queued_run_id: str | None = None
    queued_trigger: str | None = None
    queued_reschedule: bool = False
    current_trigger: str | None = None
    current_reschedule: bool = False
    resume_status: str | None = None
    resume_enabled: bool | None = None
    resume_next_run_at: float | None = None
    skipped_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "current_run_id": self.current_run_id,
            "current_run_started_at": self.current_run_started_at,
            "last_run_status": self.last_run_status,
            "last_error": self.last_error,
            "last_cancel_status": self.last_cancel_status,
            "last_cancel_error": self.last_cancel_error,
            "queued_run_id": self.queued_run_id,
            "queued_trigger": self.queued_trigger,
            "queued_reschedule": bool(self.queued_reschedule),
            "current_trigger": self.current_trigger,
            "current_reschedule": bool(self.current_reschedule),
            "resume_status": self.resume_status,
            "resume_enabled": self.resume_enabled,
            "resume_next_run_at": self.resume_next_run_at,
            "skipped_count": int(self.skipped_count),
        }
        return d

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "HeartbeatRunState":
        if not isinstance(data, dict) or not data:
            return HeartbeatRunState()
        raw_last_run_status = data.get("last_run_status")
        if (
            raw_last_run_status is not None
            and raw_last_run_status not in HEARTBEAT_RUN_STATUSES
        ):
            raise ValueError(
                "last_run_status must be one of "
                f"{', '.join(HEARTBEAT_RUN_STATUSES)} or null"
            )
        return HeartbeatRunState(
            current_run_id=data.get("current_run_id") or None,
            current_run_started_at=(
                float(data["current_run_started_at"])
                if isinstance(data.get("current_run_started_at"), (int, float))
                else None
            ),
            last_run_status=raw_last_run_status,
            last_error=data.get("last_error") or None,
            last_cancel_status=data.get("last_cancel_status") or None,
            last_cancel_error=data.get("last_cancel_error") or None,
            queued_run_id=data.get("queued_run_id") or None,
            queued_trigger=data.get("queued_trigger") or None,
            queued_reschedule=bool(
                _persisted_bool(data, "queued_reschedule", default=False)
            ),
            current_trigger=data.get("current_trigger") or None,
            current_reschedule=bool(
                _persisted_bool(data, "current_reschedule", default=False)
            ),
            resume_status=data.get("resume_status") or None,
            resume_enabled=_persisted_bool(data, "resume_enabled", default=None),
            resume_next_run_at=(
                float(data["resume_next_run_at"])
                if isinstance(data.get("resume_next_run_at"), (int, float))
                else None
            ),
            skipped_count=int(data.get("skipped_count") or 0),
        )


# ---------------------------------------------------------------------------
# HeartbeatJob
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatJob:
    """心跳任务持久化模型(heartbeat_jobs.json)。

    字段命名与 ``CronJob`` 语义一致处保持同名同义;Heartbeat 独有语义才新增。
    不保存、不接收、不修改 agent mode/model/权限/sandbox/worktree 等运行配置 ——
    心跳执行回到原 session 现场,运行配置由原 session 当前配置决定。
    """

    id: str
    name: str
    enabled: bool
    channel_id: str
    session_id: str
    prompt: str
    schedule: HeartbeatSchedule
    # 顶层默认时区,cron schedule 未显式传时使用。
    timezone: str = DEFAULT_TIMEZONE
    status: str = STATUS_SCHEDULED
    concurrency_policy: str = DEFAULT_CONCURRENCY_POLICY
    session_deleted_policy: str = DEFAULT_SESSION_DELETED_POLICY
    max_runs: int | None = DEFAULT_MAX_RUNS
    kind: str = HEARTBEAT_KIND
    created_at: float | None = None
    updated_at: float | None = None
    next_run_at: float | None = None
    last_run_at: float | None = None
    run_count: int = 0
    metadata: dict[str, Any] = field(default_factory=lambda: {"source": SOURCE_AGENT_TOOL})
    run_state: HeartbeatRunState = field(default_factory=HeartbeatRunState)

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "enabled": bool(self.enabled),
            "status": self.status,
            "channel_id": self.channel_id,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "schedule": self.schedule.to_dict(),
            "timezone": self.timezone,
            "concurrency_policy": self.concurrency_policy,
            "session_deleted_policy": self.session_deleted_policy,
            "max_runs": self.max_runs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "run_count": int(self.run_count),
            "metadata": dict(self.metadata or {}),
            "run_state": self.run_state.to_dict(),
        }
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "HeartbeatJob":
        if not isinstance(data, dict):
            raise ValueError("job must be object")

        job_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        channel_id = str(data.get("channel_id") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        prompt = str(data.get("prompt") or "").strip()

        if not job_id:
            raise ValueError("id is required")
        if not name:
            raise ValueError("name is required")
        if len(name) > HEARTBEAT_NAME_MAX_LENGTH:
            raise ValueError(
                f"name must be at most {HEARTBEAT_NAME_MAX_LENGTH} characters"
            )
        if not channel_id:
            raise ValueError("channel_id is required")
        if not session_id:
            raise ValueError("session_id is required")
        if not prompt:
            raise ValueError("prompt is required")
        if len(prompt) > HEARTBEAT_PROMPT_MAX_LENGTH:
            raise ValueError(
                f"prompt must be at most {HEARTBEAT_PROMPT_MAX_LENGTH} characters"
            )

        job_timezone = _validate_timezone(
            str(data.get("timezone") or "").strip() or DEFAULT_TIMEZONE
        )

        schedule = HeartbeatSchedule.from_dict(
            data.get("schedule") or {},
            default_timezone=job_timezone,
        )

        enabled = _persisted_bool(data, "enabled", default=True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        status = str(data.get("status") or STATUS_SCHEDULED).strip()
        if status not in HEARTBEAT_STATUSES:
            raise ValueError(f"invalid status {status!r}")

        concurrency_policy = str(
            data.get("concurrency_policy") or DEFAULT_CONCURRENCY_POLICY
        ).strip()
        if concurrency_policy not in HEARTBEAT_CONCURRENCY_POLICIES:
            raise ValueError(
                f"invalid concurrency_policy {concurrency_policy!r}"
            )

        session_deleted_policy = str(
            data.get("session_deleted_policy") or DEFAULT_SESSION_DELETED_POLICY
        ).strip()
        if session_deleted_policy not in HEARTBEAT_SESSION_DELETED_POLICIES:
            raise ValueError(
                f"invalid session_deleted_policy {session_deleted_policy!r}"
            )

        raw_max_runs = data.get("max_runs", DEFAULT_MAX_RUNS)
        max_runs: int | None
        if raw_max_runs is None:
            max_runs = None
        else:
            try:
                max_runs = int(raw_max_runs)
            except Exception as exc:  # noqa: BLE001
                raise ValueError("max_runs must be int or null") from exc
            if max_runs < 1:
                raise ValueError("max_runs must be at least 1")

        created_at = data.get("created_at", None)
        updated_at = data.get("updated_at", None)
        created_at_f = float(created_at) if isinstance(created_at, (int, float)) else None
        updated_at_f = float(updated_at) if isinstance(updated_at, (int, float)) else None

        next_run_at_raw = data.get("next_run_at", None)
        next_run_at = (
            float(next_run_at_raw)
            if isinstance(next_run_at_raw, (int, float))
            else None
        )
        last_run_at_raw = data.get("last_run_at", None)
        last_run_at = (
            float(last_run_at_raw)
            if isinstance(last_run_at_raw, (int, float))
            else None
        )

        run_count = int(data.get("run_count") or 0)
        # Heartbeat once exposed ``delete_after_run`` as a second run limit.
        # Retired records are normalized on read and serialized without it.
        if data.get("delete_after_run") is True:
            if status in HEARTBEAT_TERMINAL_STATUSES:
                max_runs = max(1, run_count)
            else:
                max_runs = run_count + 1

        metadata_raw = data.get("metadata", None)
        metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        # 旧数据缺失 source 时标记为恢复来源，不能伪造为 Agent Tool 创建。
        if "source" not in metadata or not str(metadata.get("source") or "").strip():
            metadata["source"] = SOURCE_SCHEDULE_RECOVERY

        run_state = HeartbeatRunState.from_dict(data.get("run_state"))

        return HeartbeatJob(
            id=job_id,
            kind=HEARTBEAT_KIND,
            name=name,
            enabled=enabled,
            channel_id=channel_id,
            session_id=session_id,
            prompt=prompt,
            schedule=schedule,
            timezone=job_timezone,
            status=status,
            concurrency_policy=concurrency_policy,
            session_deleted_policy=session_deleted_policy,
            max_runs=max_runs,
            created_at=created_at_f,
            updated_at=updated_at_f,
            next_run_at=next_run_at,
            last_run_at=last_run_at,
            run_count=run_count,
            metadata=metadata,
            run_state=run_state,
        )

    # ---- 状态机不变量校验 ----

    def check_invariants(self) -> None:
        """校验 status / enabled / next_run_at 三者联动不变量。

        用于 store 写回前的防御性校验,以及外部手改 heartbeat_jobs.json 后
        scheduler reload 时的兜底检查。
        """
        if self.status in HEARTBEAT_TERMINAL_STATUSES:
            if self.enabled:
                raise ValueError(
                    f"invariant violated: terminal status {self.status!r} "
                    f"requires enabled=false (job={self.id})"
                )
            if self.next_run_at is not None:
                raise ValueError(
                    f"invariant violated: terminal status {self.status!r} "
                    f"requires next_run_at=None (job={self.id})"
                )
        if self.status == STATUS_SCHEDULED:
            if not self.enabled:
                raise ValueError(
                    f"invariant violated: status=scheduled requires enabled=true "
                    f"(job={self.id})"
                )
            if self.next_run_at is None:
                raise ValueError(
                    f"invariant violated: status=scheduled requires next_run_at "
                    f"(job={self.id})"
                )
            if self.max_runs is not None and int(self.run_count) >= int(self.max_runs):
                raise ValueError(
                    "invariant violated: status=scheduled cannot have exhausted "
                    f"max_runs (job={self.id})"
                )

    # ---- 业务辅助 ----

    @property
    def source(self) -> str:
        """metadata.source 的安全读取,缺失返回 schedule_recovery 兜底值。"""
        value = str((self.metadata or {}).get("source") or "").strip()
        if value not in HEARTBEAT_SOURCES:
            return SOURCE_SCHEDULE_RECOVERY
        return value

    def is_terminal(self) -> bool:
        return self.status in HEARTBEAT_TERMINAL_STATUSES

    def is_schedulable(self) -> bool:
        """scheduler _tick_once 判断:是否可被调度执行。"""
        return self.enabled and self.status == STATUS_SCHEDULED and self.next_run_at is not None


# ---------------------------------------------------------------------------
# 顶层持久化结构
# ---------------------------------------------------------------------------

HEARTBEAT_JOBS_VERSION: int = 1


def empty_heartbeat_jobs_doc() -> dict[str, Any]:
    return {"version": HEARTBEAT_JOBS_VERSION, "jobs": []}
