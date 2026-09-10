# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""日志脱敏引擎：内置规则、按 priority DESC 顺序应用已编译规则。"""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, ClassVar

from jiuwenswarm.infrastructure.config import settings
from jiuwenswarm.infrastructure.utils import is_already_masked, masked_with_fp

_logger = logging.getLogger(__name__)

DEFAULT_REPLACEMENT = "******"
MAX_PATTERN_LENGTH = 512
MAX_REPLACEMENT_LENGTH = 64
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ALLOWED_SOURCES = frozenset({"builtin", "custom"})
_MAX_SANITIZE_TEXT_LEN = 256 * 1024
_LOG_MASKING_RULE_TABLE = "log_masking_rule"
_PATTERN_PERF_SAMPLE_LIMIT_SEC = 0.05

_SENSITIVE_KW = (
    # token/credential 用 (?![a-z0-9]) 避免误伤 tokens_used / credentialing 等。
    r"password|passwd|pwd|secret|token(?![a-z0-9])|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|credentials?|private[_-]?key|user[_-]?id"
)


def _expand_sensitive_kw_literals(kw_alternation: str) -> tuple[str, ...]:
    """从 ``_SENSITIVE_KW`` alternation 展开预检用字面量（含 ``[_-]?`` 三种形式）。"""
    literals: list[str] = []
    for alt in kw_alternation.split("|"):
        alt = alt.strip()
        if not alt:
            continue
        # 预检只关心子串是否出现，去掉零宽断言。
        alt = re.sub(r"\(\?![^)]*\)", "", alt)
        alt = re.sub(r"\(\?<![^)]*\)", "", alt)
        if "[_-]?" not in alt:
            lit = alt.lower()
            if lit and lit not in literals:
                literals.append(lit)
            continue
        left, right = alt.split("[_-]?", 1)
        for sep in ("", "_", "-"):
            lit = f"{left.lower()}{sep}{right.lower()}"
            if lit not in literals:
                literals.append(lit)
    return tuple(literals)


_SENSITIVE_KW_LITERALS = _expand_sensitive_kw_literals(_SENSITIVE_KW)

_KV_SENSITIVE_PATTERN = re.compile(
    rf'(?i)(?P<prefix>["\']?[\w.-]{{0,128}}(?:{_SENSITIVE_KW})[\w.-]{{0,128}}["\']?\s*[:=]\s*)'
    # 值：引号串，或非 JSON 结构起始的标量（避免把 `"credentials": {` 整段吃掉）。
    r'(?P<val>"[^"]*"|\'[^\']*\'|[^,\s"\'\}\]\{\[]+)'
)
_KV_SENSITIVE_REPLACEMENT = rf"\g<prefix>{DEFAULT_REPLACEMENT}"


def _fingerprint_match(match: re.Match[str]) -> str:
    """``with_fingerprint=True`` 时的统一替换。

    - 有命名组 ``prefix``+``val``（敏感 KV）：保留键，对值附指纹；跳过 JSON 结构值
    - 至少两组捕获：保留第 1 组，对第 2 组附指纹
    - 否则：整段命中附指纹
    """
    groups = match.groupdict()
    if "prefix" in groups and "val" in groups:
        prefix = groups["prefix"]
        val = groups["val"] or ""
        if val[:1] in "{[":
            return match.group(0)
        raw = (
            val[1:-1]
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\""
            else val
        )
        if is_already_masked(raw):
            return match.group(0)
        return f"{prefix}{masked_with_fp(raw)}"
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}{masked_with_fp(match.group(2))}"
    return masked_with_fp(match.group(0))


_PATTERN_PERF_PROBE_SAMPLES: tuple[str, ...] = (
    ("x" * 19) + "z",
    ("a" * 19) + "!",
    "abc sample line with token=value",
    ("b" * 28) + "!",
    ('x="' * 30) + "password=",
)

_UNSAFE_WILDCARD_QUANTIFIER_RE = re.compile(
    r"\(\.\*\)\*|\(\.\+\)\+|\(\.\*\)\+|\(\.\+\)\*"
)


@dataclass(frozen=True)
class CompiledMaskingRule:
    rule_id: str
    pattern: re.Pattern[str]
    replacement: str
    name: str = ""
    priority: int = 0
    # True：命中后走指纹替换；False：使用 replacement 静态串（可含 \\g 回引）。
    with_fingerprint: bool = False


# priority 越大越先执行。顺序：大体积 data-uri → PII（无指纹）→ 敏感 KV（有指纹）。
_BUILTIN_RULES: list[CompiledMaskingRule] = [
    CompiledMaskingRule(
        "builtin_data_image",
        re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+"),
        "data:image/*;base64,******",
        name="DataURI图片",
        priority=50,
    ),
    # --- PII（邮箱 / 手机 / 身份证）：纯掩码，不附指纹 ---
    CompiledMaskingRule(
        "builtin_email",
        re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b"),
        DEFAULT_REPLACEMENT,
        name="邮箱",
        priority=40,
    ),
    CompiledMaskingRule(
        "builtin_cn_mobile",
        re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"),
        DEFAULT_REPLACEMENT,
        name="手机号",
        priority=30,
    ),
    CompiledMaskingRule(
        "builtin_cn_id_card",
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        DEFAULT_REPLACEMENT,
        name="身份证号",
        priority=20,
    ),
    CompiledMaskingRule(
        "builtin_kv_sensitive",
        _KV_SENSITIVE_PATTERN,
        _KV_SENSITIVE_REPLACEMENT,
        name="敏感KV",
        priority=10,
        with_fingerprint=True,
    ),
]


def normalize_rule_id(rule_id: str) -> str:
    normalized = str(rule_id or "").strip()
    if not normalized or not _RULE_ID_RE.fullmatch(normalized):
        raise ValueError(
            "rule_id must be 1-64 chars of [A-Za-z0-9_.-]"
        )
    return normalized


def normalize_replacement(value: str | None) -> str:
    text = str(value or "").strip() or DEFAULT_REPLACEMENT
    if "\n" in text or "\r" in text:
        raise ValueError("replacement must not contain newlines")
    if len(text) > MAX_REPLACEMENT_LENGTH:
        raise ValueError(f"replacement must be at most {MAX_REPLACEMENT_LENGTH} chars")
    return text


def validate_pattern_structure(pattern: str) -> None:
    """静态拒绝明显 ReDoS 结构（如 ``(.*)*``）。"""
    if _UNSAFE_WILDCARD_QUANTIFIER_RE.search(pattern):
        raise ValueError(
            "pattern contains unsafe nested wildcard quantifiers like (.*)*"
        )


def validate_pattern_performance(
    pattern: re.Pattern[str],
    *,
    limit_sec: float = _PATTERN_PERF_SAMPLE_LIMIT_SEC,
) -> None:
    """拒绝在探测样例上过慢的自定义 pattern（防 ReDoS）。"""
    for sample in _PATTERN_PERF_PROBE_SAMPLES:
        t0 = time.perf_counter()
        pattern.sub("***", sample)
        elapsed = time.perf_counter() - t0
        if elapsed > limit_sec:
            raise ValueError(
                "pattern too slow "
                f"(>{limit_sec * 1000:.0f}ms on probe sample len={len(sample)})"
            )


def validate_pattern(
    pattern: str,
    *,
    check_structure: bool = True,
    check_performance: bool = True,
) -> str:
    text = str(pattern or "").strip()
    if not text:
        raise ValueError("pattern is required")
    if len(text) > MAX_PATTERN_LENGTH:
        raise ValueError(f"pattern must be at most {MAX_PATTERN_LENGTH} chars")
    try:
        compiled = re.compile(text)
    except re.error as exc:
        raise ValueError(f"invalid regex pattern: {exc}") from exc
    if check_structure:
        validate_pattern_structure(text)
    if check_performance:
        validate_pattern_performance(compiled)
    return text


def normalize_source(source: str) -> str:
    normalized = str(source or "custom").strip().lower()
    if normalized not in _ALLOWED_SOURCES:
        raise ValueError(f"source must be one of {sorted(_ALLOWED_SOURCES)}")
    return normalized


class LogMaskingEngine:
    """进程内单例脱敏引擎（仅 ``get_instance`` 创建/持有单例）。"""

    _instance: ClassVar[LogMaskingEngine | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _db_authoritative: ClassVar[bool] = False

    @staticmethod
    def compiled_default_rules() -> list[CompiledMaskingRule]:
        """返回内置规则列表（priority 越大越先执行）。"""
        return list(_BUILTIN_RULES)

    def __init__(self, rules: list[CompiledMaskingRule] | None = None) -> None:
        if rules is None:
            rules = self.compiled_default_rules()
        self._rules: list[CompiledMaskingRule] = list(rules)

    @classmethod
    def get_instance(cls) -> LogMaskingEngine:
        """返回单例；初始均使用内置规则，企业版在 GDB/WS 同步后以库为准。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """单测用：重置单例。"""
        with cls._lock:
            cls._instance = None
            cls._db_authoritative = False

    @property
    def rules(self) -> list[CompiledMaskingRule]:
        return list(self._rules)

    @property
    def uses_external_rules(self) -> bool:
        """是否已以 Gateway DB 为权威规则来源（企业版热更路径）。"""
        return bool(type(self)._db_authoritative)

    def set_rules(self, rules: list[CompiledMaskingRule]) -> None:
        self._rules = list(rules)

    @classmethod
    def reload_from_rows(
        cls,
        rows: list[dict[str, Any]] | None,
        *,
        db_authoritative: bool = False,
    ) -> None:
        """从 DB/WS 行刷新单例规则。

        - 单机版：无行或编译失败时回退内置规则。
        - 企业版（``db_authoritative=True`` 或已有可编译规则）：完全以库为准，空库即空规则。
        """
        enterprise = is_enterprise()

        if db_authoritative:
            cls._db_authoritative = True

        compiled = cls.compile_masking_rows(rows)

        if not enterprise:
            new_rules = list(compiled or cls.compiled_default_rules())
        elif cls._db_authoritative or compiled:
            if compiled:
                cls._db_authoritative = True
            new_rules = list(compiled)
        else:
            new_rules = list(cls.compiled_default_rules())

        cls.get_instance().set_rules(new_rules)

    @classmethod
    async def reload_log_masking_rule(
        cls,
        *,
        db_authoritative: bool | None = None,
    ) -> None:
        """从 Gateway 库加载 enabled 规则并刷新**本进程**单例引擎。

        单机版（``JIUWENSWARM_EDITION`` 非企业版）不访问 GDB，直接使用内置规则。
        企业版直接读本网关 DB（每网关独立库，不依赖实例 id 绑定）。
        读库走本地 ``enterprise_config.db_queries``，不依赖 ``packages/jiuwenclaw-ee``。

        ``db_authoritative``：
        - ``None``（默认）：GDB 有行时以库为准，空库保留内置；
        - ``True``：强制以库为准（WS sync / 用户改规则后），空库即空规则；
        - ``False``：仅刷新编译结果，不改变权威来源标记。
        """
        enterprise = is_enterprise()
        if not enterprise:
            cls.reload_from_rows([])
        else:
            try:
                rows = await cls.list_enabled_log_masking_rule_rows()
                authoritative = (
                    db_authoritative
                    if db_authoritative is not None
                    else bool(rows)
                )
                cls.reload_from_rows(rows, db_authoritative=authoritative)
                engine = cls.get_instance()
                _logger.info(
                    "[log_masking_db] reloaded: rows=%d rules=%d authoritative=%s",
                    len(rows),
                    len(engine.rules),
                    engine.uses_external_rules,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "[log_masking_db] log_masking_rule read failed: %s",
                    exc,
                    exc_info=True,
                )

        from .probes import maybe_emit_log_masking_probe_samples

        maybe_emit_log_masking_probe_samples(_logger)

    # 兼容旧调用名
    reload_log_masking_from_gateway_db = reload_log_masking_rule

    @classmethod
    async def list_enabled_log_masking_rule_rows(
        cls,
        *,
        table_name: str = _LOG_MASKING_RULE_TABLE,
    ) -> list[dict[str, Any]]:
        """返回 ``enabled=true`` 的规则行（priority DESC）。"""
        from jiuwenswarm.server.runtime.enterprise_config import db_queries

        rows = await db_queries.list_records(
            table_name,
            filters={"enabled": True},
            order_by="priority DESC",
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            record = cls._masking_rule_row_to_dict(row)
            if record.get("enabled"):
                result.append(record)
        result.sort(
            key=lambda r: (-int(r.get("priority") or 0), int(r.get("id") or 0)),
        )
        return result

    @staticmethod
    def _masking_rule_row_to_dict(obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            created_at = obj.get("created_at")
            updated_at = obj.get("updated_at")
            enabled = obj.get("enabled", True)
            if isinstance(enabled, int):
                enabled = bool(enabled)
            return {
                "id": obj.get("id"),
                "rule_id": obj.get("rule_id"),
                "rule_name": obj.get("rule_name"),
                "description": obj.get("description"),
                "pattern": obj.get("pattern"),
                "replacement": obj.get("replacement"),
                "priority": obj.get("priority", 0),
                "with_fingerprint": bool(obj.get("with_fingerprint", False)),
                "source": obj.get("source"),
                "enabled": bool(enabled),
                "data": obj.get("data"),
                "created_at": str(created_at) if created_at is not None else None,
                "updated_at": str(updated_at) if updated_at is not None else None,
            }
        created_at = getattr(obj, "created_at", None)
        updated_at = getattr(obj, "updated_at", None)
        return {
            "id": getattr(obj, "id", None),
            "rule_id": getattr(obj, "rule_id", None),
            "rule_name": getattr(obj, "rule_name", None),
            "description": getattr(obj, "description", None),
            "pattern": getattr(obj, "pattern", None),
            "replacement": getattr(obj, "replacement", None),
            "priority": getattr(obj, "priority", 0),
            "with_fingerprint": bool(getattr(obj, "with_fingerprint", False)),
            "source": getattr(obj, "source", None),
            "enabled": bool(getattr(obj, "enabled", True)),
            "data": getattr(obj, "data", None),
            "created_at": str(created_at) if created_at is not None else None,
            "updated_at": str(updated_at) if updated_at is not None else None,
        }

    @staticmethod
    def compile_masking_rows(
        rows: list[dict[str, Any]] | None,
        *,
        order_desc: bool = True,
    ) -> list[CompiledMaskingRule]:
        """将 DB/WS 行编译为规则列表；编译失败的行跳过。

        ``rows`` 为 ``None`` 或空列表时直接返回 ``[]``。
        """
        normalized_rows = list(rows or [])
        if not normalized_rows:
            return []

        sorted_rows = sorted(
            normalized_rows,
            key=lambda r: (
                -int(r.get("priority") or 0)
                if order_desc
                else int(r.get("priority") or 0),
                int(r.get("id") or 0),
            ),
        )
        compiled: list[CompiledMaskingRule] = []
        for row in sorted_rows:
            if not bool(row.get("enabled", True)):
                continue
            rule_id = str(row.get("rule_id") or "").strip()
            pattern_text = str(row.get("pattern") or "").strip()
            if not rule_id or not pattern_text:
                continue
            try:
                compiled.append(
                    CompiledMaskingRule(
                        rule_id=rule_id,
                        pattern=re.compile(
                            validate_pattern(
                                pattern_text,
                                check_structure=False,
                                check_performance=False,
                            )
                        ),
                        replacement=normalize_replacement(row.get("replacement")),
                        name=str(row.get("rule_name") or "").strip(),
                        priority=int(row.get("priority") or 0),
                        with_fingerprint=bool(row.get("with_fingerprint", False)),
                    )
                )
            except ValueError as exc:
                _logger.error(
                    "[LogMasking] skip rule %s: %s", rule_id or "?", exc
                )
            except re.error as exc:
                _logger.error(
                    "[LogMasking] skip rule %s compile failed: %s", rule_id, exc
                )
        return compiled

    def sanitize(self, text: str) -> str:
        if not text:
            return text
        if len(text) > _MAX_SANITIZE_TEXT_LEN:
            text = text[:_MAX_SANITIZE_TEXT_LEN]
        masked = text
        for rule in self._rules:
            try:
                if (
                    rule.rule_id == "builtin_kv_sensitive"
                    and not any(kw in masked.lower() for kw in _SENSITIVE_KW_LITERALS)
                ):
                    continue
                if rule.with_fingerprint:
                    masked = rule.pattern.sub(_fingerprint_match, masked)
                else:
                    masked = rule.pattern.sub(rule.replacement, masked)
            except Exception:
                _logger.debug(
                    "[LogMasking] rule %s sub failed", rule.rule_id, exc_info=True
                )
        return masked
