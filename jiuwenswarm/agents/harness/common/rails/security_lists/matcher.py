# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""名单目标提取（extract_targets）与记录匹配（match_record）。

- 文件路径/命令目标复用 agent-core 引擎 native 抽取
  （``fileguard.path_extract.extract_accesses_native``：工具参数 + shell
  AST/重定向/解释器；``toolguard.shell_ast`` 子命令分段）；
- 域名目标对齐引擎 netguard 覆盖范围（fetch 类工具）；shell 内网络访问
  （curl 等）走 command 规则，不在此展开 URL；
- 工具名目标恒在首位（工具级并入，设计 §10.3）；
- 匹配语义：file_path 大小写归一 + prefix/glob；domain 裸域+子域 /
  仅子域（对齐 EgressFilter.allow）；command exact 对 exe 名、
  glob/regex 对整行（复用引擎 ``match_wildcard``，Windows 大小写不敏感）；
  tool exact + 大小写不敏感。
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from openjiuwen.harness.security.permission_engine.fileguard.path_extract import (
    extract_accesses_native,
)
from openjiuwen.harness.security.permission_engine.netguard.net_guard import (
    _FETCH_TOOLS,
    extract_fetch_url,
)
from openjiuwen.harness.security.permission_engine.toolguard.pattern_matchers import (
    match_wildcard,
)
from openjiuwen.harness.security.permission_engine.toolguard.shell_ast import (
    parse_shell_for_permission,
)
from openjiuwen.harness.security.permission_engine.toolguard.tool_categories import (
    is_shell_tool,
    shell_tools_from_config,
)

from .models import SecurityListRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# extract_targets
# ---------------------------------------------------------------------------


def _hostname(url: str) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = parsed.hostname
    return host.lower() if host else None


def _exe_name(segment: str) -> str | None:
    """子命令首 token 的 exe 基名（小写、去 .exe）。"""
    try:
        tokens = shlex.split(segment.strip(), posix=False)
    except ValueError:
        tokens = segment.strip().split()
    if not tokens:
        return None
    base = Path(tokens[0].strip('"').strip("'").replace("\\", "/")).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base or None


def extract_targets(
    tool_name: str,
    tool_args: Mapping[str, Any] | None,
    *,
    workspace: str | Path | None = None,
    permission_config: Mapping[str, Any] | None = None,
) -> list[tuple[str, str, str]]:
    """工具调用 → ``[(type, target, op)]``（去重保序）。

    - 工具名 → ``(tool, tool_name, "*")``（恒在首位；工具级规则与参数无关）；
    - 路径类工具/shell 路径参数/重定向 → ``(file_path, path, read|write|exec)``；
    - shell 工具 → ``(command, 整行, "*")`` + 各子命令段 + exe 名；
    - fetch 类工具 → ``(domain, host, "*")``。
    """
    targets: list[tuple[str, str, str]] = []
    args: Mapping[str, Any] = tool_args if isinstance(tool_args, Mapping) else {}
    try:
        ws = Path(workspace) if workspace else Path.cwd()
    except (OSError, RuntimeError):
        ws = Path.cwd()

    # 0. 工具名目标（工具级并入，设计 §10.3）：任何工具调用都带，与参数无关
    name = str(tool_name or "").strip()
    if name:
        targets.append(("tool", name, "*"))

    # 1. 文件路径目标（引擎 native 抽取）
    try:
        for path, action, _src in extract_accesses_native(tool_name, args, ws, permission_config):
            targets.append(("file_path", str(path), str(action)))
    except Exception:
        logger.warning(
            "[security_lists] extract_accesses_native 失败 tool=%s", tool_name, exc_info=True,
        )

    # 2. 命令目标（shell 工具）
    if is_shell_tool(tool_name, shell_tools_from_config(permission_config)):
        cmd = str(args.get("command") or args.get("cmd") or "").strip()
        if cmd:
            targets.append(("command", cmd, "*"))
            try:
                parsed = parse_shell_for_permission(cmd)
                segments = [
                    sc.text.strip()
                    for sc in (parsed.subcommands or ())
                    if sc.text and sc.text.strip()
                ]
            except Exception:
                segments = []
            for seg in segments:
                if seg != cmd:
                    targets.append(("command", seg, "*"))
                exe = _exe_name(seg)
                if exe:
                    targets.append(("command", exe, "*"))

    # 3. 域名目标（fetch 类工具，与引擎 netguard 同覆盖）
    if str(tool_name or "") in _FETCH_TOOLS:
        url = extract_fetch_url(args)
        if url:
            host = _hostname(url)
            if host:
                targets.append(("domain", host, "*"))

    return list(dict.fromkeys(targets))


# ---------------------------------------------------------------------------
# match_record
# ---------------------------------------------------------------------------


def _norm_path_text(value: str) -> str:
    """展开 ~ / 环境变量，统一斜杠，去尾斜杠（不 resolve，保留 glob 元字符）。"""
    text = os.path.expandvars(os.path.expanduser(str(value).strip().strip('"').strip("'")))
    return text.replace("\\", "/").rstrip("/")


def _resolve_text(value: str) -> str:
    try:
        return str(Path(_norm_path_text(value)).resolve(strict=False))
    except (OSError, RuntimeError):
        return _norm_path_text(value)


def _fold(value: str) -> str:
    return value.casefold() if sys.platform == "win32" else value


def _match_file_prefix(pattern: str, target: str) -> bool:
    pat = _fold(_resolve_text(pattern)).replace("\\", "/").rstrip("/")
    tgt = _fold(_resolve_text(target)).replace("\\", "/").rstrip("/")
    if not pat or not tgt:
        return False
    # 等值或子树（等价 commonpath==pattern，天然处理跨盘符 ValueError）
    return tgt == pat or tgt.startswith(pat + "/")


def _match_file_glob(pattern: str, target: str) -> bool:
    pat = _norm_path_text(pattern)
    tgt = _norm_path_text(target)
    if not pat or not tgt:
        return False
    # 引擎同款 wildcard：全串锚定，Windows 大小写不敏感；** 由字符类*覆盖跨分隔符
    return match_wildcard(tgt, pat)


def _match_domain(rec: SecurityListRecord, host: str) -> bool:
    # 尾点在 DNS 上与裸域等价 → 两侧都归一，否则 `evil.example.` 可绕过 deny 规则（设计 §7）
    pat = rec.pattern.strip().lower().rstrip(".")
    h = host.strip().lower().rstrip(".")
    if not pat or not h:
        return False
    if rec.match == "exact":
        # 裸域 + 子域：example.com 命中 example.com / a.example.com
        return h == pat or h.endswith("." + pat)
    if rec.match == "wildcard":
        # *.example.com 仅子域（不命中裸域）
        suffix = pat[2:] if pat.startswith("*.") else pat
        return h.endswith("." + suffix)
    return False


def _match_command(rec: SecurityListRecord, target: str) -> bool:
    if rec.match == "exact":
        exe = target.strip().lower()
        pat = rec.pattern.strip().lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if pat.endswith(".exe"):
            pat = pat[:-4]
        return bool(exe) and exe == pat
    if rec.match == "glob":
        return match_wildcard(target.strip(), rec.pattern.strip())
    if rec.match == "regex":
        try:
            return bool(re.search(rec.pattern, target))
        except re.error:
            logger.warning("[security_lists] 非法 regex pattern: %r", rec.pattern)
            return False
    return False


def _match_tool(rec: SecurityListRecord, target: str) -> bool:
    """工具名匹配：exact + 大小写不敏感（工具名是标识符，大小写不构成另一个对象）。

    显式不用 :func:`_fold`——那是 Windows 路径语义；工具名在 Linux 上同样应大小写不敏感。
    """
    return bool(target) and target.strip().casefold() == rec.pattern.strip().casefold()


def match_record(rec: SecurityListRecord, target: str) -> bool:
    """记录是否命中目标（type 不匹配直接 False）。"""
    if rec.type == "tool":
        return _match_tool(rec, target)
    if rec.type == "file_path":
        if rec.match == "prefix":
            return _match_file_prefix(rec.pattern, target)
        if rec.match == "glob":
            return _match_file_glob(rec.pattern, target)
        return False
    if rec.type == "domain":
        return _match_domain(rec, target)
    if rec.type == "command":
        return _match_command(rec, target)
    return False
