# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作空间内删除豁免（scene-hook 落点）。

背景：命令层内置规则 ``shell_delete_files`` 是路径盲的命令级正则，区内 rm 同样
弹卡；路径层把 rm 目标归类为 write 轴（区内本就放行），无法表达"删除"的内外
差异。本模块在宿主场景钩子里对删除类命令做二次复核：

1. 宽松操作数审计（:func:`delete_operands_confined`）：逐段解析命令，删除家族
   段（与 ``shell_delete_files`` pattern 同族）的所有操作数必须**可静态解析且
   落在工作空间/信任目录内**；含变量、通配、无法解析的 cd/pushd 之后的相对
   路径等一律不豁免（fail-closed）。
2. 临时引擎重放：把待豁免规则从配置中摘除后构造临时
   ``PermissionEngine``（强制 ``file_guard.defaults.write=ask``），重放同一条
   命令——仅当结果为 ALLOW/UNDETERMINED（其余规则均不拦截、目标全部落在
   可写区）才放行；chmod、敏感文件读取等任何其他 ASK 规则命中时重放结果为
   ASK，自动回落原判定链。

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
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\r\n&()]")
_MSYS_PATH_RE = re.compile(r"^/([A-Za-z])/(.+)$")
_CMD_SWITCH_RE = re.compile(r"^/[A-Za-z]{1,2}$")
# 展开不了/展开后语义不定的 token：环境变量、通配、反引号、花括号扩展
_UNRESOLVABLE_RE = re.compile(r"[$%*?\[\]`{}]")
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
    rules = effective.get("rules") or ["shell_delete_files"]
    if not isinstance(rules, list):
        return {"shell_delete_files"}
    return {str(rule) for rule in rules}


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
    msys = _MSYS_PATH_RE.match(token)
    if msys:
        token = msys.group(1).upper() + ":/" + msys.group(2)
    return token


def _is_inside(path: Path, zones: list[Path]) -> bool:
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
        if resolved == zone_text or resolved.startswith(zone_text + os.sep):
            return True
    return False


def delete_operands_confined(command: str, zones: list[Path]) -> bool:
    """宽松审计：删除家族段的全部操作数必须可静态解析且落在 ``zones`` 内。

    ``zones`` 为空、任何操作数含变量/通配、或 cd/pushd 之后 cwd 不可跟踪时，
    一律返回 False（fail-closed）。非删除段不在此审计（由引擎规则覆盖）。
    """
    zone_paths = [Path(zone) for zone in zones]
    if not zone_paths:
        return False
    cwd: Path | None = zone_paths[0]
    for raw_segment in _SEGMENT_SPLIT_RE.split(command):
        segment = raw_segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        base = _cmd_base(tokens[0])
        if base == "cd":
            operand = next(
                (
                    _to_path_token(token)
                    for token in tokens[1:]
                    if _strip_quotes(token) and not _strip_quotes(token).startswith("-")
                ),
                None,
            )
            if operand is None or _UNRESOLVABLE_RE.search(operand):
                cwd = None
                continue
            candidate = Path(operand)
            if not candidate.is_absolute() and cwd is not None:
                candidate = cwd / candidate
            try:
                cwd = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                cwd = None
            continue
        if base in {"pushd", "popd"}:
            cwd = None
            continue
        if base not in _DELETE_FAMILY:
            continue
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
                if cwd is None:
                    return False
                candidate = cwd / candidate
            if not _is_inside(candidate, zone_paths):
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
    stripped = _strip_exempt_rules(base_config, exempt_rule_ids(knob))
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
    if result.permission in (PermissionLevel.ALLOW, PermissionLevel.UNDETERMINED):
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
