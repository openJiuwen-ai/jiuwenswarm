"""Transport-neutral cron models and persistence used by Agent Runtime.

共享数据模型（CronTargetChannel/CronTarget/CronJob/CronRunState 及通道校验
函数、名称长度与默认模式常量）以 ``gateway_protocol.cron_models`` 为 source
of truth，本模块 re-export 同一对象；依赖本仓实现（mode_matrix/work_mode/
cron_expr/config）的规范与校验函数留在本模块，两仓各自实现。

反序列化按包边界约定留在实现侧：本仓入口为模块级 ``cron_job_from_dict``
（严格校验：cron 表达式、mode 规范化、超时/MCP 规范化、ModelSelection）。
"""

from __future__ import annotations

import logging
from typing import Any

from gateway_protocol.cron_models import (
    CRON_JOB_DEFAULT_MODE,
    CRON_JOB_DESCRIPTION_MAX_LENGTH,
    CRON_JOB_NAME_MAX_LENGTH,
    CronJob,
    CronRunState,
    CronTarget,
    CronTargetChannel,
    is_valid_target_channel_id,
    normalize_target_channel_id,
)
from jiuwenswarm.common.mode_matrix import (
    TEAM_PLAN_CODE_MODE,
    TEAM_PLAN_NORMAL_MODE,
    deprecate_mode,
    is_team_mode,
)
from jiuwenswarm.common.work_mode import (
    DEFAULT_WEB_WORK_MODE,
    normalize_work_mode,
)
from jiuwenswarm.runtime.cron.cron_expr import validate_cron_expression

logger = logging.getLogger(__name__)


def _normalize_targets_str(raw: str) -> str:
    """将 targets 字符串规范为 CronTargetChannel 枚举值，非法则默认 web。"""
    return normalize_target_channel_id(raw, default=CronTargetChannel.WEB.value)


# Cron job execution modes (passed to AgentServer as chat.send params["mode"]).
CRON_JOB_MODES: frozenset[str] = frozenset(
    {
        "agent",  # 合并后的单一 agent 模式
        "plan",  # legacy shorthand（归一到 agent）
        "team",  # multi-agent team mode
        "agent.plan",  # legacy（真实 plan 模式，deprecate 后 agent.work.plan）
        "agent.fast",  # legacy（归一到 agent）
        "team.plan",
        TEAM_PLAN_NORMAL_MODE,
        TEAM_PLAN_CODE_MODE,
        "code.team",
        # standalone code profile canonical（与 Mode.from_raw / DEPRECATION_MAP 同源）。
        "code",
        "code.normal",
        "code.plan",
        "team.code",
        # 不走 chat.send，scheduler 消费时直接发 PROACTIVE_TICK WS 请求
        # 触发 AgentServer ProactiveEngine.tick_now()。由 proactive_cron_sync 自动注册。
        "proactive.tick",
        # 新三段命名 canonical（与 message_handler /mode 同源）。
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

_CRON_JOB_MODE_ALIASES: dict[str, str] = {
    "plan": "agent",
    # agent.plan 是真实 plan 模式，交由 deprecate_mode 映射。
    "agent.fast": "agent",
    "team.plan": TEAM_PLAN_NORMAL_MODE,
}


def normalize_cron_job_mode(raw: Any, *, default: str = CRON_JOB_DEFAULT_MODE) -> str:
    """Normalize and validate a cron job execution mode (strict, for create/update APIs).

    先处理 legacy 别名，再经 ``deprecate_mode`` 映射到当前 canonical
    三段模式；新 canonical 原样通过。
    """
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if not value:
        return default
    if value not in CRON_JOB_MODES:
        raise ValueError(
            f"Invalid cron job mode {raw!r}. Valid: {', '.join(sorted(CRON_JOB_MODES))}"
        )
    legacy = _CRON_JOB_MODE_ALIASES.get(value)
    if legacy is not None:
        logger.debug(
            "normalize_cron_job_mode: legacy alias '%s' -> '%s'", value, legacy
        )
        value = legacy
    canonical = deprecate_mode(value)
    if canonical != value:
        logger.debug(
            "normalize_cron_job_mode: deprecated '%s' -> '%s'", value, canonical
        )
    return canonical


def coerce_cron_job_mode(raw: Any, *, default: str = CRON_JOB_DEFAULT_MODE) -> str:
    """Normalize legacy stored modes; unknown values pass through lowercased."""
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


def normalize_cron_job_mcp(raw: Any) -> list[str] | None:
    """Normalize a cron job's MCP selection (list of MCP server names).

    只做类型规范化（strip/去空/去重），不校验 MCP 是否存在/连接：
    MCP 连接状态是动态的，断连后旧 job 应降级运行而非起不来；
    AgentServer 侧 reconcile 对缺失名称是 no-op + warning。
    ``None`` / 非列表 / 空列表统一归一为 ``None``（与"未选择"同语义）。
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out or None


# resolve_cron_model 返回的模型来源标记：config.yaml 自配 / Zen 免费缓存 / 登录模型目录。
CRON_MODEL_SOURCE_CONFIG = "config"
CRON_MODEL_SOURCE_ZEN = "zen"
CRON_MODEL_SOURCE_LOGIN = "login"


def _match_supplied_zen_entry(value: str, zen_entries: list[dict[str, Any]] | None) -> str | None:
    """Match a caller-supplied Zen row (public list row or raw cache entry)."""
    for row in zen_entries or []:
        if not isinstance(row, dict):
            continue
        model_config = row.get("model_client_config") or {}
        model_name = str(row.get("model_name") or model_config.get("model_name") or "").strip()
        alias = str(row.get("alias") or "").strip()
        if model_name and value in {model_name, alias}:
            return model_name
    return None


def resolve_cron_model(
    raw: Any,
    zen_entries: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str]:
    """Resolve a cron model name/alias to ``(canonical_model_name, source)``.

    source ∈ ``{"", "config", "zen", "login"}``；``""`` 表示未指定模型（None/空）。
    命中顺序 config → zen → login，同名时自配优先（与 ``get_available_models``
    的去重规则一致）。未知模型抛 ``ValueError``，语义与
    :func:`validate_cron_model`（本函数去掉来源信息的封装）完全一致。

    ``login`` 来源对调用方另有含义：controller 需在此情况下从创建连接的
    登录会话捕获凭据绑定（``CronJob.credential_ref``）；执行侧凭据注入只认
    该绑定，不再按模型名猜测来源。

    If the input is an alias, resolves to the underlying ``model_client_config.model_name``
    (including environment placeholders) so the stored value is always a key
    AgentServer ``_model_cache`` can look up. An explicitly configured name that
    resolves to empty is rejected instead of being persisted.

    Opencode Zen free models live in the AgentServer process. This function
    does not import that module. Callers that already hold the public rows
    (Gateway fetches ``models.zen_entries``) pass them as ``zen_entries``.
    AgentServer cron tools match the in-process cache after a miss here.

    登录送的免费模型（华为账号登录，``common/auth/model_catalog``）同样只在缓存里、
    永不写入 config.yaml（未登录/活动未生效时目录为空，与前端"未登录不展示
    免费模型"一致）。两个进程里的调用方（controller / cron_tools）对登录模型行为一致。
    """
    if raw is None:
        return None, ""
    value = str(raw).strip()
    if not value:
        return None, ""
    from jiuwenswarm.common.config import (
        get_model_config,
        get_model_names,
        resolve_env_vars,
    )

    entry = get_model_config(value)
    if entry is not None:
        mcc = entry.get("model_client_config") or {}
        configured_name = mcc.get("model_name")
        if not configured_name:
            return value, CRON_MODEL_SOURCE_CONFIG
        canonical = str(resolve_env_vars(configured_name) or "").strip()
        if not canonical:
            raise ValueError(
                f"Configured model {value!r} has a model_client_config.model_name "
                f"that resolves to an empty value ({configured_name!r}). Set the "
                "referenced environment variable or configure a concrete model_name."
            )
        return canonical, CRON_MODEL_SOURCE_CONFIG

    matched_zen = _match_supplied_zen_entry(value, zen_entries)
    if matched_zen:
        return matched_zen, CRON_MODEL_SOURCE_ZEN

    # 登录送的免费模型：名字可能带 "#index"
    # 通道侧全局序号后缀，取前半段再匹配（与 passthrough._build_model_auth 一致）。
    try:
        from jiuwenswarm.common.auth.login_credentials import bare_model_name
        from jiuwenswarm.common.auth.model_catalog import get_models

        bare = bare_model_name(value)
        for login_model in get_models(allow_refresh=False):
            if value == login_model.model_name or bare == login_model.model_name:
                return login_model.model_name, CRON_MODEL_SOURCE_LOGIN
    except Exception as exc:  # noqa: BLE001 - optional catalog must not break cron
        logger.debug("[cron] login model lookup failed for %r: %s", value, exc)

    available = get_model_names()
    hint = ", ".join(available[:20]) if available else "(no models configured)"
    if len(available) > 20:
        hint += f" ... and {len(available) - 20} more"
    raise ValueError(f"Unknown model {value!r}. Available models: {hint}")


def validate_cron_model(
    raw: Any,
    zen_entries: list[dict[str, Any]] | None = None,
) -> str | None:
    """Validate model name/alias against configured models. Returns canonical model_name or raises.

    :func:`resolve_cron_model` 的封装：只返回规范名，不暴露来源。需要区分模型
    来源（如登录模型的凭据绑定捕获）的调用方请直接用 ``resolve_cron_model``。
    ``zen_entries`` 是调用方已经从 AgentServer 取到的公开行；不传则不匹配 Zen。
    """
    canonical, _source = resolve_cron_model(raw, zen_entries=zen_entries)
    return canonical


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


def cron_job_from_dict(data: dict[str, Any]) -> CronJob:
    """本仓的 CronJob 反序列化入口（严格校验）。

    校验依赖本仓实现（cron_expr 校验、mode/超时/MCP 规范化、
    ModelSelection），故按包边界留在实现侧而非 protocol 数据类上。
    """
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
    created_at_f = (
        float(created_at) if isinstance(created_at, (int, float)) else None
    )
    updated_at_f = (
        float(updated_at) if isinstance(updated_at, (int, float)) else None
    )

    if not job_id:
        raise ValueError("id is required")
    if not name:
        raise ValueError("name is required")
    if len(name) > CRON_JOB_NAME_MAX_LENGTH:
        raise ValueError(
            f"name must be at most {CRON_JOB_NAME_MAX_LENGTH} characters"
        )
    if not cron_expr:
        raise ValueError("cron_expr is required")
    if not timezone:
        raise ValueError("timezone is required")
    validate_cron_expression(cron_expr, timezone=timezone)
    if not targets_str:
        raise ValueError("targets is required")

    targets_str = _normalize_targets_str(targets_str)

    sid_raw = data.get("session_id", None)
    job_session_id = (
        str(sid_raw).strip()
        if isinstance(sid_raw, str) and str(sid_raw).strip()
        else None
    )

    chat_type_raw = data.get("chat_type", None)
    job_chat_type = (
        str(chat_type_raw).strip()
        if isinstance(chat_type_raw, str) and str(chat_type_raw).strip()
        else None
    )

    mode_raw = data.get("mode", None)
    job_mode = normalize_cron_job_mode(mode_raw)

    delete_after_run = bool(data.get("delete_after_run", False))

    timeout_seconds_raw = data.get("timeout_seconds", None)
    timeout_seconds = None
    if timeout_seconds_raw is not None:
        timeout_seconds = normalize_cron_job_timeout_seconds(timeout_seconds_raw)

    # project_id / last_session_id：老数据兜底（无 project_id → ""，无 last_session_id → None）
    project_id_raw = data.get("project_id", "")
    project_id = (
        str(project_id_raw).strip() if isinstance(project_id_raw, str) else ""
    )
    last_session_id_raw = data.get("last_session_id", None)
    last_session_id = (
        str(last_session_id_raw).strip()
        if isinstance(last_session_id_raw, str) and str(last_session_id_raw).strip()
        else None
    )

    model_raw = data.get("model_name", None)
    job_model_name = (
        str(model_raw).strip()
        if isinstance(model_raw, str) and model_raw.strip()
        else None
    )
    selection_raw = data.get("model_selection")
    job_model_selection = None
    if isinstance(selection_raw, dict):
        from jiuwenswarm.common.model_selection import ModelSelection
        job_model_selection = ModelSelection.model_validate(selection_raw).model_dump()
    # mcp：老数据兜底（无 mcp 字段 → None，行为与改造前一致）
    job_mcp = normalize_cron_job_mcp(data.get("mcp", None))
    app_id_raw = data.get("app_id", "")
    job_app_id = str(app_id_raw).strip() if isinstance(app_id_raw, str) else ""

    # user_id：老数据兜底（无 user_id → ""）
    job_user_id_raw = data.get("user_id", "")
    job_user_id = (
        str(job_user_id_raw).strip() if isinstance(job_user_id_raw, str) else ""
    )

    # credential_ref：老数据兜底（无字段 → ""，即未绑定登录账号）
    job_credential_ref_raw = data.get("credential_ref", "")
    job_credential_ref = (
        str(job_credential_ref_raw).strip()
        if isinstance(job_credential_ref_raw, str)
        else ""
    )

    # work_mode：仅做 normalize + 兜底 "work"，不做跨层 Project 反查
    # （本模块是底层数据模型，不应反向依赖 server.runtime.session.project_store）
    # 精确值由创建/更新路径从 Project 记录注入，或由展示层二次查询覆盖。
    job_work_mode = normalize_work_mode(
        data.get("work_mode"), default=DEFAULT_WEB_WORK_MODE
    )

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
        model_selection=job_model_selection,
        mcp=job_mcp,
        app_id=job_app_id,
        user_id=job_user_id,
        credential_ref=job_credential_ref,
        work_mode=job_work_mode,
    )
