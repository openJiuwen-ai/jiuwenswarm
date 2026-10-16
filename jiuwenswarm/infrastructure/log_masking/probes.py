# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""日志脱敏验证用固定样例（经 SensitiveDataFilter 写入日志文件）。"""

from __future__ import annotations

import logging
import os
from typing import Final

# 与《日志脱敏规则下发.md》§10.4 一致，便于 REST 热更新后对照日志
LOG_MASKING_PROBE_SAMPLES: Final[tuple[tuple[str, str], ...]] = (
    ("S1", "password=mySecret&user=alice"),
    ("S2", "contact: user@example.com phone: 13800138000"),
    ("S3", "order ORD-1234567890 shipped"),
    # KV：无引号键值
    ("S4", "api_key=sk-plain-no-quotes"),
    # KV：键名含 token，单引号值
    ("S5", "LONG_TOKEN: 'secret-in-single-quotes'"),
    # KV：单引号键与值（值内含空格）
    ("S6", "'CAT_TOKEN': 'a-b c'"),
    # KV：JSON 片段，双引号键值
    ("S7", '{"api_key": "sk-abc", "note": "ok"}'),
    # KV：双引号值
    ("S8", 'refresh_token: "eyJhbGciOiJIUzI1NiJ9.payload.sig"'),
    # PII：18 位身份证号（末位可为 X）
    ("S9", "user id_card=110101199003078431 verified"),
    # cat_cafe-mcp env JSON（须为 "key":"value"；仅含 TOKEN/USER_ID 等敏感键名会掩码）
    (
        "S10",
        '{"cat_cafe_mcp":{"env":{"OFFICE_CLAN_CALLBACK_TOKEN":"bafjdksjfksajf",'
        '"OFEICE_CIAW_USER_ID":"wandhfk","OFEICE_CIAW_CAT_ID":"assistant"}}}',
    ),
)


def log_masking_probes_enabled() -> bool:
    flag = (
        os.environ.get("JIUWENCLAW_LOG_MASKING_PROBE", "").strip().lower()
        or os.environ.get("JIUWENSWARM_LOG_MASKING_PROBE", "").strip().lower()
    )
    return flag in (
        "1",
        "true",
        "yes",
        "on",
    )


def emit_log_masking_probe_samples(logger: logging.Logger) -> None:
    """将固定敏感样例写入日志（需已挂载 SensitiveDataFilter）。"""
    tag = "[LogMaskingProbe]"
    logger.info("%s emitting %d sample line(s)", tag, len(LOG_MASKING_PROBE_SAMPLES))
    for label, text in LOG_MASKING_PROBE_SAMPLES:
        logger.info("%s %s: %s", tag, label, text)


def maybe_emit_log_masking_probe_samples(logger: logging.Logger) -> None:
    if log_masking_probes_enabled():
        emit_log_masking_probe_samples(logger)
