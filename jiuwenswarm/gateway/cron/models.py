from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jiuwenswarm.common.work_mode import (
    DEFAULT_WEB_WORK_MODE,
    normalize_work_mode,
)
from jiuwenswarm.gateway.cron.cron_expr import validate_cron_expression


class CronTargetChannel(str, Enum):
    """推送频道枚举。"""

    WEB = "web"
    TUI = "tui"
    FEISHU = "feishu"
    WHATSAPP = "whatsapp"
    WECOM = "wecom"
    XIAOYI = "xiaoyi"
    WECHAT = "wechat"
    DINGTALK = "dingtalk"


def _feishu_enterprise_app_id(s: str) -> str:
    """feishu_enterprise 通道键仅为 feishu_enterprise:<app_id>；忽略 :chat: 等后续后缀。"""
    parts = str(s or "").strip().split(":")
    if len(parts) < 2 or parts[0].strip().lower() != "feishu_enterprise":
        return ""
    return parts[1].strip()


def is_valid_target_channel_id(raw: str) -> bool:
    s = str(raw or "").strip()
    if not s:
        return False
    if s.startswith("feishu_enterprise:"):
        return bool(_feishu_enterprise_app_id(s))
    try:
        CronTargetChannel(s.lower())
        return True
    except ValueError:
        return False


def normalize_target_channel_id(raw: str, *, default: str = CronTargetChannel.WEB.value) -> str:
    s = str(raw or "").strip()
    if not s:
        return default
    if s.startswith("feishu_enterprise:"):
        app_id = _feishu_enterprise_app_id(s)
        if app_id:
            return f"feishu_enterprise:{app_id}"
        return default
    low = s.lower()
    try:
        return CronTargetChannel(low).value
    except ValueError:
        return default


def _normalize_targets_str(raw: str) -> str:
    """将 targets 字符串规范为 CronTargetChannel 枚举值，非法则默认 web。"""
    return normalize_target_channel_id(raw, default=CronTargetChannel.WEB.value)


# Cron job execution modes (passed to AgentServer as chat.send params["mode"]).
CRON_JOB_MODES: frozenset[str] = frozenset(
    {
        "agent",       # 合并后的单一 agent 模式
        "plan",        # legacy shorthand（归一到 agent）
        "team",        # multi-agent team mode
        "agent.plan",  # legacy（归一到 agent）
        "agent.fast",  # legacy（归一到 agent）
        "team.plan",
        "code.team",
        # 不走 chat.send，scheduler 消费时直接发 PROACTIVE_TICK WS 请求
        # 触发 AgentServer ProactiveEngine.tick_now()。由 proactive_cron_sync 自动注册。
        "proactive.tick",
    }
)


# Canonical default when create/update/runtime do not specify mode.
CRON_JOB_DEFAULT_MODE: str = "agent"

# 名称/描述最大长度（前后端保持一致，见 CronTaskDrawer.tsx 同名常量）。
# 名称对齐 ConfigPanel 里 Agent 名称字段的 64；描述对齐产品确认的 500。
CRON_JOB_NAME_MAX_LENGTH: int = 64
CRON_JOB_DESCRIPTION_MAX_LENGTH: int = 500

_CRON_JOB_MODE_ALIASES: dict[str, str] = {
    "plan": "agent",
    "agent.plan": "agent",
    "agent.fast": "agent",
}


def normalize_cron_job_mode(raw: Any, *, default: str = CRON_JOB_DEFAULT_MODE) -> str:
    """Normalize and validate a cron job execution mode (strict, for create/update APIs).

    legacy 别名（plan / agent.plan / agent.fast）在此处即归一为 "agent" 后落库，
    而非仅依赖运行时（AgentServer 侧）兜底归一。
    """
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if not value:
        return default
    if value not in CRON_JOB_MODES:
        raise ValueError(
            f"Invalid cron job mode {raw!r}. "
            f"Valid: {', '.join(sorted(CRON_JOB_MODES))}"
        )
    return _CRON_JOB_MODE_ALIASES.get(value, value)


def coerce_cron_job_mode(raw: Any, *, default: str = CRON_JOB_DEFAULT_MODE) -> str:
    """Normalize mode for runtime/persistence; unknown values pass through lowercased."""
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if not value:
        return default
    return _CRON_JOB_MODE_ALIASES.get(value, value)


def is_cron_job_mode(raw: Any) -> bool:
    """Whether ``raw`` is a known cron execution mode (including legacy aliases)."""
    if raw is None:
        return False
    value = str(raw).strip().lower()
    return bool(value) and value in CRON_JOB_MODES


def cron_job_modes_for_tools() -> list[str]:
    return sorted(CRON_JOB_MODES)


def normalize_required_device_intents(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("required_device_intents must be a list")
    intents: list[str] = []
    for item in raw:
        intent = str(item or "").strip()
        if intent and intent not in intents:
            intents.append(intent)
    return intents


def cron_job_metadata() -> dict[str, str | list[str] | int]:
    """Cron job schema for clients (TUI/Web); single source for supported modes."""
    return {
        "modes": cron_job_modes_for_tools(),
        "default_mode": CRON_JOB_DEFAULT_MODE,
        "default_timeout_seconds": CRON_DEFAULT_TIMEOUT_SECONDS,
        "default_team_timeout_seconds": CRON_TEAM_DEFAULT_TIMEOUT_SECONDS,
        "max_timeout_seconds": CRON_MAX_TIMEOUT_SECONDS,
    }


_TEAM_CRON_MODES: frozenset[str] = frozenset({"team", "team.plan", "code.team"})

CRON_DEFAULT_TIMEOUT_SECONDS: int = 60 * 60
CRON_TEAM_DEFAULT_TIMEOUT_SECONDS: int = 60 * 60
CRON_MAX_TIMEOUT_SECONDS: int = 72 * 60 * 60
# Backward-compatible alias used by older imports/tests.
CRON_TEAM_STREAM_TIMEOUT_SECONDS: float = float(CRON_TEAM_DEFAULT_TIMEOUT_SECONDS)


def normalize_cron_job_timeout_seconds(raw: Any) -> int | None:
    """Validate optional per-job timeout override (seconds)."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("timeout_seconds must be int") from exc
    if value < 60:
        raise ValueError("timeout_seconds must be at least 60")
    if value > CRON_MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be at most {CRON_MAX_TIMEOUT_SECONDS}")
    return value


def validate_cron_model(raw: Any) -> str | None:
    """Validate model name/alias against configured models. Returns canonical model_name or raises.

    If the input is an alias, resolves to the underlying ``model_client_config.model_name``
    so the stored value is always a key AgentServer ``_model_cache`` can look up.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    from jiuwenswarm.common.config import (
        get_model_config,
        get_model_names,
        resolve_env_vars,
    )

    entry = get_model_config(value)
    if entry is not None:
        mcc = entry.get("model_client_config") or {}
        canonical = (mcc.get("model_name") or "").strip()
        if not canonical:
            return value
        # models.defaults 模板用 ${MODEL_NAME} 等占位符（真值在 .env）。
        # get_model_config 按契约返回「未解析环境变量的原始配置」，直接回存
        # 会把字面 "${MODEL_NAME}" 写进任务记录并在 cron.job.create 应答里
        # 原样返回；执行侧 chat.send 还会把它当显式 model_name 写进会话元
        # 数据。这里统一解析成真值，与 get_model_names 的展示口径一致；
        # 解析为空（无对应 env）时保守回退原值。
        resolved = str(resolve_env_vars(canonical) or "").strip()
        return resolved or canonical
    available = get_model_names()
    hint = ", ".join(available[:20]) if available else "(no models configured)"
    if len(available) > 20:
        hint += f" ... and {len(available) - 20} more"
    raise ValueError(
        f"Unknown model {value!r}. Available models: {hint}"
    )


def resolve_cron_job_timeout_seconds(job: "CronJob") -> float:
    """Return effective execution timeout for a cron job."""
    raw = getattr(job, "timeout_seconds", None)
    if raw is not None:
        return float(int(raw))
    if is_team_cron_mode(job.mode):
        return float(CRON_TEAM_DEFAULT_TIMEOUT_SECONDS)
    return float(CRON_DEFAULT_TIMEOUT_SECONDS)


def is_team_cron_mode(mode: str | None) -> bool:
    """Return True when a cron job should run via Team + SwarmFlow streaming."""
    value = str(mode or "").strip().lower()
    return value in _TEAM_CRON_MODES


@dataclass(frozen=True)
class CronTarget:
    """Where to push cron results."""

    channel_id: str
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "session_id": self.session_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CronTarget":
        channel_id = str(data.get("channel_id") or "").strip()
        session_id_raw = data.get("session_id", None)
        session_id = str(session_id_raw).strip() if isinstance(session_id_raw, str) else None
        if not channel_id:
            raise ValueError("target.channel_id is required")
        return CronTarget(channel_id=channel_id, session_id=session_id or None)


@dataclass
class CronJob:
    """Cron job persisted in cron_jobs.json."""

    id: str
    name: str
    enabled: bool
    cron_expr: str
    timezone: str
    wake_offset_seconds: int = 0
    description: str = ""
    # For one-shot schedules where croniter has no "next" after the run.
    expired: bool = False
    # Target channel ID to push results to (e.g. "web").
    # JSON 字段名仍然叫 targets，用字符串保存频道 ID，兼容旧数据。
    targets: str = ""
    # SessionMap 形态（如 feishu::chat_id::bot_id::...），仅 feishu_enterprise 投递用；由 AgentServer 上下文写入。
    session_id: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
    # 记录定时任务是在群聊("group")还是私聊("p2p")中创建的，用于推送时决定是否走 IMOutboundPipeline
    chat_type: str | None = None
    # 定时任务执行时使用的 Agent 模式；未指定时默认 agent（plan/fast 已合并）
    mode: str = CRON_JOB_DEFAULT_MODE
    # 执行一次后自动删除（用于提醒类任务）
    delete_after_run: bool = False
    # 单次执行超时（秒）；未配置时普通模式与 team 模式默认均为 1 小时
    timeout_seconds: int | None = None
    # 归属项目 ID：真实工程为 proj_*；未挂工程时为虚拟桶
    # default / default_code / default_design（或空串）。聊天创建可继承会话
    # project_id；否则可由 create 入参 project_dir 按 work_mode 匹配可见工程。
    project_id: str = ""
    # 执行 cwd（桌面独立「定时任务-*」目录）。与 project_id 分桶正交，不是归属解析入参。
    # 仅绝对路径落库；空串表示尚未绑定 / 已清空。
    project_dir: str = ""
    # 最近一次执行产生的会话 ID；调度器在创建执行会话后回写，未执行过为 None
    last_session_id: str | None = None
    # 执行时使用的模型；None 表示使用 AgentServer 默认模型
    model_name: str | None = None
    # 飞书多应用场景：创建该定时任务的 app_id，用于推送时定位到正确的 app 配置
    app_id: str = ""
    # 工作模式派生快照：由 project_id 归属推导（"code" / "work" / "design"）。
    # 不作为独立隔离维度，任务归属仍以 project_id 为准。
    # from_dict 仅做 normalize + 兜底 "work"，不做跨层 Project 反查；
    # 精确值由创建/更新路径从 Project 记录注入，或由展示层二次查询覆盖。
    work_mode: str = DEFAULT_WEB_WORK_MODE
    required_device_intents: list[str] = field(default_factory=list)
    xiaoyi_push_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "expired": bool(self.expired),
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "wake_offset_seconds": int(self.wake_offset_seconds),
            "description": self.description,
            "targets": self.targets,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.session_id:
            d["session_id"] = self.session_id
        if self.chat_type:
            d["chat_type"] = self.chat_type
        if self.mode:
            d["mode"] = self.mode
        if self.delete_after_run:
            d["delete_after_run"] = bool(self.delete_after_run)
        if self.timeout_seconds is not None:
            d["timeout_seconds"] = int(self.timeout_seconds)
        # project_id 始终输出（空串表示默认项目，与 SessionInfo.project_id 语义一致）
        d["project_id"] = self.project_id or ""
        if self.project_dir:
            d["project_dir"] = self.project_dir
        # work_mode 始终输出（派生快照字段，由 project_id 归属推导，与 project_id 一致）
        d["work_mode"] = self.work_mode or DEFAULT_WEB_WORK_MODE
        # last_session_id 仅在非空时输出（与 session_id/chat_type 可选字段策略一致）
        if self.last_session_id:
            d["last_session_id"] = self.last_session_id
        if self.model_name:
            d["model_name"] = self.model_name
        if self.app_id:
            d["app_id"] = self.app_id
        if self.required_device_intents:
            d["required_device_intents"] = list(self.required_device_intents)
        if self.xiaoyi_push_id:
            d["xiaoyi_push_id"] = self.xiaoyi_push_id
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CronJob":
        job_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        cron_expr = str(data.get("cron_expr") or "").strip()
        timezone = str(data.get("timezone") or "").strip()
        enabled = bool(data.get("enabled", False))
        expired = bool(data.get("expired", False))

        wake_offset_seconds_raw = data.get("wake_offset_seconds", 0)
        try:
            wake_offset_seconds = int(wake_offset_seconds_raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("wake_offset_seconds must be int") from exc
        if wake_offset_seconds < 0:
            wake_offset_seconds = 0

        description = str(data.get("description") or "").strip()
        if not description:
            raise ValueError("description is required")
        if len(description) > CRON_JOB_DESCRIPTION_MAX_LENGTH:
            raise ValueError(
                f"description must be at most {CRON_JOB_DESCRIPTION_MAX_LENGTH} characters"
            )

        # targets 新格式是字符串；旧格式是 list[dict]，此处做兼容。
        targets_raw = data.get("targets", "")
        targets_str = ""
        if isinstance(targets_raw, str):
            targets_str = targets_raw.strip()
        elif isinstance(targets_raw, list):
            # legacy: list of {channel_id, session_id?}
            for item in targets_raw:
                if isinstance(item, dict):
                    ch = str(item.get("channel_id") or "").strip()
                    if ch:
                        targets_str = ch
                        break

        created_at = data.get("created_at", None)
        updated_at = data.get("updated_at", None)
        created_at_f = float(created_at) if isinstance(created_at, (int, float)) else None
        updated_at_f = float(updated_at) if isinstance(updated_at, (int, float)) else None

        if not job_id:
            raise ValueError("id is required")
        if not name:
            raise ValueError("name is required")
        if len(name) > CRON_JOB_NAME_MAX_LENGTH:
            raise ValueError(f"name must be at most {CRON_JOB_NAME_MAX_LENGTH} characters")
        if not cron_expr:
            raise ValueError("cron_expr is required")
        if not timezone:
            raise ValueError("timezone is required")
        validate_cron_expression(cron_expr, timezone=timezone)
        if not targets_str:
            raise ValueError("targets is required")

        targets_str = _normalize_targets_str(targets_str)

        sid_raw = data.get("session_id", None)
        job_session_id = str(sid_raw).strip() if isinstance(sid_raw, str) and str(sid_raw).strip() else None

        chat_type_raw = data.get("chat_type", None)
        job_chat_type = (
            str(chat_type_raw).strip()
            if isinstance(chat_type_raw, str) and str(chat_type_raw).strip()
            else None
        )

        mode_raw = data.get("mode", None)
        if isinstance(mode_raw, str) and str(mode_raw).strip():
            job_mode = coerce_cron_job_mode(mode_raw)
        else:
            job_mode = CRON_JOB_DEFAULT_MODE

        delete_after_run = bool(data.get("delete_after_run", False))

        timeout_seconds_raw = data.get("timeout_seconds", None)
        timeout_seconds = None
        if timeout_seconds_raw is not None:
            timeout_seconds = normalize_cron_job_timeout_seconds(timeout_seconds_raw)

        # project_id / last_session_id：老数据兜底（无 project_id → ""，无 last_session_id → None）
        project_id_raw = data.get("project_id", "")
        project_id = str(project_id_raw).strip() if isinstance(project_id_raw, str) else ""
        project_dir_raw = data.get("project_dir", "")
        project_dir = (
            str(project_dir_raw).strip()
            if isinstance(project_dir_raw, str) and str(project_dir_raw).strip()
            else ""
        )
        if project_dir and not os.path.isabs(project_dir):
            project_dir = ""
        last_session_id_raw = data.get("last_session_id", None)
        last_session_id = (
            str(last_session_id_raw).strip()
            if isinstance(last_session_id_raw, str) and str(last_session_id_raw).strip()
            else None
        )

        model_raw = data.get("model_name", None)
        job_model_name = str(model_raw).strip() if isinstance(model_raw, str) and str(model_raw).strip() else None
        app_id_raw = data.get("app_id", "")
        job_app_id = str(app_id_raw).strip() if isinstance(app_id_raw, str) else ""

        # work_mode：仅做 normalize + 兜底 "work"，不做跨层 Project 反查
        # （gateway.cron.models 是底层数据模型，不应反向依赖 server.runtime.session.project_store）
        # 精确值由创建/更新路径从 Project 记录注入，或由展示层二次查询覆盖。
        job_work_mode = normalize_work_mode(data.get("work_mode"), default=DEFAULT_WEB_WORK_MODE)
        required_device_intents = normalize_required_device_intents(
            data.get("required_device_intents")
        )
        push_id_raw = data.get("xiaoyi_push_id")
        xiaoyi_push_id = (
            str(push_id_raw).strip()
            if isinstance(push_id_raw, str) and push_id_raw.strip()
            else None
        )
        if required_device_intents and not xiaoyi_push_id:
            raise ValueError("xiaoyi_push_id is required for device cron jobs")

        return CronJob(
            id=job_id,
            name=name,
            enabled=enabled,
            expired=expired,
            cron_expr=cron_expr,
            timezone=timezone,
            wake_offset_seconds=wake_offset_seconds,
            description=description,
            targets=targets_str,
            session_id=job_session_id,
            created_at=created_at_f,
            updated_at=updated_at_f,
            chat_type=job_chat_type,
            mode=job_mode,
            delete_after_run=delete_after_run,
            timeout_seconds=timeout_seconds,
            project_id=project_id,
            project_dir=project_dir,
            last_session_id=last_session_id,
            model_name=job_model_name,
            app_id=job_app_id,
            work_mode=job_work_mode,
            required_device_intents=required_device_intents,
            xiaoyi_push_id=xiaoyi_push_id,
        )

    # ── A2A CronQuery protocol conversion ──────────────────────────────

    def to_a2a_job(
        self,
        include_description: bool = False,
        *,
        next_run_at_ms: int = 0,
        last_run_at_ms: int = 0,
        job_run_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Convert to A2A CronQuery ``list``/``add``/``update`` ans format.

        Maps the internal flat ``CronJob`` structure to the nested A2A shape
        defined in ``定时任务 RPC 接口协议文档.md`` (schedule / payload /
        delivery / state objects).

        Args:
            include_description: If True, include ``description`` field
                (used by ``add``/``update`` responses per protocol doc).
                ``list`` response omits it.
            next_run_at_ms: Next scheduled run time in milliseconds (from
                scheduler events heap). 0 if unknown/disabled.
            last_run_at_ms: Last run start time in milliseconds (from
                scheduler runs history). 0 if never run.
            job_run_stats: Aggregated run statistics dict from
                ``controller._compute_job_run_stats``. Used only for ``list``
                responses (include_description=False). Keys: last_run_status,
                last_duration_ms, last_delivery_status, consecutive_errors,
                consecutive_skipped. If None, defaults are used.
        """
        created_ms = int(self.created_at * 1000) if isinstance(self.created_at, (int, float)) else 0
        updated_ms = int(self.updated_at * 1000) if isinstance(self.updated_at, (int, float)) else 0

        # delivery: targets maps to delivery.channel; keep "announce" mode.
        # Per protocol doc:
        #   - list response: delivery has only {mode, channel} (no "to")
        #   - add/update response: delivery has {mode, channel, to}
        # We use include_description to distinguish: list=False, add/update=True.
        channel = self.targets or CronTargetChannel.WEB.value
        delivery: dict[str, Any] = {
            "mode": "announce",
            "channel": channel,
        }
        if include_description:
            delivery["to"] = "default"

        # state: per protocol doc, list (§2.1) has full state object,
        # add (§2.4) and update (§2.5) have only {nextRunAtMs} (or {} if disabled).
        if include_description:
            state: dict[str, Any] = {
                "nextRunAtMs": next_run_at_ms,
            }
        else:
            stats = job_run_stats or {}
            last_run_status = stats.get("last_run_status", "ok")
            last_duration_ms = stats.get("last_duration_ms", 0)
            last_delivery_status = stats.get("last_delivery_status", "unknown")
            consecutive_errors = stats.get("consecutive_errors", 0)
            consecutive_skipped = stats.get("consecutive_skipped", 0)
            state: dict[str, Any] = {
                "nextRunAtMs": next_run_at_ms,
                "lastRunStatus": last_run_status,
                "lastStatus": last_run_status,
                "lastDurationMs": last_duration_ms,
                "lastDeliveryStatus": last_delivery_status,
                "consecutiveErrors": consecutive_errors,
                "consecutiveSkipped": consecutive_skipped,
            }
            # Only include lastRunAtMs when there is a real value (non-zero).
            # 0 means the job has never run — don't return a misleading default.
            if last_run_at_ms:
                state["lastRunAtMs"] = last_run_at_ms

        # schedule: always kind="cron" with 5-field expr.
        # One-shot tasks are encoded as a 5-field cron with a matching
        # month/day/hour/minute and rely on delete_after_run=True for
        # one-shot semantics. The device protocol only defines kind="cron".
        schedule_obj = {
            "kind": "cron",
            "expr": self.cron_expr,
            "tz": self.timezone,
        }

        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "createdAtMs": created_ms,
            "updatedAtMs": updated_ms,
            "schedule": schedule_obj,
            "sessionTarget": "isolated",
            "wakeMode": "now",
            "payload": {
                "kind": "agentTurn",
                "message": self.description or "",
            },
            "delivery": delivery,
            "state": state,
        }
        if include_description:
            result["description"] = self.description or ""
        return result


@dataclass
class CronRunState:
    """In-memory state for a single scheduled run (not persisted)."""

    run_id: str
    job_id: str
    wake_at_iso: str
    push_at_iso: str
    status: str = "pending"  # pending|running|succeeded|failed
    placeholder_sent: bool = False
    pushed_final: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    result_text: str | None = None
    error: str | None = None
    job_name: str | None = None
    targets: str | None = None
    session_id: str | None = None
    chat_type: str | None = None
    timezone: str | None = None
    exec_channel_id: str | None = None
    exec_session_id: str | None = None
    # Delivery tracking (filled by _push_to_targets)
    delivered: bool = False
    delivery_status: str = "unknown"  # unknown|delivered|failed|not-requested
    # Telemetry (filled when available from agent response)
    model: str = ""
    provider: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def to_a2a_run_entry(
        self,
        *,
        for_runs: bool = False,
        next_run_at_ms: int = 0,
    ) -> dict[str, Any]:
        """Convert to A2A CronQuery ``runs`` / ``queryTimeList`` entry format.

        Per protocol doc:
        - **runs** (§2.3): entries include ts, jobId, action, status, error,
          summary, diagnostics, runAtMs, durationMs, nextRunAtMs, model,
          provider, usage, deliveryStatus, delivery, sessionId, sessionKey.
          Status values: ``"error"`` | ``"success"`` | ``"running"``.
        - **queryTimeList** (§2.8): entries include ts, jobId, action, status,
          error, summary, diagnostics, delivered, deliveryStatus, deliveryError,
          delivery, sessionId, sessionKey, runId, runAtMs, durationMs, nextRunAtMs.
          Status values: ``"ok"`` | ``"error"`` | ``"skipped"``.

        Args:
            for_runs: If True, format for ``runs`` action (status "success",
                includes model/provider/usage/delivery full structure).
                If False, format for ``queryTimeList`` (status "ok").
            next_run_at_ms: Next scheduled run time in milliseconds for this
                job (from scheduler events heap). 0 if unknown.
        """
        # Map internal status to A2A status.
        # runs uses "success"; queryTimeList uses "ok".
        if for_runs:
            status_map = {
                "succeeded": "success",
                "failed": "error",
                "running": "running",
                "pending": "skipped",
            }
        else:
            status_map = {
                "succeeded": "ok",
                "failed": "error",
                "running": "running",
                "pending": "skipped",
            }
        a2a_status = status_map.get(self.status, "skipped")

        started_ms = int(self.started_at * 1000) if isinstance(self.started_at, (int, float)) else 0
        finished_ms = int(self.finished_at * 1000) if isinstance(self.finished_at, (int, float)) else 0
        duration_ms = max(0, finished_ms - started_ms) if started_ms and finished_ms else 0
        ts_ms = finished_ms or started_ms

        entry: dict[str, Any] = {
            "ts": ts_ms,
            "jobId": self.job_id,
            "action": "finished",
            "status": a2a_status,
            "runAtMs": started_ms,
            "durationMs": duration_ms,
            "nextRunAtMs": next_run_at_ms,
            "deliveryStatus": self.delivery_status or "unknown",
            "sessionId": self.session_id or "",
            "sessionKey": f"agent:main:cron:{self.job_id}:run:{self.run_id}",
        }

        if for_runs:
            # runs action (§2.3): model, provider, usage, delivery are required.
            # Use actual values from CronRunState (filled by scheduler when available).
            usage = self.usage or {}
            entry["model"] = self.model or ""
            entry["provider"] = self.provider or ""
            entry["usage"] = {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }
            # delivery: build from actual delivery tracking state
            resolved_channel = self.targets or ""
            # Normalize channel name: internal enum → device-facing format
            if resolved_channel and not resolved_channel.endswith("-channel"):
                resolved_channel = f"{resolved_channel}-channel"
            entry["delivery"] = {
                "intended": {
                    "channel": "last",
                    "to": None,
                    "source": "last",
                },
                "resolved": {
                    "ok": bool(self.targets),
                    "channel": resolved_channel or "unknown",
                    "to": "default",
                    "source": "explicit",
                },
                "fallbackUsed": False,
                "delivered": bool(self.delivered),
            }

        if self.error:
            entry["error"] = self.error
            # Build diagnostics for error entries per protocol doc
            entry["diagnostics"] = {
                "summary": self.error,
                "entries": [
                    {
                        "ts": ts_ms,
                        "source": "delivery",
                        "severity": "error",
                        "message": self.error,
                    }
                ],
            }
        if self.result_text:
            entry["summary"] = self.result_text
        if self.job_name:
            entry["name"] = self.job_name
        return entry


