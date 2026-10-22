# -*- coding: utf-8 -*-
"""用量与费用看板的聚合引擎（纯标准库）。

只读数据源与计总口径见同目录 ``README.md``；每条结论都带宿主源码依据（文件:行号），
基线为 ``openJiuwen/jiuwenswarm`` ``develop@c4440433``。

引擎不碰网络、不写文件、不 import 被检对象；所有路径由调用方传入，
宿主相关的路径解析放在 ``extension.py`` 一侧。
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

#: 导出/报告结构版本，便于前端与消费方对齐。
SCHEMA_VERSION = 1

#: 会话历史文件名（定义在 ``server/runtime/session/session_history.py:33-34``）。
HISTORY_FILENAME = "history.jsonl"

#: 价格表相对用户数据根目录的位置（路径解析见 ``common/utils.py:432-449``）。
PRICES_RELATIVE_PATH = "usage_board/prices.json"

#: 会话 id 白名单：只允许安全字符，杜绝路径穿越。
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

DEFAULT_DAYS = 30
DEFAULT_LIMIT = 50
DEFAULT_SESSION_LIMIT = 200
MAX_LIMIT = 200
MAX_SESSION_LIMIT = 2000

#: 事件里没有模型名时，按模型分桶用的占位名。
UNKNOWN_MODEL_LABEL = "(未标注模型)"

#: 单会话最多解析的事件条数，超出即截断并记 UB006。
MAX_EVENTS_PER_SESSION = 20000

#: 单会话详情最多返回的回合行数（取最新的若干条）。
MAX_DETAIL_TURNS = 500

#: 单行超过该字符数直接跳过，避免畸形行拖垮扫描。
MAX_LINE_CHARS = 512 * 1024

#: summary 与 metadata 的 total_tokens 相对偏差阈值（超过记 UB003）。
CROSS_CHECK_TOLERANCE = 0.01

#: 金额保留小数位，与宿主一致（``interface_deep.py:19197``）。
COST_ROUND = 6

EXPORT_FORMATS = ("json", "markdown", "csv")
SEVERITIES = ("info", "warning")
USAGE_EVENT_TYPES = ("chat.usage_summary", "chat.usage_metadata", "context.usage")


@dataclass(frozen=True)
class FindingDef:
    """一条诊断结论的静态定义（README 与测试都以此为准）。"""

    code: str
    severity: str
    title: str
    trigger: str
    evidence: str


_FINDING_DEFS = (
    FindingDef(
        "UB001",
        "info",
        "扫描范围内没有任何用量事件",
        "会话目录里没有可解析的 chat.usage_summary / chat.usage_metadata",
        "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:18772-18798; "
        "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:19239-19249",
    ),
    FindingDef(
        "UB002",
        "warning",
        "有 token 但没有价格表",
        "没有 <data_dir>/usage_board/prices.json 时无法给出任何金额"
        "（价格表是插件自定义格式，宿主没有对应概念）",
        "自身：价格表为插件自定义格式（README「价格表」小节）; "
        "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:19193-19197",
    ),
    FindingDef(
        "UB003",
        "info",
        "回合汇总与逐次调用口径不一致",
        "两者 total_tokens 相对偏差 > 1%",
        "jiuwenswarm/channels/process_cli/machine_result.py:191-198",
    ),
    FindingDef(
        "UB004",
        "warning",
        "history.jsonl 有无法解析的行",
        "单行不是 JSON 对象或超过单行上限，已跳过",
        "jiuwenswarm/server/runtime/session/session_history.py:401-423",
    ),
    FindingDef(
        "UB005",
        "info",
        "有回合既没上报金额也匹配不到单价",
        "登录/积分模型不报金额，价格表也可能未覆盖该模型",
        "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:19193-19197",
    ),
    FindingDef(
        "UB006",
        "info",
        "命中扫描上限被截断",
        "事件条数或会话数超过上限，统计结果只是下界（上限是插件常量，"
        "宿主对超限是直接判失败，语义不同）",
        "自身：扫描上限为本插件常量（宿主超限语义见 "
        "jiuwenswarm/channels/process_cli/machine_policy.py:83-85）",
    ),
    FindingDef(
        "UB007",
        "warning",
        "用量字段是负数或非有限数（本插件按 0 处理）",
        "该字段已按 0 处理，避免污染总计；宿主对非法金额是直接判"
        " BUDGET_METER_UNAVAILABLE、对非法 token 计数是跳过，都不按 0",
        "jiuwenswarm/channels/process_cli/machine_policy.py:74-79",
    ),
    FindingDef(
        "UB008",
        "warning",
        "价格表存在但解析失败",
        "prices.json 不是合法 JSON/映射，费用一律按未配置处理",
        "自身：价格表为插件自定义格式（README「价格表」小节）",
    ),
    FindingDef(
        "UB009",
        "info",
        "会话目录不可读或不存在",
        "取不到 <data_dir>/agent/sessions 时无法统计",
        "jiuwenswarm/common/utils.py:2361-2362",
    ),
)

#: 按 code 升序的结论目录。
FINDING_DEFS = tuple(sorted(_FINDING_DEFS, key=lambda item: item.code))
FINDINGS_BY_CODE = {item.code: item for item in FINDING_DEFS}


@dataclass(frozen=True)
class Finding:
    """一次扫描里真实命中的一条结论。"""

    code: str
    severity: str
    subject: str
    message: str
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "message": self.message,
            "evidence": self.evidence,
        }


def make_finding(code: str, subject: str, message: str) -> Finding:
    """按定义表生成结论，级别与依据永远取自 ``FINDING_DEFS``。"""
    definition = FINDINGS_BY_CODE[code]
    return Finding(
        code=definition.code,
        severity=definition.severity,
        subject=subject,
        message=message,
        evidence=definition.evidence,
    )


@dataclass(frozen=True)
class PriceEntry:
    """价格表里的一条单价（按每 100 万 token 计）。"""

    pattern: str
    input_per_1m: float
    output_per_1m: float
    currency: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "input_per_1m": self.input_per_1m,
            "output_per_1m": self.output_per_1m,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class PriceTable:
    """价格表：可缺失、可损坏，缺失时费用列为 ``None`` 而不是 0。"""

    entries: tuple[PriceEntry, ...] = ()
    currency: str = ""
    source: str | None = None
    error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.entries)

    def match(self, model: str | None) -> PriceEntry | None:
        """按「完全相等 > 字面长度降序」匹配模型名；不中返回 ``None``。"""
        name = str(model or "").strip().lower()
        if not name:
            return None
        exact = [item for item in self.entries if item.pattern == name]
        if exact:
            return exact[0]
        for item in self.entries:
            if _wildcard_matches(item.pattern, name):
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "path": self.source,
            "source": self.source,
            "models": len(self.entries),
            "currency": self.currency,
            "error": self.error,
            "entries": [item.as_dict() for item in self.entries],
        }


def _wildcard_matches(pattern: str, name: str) -> bool:
    """把 ``*`` 当任意串做全匹配；无通配符时不再重复比较。"""
    text = str(pattern or "").strip().lower()
    if not text or "*" not in text:
        return False
    regex = "^" + re.escape(text).replace(r"\*", ".*") + "$"
    return re.match(regex, name) is not None


def load_price_table(path: Path | None, *, display_path: str | None = None) -> PriceTable:
    """读价格表；文件不存在返回空表，损坏返回空表 + ``error``。"""
    shown = display_path or (str(path) if path is not None else None)
    if path is None:
        return PriceTable(source=shown)
    try:
        if not path.is_file():
            return PriceTable(source=shown)
    except OSError:
        return PriceTable(source=shown)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return PriceTable(source=shown, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(raw, dict):
        return PriceTable(source=shown, error="价格表根节点不是对象")
    models = raw.get("models")
    if not isinstance(models, dict):
        return PriceTable(source=shown, error="价格表缺少 models 映射")
    default_currency = str(raw.get("currency") or "").strip()
    entries: list[PriceEntry] = []
    for pattern, value in models.items():
        if not isinstance(value, dict):
            continue
        price_in = _positive_number(value.get("input_per_1m"))
        price_out = _positive_number(value.get("output_per_1m"))
        if price_in is None and price_out is None:
            continue
        name = str(pattern).strip().lower()
        if not name:
            continue
        entries.append(
            PriceEntry(
                pattern=name,
                input_per_1m=price_in or 0.0,
                output_per_1m=price_out or 0.0,
                currency=str(value.get("currency") or default_currency).strip(),
            )
        )
    entries.sort(key=lambda item: (-len(item.pattern.replace("*", "")), item.pattern))
    return PriceTable(
        entries=tuple(entries),
        currency=default_currency,
        source=shown,
    )


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def parse_epoch(value: Any) -> float | None:
    """把 epoch 秒或 ISO 时间串解析成 epoch 秒；解析不了返回 ``None``。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.timestamp()
    return moment.timestamp()


def day_of(epoch: float | None) -> str:
    """把 epoch 秒按本机时区归到自然日；取不到时间返回空串。"""
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _usage_int(value: Any) -> tuple[int, bool]:
    """返回 (非负整数, 是否被判为异常)。"""
    if isinstance(value, bool):
        return 0, True
    if isinstance(value, int):
        return (value, False) if value >= 0 else (0, True)
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0, True
        if value < 0:
            return 0, True
        return int(value), False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0, False
        try:
            return _usage_int(float(text))
        except ValueError:
            return 0, True
    return (0, True) if value is not None else (0, False)


def _usage_cost(value: Any) -> tuple[float | None, bool]:
    """返回 (金额或 None, 是否被判为异常)；负值/非有限数视为异常。

    显式上报的 ``0`` 是**有效金额**（免费/按积分计量），必须与「字段缺失」区分：
    否则下游会把免费回合当成未上报，回退到价格表估出一个虚假的非零费用。
    """
    if isinstance(value, bool):
        return None, True
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None, True
        return round(number, COST_ROUND), False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, False
        try:
            return _usage_cost(float(text))
        except ValueError:
            return None, True
    return (None, True) if value is not None else (None, False)


@dataclass(frozen=True)
class CallUsage:
    """一次计费单元：``usage_summary`` 的一个回合，或 ``usage_metadata`` 的一次调用。"""

    source: str
    ts: float | None
    day: str
    model: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_reported: float | None

    def as_dict(self, prices: PriceTable) -> dict[str, Any]:
        cost, cost_source = cost_for_call(self, prices)
        return {
            "ts": _iso(self.ts),
            "day": self.day,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_tokens": self.cache_tokens,
            "cost": cost,
            "cost_source": cost_source,
            "source": self.source,
        }


def cost_for_call(call: CallUsage, prices: PriceTable) -> tuple[float | None, str]:
    """优先用上报金额；没有则按价格表估算；都没有返回 ``(None, "missing")``。"""
    if call.cost_reported is not None:
        return call.cost_reported, "reported"
    entry = prices.match(call.model)
    if entry is None:
        return None, "missing"
    amount = (
        call.input_tokens / 1_000_000 * entry.input_per_1m
        + call.output_tokens / 1_000_000 * entry.output_per_1m
    )
    if not math.isfinite(amount) or amount < 0:
        return None, "missing"
    return round(amount, COST_ROUND), "estimated"


@dataclass
class _Totals:
    """聚合累加器：总计与各维度分桶共用同一套口径。"""

    sessions: int = 0
    turns: int = 0
    calls: int = 0
    calls_known: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0
    cost_reported: float = 0.0
    cost_estimated: float = 0.0
    priced_turns: int = 0
    unpriced_turns: int = 0
    #: 是否有回合**上报过**金额（哪怕上报的是 0），用于区分「上报 0」与「完全没上报」。
    cost_reported_known: bool = False

    def add_call(self, call: CallUsage, prices: PriceTable) -> None:
        self.turns += 1
        self.input_tokens += call.input_tokens
        self.output_tokens += call.output_tokens
        self.total_tokens += call.total_tokens
        self.cache_tokens += call.cache_tokens
        amount, source = cost_for_call(call, prices)
        if source == "reported":
            self.cost_reported += amount or 0.0
            self.priced_turns += 1
            self.cost_reported_known = True
        elif source == "estimated":
            self.cost_estimated += amount or 0.0
            self.priced_turns += 1
        else:
            self.unpriced_turns += 1

    @property
    def cost_total(self) -> float:
        return round(self.cost_reported + self.cost_estimated, COST_ROUND)

    @property
    def cost_source(self) -> str:
        reported = self.cost_reported_known or round(self.cost_reported, COST_ROUND) > 0
        estimated = round(self.cost_estimated, COST_ROUND) > 0
        if reported and estimated:
            return "mixed"
        if reported:
            return "reported"
        if estimated:
            return "estimated"
        return "missing"

    def as_dict(self, *, sessions: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "turns": self.turns,
            "calls": self.calls if self.calls_known else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_tokens": self.cache_tokens,
            "cost_reported": round(self.cost_reported, COST_ROUND),
            "cost_estimated": round(self.cost_estimated, COST_ROUND),
            "cost_total": self.cost_total,
            "cost_source": self.cost_source,
            "priced_turns": self.priced_turns,
            "unpriced_turns": self.unpriced_turns,
        }
        if sessions is not None:
            payload["sessions"] = sessions
        return payload


@dataclass
class SessionUsage:
    """一个会话的解析结果。"""

    session_id: str
    path: str
    size_bytes: int = 0
    mtime: float | None = None
    truncated: bool = False
    skipped_lines: int = 0
    invalid_fields: int = 0
    summary_calls: list[CallUsage] = field(default_factory=list)
    metadata_calls: list[CallUsage] = field(default_factory=list)
    context: dict[str, Any] | None = None
    summary_tokens: int = 0
    metadata_tokens: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def totals_source(self) -> str:
        """计总口径：有回合汇总就用汇总（宿主同口径），否则用逐次调用。"""
        return "usage_summary" if self.summary_calls else "usage_metadata"

    @property
    def billing_calls(self) -> list[CallUsage]:
        if self.summary_calls:
            return self.summary_calls
        return self.metadata_calls

    @property
    def calls(self) -> int | None:
        return len(self.metadata_calls) if self.metadata_calls else None

    @property
    def last_ts(self) -> float | None:
        stamps = [
            call.ts
            for call in [*self.summary_calls, *self.metadata_calls]
            if call.ts is not None
        ]
        if self.context and isinstance(self.context.get("ts"), float):
            stamps.append(float(self.context["ts"]))
        if stamps:
            return max(stamps)
        return self.mtime

    def models(self) -> list[str]:
        seen: dict[str, None] = {}
        for call in self.billing_calls:
            name = str(call.model or "").strip()
            if name:
                seen.setdefault(name, None)
        return list(seen)


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch).astimezone().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _extract_usage(record: dict[str, Any], event_type: str) -> dict[str, Any] | None:
    if event_type == "chat.usage_summary":
        usage = record.get("usage")
        return usage if isinstance(usage, dict) else None
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return None
    usage = metadata.get("usage_metadata")
    return usage if isinstance(usage, dict) else None


def _extract_model(record: dict[str, Any], event_type: str) -> str | None:
    for candidate in (record.get("model"),):
        name = str(candidate or "").strip()
        if name:
            return name
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in ("model", "model_name"):
            name = str(metadata.get(key) or "").strip()
            if name:
                return name
    if event_type == "context.usage":
        name = str(record.get("model") or "").strip()
        if name:
            return name
    return None


def _call_from_record(
    record: dict[str, Any], event_type: str, stamp: float | None
) -> tuple[CallUsage | None, int]:
    usage = _extract_usage(record, event_type)
    if usage is None:
        return None, 0
    invalid = 0
    tokens: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "cache_tokens"):
        value, bad = _usage_int(usage.get(key))
        tokens[key] = value
        if bad:
            invalid += 1
    if tokens["total_tokens"] <= 0:
        tokens["total_tokens"] = tokens["input_tokens"] + tokens["output_tokens"]
    cost, bad_cost = _usage_cost(usage.get("total_cost"))
    if bad_cost:
        invalid += 1
    call = CallUsage(
        source="usage_summary" if event_type == "chat.usage_summary" else "usage_metadata",
        ts=stamp,
        day=day_of(stamp),
        model=_extract_model(record, event_type),
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        total_tokens=tokens["total_tokens"],
        cache_tokens=tokens["cache_tokens"],
        cost_reported=cost,
    )
    return call, invalid


def _nonneg_number(value: Any) -> float | None:
    """收 0 与正数，拒绝负数/非有限数/非数字。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _context_from_record(record: dict[str, Any], stamp: float | None) -> dict[str, Any] | None:
    window = record.get("context_window")
    if not isinstance(window, dict):
        return None
    limit = _usage_int(window.get("limit_tokens"))[0]
    used = _usage_int(window.get("input_tokens"))[0]
    return {
        "limit_tokens": limit,
        "input_tokens": used,
        "occupancy_rate": _nonneg_number(window.get("occupancy_rate")),
        "model": _extract_model(record, "context.usage"),
        "provider": str(record.get("provider") or "").strip() or None,
        "agent_id": str(record.get("agent_id") or "").strip() or None,
        "ts": stamp,
    }


def parse_session_lines(
    lines: Iterable[str],
    *,
    session_id: str,
    path: str = "",
    size_bytes: int = 0,
    mtime: float | None = None,
    max_events: int = MAX_EVENTS_PER_SESSION,
) -> SessionUsage:
    """解析一个会话的 JSONL 行；损坏行只计数不抛错。"""
    session = SessionUsage(
        session_id=session_id,
        path=path,
        size_bytes=size_bytes,
        mtime=mtime,
    )
    events = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if len(line) > MAX_LINE_CHARS:
            session.skipped_lines += 1
            continue
        if events >= max_events:
            session.truncated = True
            break
        events += 1
        try:
            record = json.loads(line)
        except ValueError:
            session.skipped_lines += 1
            continue
        if not isinstance(record, dict):
            session.skipped_lines += 1
            continue
        event_type = str(record.get("event_type") or "").strip()
        if event_type not in USAGE_EVENT_TYPES:
            continue
        stamp = parse_epoch(record.get("timestamp"))
        if event_type == "context.usage":
            snapshot = _context_from_record(record, stamp)
            if snapshot is not None:
                current = session.context
                if current is None or (stamp or 0) >= (current.get("ts") or 0):
                    session.context = snapshot
            continue
        call, invalid = _call_from_record(record, event_type, stamp)
        session.invalid_fields += invalid
        if call is None:
            continue
        if call.source == "usage_summary":
            session.summary_calls.append(call)
            session.summary_tokens += call.total_tokens
        else:
            session.metadata_calls.append(call)
            session.metadata_tokens += call.total_tokens
    return session


def load_session_file(
    path: Path,
    *,
    session_id: str,
    max_events: int = MAX_EVENTS_PER_SESSION,
) -> SessionUsage:
    """读一个 ``history.jsonl``；文件不可读时返回带 UB009 的空会话。"""
    size_bytes = 0
    mtime: float | None = None
    try:
        stat = path.stat()
        size_bytes = int(stat.st_size)
        mtime = float(stat.st_mtime)
    except OSError:
        pass
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            session = parse_session_lines(
                handle,
                session_id=session_id,
                path=str(path),
                size_bytes=size_bytes,
                mtime=mtime,
                max_events=max_events,
            )
    except OSError as exc:
        session = SessionUsage(
            session_id=session_id,
            path=str(path),
            size_bytes=size_bytes,
            mtime=mtime,
        )
        session.findings.append(
            make_finding("UB009", session_id, f"读取失败：{type(exc).__name__}")
        )
        return session
    return session


def session_cross_check(session: SessionUsage) -> dict[str, Any] | None:
    """回合汇总 vs 逐次调用的 token 交叉核对。"""
    if not session.summary_calls or not session.metadata_calls:
        return None
    baseline = max(1, session.summary_tokens)
    gap = abs(session.metadata_tokens - session.summary_tokens) / baseline
    return {
        "summary_total_tokens": session.summary_tokens,
        "metadata_total_tokens": session.metadata_tokens,
        "relative_gap": round(gap, 4),
    }


def session_findings(session: SessionUsage, prices: PriceTable) -> list[Finding]:
    """一个会话命中的结论（供详情与总览共用）。"""
    findings: list[Finding] = []
    if session.skipped_lines:
        findings.append(
            make_finding(
                "UB004",
                session.session_id,
                f"跳过 {session.skipped_lines} 行无法解析的历史记录",
            )
        )
    if session.invalid_fields:
        findings.append(
            make_finding(
                "UB007",
                session.session_id,
                f"{session.invalid_fields} 个用量字段为负数或非有限数，已按 0 处理",
            )
        )
    if session.truncated:
        findings.append(
            make_finding(
                "UB006",
                session.session_id,
                f"事件超过 {MAX_EVENTS_PER_SESSION} 条，只统计了前 {MAX_EVENTS_PER_SESSION} 条",
            )
        )
    cross = session_cross_check(session)
    if cross is not None and cross["relative_gap"] > CROSS_CHECK_TOLERANCE:
        findings.append(
            make_finding(
                "UB003",
                session.session_id,
                "回合汇总与逐次调用 token 偏差 "
                f"{cross['relative_gap'] * 100:.2f}%（汇总 {session.summary_tokens}、"
                f"逐次 {session.metadata_tokens}）",
            )
        )
    return findings


def _session_totals(session: SessionUsage, prices: PriceTable) -> _Totals:
    totals = _Totals(sessions=1)
    if session.metadata_calls:
        totals.calls = len(session.metadata_calls)
        totals.calls_known = True
    for call in session.billing_calls:
        totals.add_call(call, prices)
    return totals


def _iter_session_dirs(sessions_dir: Path) -> list[Path]:
    try:
        entries = list(sessions_dir.iterdir())
    except OSError:
        return []
    dirs = [item for item in entries if item.is_dir()]
    dirs.sort(key=lambda item: item.name)
    return dirs


def scan_sessions(
    sessions_dir: Path,
    *,
    prices: PriceTable | None = None,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    session_limit: int = DEFAULT_SESSION_LIMIT,
    now: float | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """扫描会话目录，产出一份可直接下发的总览。"""
    table = prices or PriceTable()
    days = _clamp(days, 0, 3650)
    limit = _clamp(limit, 1, MAX_LIMIT)
    session_limit = _clamp(session_limit, 1, MAX_SESSION_LIMIT)
    moment = now if isinstance(now, (int, float)) and math.isfinite(float(now)) else None
    cutoff = None
    if moment is not None and days > 0:
        cutoff = float(moment) - days * 86400.0

    findings: list[Finding] = []
    candidates = _iter_session_dirs(sessions_dir) if sessions_dir.is_dir() else []
    scanned_sessions = 0
    files_read = 0
    total_bytes = 0
    skipped_by_days = 0
    skipped_no_history = 0
    truncated_sessions = 0
    skipped_lines = 0
    used: list[SessionUsage] = []
    reachable = bool(candidates) or sessions_dir.is_dir()

    for folder in candidates:
        if scanned_sessions >= session_limit:
            findings.append(
                make_finding(
                    "UB006",
                    "scope",
                    f"会话目录超过 {session_limit} 个，只扫描了前 {session_limit} 个",
                )
            )
            break
        history = folder / HISTORY_FILENAME
        if not history.is_file():
            skipped_no_history += 1
            continue
        if cutoff is not None:
            # 先用 mtime 粗筛：日志是追加写的，最后修改时间早于窗口下界的文件
            # 不可能含有窗口内的事件，不必整文件解析（过期会话很多时省掉大量 IO）。
            try:
                if history.stat().st_mtime < cutoff:
                    skipped_by_days += 1
                    continue
            except OSError:
                pass
        session = load_session_file(history, session_id=folder.name)
        files_read += 1
        total_bytes += session.size_bytes
        skipped_lines += session.skipped_lines
        if session.truncated:
            truncated_sessions += 1
        last_ts = session.last_ts
        if cutoff is not None and last_ts is not None and last_ts < cutoff:
            skipped_by_days += 1
            continue
        scanned_sessions += 1
        if not session.summary_calls and not session.metadata_calls:
            continue
        used.append(session)

    totals = _Totals()
    by_model: dict[str, _Totals] = {}
    by_day: dict[str, _Totals] = {}
    session_rows: list[dict[str, Any]] = []
    cross_sessions = 0
    summary_tokens_all = 0
    metadata_tokens_all = 0
    unpriced_total = 0

    for session in used:
        session_totals = _session_totals(session, table)
        totals.sessions += 1
        totals.turns += session_totals.turns
        totals.calls += session_totals.calls
        totals.calls_known = totals.calls_known or session_totals.calls_known
        totals.input_tokens += session_totals.input_tokens
        totals.output_tokens += session_totals.output_tokens
        totals.total_tokens += session_totals.total_tokens
        totals.cache_tokens += session_totals.cache_tokens
        totals.cost_reported += session_totals.cost_reported
        totals.cost_estimated += session_totals.cost_estimated
        totals.cost_reported_known = (
            totals.cost_reported_known or session_totals.cost_reported_known
        )
        totals.priced_turns += session_totals.priced_turns
        totals.unpriced_turns += session_totals.unpriced_turns
        unpriced_total += session_totals.unpriced_turns
        for call in session.billing_calls:
            model_key = _model_key(call)
            by_model.setdefault(model_key, _Totals()).add_call(call, table)
            if call.day:
                by_day.setdefault(call.day, _Totals()).add_call(call, table)
        _count_bucket_calls(by_model, session.metadata_calls, _model_key)
        _count_bucket_calls(by_day, session.metadata_calls, lambda call: call.day)
        cross = session_cross_check(session)
        if cross is not None:
            cross_sessions += 1
            summary_tokens_all += cross["summary_total_tokens"]
            metadata_tokens_all += cross["metadata_total_tokens"]
        findings.extend(session_findings(session, table))
        session_rows.append(_session_row(session, table))

    if table.error:
        findings.append(make_finding("UB008", "price_table", table.error))
    if not used and reachable:
        findings.append(
            make_finding("UB001", "scope", "扫描范围内没有可计费的用量事件")
        )
    if not reachable:
        findings.append(
            make_finding("UB009", "scope", f"会话目录不存在或不可读：{sessions_dir}")
        )
    if totals.total_tokens > 0 and not table.configured:
        findings.append(
            make_finding(
                "UB002",
                "price_table",
                "统计到 token 但未配置价格表，费用列一律为空（不显示 0）",
            )
        )
    if unpriced_total:
        findings.append(
            make_finding(
                "UB005",
                "scope",
                f"{unpriced_total} 个回合既没有上报金额也没匹配到单价",
            )
        )

    session_rows.sort(key=lambda row: (-row["total_tokens"], row["session_id"]))
    cross_check = None
    if cross_sessions:
        baseline = max(1, summary_tokens_all)
        cross_check = {
            "comparable_sessions": cross_sessions,
            "summary_total_tokens": summary_tokens_all,
            "metadata_total_tokens": metadata_tokens_all,
            "relative_gap": round(abs(metadata_tokens_all - summary_tokens_all) / baseline, 4),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(moment if moment is not None else time.time()),
        "data_dir": str(sessions_dir.parent.parent) if sessions_dir.name == "sessions" else "",
        "sessions_dir": str(sessions_dir),
        "days": days,
        "limit": limit,
        "session_limit": session_limit,
        "scanned": {
            "sessions": totals.sessions,
            "files_read": files_read,
            "bytes": total_bytes,
            "truncated_sessions": truncated_sessions,
            "skipped_lines": skipped_lines,
            "skipped_by_days": skipped_by_days,
            "skipped_no_history": skipped_no_history,
        },
        "price_table": table.as_dict(),
        "totals": totals.as_dict(sessions=totals.sessions),
        "by_model": _bucket_rows(by_model, table, key="model"),
        "by_day": _bucket_rows(by_day, table, key="day", ascending=True),
        "sessions": session_rows[:limit],
        "cross_check": cross_check,
        "budget": budget or {"configured": False, "source": None},
        "findings": _sorted_findings(findings),
    }


def session_totals_tokens(session: SessionUsage) -> int:
    """会话的计总 token（用于跳过空会话）。"""
    return (
        session.summary_tokens if session.summary_calls else session.metadata_tokens
    )


def _bucket_rows(
    buckets: dict[str, _Totals],
    prices: PriceTable,
    *,
    key: str,
    ascending: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, bucket in buckets.items():
        row = {key: name, **bucket.as_dict()}
        row["cost"] = bucket.cost_total
        rows.append(row)
    if ascending:
        rows.sort(key=lambda item: str(item[key]))
    else:
        rows.sort(key=lambda item: (-int(item["total_tokens"]), str(item[key])))
    return rows


def _model_key(call: CallUsage) -> str:
    return str(call.model or "").strip() or UNKNOWN_MODEL_LABEL


def _count_bucket_calls(
    buckets: dict[str, _Totals],
    calls: Iterable[CallUsage],
    key_for: Any,
) -> None:
    """把逐次调用事件的次数记到对应桶上（桶不存在时补建，保证分桶 calls 不丢）。

    计总用的是 ``billing_calls``（有 summary 时是 summary），而模型调用次数只来自
    ``chat.usage_metadata``，所以这里单独按 ``metadata_calls`` 记数；某个模型只在
    metadata 里出现（没进 summary）时，也必须为它建桶，否则各桶 ``calls`` 之和会小于
    ``totals.calls``。
    """

    for call in calls:
        key = key_for(call)
        if not key:
            continue
        bucket = buckets.setdefault(key, _Totals())
        bucket.calls += 1
        bucket.calls_known = True


def _session_row(session: SessionUsage, prices: PriceTable) -> dict[str, Any]:
    totals = _session_totals(session, prices)
    return {
        "session_id": session.session_id,
        "path": session.path,
        "size_bytes": session.size_bytes,
        "truncated": session.truncated,
        "totals_source": session.totals_source,
        "turns": totals.turns,
        "calls": totals.calls if totals.calls_known else None,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "total_tokens": totals.total_tokens,
        "cache_tokens": totals.cache_tokens,
        "cost": totals.cost_total,
        "cost_source": totals.cost_source,
        "first_ts": _iso(min_ts(session)),
        "last_ts": _iso(session.last_ts),
        "models": session.models(),
    }


def min_ts(session: SessionUsage) -> float | None:
    stamps = [
        call.ts
        for call in [*session.summary_calls, *session.metadata_calls]
        if call.ts is not None
    ]
    return min(stamps) if stamps else None


def session_detail(
    sessions_dir: Path,
    session_id: Any,
    *,
    prices: PriceTable | None = None,
    max_events: int = MAX_EVENTS_PER_SESSION,
) -> dict[str, Any]:
    """单个会话的明细；session_id 非法或不存在抛 ``ValueError``。"""
    name = str(session_id or "").strip()
    if not SESSION_ID_PATTERN.match(name):
        raise ValueError("session_id 非法：只允许字母数字与 . _ -（1..128 个字符）")
    table = prices or PriceTable()
    path = sessions_dir / name / HISTORY_FILENAME
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(sessions_dir.resolve(strict=False))
    except (OSError, ValueError):
        raise ValueError("session_id 非法：越出会话目录") from None
    if not path.is_file():
        raise ValueError(f"会话不存在：{name}")
    session = load_session_file(path, session_id=name, max_events=max_events)
    totals = _session_totals(session, table)
    turns = [call.as_dict(table) for call in session.billing_calls]
    turns_truncated = len(turns) > MAX_DETAIL_TURNS
    if turns_truncated:
        turns = turns[-MAX_DETAIL_TURNS:]
    model_buckets: dict[str, _Totals] = {}
    day_buckets: dict[str, _Totals] = {}
    for call in session.billing_calls:
        model_buckets.setdefault(_model_key(call), _Totals()).add_call(call, table)
        if call.day:
            day_buckets.setdefault(call.day, _Totals()).add_call(call, table)
    _count_bucket_calls(model_buckets, session.metadata_calls, _model_key)
    _count_bucket_calls(day_buckets, session.metadata_calls, lambda call: call.day)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": name,
        "path": session.path,
        "size_bytes": session.size_bytes,
        "truncated": session.truncated,
        "totals_source": session.totals_source,
        "totals": totals.as_dict(),
        "turns": turns,
        "turns_truncated": turns_truncated,
        "models": _bucket_rows(model_buckets, table, key="model"),
        "by_day": _bucket_rows(day_buckets, table, key="day", ascending=True),
        "context": session.context,
        "cross_check": session_cross_check(session),
        "price_table": table.as_dict(),
        "findings": _sorted_findings(session_findings(session, table)),
    }


def _sorted_findings(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    ordered = sorted(findings, key=lambda item: (item.code, item.subject))
    seen: dict[tuple[str, str, str], Finding] = {}
    for item in ordered:
        seen.setdefault((item.code, item.subject, item.message), item)
    return [item.as_dict() for item in seen.values()]


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def render_markdown(overview: dict[str, Any]) -> str:
    """把总览渲染成 Markdown 报告（导出用）。"""
    totals = overview.get("totals") or {}
    lines = [
        "# 用量与费用看板",
        "",
        f"- 生成时间：{overview.get('generated_at')}",
        f"- 会话目录：`{_redact_path(overview.get('sessions_dir'))}`（已脱敏）",
        f"- 统计范围：最近 {overview.get('days')} 天，会话 {overview.get('scanned', {}).get('sessions', 0)} 个",
        "",
        "## 合计",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 会话 | {totals.get('sessions', 0)} |",
        f"| 回合 | {totals.get('turns', 0)} |",
        f"| 模型调用 | {totals.get('calls') if totals.get('calls') is not None else '—'} |",
        f"| 输入 token | {totals.get('input_tokens', 0)} |",
        f"| 输出 token | {totals.get('output_tokens', 0)} |",
        f"| 合计 token | {totals.get('total_tokens', 0)} |",
        f"| 缓存 token | {totals.get('cache_tokens', 0)} |",
        f"| 费用 | {_cost_text(totals.get('cost_total'), totals.get('cost_source'))} |",
        "",
        "## 按模型",
        "",
        "| 模型 | 回合 | 总 token | 费用 | 来源 |",
        "|---|---|---|---|---|",
    ]
    for row in overview.get("by_model") or []:
        lines.append(
            "| {model} | {turns} | {tokens} | {cost} | {source} |".format(
                model=row.get("model") or "—",
                turns=row.get("turns", 0),
                tokens=row.get("total_tokens", 0),
                cost=_cost_text(row.get("cost"), row.get("cost_source")),
                source=_source_text(row.get("cost_source")),
            )
        )
    lines.extend(["", "## 会话（按 token 降序）", "", "| 会话 | 回合 | 总 token | 费用 | 最后活动 |", "|---|---|---|---|---|"])
    for row in overview.get("sessions") or []:
        lines.append(
            "| {sid} | {turns} | {tokens} | {cost} | {last} |".format(
                sid=row.get("session_id"),
                turns=row.get("turns", 0),
                tokens=row.get("total_tokens", 0),
                cost=_cost_text(row.get("cost"), row.get("cost_source")),
                last=row.get("last_ts") or "—",
            )
        )
    findings = overview.get("findings") or []
    lines.extend(["", "## 结论", ""])
    if not findings:
        lines.append("- 无")
    for item in findings:
        lines.append(
            "- `{code}` {subject}：{message}（依据 {evidence}）".format(
                code=item.get("code"),
                subject=item.get("subject"),
                message=item.get("message"),
                evidence=item.get("evidence"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def render_csv(overview: dict[str, Any]) -> str:
    """会话级 CSV（导出用，字段固定，便于二次加工）。"""
    header = [
        "session_id",
        "turns",
        "calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "cost_total",
        "cost_source",
        "first_ts",
        "last_ts",
        "models",
    ]
    rows = [",".join(header)]
    for row in overview.get("sessions") or []:
        rows.append(
            ",".join(
                [
                    _csv(row.get("session_id")),
                    _csv(row.get("turns")),
                    _csv(row.get("calls")),
                    _csv(row.get("input_tokens")),
                    _csv(row.get("output_tokens")),
                    _csv(row.get("total_tokens")),
                    _csv(row.get("cache_tokens")),
                    _csv(row.get("cost")),
                    _csv(row.get("cost_source")),
                    _csv(row.get("first_ts")),
                    _csv(row.get("last_ts")),
                    _csv("; ".join(row.get("models") or [])),
                ]
            )
        )
    return "\n".join(rows) + "\n"


def _csv(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(char in text for char in ',"\n\r'):
        return '"' + text.replace('"', '""') + '"'
    return text


def _redact_path(value: Any) -> str:
    """导出用：只保留路径最后两段，避免把本机绝对路径（含用户名）写进报告。"""

    text = str(value or "").strip()
    if not text:
        return "—"
    parts = [part for part in Path(text).parts if part not in ("/", "\\", "")]
    if len(parts) <= 2:
        return text
    return "…/" + "/".join(parts[-2:])


#: 导出时必须摘掉的绝对路径字段（只在本地方便用的字段，不外带）。
_EXPORT_PATH_KEYS = ("data_dir", "sessions_dir", "path")


def _export_payload(overview: dict[str, Any]) -> dict[str, Any]:
    """导出用副本：剔除本机绝对路径，避免报告被分享时泄露用户名/目录结构。"""

    payload = {
        key: value for key, value in overview.items() if key not in _EXPORT_PATH_KEYS
    }
    sessions = payload.get("sessions")
    if isinstance(sessions, list):
        payload["sessions"] = [
            {key: value for key, value in row.items() if key not in _EXPORT_PATH_KEYS}
            if isinstance(row, dict)
            else row
            for row in sessions
        ]
    return payload


def _cost_text(cost: Any, source: Any) -> str:
    if cost in (None, 0, 0.0) and source == "missing":
        return "—（未上报且无价格表）"
    return f"{cost}（{_source_text(source)}）"


def _source_text(source: Any) -> str:
    return {
        "reported": "模型上报",
        "estimated": "按价格表估算",
        "mixed": "部分上报 + 部分估算",
        "missing": "未知",
    }.get(str(source), "未知")


def build_export(
    overview: dict[str, Any],
    fmt: Any,
    *,
    stamp: str | None = None,
) -> dict[str, Any]:
    """把总览导出成指定格式；格式不支持抛 ``ValueError``。"""
    name = str(fmt or "").strip().lower()
    if name not in EXPORT_FORMATS:
        raise ValueError(f"不支持的导出格式：{fmt!r}（可选 {', '.join(EXPORT_FORMATS)}）")
    if name == "json":
        content = json.dumps(
            _export_payload(overview), ensure_ascii=False, indent=2, sort_keys=False
        )
    elif name == "markdown":
        content = render_markdown(overview)
    else:
        content = render_csv(overview)
    suffix = {"json": "json", "markdown": "md", "csv": "csv"}[name]
    tag = (stamp or overview.get("generated_at") or "").replace("-", "").replace(":", "")
    tag = re.sub(r"[^0-9A-Za-z]", "", tag)[:14] or "now"
    return {
        "format": name,
        "filename": f"usage-board-{tag}.{suffix}",
        "content": content,
        "bytes": len(content.encode("utf-8")),
    }
