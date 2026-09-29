# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""工具调用风险分级（设计文档 5.1，spec 7.1）。

输入工具名+参数，输出 ``P2/P3/P4/P5``：

- **P2** 疑似凭证/密钥外传：参数命中敏感模式（``.pem/.key/id_rsa/.env``、``sk-``
  前缀等，模式表为内置常量）**且**目标为网络外传类通道（shell 外发 sink
  ``curl/wget/nc/scp/Invoke-WebRequest`` 等、fetch/search、消息发送类工具）；
- **P3** 脚本执行（bash/powershell 等 shell 工具）且未命中 P2；
- **P4** 普通网络/外发内容工具（fetch/search/消息类）且未命中 P2；
- **P5** 其余（只读/内部工具），不涉及兜底。

判定结果写入 ``ctx.extra["risk.level"]`` 供各兜底点使用并落审计。
"""
from __future__ import annotations

import json
import re
from typing import Any

from jiuwenswarm.agents.harness.common.rails.cspl.constants import (
    MESSAGE_TOOLS,
    SHELL_TOOLS,
    WEB_FETCH_TOOLS,
)
from jiuwenswarm.agents.harness.common.rails.cspl.scanners import normalize_tool_name

RISK_P2 = "P2"
RISK_P3 = "P3"
RISK_P4 = "P4"
RISK_P5 = "P5"

RISK_LEVEL_KEY = "risk.level"

# 疑似凭证/密钥模式表（内置常量；命中任一即"sensitive"）。
# 误报代价可控：仅在 CSPL 同时不可用时才升级为 fail-closed 拦截。
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\.pem\b",
        r"\.key\b",
        r"\.pfx\b",
        r"\.p12\b",
        r"\bid_rsa\b",
        r"\bid_dsa\b",
        r"\bid_ecdsa\b",
        r"\bid_ed25519\b",
        r"\.env\b",
        r"\.ssh[/\\]",
        r"\.aws[/\\]",
        r"\.kube[/\\]",
        r"\.npmrc\b",
        r"\.pypirc\b",
        r"\.netrc\b",
        r"\.git-credentials\b",
        r"\bsk-[A-Za-z0-9_\-]{8,}",
        r"\bsk_live_[A-Za-z0-9]{8,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bghp_[A-Za-z0-9]{20,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
        r"BEGIN [A-Z ]*PRIVATE KEY",
    )
)

# shell 命令中的网络外发 sink（作为独立命令词命中）。
_EXFIL_SINK_RE = re.compile(
    r"(?i)\b(?:curl|wget|nc|ncat|netcat|socat|scp|sftp|ftp|rsync|telnet"
    r"|Invoke-WebRequest|Invoke-RestMethod|iwr|irm"
    r"|curl\.exe|wget\.exe)\b"
)

# fetch/search 类网络工具（cspl WEB_FETCH_TOOLS 之外补搜索类）。
_SEARCH_TOOLS = frozenset({
    "mcp_free_search",
    "mcp_paid_search",
    "web_search",
    "free_search",
    "paid_search",
})

_NETWORK_TOOLS = WEB_FETCH_TOOLS | _SEARCH_TOOLS


def _extract_text(tool_name: str, tool_args: Any) -> str:
    """从工具参数提取待判定文本（shell 取命令、消息取正文、其余取 JSON）。"""
    if isinstance(tool_args, str):
        return tool_args
    if not isinstance(tool_args, dict) or not tool_args:
        return ""
    if tool_name in SHELL_TOOLS:
        return str(tool_args.get("command") or tool_args.get("cmd") or "")
    if tool_name in MESSAGE_TOOLS:
        return str(
            tool_args.get("message")
            or tool_args.get("content")
            or tool_args.get("text")
            or ""
        )
    try:
        return json.dumps(tool_args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(tool_args)


def _is_sensitive(text: str) -> bool:
    return any(p.search(text) for p in _SENSITIVE_PATTERNS) if text else False


def classify_risk(tool_name: str, tool_args: Any) -> str:
    """工具调用风险分级：``P2/P3/P4/P5``（见模块 docstring）。"""
    name = normalize_tool_name(tool_name or "")
    text = _extract_text(name, tool_args)
    sensitive = _is_sensitive(text)

    if name in SHELL_TOOLS:
        egress = bool(_EXFIL_SINK_RE.search(text))
        return RISK_P2 if (sensitive and egress) else RISK_P3
    if name in MESSAGE_TOOLS or name in _NETWORK_TOOLS:
        return RISK_P2 if sensitive else RISK_P4
    return RISK_P5


__all__ = [
    "RISK_LEVEL_KEY",
    "RISK_P2",
    "RISK_P3",
    "RISK_P4",
    "RISK_P5",
    "classify_risk",
]
