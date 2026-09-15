# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash 模式合并 cron 工具：单 ToolCard + 10 action（无 action 字段，对象键判别）。

该模块只由 ``JiuwenSwarmFlashAdapter`` 注册；普通 DeepAgent 继续使用独立 cron 工具。

* **Option B 形态**：无 ``action`` 字段，模型填哪个 action 对象键（add/get/...）
  即表达调用意图，dispatch 按对象键判别。job 字段直接放 action 对象内（无 ``job``
  壳）。schedule 是唯一嵌套（kind=cron|at 多态）。
* **schema style 开关**（``CRON_FLASH_SCHEMA_STYLE`` 环境变量，默认 ``description``）：
  - ``properties``：action 对象有完整 properties + enum 硬约束。
  - ``description``：action 对象保留结构骨架，但去掉逐字段说明；字段文档集中到参数级
    description——用于 A/B 测试模型对结构化 schema vs prose 的跟随度。
* **主线 8 工具合并为 1 卡**（补回 get/toggle/preview；runs 因 backend 无 run 历史而不暴露）。
  主线有实现、fork 副本曾漂移丢的 schedule.kind=at（dispatch 调共享
  ``iso_to_seven_field_cron``）已补回。只接受当前 schema 能生成的输入，不保留旧
  action/job/payload/delivery 形态兼容。
* **dispatch 是唯一翻译器**：``_translate_to_native`` 把输入译成 CronTools 扁平键，
  输出恒不含 schedule，backend 仅接收当前原生字段。model_name 暂不暴露（产品决策）。
"""
from __future__ import annotations

import os
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.harness.tools.cron import CronToolBackend, CronToolContext

# 10 个 action（无 action 字段；模型填哪个键即该 action）。runs 删（backend 无 run 历史）。
_ACTIONS = [
    "status", "list", "add", "get", "update", "remove",
    "toggle", "preview", "run", "wake",
]

_TARGETS_ENUM = ["web", "tui", "feishu", "dingtalk", "whatsapp", "wecom", "xiaoyi", "wechat"]
_MODE_ENUM = ["agent", "team", "team.plan", "code.team"]

_SCHEMA_STYLE_ENV = "CRON_FLASH_SCHEMA_STYLE"


def _schema_style() -> str:
    """返回 schema 风格；未知配置安全回退到默认 description。"""
    style = (os.getenv(_SCHEMA_STYLE_ENV, "description") or "description").strip().lower()
    return style if style in {"properties", "description"} else "description"


# --------------------------------------------------------------------------- #
# 描述
# --------------------------------------------------------------------------- #

_FIELD_DESC: dict[str, dict[str, str]] = {
    "name": {"cn": "任务名称，≤64字符", "en": "Job name, <=64 characters"},
    "schedule": {
        "cn": "完整调度计划；kind=cron(周期表达式) 或 kind=at(指定时刻)",
        "en": "Complete schedule: kind=cron (recurring) or kind=at (one time)",
    },
    "schedule_kind": {
        "cn": "cron=周期表达式；at=指定一次性时刻",
        "en": "cron=recurring expression; at=one-time instant",
    },
    "schedule_at": {
        "cn": "ISO 8601 时刻，kind=at 时必填，如 2026-09-11T17:30:00+08:00；过去时刻会被标记过期不执行",
        "en": "ISO 8601 instant required for kind=at; past instants expire without running",
    },
    "tz": {"cn": "IANA时区名，缺省 Asia/Shanghai", "en": "IANA timezone, default Asia/Shanghai"},
    "expr": {"cn": "cron表达式(kind=cron)", "en": "Cron expression for kind=cron"},
    "description": {
        "cn": "到点发给agent执行的任务指令文本，≤500字符；不要包含时间/频率信息",
        "en": "Task instruction sent to the agent at run time, <=500 characters; omit schedule details",
    },
    "targets": {
        "cn": "推送频道。用户未明确指定时省略=当前会话频道；禁止从历史记录推断",
        "en": "Delivery channel; omit to use the current channel and never infer it from history",
    },
    "enabled": {"cn": "创建后是否立即启用，默认true", "en": "Whether the new job is enabled, default true"},
    "wake_offset_seconds": {
        "cn": "触发前提前唤醒秒数，默认0(到点执行)。除非用户明确说'提前X秒/分钟唤醒'，禁止传",
        "en": "Seconds to wake early, default 0; set only when explicitly requested",
    },
    "delete_after_run": {
        "cn": "一次性任务标记：执行一次后标记过期。全固定7段表达式自动推断为true；显式传true亦可",
        "en": "One-shot flag; inferred true for an all-fixed 7-field expression",
    },
    "timeout_seconds": {
        "cn": "单次执行超时秒数；普通模式默认600，team模式1200。长任务(如pptx生成)可放宽",
        "en": "Per-run timeout; default 600 seconds, or 1200 for team modes",
    },
    "mode": {"cn": "任务触发时的agent运行模式，默认agent", "en": "Agent run mode used when the job fires, default agent"},
    "id": {"cn": "自定义任务ID（可选，缺省自动生成）", "en": "Optional custom job ID; generated when omitted"},
    "job_id": {"cn": "任务ID（来自 list/add 返回值）", "en": "Job ID returned by list/add"},
    "count": {"cn": "条数，默认5，范围1-50", "en": "Result count, default 5, range 1-50"},
    "include_disabled": {"cn": "是否包含已停用任务，默认false", "en": "Whether to include disabled jobs, default false"},
    "text": {"cn": "发给当前会话的消息文本", "en": "Message text sent to the current session"},
    "toggle_enabled": {"cn": "目标状态：true=启用，false=停用", "en": "Target state: true=enabled, false=disabled"},
}


def _field(key: str, language: str = "cn") -> str:
    d = _FIELD_DESC.get(key, {})
    return d.get(language, d.get("cn", ""))


# 各 action 的操作语义（两风格共用，只写"做什么"，不写字段表、不出现 action=）。
# job 字段说明放顶层 _merged_cron_description（description 风格）或 properties（properties 风格）。
_ACTION_DESCS: dict[str, dict[str, str]] = {
    "status": {"cn": "任务数概览", "en": "job count summary (no params)"},
    "list": {"cn": "列任务（可选 include_disabled 含停用）", "en": "list jobs (optional include_disabled)"},
    "add": {"cn": "创建定时任务（需 name+schedule+description；可选 id 自定义任务ID，不带则系统生成）", "en": "create a job (requires name+schedule+description; optional id for a custom job id, auto-generated if absent)"},
    "get": {"cn": "按 job_id 查询单个任务", "en": "lookup one job by job_id"},
    "update": {
        "cn": "修改定时任务（必填 job_id，其余字段只传要改的，不可改 id）",
        "en": "modify a job (job_id required; send only fields to change; id immutable)",
    },
    "remove": {"cn": "按 job_id 删除任务", "en": "delete a job by job_id"},
    "toggle": {"cn": "启停任务（必填 job_id + enabled）", "en": "enable/disable a job (requires job_id + enabled)"},
    "preview": {"cn": "预览下N次触发（必填 job_id，可选 count）", "en": "preview next N runs (requires job_id, optional count)"},
    "run": {
        "cn": "按 job_id 立即触发一次（转发网关，不等结果）",
        "en": "trigger a job once now by job_id (forwarded to gateway, no result)",
    },
    "wake": {"cn": "向当前会话发消息唤醒（必填 text）", "en": "wake the current session with a message (requires text)"},
}


def _action_desc(action: str, language: str = "cn") -> str:
    d = _ACTION_DESCS.get(action, {})
    return d.get(language, d.get("cn", ""))


def _merged_cron_description(language: str = "cn", style: str = "description") -> str:
    """简短工具级描述；详细参数契约只放在 input schema。"""
    _ = style  # 保留兼容签名，风格只影响参数 schema。
    if language == "en":
        return (
            "Cron task manager (cron_flash): fill one operation object (add/get/update/remove/"
            "toggle/preview/run/wake/list/status); the object key is the operation. "
            "Provide exactly one operation object per call. "
            "protected=true jobs are system-managed, immutable."
        )
    return (
        "定时任务管理(cron_flash)：每次必须且只能填写一个操作对象（add/get/update/remove/toggle/preview/"
        "run/wake/list/status），对象键即操作类型。protected=true 的任务由系统管理，不可删改启停。"
    )


def _merged_cron_params_description(language: str = "cn", style: str = "description") -> str:
    """参数级说明；description 风格在这里集中承载字段文档，避免与 ToolCard 重复。"""
    if style == "description":
        label = "Job object fields: " if language == "en" else "job 对象字段："
        separator = ". " if language == "en" else "。"
        return f"{label}{_job_fields_prose(language, with_id=False)}{separator}{_expr_desc(language)}"
    return "Provide exactly one operation object." if language == "en" else "每次必须且只能填写一个操作对象。"


def _expr_desc(language: str = "cn") -> str:
    """cron 表达式规则（按 croniter 实测真值重写，挂在 add.schedule.expr）。"""
    if language == "en":
        return (
            "Cron expression (kind=cron). 7-field Quartz (sec min hour day month dow year) "
            "or 5-field (min hour day month dow, auto sec=0 year=*). Dow 0-6 (0=Sun..6=Sat) "
            "or MON/SUN; 7 invalid. */X=step X from field min, NOT every X: min/sec X|60, hour X|24; "
            "day/month/dow forbid */X. One-shot: 7 fields all fixed + year + dow=?, "
            "e.g. '0 30 17 29 4 ? 2026' (auto-infers delete_after_run); or use kind=at+ISO. "
            "Examples: daily 9am='0 0 9 * * ? *'; every 15min='0 */15 * * * ? *'; Mon 9am='0 0 9 ? * MON *'"
        )
    return (
        "cron表达式(kind=cron)。7段Quartz(秒 分 时 日 月 周 年)或5段(分 时 日 月 周，自动补秒=0、年=*)。"
        "周字段0-6(0=周日…6=周六)或MON/SUN，数字7非法。*/X=从最小值起步长X，非每隔X：分/秒X须整除60、时X须整除24；日/月/周禁用*/X。"
        "一次性：7段全固定+年份、周?，如'0 30 17 29 4 ? 2026'（自动推断delete_after_run）；或用kind=at+ISO。"
        "例：每天9点='0 0 9 * * ? *'；每15分钟='0 */15 * * * ? *'；每周一9点='0 0 9 ? * MON *'"
    )


def _job_fields_prose(language: str = "cn", *, with_id: bool = True) -> str:
    """B+description 用的 job 字段 prose 说明（无 properties 时的字段契约）。"""
    if language == "en":
        parts = [
            "name(str,required,<=64chars)",
            "schedule{kind=cron|at, expr(cron), at(ISO,kind=at), tz(IANA,default Asia/Shanghai)}(required)",
            "description(str,required,task instruction)",
            f"targets({'|'.join(_TARGETS_ENUM)}, default=current channel)",
            f"mode({'|'.join(_MODE_ENUM)}, default=agent)",
            "enabled(bool,default=true)",
            "wake_offset_seconds(int,default0)",
            "delete_after_run(bool,one-shot; auto-inferred true for all-fixed 7-field)",
            "timeout_seconds(int,default 600/team1200)",
        ]
        if with_id:
            parts.append("id(str,optional,custom job id)")
        return ", ".join(parts)
    parts = [
        "name(必填,≤64字符)",
        "schedule{kind=cron|at, expr(cron表达式), at(ISO时刻,kind=at时), tz(时区,缺省Asia/Shanghai)}(必填)",
        "description(必填,任务指令文本)",
        f"targets({'|'.join(_TARGETS_ENUM)},缺省=当前会话频道)",
        f"mode({'|'.join(_MODE_ENUM)},缺省agent)",
        "enabled(默认true)",
        "wake_offset_seconds(默认0)",
        "delete_after_run(一次性;全固定7段自动推断true)",
        "timeout_seconds(默认600/team1200)",
    ]
    if with_id:
        parts.append("id(可选,自定义ID)")
    return "；".join(parts)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

def _strip_desc(node: Any) -> Any:
    """递归去掉 schema 注释，但保留 properties 中名为 description 的业务字段。"""
    if isinstance(node, dict):
        stripped: dict[str, Any] = {}
        for key, value in node.items():
            if key == "description":
                continue
            if key == "properties" and isinstance(value, dict):
                stripped[key] = {
                    property_name: _strip_desc(property_schema)
                    for property_name, property_schema in value.items()
                }
            else:
                stripped[key] = _strip_desc(value)
        return stripped
    if isinstance(node, list):
        return [_strip_desc(x) for x in node]
    return node


def _schedule_schema(language: str, *, with_field_desc: bool) -> dict[str, Any]:
    """完整 schedule 多态；显式 kind 决定且只允许对应的 expr/at 形态。"""
    tz = {
        "type": "string",
        "minLength": 1,
        "description": _field("tz", language),
    }
    schedule = {
        "description": _field("schedule", language),
        "oneOf": [
            {
                "type": "object",
                "required": ["kind", "expr"],
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "const": "cron",
                        "description": _field("schedule_kind", language),
                    },
                    "expr": {
                        "type": "string",
                        "minLength": 1,
                        "description": _expr_desc(language),
                    },
                    "tz": tz,
                },
            },
            {
                "type": "object",
                "required": ["kind", "at"],
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "const": "at",
                        "description": _field("schedule_kind", language),
                    },
                    "at": {
                        "type": "string",
                        "minLength": 1,
                        "format": "date-time",
                        "description": _field("schedule_at", language),
                    },
                    "tz": tz,
                },
            },
        ],
    }
    return schedule if with_field_desc else _strip_desc(schedule)


def _job_properties(language: str = "cn", *, with_id: bool = True, with_field_desc: bool = True) -> dict[str, Any]:
    """job 字段 properties。

    - with_field_desc=True（properties 风格）：每字段带 description（cron 规则在 schedule.expr）。
    - with_field_desc=False（description 风格）：只留 type/enum/required/properties 骨架，
      字段说明 + cron 规则集中到顶层 _merged_cron_description（DRY 一份，不在 add/update 重复）。
    - with_id=False：去 id（update 用）。
    """
    props: dict[str, Any] = {
        "name": {"type": "string", "minLength": 1, "maxLength": 64, "description": _field("name", language)},
        "schedule": _schedule_schema(language, with_field_desc=with_field_desc),
        "description": {
            "type": "string", "minLength": 1, "maxLength": 500,
            "description": _field("description", language),
        },
        "targets": {"type": "string", "enum": _TARGETS_ENUM, "description": _field("targets", language)},
        "enabled": {"type": "boolean", "description": _field("enabled", language)},
        "wake_offset_seconds": {
            "type": "integer", "minimum": 0,
            "description": _field("wake_offset_seconds", language),
        },
        "delete_after_run": {"type": "boolean", "description": _field("delete_after_run", language)},
        "timeout_seconds": {
            "type": "integer", "minimum": 1,
            "description": _field("timeout_seconds", language),
        },
        "mode": {"type": "string", "enum": _MODE_ENUM, "description": _field("mode", language)},
    }
    if with_id:
        props["id"] = {"type": "string", "minLength": 1, "description": _field("id", language)}
    if not with_field_desc:
        props = {name: _strip_desc(schema) for name, schema in props.items()}
    return props


def _action_object(
    *, style: str, props: dict[str, Any] | None, required: list[str], desc: str,
    min_properties: int | None = None,
) -> dict[str, Any]:
    """构造一个 action 对象。两风格都用 properties（结构骨架）；
    description 风格额外 strip 掉 per-field description（集中顶层 + action 语义 desc）。"""
    props = props or {}
    if style == "description":
        props = {name: _strip_desc(schema) for name, schema in props.items()}
    schema = {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "description": desc,
        "properties": props,
    }
    if min_properties is not None:
        schema["minProperties"] = min_properties
    return schema


def _merged_cron_input_params(language: str = "cn") -> dict[str, Any]:
    """cron_flash schema：无 action 字段，10 个 action 对象键判别。style 开关控制 properties/description。"""
    style = _schema_style()
    job_id_prop = {"type": "string", "minLength": 1, "description": _field("job_id", language)}
    # properties 风格：每字段带 description；description 风格：只留骨架（per-field desc 集中顶层）
    with_fd = (style == "properties")
    add_props = _job_properties(language, with_id=True, with_field_desc=with_fd)
    update_props = _job_properties(language, with_id=False, with_field_desc=with_fd)

    # 两风格共用同一组操作语义（只写"做什么"；字段表在顶层 description / properties）
    add_desc = _action_desc("add", language)
    update_desc = _action_desc("update", language)
    get_desc = _action_desc("get", language)
    remove_desc = _action_desc("remove", language)
    run_desc = _action_desc("run", language)
    toggle_desc = _action_desc("toggle", language)
    preview_desc = _action_desc("preview", language)
    list_desc = _action_desc("list", language)
    wake_desc = _action_desc("wake", language)
    status_desc = _action_desc("status", language)

    def _id_action(desc: str) -> dict[str, Any]:
        return _action_object(
            style=style, props={"job_id": job_id_prop}, required=["job_id"], desc=desc,
        )

    return {
        "type": "object",
        "required": [],
        "minProperties": 1,
        "maxProperties": 1,
        "additionalProperties": False,
        "description": _merged_cron_params_description(language, style),
        "properties": {
            "status": _action_object(style=style, props={}, required=[], desc=status_desc),
            "list": _action_object(
                style=style,
                props={"include_disabled": {"type": "boolean", "description": _field("include_disabled", language)}},
                required=[], desc=list_desc,
            ),
            "add": _action_object(
                style=style, props=add_props,
                required=["name", "schedule", "description"], desc=add_desc,
            ),
            "get": _id_action(get_desc),
            "update": _action_object(
                style=style, props={"job_id": job_id_prop, **update_props},
                required=["job_id"], desc=update_desc, min_properties=2,
            ),
            "remove": _id_action(remove_desc),
            "toggle": _action_object(
                style=style,
                props={
                    "job_id": job_id_prop,
                    "enabled": {
                        "type": "boolean",
                        "description": _field("toggle_enabled", language),
                    },
                },
                required=["job_id", "enabled"], desc=toggle_desc,
            ),
            "preview": _action_object(
                style=style,
                props={
                    "job_id": job_id_prop,
                    "count": {
                        "type": "integer", "minimum": 1, "maximum": 50, "default": 5,
                        "description": _field("count", language),
                    },
                },
                required=["job_id"], desc=preview_desc,
            ),
            "run": _id_action(run_desc),
            "wake": _action_object(
                style=style,
                props={"text": {"type": "string", "minLength": 1, "description": _field("text", language)}},
                required=["text"], desc=wake_desc,
            ),
        },
    }


# --------------------------------------------------------------------------- #
# dispatch：对象键判别 + 翻译护栏
# --------------------------------------------------------------------------- #

def _detect_action(inputs: dict[str, Any]) -> str:
    """B 形：顶层必须且只能包含一个当前 schema 声明的 action 对象。"""
    if not inputs:
        return ""
    unknown = [
        name
        for name, value in inputs.items()
        if name not in _ACTIONS and value is not None
    ]
    if unknown:
        raise ValueError(f"unsupported top-level cron fields: {unknown}")
    selected = [name for name in _ACTIONS if isinstance(inputs.get(name), dict)]
    if len(selected) > 1:
        raise ValueError(f"multiple cron actions provided: {selected}")
    return selected[0] if selected else ""


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], *, scope: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unsupported fields for {scope}: {unknown}")


def _required_text(data: dict[str, Any], key: str, *, max_length: int | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{key} must be at most {max_length} characters")
    return value.strip()


def _validate_schedule(schedule: Any) -> None:
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    kind = _required_text(schedule, "kind")
    if kind == "cron":
        _reject_unknown_fields(schedule, {"kind", "expr", "tz"}, scope="schedule(kind=cron)")
        _required_text(schedule, "expr")
    elif kind == "at":
        _reject_unknown_fields(schedule, {"kind", "at", "tz"}, scope="schedule(kind=at)")
        _required_text(schedule, "at")
    else:
        raise ValueError(f"unsupported schedule.kind={kind!r}; expected 'cron' or 'at'")
    if "tz" in schedule:
        _required_text(schedule, "tz")


_JOB_FIELDS = {
    "name", "schedule", "description", "targets", "enabled",
    "wake_offset_seconds", "delete_after_run", "timeout_seconds", "mode", "id",
}


def _validate_job_data(data: dict[str, Any], *, is_update: bool) -> None:
    allowed = _JOB_FIELDS - ({"id"} if is_update else set())
    _reject_unknown_fields(data, allowed, scope="update" if is_update else "add")
    if is_update:
        if not data:
            raise ValueError("update requires at least one field to change")
    else:
        missing = [key for key in ("name", "schedule", "description") if key not in data]
        if missing:
            raise ValueError(f"add missing required fields: {missing}")

    if "name" in data:
        _required_text(data, "name", max_length=64)
    if "description" in data:
        _required_text(data, "description", max_length=500)
    if "id" in data:
        _required_text(data, "id")
    if "schedule" in data:
        _validate_schedule(data["schedule"])
    if "targets" in data:
        targets = _required_text(data, "targets")
        if targets not in _TARGETS_ENUM:
            raise ValueError(f"unsupported targets={targets!r}")
    if "mode" in data:
        mode = _required_text(data, "mode")
        if mode not in _MODE_ENUM:
            raise ValueError(f"unsupported mode={mode!r}")
    for key in ("enabled", "delete_after_run"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"{key} must be a boolean")
    for key, minimum in (("wake_offset_seconds", 0), ("timeout_seconds", 1)):
        if key in data:
            value = data[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"{key} must be an integer >= {minimum}")


def _translate_to_native(
    data: dict[str, Any],
    *,
    is_update: bool,
    context: CronToolContext | None,
) -> dict[str, Any]:
    """把当前 schema job 对象翻译成 CronTools 原生扁平键。

    - schedule{kind, expr|at, tz} → cron_expr/timezone；kind=at 调
      ``iso_to_seven_field_cron``
    - 白名单直通 name/description/targets/enabled/wake_offset_seconds/
      delete_after_run/mode/timeout_seconds + id(仅 add)
    - add 缺 mode 时注入 context.mode；data.mode 优先；update 永不注入
    """
    if not isinstance(data, dict):
        raise ValueError("job data must be an object")
    _validate_job_data(data, is_update=is_update)
    out: dict[str, Any] = {}

    sched = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    kind = str(sched.get("kind") or "").strip()
    timezone = str(sched.get("tz") or "").strip()
    if kind == "at":
        at_raw = str(sched.get("at") or "").strip()
        tz_for_at = timezone or "Asia/Shanghai"
        try:
            from jiuwenswarm.gateway.cron.cron_expr import iso_to_seven_field_cron
            cron_expr = iso_to_seven_field_cron(at_raw, timezone=tz_for_at)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Cannot convert schedule.at='{at_raw}': {exc}") from exc
    elif kind == "cron":
        cron_expr = str(sched.get("expr") or "").strip()
    else:
        cron_expr = ""
    if cron_expr:
        out["cron_expr"] = cron_expr
    if timezone:
        out["timezone"] = timezone

    for k in (
        "name", "description", "targets", "enabled",
        "wake_offset_seconds", "delete_after_run", "mode", "timeout_seconds",
    ):
        if k in data:
            out[k] = data[k]
    if not is_update and "id" in data:
        out["id"] = str(data.get("id") or "").strip()

    if not is_update and "mode" not in out:
        ctx_mode = getattr(context, "mode", None)
        normalized_mode = ctx_mode.strip() if isinstance(ctx_mode, str) else ""
        # ``flash`` is an AgentServer request profile, not a supported cron job
        # execution mode.  Keep the schema's documented default instead of
        # leaking the profile name into CronTools validation.
        out["mode"] = normalized_mode if normalized_mode in _MODE_ENUM else "agent"

    return out


async def _cron_dispatch(
    backend: CronToolBackend,
    context: CronToolContext | None,
    inputs: dict[str, Any],
) -> Any:
    """按唯一 action 对象键分派；只接受当前 schema 能生成的输入。"""
    action = _detect_action(inputs)
    if not action:
        raise ValueError(
            f"no cron action object provided; fill one of {_ACTIONS} as an object key"
        )
    scoped = inputs.get(action)
    if not isinstance(scoped, dict):
        raise ValueError(f"cron action {action!r} must be an object")

    if action == "status":
        _reject_unknown_fields(scoped, set(), scope="status")
        status = await backend.status()
        # The shared backend currently cannot observe scheduler liveness and
        # reports a constant ``running=false``.  Flash exposes only the field
        # it can state truthfully rather than presenting that placeholder as
        # runtime state.
        return {"job_count": status.get("job_count", 0)}
    if action == "list":
        _reject_unknown_fields(scoped, {"include_disabled"}, scope="list")
        include = scoped.get("include_disabled", False)
        if not isinstance(include, bool):
            raise ValueError("include_disabled must be a boolean")
        return {"jobs": await backend.list_jobs(include_disabled=include)}
    if action == "get":
        _reject_unknown_fields(scoped, {"job_id"}, scope="get")
        job_id = _required_text(scoped, "job_id")
        return await backend.get_job(job_id)
    if action == "add":
        create_input = _translate_to_native(scoped, is_update=False, context=context)
        return await backend.create_job(create_input, context=context)
    if action == "update":
        job_id = _required_text(scoped, "job_id")
        patch = {k: v for k, v in scoped.items() if k != "job_id"}
        patch_input = _translate_to_native(patch, is_update=True, context=context)
        return await backend.update_job(job_id, patch_input, context=context)
    if action == "remove":
        _reject_unknown_fields(scoped, {"job_id"}, scope="remove")
        job_id = _required_text(scoped, "job_id")
        return {"deleted": await backend.delete_job(job_id)}
    if action == "toggle":
        _reject_unknown_fields(scoped, {"job_id", "enabled"}, scope="toggle")
        job_id = _required_text(scoped, "job_id")
        enabled = scoped.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        return await backend.toggle_job(job_id, enabled)
    if action == "preview":
        _reject_unknown_fields(scoped, {"job_id", "count"}, scope="preview")
        job_id = _required_text(scoped, "job_id")
        count = scoped.get("count", 5)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 50:
            raise ValueError("count must be an integer between 1 and 50")
        return await backend.preview_job(job_id, count)
    if action == "run":
        _reject_unknown_fields(scoped, {"job_id"}, scope="run")
        job_id = _required_text(scoped, "job_id")
        return {"run_id": await backend.run_now(job_id)}
    if action == "wake":
        _reject_unknown_fields(scoped, {"text"}, scope="wake")
        text = _required_text(scoped, "text")
        return await backend.wake(text, context=context, mode=None)
    raise ValueError(f"unsupported cron action: {action!r}")


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #

def build_cron_flash_tool(
    backend: CronToolBackend,
    *,
    context: Any,
    agent_id: str | None,
    language: str = "cn",
) -> LocalFunction:
    """构造 cron_flash 工具（单个 LocalFunction），合并公共后端的独立 cron 工具。"""
    async def _invoke(**kwargs: Any) -> Any:
        return await _cron_dispatch(backend, context, kwargs or {})

    tool_id = f"cron_flash_{agent_id}" if agent_id else "cron_flash_merged"
    style = _schema_style()
    card = ToolCard(
        id=tool_id,
        name="cron_flash",
        description=_merged_cron_description(language, style),
        input_params=_merged_cron_input_params(language),
    )
    return LocalFunction(card=card, func=_invoke)


__all__ = ["build_cron_flash_tool"]
