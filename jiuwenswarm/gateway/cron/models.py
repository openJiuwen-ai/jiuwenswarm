from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jiuwenswarm.common.work_mode import (
    DEFAULT_WEB_WORK_MODE,
    normalize_work_mode,
)
from jiuwenswarm.common.mode_matrix import (
    TEAM_PLAN_CODE_MODE,
    TEAM_PLAN_NORMAL_MODE,
    deprecate_mode,
    is_team_mode,
)
from jiuwenswarm.gateway.cron.cron_expr import validate_cron_expression

logger = logging.getLogger(__name__)


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
        "agent.plan",  # legacy（真实 plan 模式，deprecate 后 agent.work.plan）
        "agent.fast",  # legacy（归一到 agent）
        "team.plan",
        TEAM_PLAN_NORMAL_MODE,
        TEAM_PLAN_CODE_MODE,
        "code.team",
        # standalone code profile canonical（与 Mode.from_raw / DEPRECATION_MAP 同源）。
        # 不加会导致同串在非 cron 路径合法、在 cron 路径被拒（400）。
        "code",
        "code.normal",
        "code.plan",
        "team.code",
        # 不走 chat.send，scheduler 消费时直接发 PROACTIVE_TICK WS 请求
        # 触发 AgentServer ProactiveEngine.tick_now()。由 proactive_cron_sync 自动注册。
        "proactive.tick",
        # 新三段命名 canonical（P3 引入，与 message_handler /mode 同源）。
        # 创建/更新 cron 任务时可直接传新串；存量 cron 数据读取走 coerce + 运行时
        # deprecate_mode 兜底，二者均无需扩 _CRON_JOB_MODE_ALIASES。
        "agent.work.normal",
        "agent.work.plan",
        "agent.code.normal",
        "agent.code.plan",
        "team.work.normal",
        "team.work.plan",
        "team.code.normal",
        "team.code.plan",
    }
)


# Canonical default when create/update/runtime do not specify mode.
CRON_JOB_DEFAULT_MODE: str = "agent"

# 名称/描述最大长度（前后端保持一致，见 CronTaskDrawer.tsx 同名常量）。
# 名称对齐 ConfigPanel 里 Agent 名称字段的 64。
# The description is a prompt, not a label: the scheduler passes it verbatim as
# the agent's request content when the job fires, and it is never rendered in
# the task list or in any dialog, so nothing in the panel constrains its length.
# The sibling heartbeat scheduler already sizes the equivalent field as a prompt
# (HEARTBEAT_PROMPT_MAX_LENGTH is 2000).
CRON_JOB_NAME_MAX_LENGTH: int = 64
CRON_JOB_DESCRIPTION_MAX_LENGTH: int = 4096

_CRON_JOB_MODE_ALIASES: dict[str, str] = {
    "plan": "agent",
    # agent.plan 是真实 plan 模式：不并入 agent，交由 deprecate_mode
    # 映射到 agent.work.plan，与运行时 resolve_agent_request_mode / 会话迁移一致。
    "agent.fast": "agent",
    "team.plan": TEAM_PLAN_NORMAL_MODE,
}


def normalize_cron_job_mode(raw: Any, *, default: str = CRON_JOB_DEFAULT_MODE) -> str:
    """Normalize and validate a cron job execution mode (strict, for create/update APIs).

    两步归一（P3 重写）：
    1. legacy 别名经 ``_CRON_JOB_MODE_ALIASES`` 映射到旧 canonical
       （plan → agent；team.plan → team.plan.normal；agent.fast → agent；
       agent.plan 不并入 agent，是真实 plan 模式，经第 2 步落到 agent.work.plan）。
       这一步把 ``team.plan`` 单一映射到 ``team.plan.normal``，避免原方案中
       ``team.plan`` 同时被 ``_CRON_JOB_MODE_ALIASES`` 映成
       ``TEAM_PLAN_NORMAL_MODE``、又被 deprecate_mode 映成 ``team.work.normal``
       的双键冲突。
    2. 旧 canonical 经 ``deprecate_mode`` 静默映射到新 canonical（如
       agent → agent.work.normal；agent.plan → agent.work.plan；
       team.plan.normal → team.work.plan）。

    新 canonical（8 个新串）不在 ``_CRON_JOB_MODE_ALIASES`` 中，第一步直通，
    第二步 deprecate_mode 也原样返回，最终落库为新 canonical。
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
    # 步骤1：legacy 别名归一到旧 canonical。
    legacy = _CRON_JOB_MODE_ALIASES.get(value)
    if legacy is not None:
        logger.debug(
            "normalize_cron_job_mode: step1 legacy alias '%s' -> '%s'",
            value, legacy,
        )
        value = legacy
    # 步骤2：旧 canonical 经 deprecate_mode 转新 canonical。
    new_value = deprecate_mode(value)
    if new_value != value:
        logger.debug(
            "normalize_cron_job_mode: step2 deprecate '%s' -> '%s'",
            value, new_value,
        )
    return new_value


def coerce_cron_job_mode(raw: Any, *, default: str = CRON_JOB_DEFAULT_MODE) -> str:
    """Normalize mode for runtime/persistence; unknown values pass through lowercased.

    与 ``normalize_cron_job_mode`` 同走两步归一：legacy 别名（``_CRON_JOB_MODE_ALIASES``）
    → 旧 canonical → ``deprecate_mode`` → 新 canonical。这样磁盘读出的旧串在 coerce
    后即落新 canonical，cron_jobs.json 经一次读改写回即完成迁移。
    """
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if not value:
        return default
    legacy = _CRON_JOB_MODE_ALIASES.get(value)
    if legacy is not None:
        value = legacy
    return deprecate_mode(value)


def cron_job_modes_for_tools() -> list[str]:
    return sorted(CRON_JOB_MODES)


def cron_job_metadata() -> dict[str, str | list[str] | int]:
    """Cron job schema for clients (TUI/Web); single source for supported modes."""
    return {
        "modes": cron_job_modes_for_tools(),
        "default_mode": CRON_JOB_DEFAULT_MODE,
        "default_timeout_seconds": CRON_DEFAULT_TIMEOUT_SECONDS,
        "default_team_timeout_seconds": CRON_TEAM_DEFAULT_TIMEOUT_SECONDS,
        "max_timeout_seconds": CRON_MAX_TIMEOUT_SECONDS,
    }


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

    Opencode Zen free models are in-memory only (never written to config.yaml), so a
    configured-models miss falls back to the Zen free-model cache: the frontend appends
    free models to ``models.list`` and lets the user pick one for a cron job, and the
    stored canonical id is later resolved by AgentServer the same way. When the free-model
    toggle is off or Zen is unreachable the cache is empty and the Unknown-model error is
    raised as before.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    from jiuwenswarm.common.config import get_model_config, get_model_names

    entry = get_model_config(value)
    if entry is not None:
        mcc = entry.get("model_client_config") or {}
        canonical = (mcc.get("model_name") or "").strip()
        return canonical if canonical else value

    # Opencode Zen 免费模型（纯内存态，不入 config.yaml）：按 model_name 或 alias
    # 匹配内存缓存，返回 canonical model_name（即 Zen API id），与 get_model_config
    # 的解析语义保持一致。匹配失败/缓存为空时保持原 Unknown model 行为。
    try:
        from jiuwenswarm.server.runtime.opencode_zen import get_zen_free_model_entries
        for zent in get_zen_free_model_entries():
            zmcc = zent.get("model_client_config") or {}
            zname = (zmcc.get("model_name") or "").strip()
            if zname and (
                zname == value or str(zent.get("alias") or "").strip() == value
            ):
                return zname
    except Exception as exc:  # noqa: BLE001 - free-model lookup must never break cron create
        logger.debug("[cron] zen free-model lookup failed for %r: %s", value, exc)

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
    return is_team_mode(mode)


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
    # 归属项目 ID；由创建时 project_dir 匹配获得，匹配不到可见项目为空串（默认项目）
    project_id: str = ""
    # 最近一次执行产生的会话 ID；调度器在创建执行会话后回写，未执行过为 None
    last_session_id: str | None = None
    # 执行时使用的模型；None 表示使用 AgentServer 默认模型
    model_name: str | None = None
    # 飞书多应用场景：创建该定时任务的 app_id，用于推送时定位到正确的 app 配置
    app_id: str = ""
    # 创建者标识（web 端 user_id）。执行时透传给 faas 的 X-Session-Context，
    # 否则 CreateSandbox 拉不起导致 60s 超时（见 plan-cron-user-id）。
    # 默认空串兼容旧数据；语义=创建者，创建后不可变。
    user_id: str = ""
    # 工作模式派生快照：由 project_id 归属推导（"code" / "work"）。
    # 不作为独立隔离维度，任务归属仍以 project_id 为准。
    # from_dict 仅做 normalize + 兜底 "work"，不做跨层 Project 反查；
    # 精确值由创建/更新路径从 Project 记录注入，或由展示层二次查询覆盖。
    work_mode: str = DEFAULT_WEB_WORK_MODE

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
        # work_mode 始终输出（派生快照字段，由 project_id 归属推导，与 project_id 一致）
        d["work_mode"] = self.work_mode or DEFAULT_WEB_WORK_MODE
        # last_session_id 仅在非空时输出（与 session_id/chat_type 可选字段策略一致）
        if self.last_session_id:
            d["last_session_id"] = self.last_session_id
        if self.model_name:
            d["model_name"] = self.model_name
        if self.app_id:
            d["app_id"] = self.app_id
        if self.user_id:
            d["user_id"] = self.user_id
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

        # user_id：老数据兜底（无 user_id → ""）
        job_user_id_raw = data.get("user_id", "")
        job_user_id = str(job_user_id_raw).strip() if isinstance(job_user_id_raw, str) else ""

        # work_mode：仅做 normalize + 兜底 "work"，不做跨层 Project 反查
        # （gateway.cron.models 是底层数据模型，不应反向依赖 server.runtime.session.project_store）
        # 精确值由创建/更新路径从 Project 记录注入，或由展示层二次查询覆盖。
        job_work_mode = normalize_work_mode(data.get("work_mode"), default=DEFAULT_WEB_WORK_MODE)

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
            last_session_id=last_session_id,
            model_name=job_model_name,
            app_id=job_app_id,
            user_id=job_user_id,
            work_mode=job_work_mode,
        )


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
