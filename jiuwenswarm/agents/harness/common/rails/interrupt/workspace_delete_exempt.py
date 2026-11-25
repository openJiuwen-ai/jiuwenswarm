# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作空间内删除豁免（scene-hook 落点）。

背景：命令层内置规则 ``shell_delete_files`` 是路径盲的命令级正则，区内 rm 同样
弹卡；路径层把 rm 目标归类为 write 轴（区内本就放行），无法表达"删除"的内外
差异。本模块在宿主场景钩子里对删除类命令做二次复核：

1. 严格操作数审计（:func:`delete_operands_confined`）：只接受**单一简单命令**
   ——出现任何复合结构（``&&``/``||``/管道/括号/换行）或间接执行器
   （bash/sh/powershell/eval/xargs/find 等）直接拒绝；删除家族段的操作数必须
   为**可静态解析的绝对路径**且严格落在工作空间/信任目录内（zone 根本身拒绝）；
   变量、通配、PowerShell 数组等不可静态解析的 token 一律不豁免（fail-closed）。
2. 临时引擎重放：把待豁免规则从配置中摘除后构造临时
   ``PermissionEngine``（强制 ``file_guard.defaults.write=ask`` 并关闭
   strict merge / defer_unmatched），重放同一条命令——仅当结果为 ALLOW
   （无任何其他规则拦截）才放行；chmod、敏感文件读取等任何其他 ASK 规则
   命中时重放结果为 ASK，自动回落原判定链。

任何一步不确定都不豁免，回落后由原链路弹卡，行为与现状完全一致。
"""
from __future__ import annotations

import os
import re
import shlex
from copy import deepcopy
from pathlib import Path
from typing import Any

from openjiuwen.harness.security.permission_engine.core import PermissionEngine
from openjiuwen.harness.security.permission_engine.models import PermissionLevel

from jiuwenswarm.common.utils import logger

# 与 resources/config.yaml 内置规则 shell_delete_files 的命令家族保持一致
# （pattern 同族，规则 id 由 permissions.in_workspace_delete_exempt.rules 配置）。
_DELETE_FAMILY = frozenset({
    "rm", "remove-item", "ri", "del", "erase", "rd", "rmdir", "unlink",
})
_DELETE_FAMILY_RE = re.compile(
    r"(?i)(?:^|[;&|()\r\n])\s*[\"']?(?:[^\s\"';&|()]*[\\/])?"
    r"(?:rm|remove-item|ri|del|erase|rd|rmdir|unlink)(?:\.exe)?[\"']?(?=[ \t]|$|/)"
)
# 复合结构：出现即拒绝（不尝试解析——审计只背书能被完整证明的单一简单命令）
_COMPOUND_RE = re.compile(r"&&|\|\||[;|\r\n&()]")
# 间接执行器/无法静态追踪删除语义的命令：出现即拒绝
_INDIRECT_EXEC = frozenset({
    "bash", "sh", "dash", "zsh", "fish", "csh", "tcsh", "cmd",
    "powershell", "pwsh", "eval", "exec", "source", "xargs", "find",
})
_MSYS_PATH_RE = re.compile(r"^/([A-Za-z])/(.+)$")
_CMD_SWITCH_RE = re.compile(r"^/[A-Za-z]{1,2}$")
# 展开不了/展开后语义不定的 token：环境变量、通配、反引号、花括号扩展、
# PowerShell 数组（逗号）、历史/子表达式（! @）、~+/~- 定位符
_UNRESOLVABLE_RE = re.compile(r"[$%*?\[\]`{}!,@]|~[+\-]")
_REDIRECT_TOKENS = frozenset({">", "<", ">>", "2>", "&>", "1>", "2>&1"})


def _read_knob() -> dict[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config() or {}
    except Exception:
        return {}
    permissions = cfg.get("permissions") if isinstance(cfg, dict) else None
    knob = permissions.get("in_workspace_delete_exempt") if isinstance(permissions, dict) else None
    return knob if isinstance(knob, dict) else {}


def in_workspace_delete_exempt_enabled(knob: dict[str, Any] | None = None) -> bool:
    """缺省开启；配置 ``permissions.in_workspace_delete_exempt.enabled: false`` 显式关闭。"""
    effective = knob if knob is not None else _read_knob()
    return effective.get("enabled", True) is not False


def exempt_rule_ids(knob: dict[str, Any] | None = None) -> set[str]:
    effective = knob if knob is not None else _read_knob()
    rules = effective.get("rules")
    if rules is None or not isinstance(rules, list):
        return {"shell_delete_files"}
    # 显式空列表 = 不豁免任何规则（诚实语义，不回落默认）
    return {str(rule) for rule in rules if str(rule).strip()}


def _strip_quotes(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _cmd_base(token: str) -> str:
    base = _strip_quotes(token)
    base = re.split(r"[\\/]", base)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _to_path_token(token: str) -> str:
    token = _strip_quotes(token)
    token = os.path.expanduser(token)
    if os.name == "nt":
        # MSYS/git-bash 盘符映射是 Windows 专属语义；POSIX 上 /c/x 是真实存在的
        # 绝对路径，不得改写（否则区外绝对路径会被降级成相对路径误判为区内）。
        msys = _MSYS_PATH_RE.match(token)
        if msys:
            token = msys.group(1).upper() + ":/" + msys.group(2)
    return token


def _is_strictly_inside(path: Path, zones: list[Path]) -> bool:
    """仅当 ``path`` 是某个 zone 的**严格子路径**时为 True；等于 zone 根本身不算
    （``rm -rf <工作空间根>`` 这类整目录删除不放行）。"""
    try:
        resolved = os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))
    except (OSError, RuntimeError, ValueError):
        return False
    for zone in zones:
        try:
            zone_text = os.path.normcase(
                os.path.normpath(str(zone.resolve(strict=False))).rstrip(os.sep)
            )
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved == zone_text:
            return False
        if resolved.startswith(zone_text + os.sep):
            return True
    return False


def delete_operands_confined(command: str, zones: list[Path]) -> bool:
    """严格审计：只接受**能被完整证明**的单一简单删除命令。

    拒绝（全部 fail-closed）：
    - 任何复合结构（``&&``/``||``/``;``/管道/括号/换行）——嵌套 shell、解释器
      内联代码（``bash -c``/``powershell -Command``）、管道入删除命令等形态
      静态不可证；
    - 间接执行器/删除语义无法静态追踪的命令（bash/sh/cmd/powershell/eval/
      xargs/find 等）；
    - 操作数含变量/通配/PowerShell 数组（逗号）等不可静态解析的 token；
    - 相对路径操作数——bash 工具的 cwd 在调用间持久保留，静态审计无法得知
      实际 cwd（只接受绝对路径）；
    - 等于 zone 根本身的目标（整目录删除不放行）。

    通过以上闸门的操作数必须全部是**可解析且严格落在 zones 内**的绝对路径。
    """
    zone_paths = [Path(zone) for zone in zones]
    if not zone_paths:
        return False
    if _COMPOUND_RE.search(command):
        return False
    try:
        tokens = shlex.split(command.strip(), posix=False)
    except ValueError:
        tokens = command.strip().split()
    if not tokens:
        return False
    if _cmd_base(tokens[0]) not in _DELETE_FAMILY:
        return False
    if _cmd_base(tokens[0]) in _INDIRECT_EXEC:
        return False
    for token in tokens[1:]:
        stripped = _strip_quotes(token)
        if not stripped or stripped in _REDIRECT_TOKENS:
            continue
        if stripped.startswith("-") or stripped.startswith(">") or stripped.startswith("<"):
            continue
        if _CMD_SWITCH_RE.match(stripped):
            continue
        if _UNRESOLVABLE_RE.search(stripped):
            return False
        candidate = Path(_to_path_token(stripped))
        if not candidate.is_absolute():
            return False
        if not _is_strictly_inside(candidate, zone_paths):
            return False
    return True


def _strip_exempt_rules(config: dict[str, Any], rule_ids: set[str]) -> dict[str, Any]:
    stripped = deepcopy(config)
    rules = stripped.get("rules")
    if isinstance(rules, list):
        stripped["rules"] = [
            rule
            for rule in rules
            if not (isinstance(rule, dict) and str(rule.get("id") or "") in rule_ids)
        ]
    # 重放确定性：关闭 strict merge / defer_unmatched，未命中命令回落工具基线，
    # 使重放结果不含 UNDETERMINED 形态（放行条件可收敛为仅 ALLOW）。
    stripped["strict_tool_command_merge"] = False
    stripped["defer_unmatched"] = False
    file_guard = stripped.get("file_guard")
    if not isinstance(file_guard, dict):
        file_guard = {}
        stripped["file_guard"] = file_guard
    defaults = file_guard.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        file_guard["defaults"] = defaults
    # 区外/未知目标在重放中必然 ASK（fail-closed 第二道防线）
    defaults["write"] = "ask"
    return stripped


def _trim(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


async def maybe_approve_in_workspace_delete(
    inp: Any,
    *,
    zones: list[Path],
    knob: dict[str, Any] | None = None,
) -> bool:
    """判定场景钩子是否应直接放行这条删除命令（见模块 docstring）。

    ``inp`` 为 openjiuwen 的 ``PermissionSceneHookInput``；``zones`` 为当前
    信任目录（工作空间）归一化列表；``knob`` 供测试注入，缺省读全局配置。
    """
    if inp.user_input is not None:
        return False
    if not in_workspace_delete_exempt_enabled(knob):
        return False
    tool_args = inp.tool_args if isinstance(inp.tool_args, dict) else {}
    command = tool_args.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    if not _DELETE_FAMILY_RE.search(command):
        return False
    if not delete_operands_confined(command, zones):
        logger.info(
            "[WorkspaceDeleteExempt] audit rejected (unresolvable/out-of-zone target): command=%s",
            _trim(command),
        )
        return False
    engine = getattr(inp, "engine", None)
    base_config = getattr(engine, "config", None)
    if not isinstance(base_config, dict):
        return False
    rule_ids = exempt_rule_ids(knob)
    if not rule_ids:
        return False
    stripped = _strip_exempt_rules(base_config, rule_ids)
    try:
        temp_engine = PermissionEngine(
            config=stripped,
            workspace_root=getattr(engine, "_workspace_root", None),
            immutable_control_roots=[
                Path(os.path.expandvars(os.path.expanduser(root)))
                for root in _immutable_control_roots()
                if root
            ],
        )
        result = await temp_engine.check_permission(
            inp.normalized_tool_name,
            tool_args,
        )
    except Exception:
        logger.warning(
            "[WorkspaceDeleteExempt] replay failed, keep original approval flow: command=%s",
            _trim(command),
            exc_info=True,
        )
        return False
    # 重放已强制关闭 strict merge / defer_unmatched：未命中命令回落工具基线，
    # 结果为确定性 ALLOW/ASK/DENY——仅 ALLOW 视为可豁免（不放行任何"未定"形态）。
    if result.permission == PermissionLevel.ALLOW:
        logger.info(
            "[WorkspaceDeleteExempt] approved in-workspace delete: tool=%s replay=%s rule=%s command=%s",
            inp.normalized_tool_name,
            result.permission,
            result.matched_rule,
            _trim(command),
        )
        return True
    logger.info(
        "[WorkspaceDeleteExempt] replay kept approval: tool=%s replay=%s rule=%s command=%s",
        inp.normalized_tool_name,
        result.permission,
        result.matched_rule,
        _trim(command),
    )
    return False


def _immutable_control_roots() -> list[str]:
    """与 interrupt_helpers 宿主组装保持一致的不可变控制面目录。"""
    try:
        from jiuwenswarm.common.utils import get_agent_skills_dir, get_config_dir

        return [str(get_config_dir()), str(get_agent_skills_dir())]
    except Exception:
        return []
