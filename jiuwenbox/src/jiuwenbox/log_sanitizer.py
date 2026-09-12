# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lightweight log sanitizer for jiuwenbox.

This module is intentionally independent of ``jiuwenswarm.common.utils`` to
avoid circular imports and ``setup_logger()`` side-effects at import time
(``inference_privacy_proxy`` calls ``configure_logging()`` during import).

The sanitization logic mirrors ``jiuwenswarm.common.utils`` (same regexes,
fingerprint, PII/credential split, ``_is_already_masked`` repeat-skip). Any
change here must be mirrored back to ``jiuwenswarm/common/utils.py`` and vice
versa — the two are maintained in lockstep.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

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
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|user[_-]?id|userid)"
    r"(?![A-Za-z0-9])(\s*[:=]\s*)([\"']?)([^,\s\"'\]\}]+)([\"']?)"
)
# 匹配“键名包含敏感关键词”且“值被引号包裹”的场景，覆盖:
# - 'CAT_CAFE_CALLBACK_TOKEN': 'xxxx'
# - 'CAT_CAFE_USER_ID': 'CSDN-weixin'
# - "my_private_key"="xxxx"
# 分组说明：
# 1) 完整的 key + 分隔符（含可选引号）
# 2) 值的起始引号（' 或 "）
# 3) 值内容（非贪婪）
# 4) 结束引号（通过 (\2) 强制与起始引号一致）
_NAMED_SENSITIVE_KV_PATTERN = re.compile(
    r"(?i)([\"']?[A-Za-z0-9_.-]*"
    r"(?:token|secret|password|passwd|pwd|api[_-]?key|authorization|"
    r"credential|private[_-]?key|user[_-]?id|userid)"
    r"[A-Za-z0-9_.-]*[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)"
)
# 匹配 Authorization Bearer 令牌，保留 "Bearer " 前缀，仅掩码后面的令牌值。
# 分组：1) "Bearer " 前缀；2) 令牌值本体（用于算指纹）。
_BEARER_SENSITIVE_PATTERN = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)")
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
    re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b"),
    # 匹配中国大陆手机号（可带 +86 或 86 前缀，支持空格/短横线分隔）。
    re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"),
    # 匹配中国身份证号（18 位，最后一位可为 X/x）。
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
]
# PII / 非凭证类 pattern：掩码但不附指纹（关联意义不大，且避免引入额外可逆性顾虑）。
_SENSITIVE_PII_PATTERNS: tuple[re.Pattern[str], ...] = tuple(_SENSITIVE_PATTERNS[-3:])
# 凭证类 prefix pattern：掩码并附指纹（同 key 指纹一致可关联、不可逆）。
_SENSITIVE_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    _SENSITIVE_PATTERNS[:4]
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
# 导致跨日志关联失效。
_ALREADY_MASKED_PATTERN = re.compile(
    rf"^{re.escape(_SENSITIVE_MASK)}(\(fp:[0-9a-f]{{8}}\))?$"
)


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


def _sanitize_log_text(text: str) -> str:
    if not text:
        return text

    masked = text
    masked = _DATA_IMAGE_PATTERN.sub("data:image/*;base64,******", masked)
    # Apply specific patterns before the generic key-value matcher so the
    # latter cannot consume only the first word of a structured value.
    # _BEARER_SENSITIVE_PATTERN: 组1=Bearer 前缀, 组2=令牌值。
    masked = _BEARER_SENSITIVE_PATTERN.sub(
        lambda m: f"{m.group(1)}{_masked_with_fp(m.group(2))}", masked
    )
    # _NAMED_SENSITIVE_KV_PATTERN: 组1=键+分隔符, 组2=起始引号, 组3=值, 组4=结束引号。
    masked = _NAMED_SENSITIVE_KV_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_masked_with_fp(m.group(3))}{m.group(4)}",
        masked,
    )
    # _KV_SENSITIVE_PATTERN: 组1=键名, 组2=分隔符, 组4=值（组3/5 为可选引号）。
    masked = _KV_SENSITIVE_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_masked_with_fp(m.group(4))}", masked
    )
    # 凭证类 prefix key（JWT/sk-/ghp_/glpat-）：掩码并附指纹。
    for pattern in _SENSITIVE_CREDENTIAL_PATTERNS:
        masked = pattern.sub(
            lambda m, _p=pattern: _masked_with_fp(m.group(0)), masked
        )
    # PII（邮箱/手机/身份证）：纯掩码，不附指纹。
    for pattern in _SENSITIVE_PII_PATTERNS:
        masked = pattern.sub(_SENSITIVE_MASK, masked)
    return masked


def sanitize_text(text: Any) -> str:
    """Replace sensitive patterns in *text* with a masked value.

    Public entry point for jiuwenbox modules. Mirrors
    ``jiuwenswarm.common.utils.mask_sensitive``.
    """
    if text is None:
        return ""
    return _sanitize_log_text(str(text))
