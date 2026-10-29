# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""日志敏感信息脱敏（``gateway.common.paths`` 同名实现的 Gateway 副本）。

Gateway 独立安装时必须自带脱敏能力：api_key / token / 个人信息不得明文落盘。
本文件为母仓实现的逐行副本，改动请同步两侧。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from typing import Any

# 统一敏感信息掩码值。
_SENSITIVE_MASK = "******"
_DATA_IMAGE_PATTERN = re.compile(
    r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+"
)
# 匹配常见敏感字段键值对（不要求值必须带引号），用于覆盖:
# - token=abc
# - api_key: sk-xxx
# - authorization = Bearer ...
# 分组说明：
# 1) 敏感键名；2) 分隔符及两侧空白（: 或 =）；3) 可选起始引号；
# 4) 值本体（用于脱敏后附指纹）；5) 可选结束引号。
_KV_SENSITIVE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"auth[_-]?code|auth[_-]?token|"
    r"user[_-]?id|userid|project[_-]?id|"
    r"amap[_-]?key|map[_-]?ak)"
    r"(?![A-Za-z0-9])(\s*[:=]\s*)([\"']?)([^,\s\"'\]\}]+)([\"']?)"
)
# 匹配被引号包裹的通用键值对起点；键是否敏感在 Python 代码中判断。
#
# 旧实现把两个无界 ``[A-Za-z0-9_.-]*``、关键词分支、``.*?`` 与反向
# 引用组合在一个正则中。面对 10KB 连续标识符且最终不匹配时，Python ``re``
# 会从大量位置反复回溯，呈近似 O(n²) 退化。这里用左边界保证每个 key token
# 只尝试一次，并用单向扫描查找结束引号，避免日志输入阻塞事件循环。
# 与 upstream 1236f407 的 lookbehind 单正则修复等价地消除该回溯，并在其
# 之上额外修复未闭合引号场景的明文泄露——勿回退为单正则形态。
_NAMED_QUOTED_KV_START_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"(?P<prefix>[\"']?(?P<key>[A-Za-z0-9_.-]+)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])"
)
_NAMED_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:token|secret|password|passwd|pwd|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|authorization|auth[_-]?code|auth[_-]?token|"
    r"credential|private[_-]?key|user[_-]?id|userid|project[_-]?id|"
    r"amap[_-]?key|map[_-]?ak)"
)
# 匹配 Authorization Bearer 令牌，保留 "Bearer " 前缀，仅掩码后面的令牌值。
# 分组：1) "Bearer " 前缀；2) 令牌值本体（用于算指纹）。
_BEARER_SENSITIVE_PATTERN = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)")
# 匹配命令行 flag 后跟明文凭证值（如 args=['--token', 'xxx', '--api-key', 'yyy']）：
# ``--token VALUE`` / ``--api-key VALUE`` 不是 KV 语法，KV 正则覆盖不到，单列一条。
# 覆盖三种形态：``--token VALUE``（空格）、``--token=VALUE``、``'--token', 'VALUE'``
# （pydantic repr 把 list 序列化成引号逗号分隔的元素）。
# 分组：1) flag 名 + 分隔（空白/= 或引号逗号引号）；2) 凭证值本体（用于算指纹）。
_CLI_FLAG_SENSITIVE_PATTERN = re.compile(
    r"(?i)(--(?:[a-z]+[_-])*(?:token|secret|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?key|secret[_-]?key|auth[_-]?code|auth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|apikey|authorization)"
    r"(?:[a-z0-9_-]*)(?:\s*=|',\s*'|\s+))([A-Za-z0-9\-._~+/]+=*)"
)
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    # 匹配 JWT（header.payload.signature 三段式，常见以 eyJ 开头）。
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    # 匹配 OpenAI 风格 key（sk- 前缀）。
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    # 匹配 GitHub Personal Access Token（ghp_ 前缀）。
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    # 匹配 GitLab Personal Access Token（glpat- 前缀）。
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    # 匹配邮箱地址（避免日志中泄露个人身份信息）。
    # A word boundary retries after every '+'/'%' in a long non-email run.
    # Start only at the beginning of a complete local-part token.
    re.compile(r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b"),
    # 匹配中国大陆手机号（可带 +86 或 86 前缀，支持空格/短横线分隔）。
    re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"),
    # 匹配中国身份证号（18 位，最后一位可为 X/x）。
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
]
# PII / 非凭证类 pattern：掩码但不附指纹（关联意义不大，且避免引入额外可逆性顾虑）。
_SENSITIVE_PII_PATTERNS: tuple[re.Pattern[str], ...] = tuple(_SENSITIVE_PATTERNS[-3:])
# 凭证类 prefix pattern：掩码并附指纹（同 key 指纹一致可关联、不可逆）。
_SENSITIVE_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(_SENSITIVE_PATTERNS[:4])
_SAFE_AUTHORIZATION_OUTCOME_PATTERN = re.compile(
    r'"authorization_outcome":"(?:allow|deny|block|cancel)"'
)


def _fingerprint(value: str) -> str:
    """返回 value 的 SHA256 前 4 字节（8 位 hex）指纹，用于脱敏后的关联。

    不可逆：拿到 ``fp:7f3a2c19`` 无法还原原值。同一 key 每次指纹一致，
    可在日志中把同一账号/会话的多次请求串起来排查；key 轮换后指纹自然变化。
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]


# 已脱敏产物形态：纯 ****** 或 ******(fp:xxxxxxxx)。
# 用于在二次脱敏时识别"已是脱敏值"，跳过重算指纹，避免产生"指纹的指纹"
# 导致跨日志关联失效（如 stream_logger._mask_secrets 先脱敏，_write_raw 再脱敏）。
_ALREADY_MASKED_PATTERN = re.compile(rf"^{re.escape(_SENSITIVE_MASK)}(\(fp:[0-9a-f]{{8}}\))?$")


def _is_already_masked(value: Any) -> bool:
    """判断 value 是否已是脱敏产物（纯掩码或带指纹），避免重复脱敏。"""
    try:
        v = str(value) if value is not None else ""
    except Exception:
        return False
    return bool(v) and bool(_ALREADY_MASKED_PATTERN.match(v))


def _masked_with_fp(value: Any) -> str:
    """脱敏并附指纹：``******(fp:xxxxxxxx)``。value 为空或失败时退化为纯掩码。

    若 value 本身已是脱敏产物（``******`` 或 ``******(fp:..)``），原样返回，
    不重算指纹——避免对"指纹值"再算指纹导致跨日志关联失效。
    """
    try:
        v = str(value) if value is not None else ""
    except Exception:
        return _SENSITIVE_MASK
    if _is_already_masked(v):
        return v
    fp = _fingerprint(v)
    if not fp:
        return _SENSITIVE_MASK
    return f"{_SENSITIVE_MASK}(fp:{fp})"


def _mask_named_sensitive_kv(text: str) -> str:
    """Mask quoted values whose key contains a sensitive keyword.

    The scan advances monotonically.  In particular, a long identifier that
    is not a quoted key/value pair is inspected once instead of being retried
    from every character position by a backtracking regular expression.
    The closing-quote lookup never crosses a newline: an unclosed quote masks
    only to the end of the current line, and scanning resumes on the next
    line so its sensitive pairs stay matchable and unrelated lines stay
    readable.
    """
    chunks: list[str] = []
    copy_from = 0
    search_from = 0

    while True:
        match = _NAMED_QUOTED_KV_START_PATTERN.search(text, search_from)
        if match is None:
            break
        search_from = match.end()
        if _NAMED_SENSITIVE_KEY_PATTERN.search(match.group("key")) is None:
            continue

        quote = match.group("quote")
        value_start = match.end()
        line_end = text.find("\n", value_start)
        if line_end < 0:
            line_end = len(text)
        value_end = text.find(quote, value_start, line_end)
        if value_end < 0:
            # A malformed quoted secret must not leak.  Mask the rest of the
            # current line and resume after the newline instead of swallowing
            # later lines: their quotes must remain available as value
            # boundaries for their own sensitive keys.
            chunks.append(text[copy_from:value_start])
            chunks.append(_masked_with_fp(text[value_start:line_end]))
            copy_from = line_end
            search_from = line_end
            continue

        chunks.append(text[copy_from:value_start])
        chunks.append(_masked_with_fp(text[value_start:value_end]))
        chunks.append(quote)
        copy_from = value_end + 1
        search_from = copy_from

    if not chunks:
        return text
    chunks.append(text[copy_from:])
    return "".join(chunks)


def _sanitize_log_text(text: str) -> str:
    """Redact JSON by value, preserving numbers and valid serialization.

    Metrics can contain 18 decimal digits; applying an identity-number regex to
    serialized JSON corrupts them. Plain text retains the existing redaction.
    """
    decoder = json.JSONDecoder()
    start_pattern = re.compile(r'[\{\[]')
    chunks: list[str] = []
    copy_from = 0
    search_from = 0
    candidates = 0
    # Limit each unconsumed segment to 32 candidates, resetting after success.
    # Scan the original text iteratively so shallow fragments do not grow the
    # call stack or repeatedly scan and copy the remaining suffix.
    while candidates < 32:
        match = start_pattern.search(text, search_from)
        if match is None:
            break
        start = match.start()
        search_from = start + 1
        candidates += 1
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        if not isinstance(value, (dict, list)):
            continue
        chunks.append(_sanitize_plain_log_text(text[copy_from:start]))
        chunks.append(
            json.dumps(_sanitize_log_value(value), ensure_ascii=False, separators=(",", ":"))
        )
        copy_from = end
        search_from = end
        candidates = 0
    chunks.append(_sanitize_plain_log_text(text[copy_from:]))
    return "".join(chunks)


_SAFE_AUTHORIZATION_OUTCOMES = ("allow", "deny", "block", "cancel")
_TOKEN_COUNT_KEYS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "tokens_used"}
)


def _sanitize_log_value(value: Any, *, mask_values: bool = False) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            safe_outcome = key == "authorization_outcome" and item in _SAFE_AUTHORIZATION_OUTCOMES
            # Reuse the existing key classifier without letting quotes/escapes in
            # actual values change the matching boundaries.
            sensitive = mask_values or _NAMED_SENSITIVE_KEY_PATTERN.search(str(key)) is not None
            if key in _TOKEN_COUNT_KEYS and _is_plain_number(item):
                sensitive = False
            result[_sanitize_plain_log_text(str(key))] = _sanitize_log_value(
                item, mask_values=sensitive and not safe_outcome
            )
        return result
    if isinstance(value, list):
        sanitized = []
        for index, item in enumerate(value):
            previous = value[index - 1] if index else None
            flag_value = isinstance(previous, str) and _CLI_FLAG_SENSITIVE_PATTERN.fullmatch(
                previous + " value"
            )
            sanitized.append(_sanitize_log_value(item, mask_values=mask_values or bool(flag_value)))
        return sanitized
    if mask_values:
        return _masked_with_fp(value)
    if isinstance(value, str):
        return _sanitize_log_text(value)
    if _is_plain_int(value) and any(
        pattern.fullmatch(str(value)) for pattern in _SENSITIVE_PII_PATTERNS
    ):
        return _SENSITIVE_MASK
    return value


def _is_plain_int(value: Any) -> bool:
    # bool is an int subclass; True/False are never token counts or identity numbers.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_plain_number(value: Any) -> bool:
    return _is_plain_int(value) or isinstance(value, float)


def _sanitize_plain_log_text(text: str) -> str:
    if not text:
        return text

    protected = text
    replacements: list[tuple[str, str]] = []
    for index, match in enumerate(
        tuple(_SAFE_AUTHORIZATION_OUTCOME_PATTERN.finditer(text))
    ):
        marker = f"__JIUWEN_SAFE_OUTCOME_{index}__"
        while marker in text:
            marker += "_"
        protected = protected.replace(match.group(0), marker, 1)
        replacements.append((marker, match.group(0)))

    masked = protected
    masked = _DATA_IMAGE_PATTERN.sub("data:image/*;base64,******", masked)
    # _KV_SENSITIVE_PATTERN: 组1=键名, 组2=分隔符, 组4=值（组3/5 为可选引号）。
    masked = _KV_SENSITIVE_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_masked_with_fp(m.group(4))}", masked
    )
    masked = _mask_named_sensitive_kv(masked)
    # _BEARER_SENSITIVE_PATTERN: 组1=Bearer 前缀, 组2=令牌值。
    masked = _BEARER_SENSITIVE_PATTERN.sub(
        lambda m: f"{m.group(1)}{_masked_with_fp(m.group(2))}", masked
    )
    # _CLI_FLAG_SENSITIVE_PATTERN: 组1=flag 名+分隔, 组2=凭证值。
    masked = _CLI_FLAG_SENSITIVE_PATTERN.sub(
        lambda m: f"{m.group(1)}{_masked_with_fp(m.group(2))}", masked
    )
    # 凭证类 prefix key（JWT/sk-/ghp_/glpat-）：掩码并附指纹。
    for pattern in _SENSITIVE_CREDENTIAL_PATTERNS:
        masked = pattern.sub(lambda m, _p=pattern: _masked_with_fp(m.group(0)), masked)
    # PII（邮箱/手机/身份证）：纯掩码，不附指纹。
    for pattern in _SENSITIVE_PII_PATTERNS:
        masked = pattern.sub(_SENSITIVE_MASK, masked)
    for marker, original in replacements:
        masked = masked.replace(marker, original)
    return masked


def mask_sensitive(text: Any) -> str:
    """对任意文本做敏感信息脱敏，返回脱敏后的字符串。

    作为对外稳定接口：调用方（如 ``agent_ws_server`` 打印模型配置/环境变量）
    应统一走本函数，避免各自硬编码键名匹配（例如只命中 ``API_KEY``、漏掉
    ``OPENAI_API_KEY`` / ``VISION_API_KEY`` 等带前缀变体）造成明文泄露。
    """
    if text is None:
        return ""
    return _sanitize_log_text(str(text))


class SensitiveDataFilter(logging.Filter):
    """Mask sensitive data in all log messages and tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "jiuwen_sensitive_sanitized", False):
            return True

        sanitized = True
        try:
            message = record.getMessage()
            record.msg = _sanitize_record_text(message)
            record.args = ()
        except Exception:
            # Never block logging because of desensitization failure.
            sanitized = False

        # Traceback 由 Formatter.formatException() 在 record.exc_text 中单独渲染，
        # 不经过 record.getMessage()，因此 message 脱敏覆盖不到。这里提前把
        # traceback 文本脱敏写入 record.exc_text 并清空 record.exc_info，
        # 使 logger.exception()/exc_info=True 的异常栈也不会泄露 api_key 等。
        try:
            exc_info = record.exc_info
            if exc_info and not record.exc_text:
                import traceback as _traceback

                # exc_info 是 (type, value, tb) 三元组。Python 3.10+ 的
                # traceback.format_exception 新签名只接受单个异常实例：
                # format_exception(exc, /, limit=None, chain=True)。
                # 旧的 format_exception(*exc_info)（拆包成 3 个位置参数）依赖
                # 兼容层，未来版本可能移除；改用 exc_info[1]（异常实例）是
                # 官方推荐写法，面向未来且行为等价（None 时输出 "NoneType: None"）。
                formatted = "".join(_traceback.format_exception(exc_info[1]))
                record.exc_text = _sanitize_record_text(formatted)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = _sanitize_record_text(record.exc_text)
        except Exception:
            # 同样不因脱敏失败而阻断日志输出。
            sanitized = False
        if sanitized:
            record.jiuwen_sensitive_sanitized = True
        return True


class JsonOnlyFormatter(logging.Formatter):
    """只输出message内容，不添加任何前缀（时间戳、级别、logger名）"""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


# 源头脱敏是否已安装（全局，避免重复设置 LogRecordFactory）。
_source_record_masking_installed = False
# 源头脱敏失败计数（脱敏异常时递增；运维可通过此值监控脱敏失效，避免静默泄露）。
_source_masking_failures = 0


def install_source_record_masking() -> None:
    """在 LogRecord 创建层（``logging.setLogRecordFactory``）安装源头脱敏。

    这是比 handler 上挂 ``SensitiveDataFilter`` 更彻底的兜底层：无论哪个 logger
    发出的 record——包括 **jiuwenswarm 命名空间之外**的第三方库（openjiuwen /
    openai / httpx / urllib3 等，它们自带 handler、不 propagate 到 jiuwenswarm
    根 logger），在 LogRecord **创建瞬间**就被脱敏 message 与 traceback，
    保证任何来源的 api_key 都不明文落盘。

    覆盖场景：
    - ``app_agentserver.py`` 用 ``logging.getLogger("openjiuwen.harness.security")``
      等 openjiuwen 命名空间 logger；
    - clawee（yuanrong faas）拉起的 AgentServer 内 openjiuwen SDK 自有 logger；
    - httpx/openai SDK 在 DEBUG 级别打印请求头（含 Authorization）。

    复用带指纹的 ``_sanitize_log_text``，与 handler 层 ``SensitiveDataFilter``
    共存为双保险：若 record 已在源头脱敏，handler 层的 ``_is_already_masked``
    会跳过重算，不破坏指纹。幂等，重复调用安全。
    """
    global _source_record_masking_installed
    if _source_record_masking_installed:
        return

    old_factory = logging.getLogRecordFactory()

    def _sanitizing_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        try:
            # message 脱敏（含 %s/format 格式化后的最终文本）。
            msg = record.getMessage()
            record.msg = _sanitize_record_text(msg)
            record.args = ()
            # traceback 脱敏：traceback 由 Formatter.formatException 从
            # record.exc_text 单独渲染，getMessage 覆盖不到。此处提前渲染并脱敏，
            # 清空 exc_info 使 Formatter 复用已脱敏的 exc_text。
            exc_info = record.exc_info
            if exc_info and not record.exc_text:
                import traceback as _tb

                formatted = "".join(_tb.format_exception(exc_info[1]))
                record.exc_text = _sanitize_record_text(formatted)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = _sanitize_record_text(record.exc_text)
            record.jiuwen_sensitive_sanitized = True
        except Exception:
            # 永不因脱敏失败而阻断日志输出。但记录失败（计数 + 首次 stderr 提示），
            # 避免静默吞掉异常导致 api_key 在无感知下明文泄露。
            global _source_masking_failures
            _source_masking_failures += 1
            if _source_masking_failures == 1:
                # 仅首次打 stderr（不用 logging，避免自循环），提示运维脱敏失效。
                # 后续仅靠计数器累积，避免高频失败刷屏。
                print(
                    "[jiuwenswarm] source record masking failed — secrets may be "
                    "exposed in logs; check _source_masking_failures counter",
                    file=sys.stderr,
                )
        return record

    logging.setLogRecordFactory(_sanitizing_record_factory)
    _source_record_masking_installed = True


def _sanitize_record_text(text: str) -> str:
    """Bound regex/JSON work per log record without truncating a credential.

    Oversized payloads belong in session history rather than operational logs.
    Omitting the whole payload also avoids breaking serialized JSON or exposing
    a partial credential at a truncation boundary. Public mask_sensitive keeps
    its full-text behavior.
    """
    if len(text) > 64 * 1024:
        if text.lstrip().startswith(("{", "[")):
            return json.dumps({
                "log_payload_omitted": True,
                "original_characters": len(text),
                "character_limit": 64 * 1024,
            }, separators=(",", ":"))
        return f"[log payload omitted: {len(text)} characters exceeds 65536 character limit]"
    return _sanitize_log_text(text)
