# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase-1 ``file_guard``：``workspace`` / ``global`` / ``trusted_exec_directory`` 三轴文件权限。

替换旧 ``ExternalDirectoryChecker``。本模块只做"按轴判定 + 持久化"，**不**抽路径——
路径来源由两条独立通道提供：
 1. **注册表通道**（``files/registry``）：根据 ``FileToolSpec`` 直接读 ``tool_args`` 中的
    ``file_path`` / ``path`` 等字段，得到 ``(Path, action, "tool_arg")``；
 2. **命令意图通道**（``command_intent``）：L1 shlex + L3-Cmd LLM 输出 ``CommandIntent[]``，
    由 ``evaluate_command_intents`` 转成判定结果。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

from jiuwenclaw.agentserver.permissions.files.extract import (
    iter_config_tool_bindings,
)
from jiuwenclaw.agentserver.permissions.files.registry import (
    FileToolSpec,
    lookup_file_tool_specs,
)
from jiuwenclaw.agentserver.permissions.models import (
    FileOperation,
    PermissionLevel,
    PermissionResult,
)
from jiuwenclaw.agentserver.permissions.patterns import contains_path
from jiuwenclaw.agentserver.permissions.tiered_policy import (
    _PATH_TOOLS,
    _iter_path_strings,
)

logger = logging.getLogger(__name__)

_WRITE_PATH_TOOLS = frozenset({
    "write_file", "edit_file", "write_text_file", "write", "search_replace",
})

_VERB_CN = {"read": "读取", "write": "写入", "exec": "执行"}

# ``FileOperation.source`` 合法取值。命中以外的来源（异常路径 / 未来扩展）统一回退到 ``"shlex"``。
_VALID_OP_SOURCES = ("tool_arg", "shlex", "script_scan", "llm")

# Phase-1：用于"加载期 ERROR"的 path 类工具集合。任何 ``rules[*].tools`` 命中其中之一，
# 且 ``pattern`` 像路径（含 ``/`` / ``\\`` / 通配符 ``**`` 等），就视为残留 path-class 规则。
_PATH_CLASS_TOOLS = frozenset({
    "read_file", "write_file", "edit_file",
    "read_text_file", "write_text_file",
    "Read", "Write", "Edit",
    "search_replace",
    "grep", "grep_search",
    "glob", "glob_file_search",
    "list_dir", "list_files",
})

_LOAD_ERROR_REPORTED_RULE_IDS: set[str] = set()


# ---------- 配置归一 / 路径规范 ----------


def merged_file_guard_config(permissions: dict[str, Any]) -> dict[str, Any]:
    """生成生效的 ``file_guard`` 配置；缺省时合并旧 ``external_directory``。

    ``permissions`` 入参**只读**：所有可写操作均在内部副本上完成，避免污染调用方
    （否则 ``external_directory`` 项会被就地"迁移"进 ``permissions["file_guard"]["global"]``，
    后续 ``update_config`` 将无法再次迁移或反向移除）。
    """
    raw_fg = permissions.get("file_guard")
    fg: dict[str, Any] = {}
    if isinstance(raw_fg, dict):
        for k, v in raw_fg.items():
            if isinstance(v, dict):
                fg[k] = dict(v)
            elif isinstance(v, list):
                fg[k] = list(v)
            else:
                fg[k] = v

    raw_global = fg.get("global")
    global_map: dict[str, Any] = {}
    if isinstance(raw_global, dict):
        for k, v in raw_global.items():
            global_map[k] = dict(v) if isinstance(v, dict) else v

    ext = permissions.get("external_directory")
    if isinstance(ext, dict):
        for k, v in ext.items():
            if k == "*" or not isinstance(k, str):
                continue
            key_norm = k.strip()
            if not key_norm or key_norm in global_map:
                continue
            if v == "allow":
                global_map[key_norm] = {"read_enable": True, "write_enable": True}
            elif v in ("ask", "deny"):
                global_map[key_norm] = {"read_enable": False, "write_enable": False}
    fg["global"] = global_map

    ws = fg.get("workspace")
    if not isinstance(ws, dict):
        fg["workspace"] = {"rw_enabled": True, "description": ""}
    ted = fg.get("trusted_exec_directory")
    if not isinstance(ted, list):
        fg["trusted_exec_directory"] = []
    return fg


def _expand_path_str(s: str) -> str:
    return os.path.expandvars(os.path.expanduser(s.strip()))


def _resolve_path_str(raw: str, workspace: Path) -> Path | None:
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return None
    try:
        p = Path(_expand_path_str(raw))
        if not p.is_absolute():
            p = (workspace / p).resolve()
        else:
            p = p.resolve()
        return p
    except (OSError, RuntimeError):
        return None


def _posix_str(p: Path) -> str:
    try:
        return p.resolve().as_posix()
    except (OSError, RuntimeError):
        return p.as_posix()


def _longest_prefix_match(abs_posix: str, global_map: dict[str, Any]) -> dict[str, Any] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for key, entry in global_map.items():
        if not isinstance(key, str) or key == "*":
            continue
        if not isinstance(entry, dict):
            continue
        prefix = _posix_str(Path(_expand_path_str(key)))
        if abs_posix == prefix or abs_posix.startswith(prefix + "/"):
            ln = len(prefix)
            if best is None or ln > best[0]:
                best = (ln, entry)
    return best[1] if best else None


def _trusted_matches(abs_posix: str, trusted_list: list[Any]) -> bool:
    for raw in trusted_list:
        if not isinstance(raw, str) or not raw.strip():
            continue
        prefix = _posix_str(Path(_expand_path_str(raw.strip())))
        if abs_posix == prefix or abs_posix.startswith(prefix + "/"):
            return True
    return False


def _read_flag(entry: dict[str, Any]) -> bool:
    return bool(entry.get("read_enable", entry.get("read_enabled", False)))


def _write_flag(entry: dict[str, Any]) -> bool:
    return bool(entry.get("write_enable", entry.get("write_enabled", False)))


# ---------- FileGuardChecker ----------


class FileGuardChecker:
    """三轴文件权限判定：``read`` / ``write`` 走 workspace+global，``exec`` 走 trusted_exec_directory。"""

    def __init__(self, permissions: dict[str, Any], workspace_root: Path | None = None):
        self._permissions = permissions
        self._fg = merged_file_guard_config(permissions)
        self._workspace_root = workspace_root
        self._tool_bindings = iter_config_tool_bindings(self._fg)

    @staticmethod
    def _strictest(a: PermissionLevel, b: PermissionLevel) -> PermissionLevel:
        order = {PermissionLevel.DENY: 0, PermissionLevel.ASK: 1, PermissionLevel.ALLOW: 2}
        oa = order.get(a, 0)
        ob = order.get(b, 0)
        return a if oa <= ob else b

    def workspace_root(self) -> Path:
        if self._workspace_root is not None:
            p = Path(self._workspace_root).resolve()
            logger.info(
                "[file_guard] workspace_root source=constructor path=%s",
                p,
            )
            return p
        # Align with RuntimePromptRail / _update_runtime_config: metadata effective_project_dir
        # (set via set_effective_request_workspace_dir on each request).
        try:
            from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
                get_effective_request_workspace_dir,
            )
            req_ws = get_effective_request_workspace_dir()
            if isinstance(req_ws, str) and req_ws.strip():
                p = Path(req_ws.strip()).resolve()
                logger.info(
                    "[file_guard] workspace_root source=effective_request "
                    "(metadata effective_project_dir / RuntimePromptRail) path=%s",
                    p,
                )
                return p
        except ImportError:
            pass
        try:
            from jiuwenclaw.utils import get_agent_workspace_dir
            p = Path(get_agent_workspace_dir()).resolve()
            logger.debug(
                "[file_guard] workspace_root source=agent_default (get_agent_workspace_dir) path=%s",
                p,
            )
            return p
        except ImportError:
            p = Path.cwd().resolve()
            logger.debug(
                "[file_guard] workspace_root source=cwd_fallback path=%s",
                p,
            )
            return p

    def _workspace_rw_enabled(self) -> bool:
        ws = self._fg.get("workspace") or {}
        if not isinstance(ws, dict):
            return True
        return bool(ws.get("rw_enabled", True))

    def _global_map(self) -> dict[str, Any]:
        g = self._fg.get("global")
        return g if isinstance(g, dict) else {}

    def _trusted_list(self) -> list[Any]:
        t = self._fg.get("trusted_exec_directory")
        return t if isinstance(t, list) else []

    def _check_one(self, abs_path: Path, action: str) -> PermissionLevel:
        abs_posix = _posix_str(abs_path)
        ws = self.workspace_root()
        in_ws = contains_path(ws, abs_path)

        if action == "exec":
            if _trusted_matches(abs_posix, self._trusted_list()):
                return PermissionLevel.ALLOW
            return PermissionLevel.ASK

        if in_ws and self._workspace_rw_enabled():
            return PermissionLevel.ALLOW

        entry = _longest_prefix_match(abs_posix, self._global_map())
        if entry is None:
            return PermissionLevel.ASK
        if action == "read":
            return PermissionLevel.ALLOW if _read_flag(entry) else PermissionLevel.ASK
        if action == "write":
            return PermissionLevel.ALLOW if _write_flag(entry) else PermissionLevel.ASK
        return PermissionLevel.ASK

    # ----- 路径来源（仅注册表通道） -----

    def _specs_for_tool(self, tool_name: str) -> list[FileToolSpec] | None:
        specs = lookup_file_tool_specs(tool_name)
        if specs:
            return specs
        raw_bindings = self._tool_bindings.get(tool_name)
        if raw_bindings:
            out: list[FileToolSpec] = []
            for b in raw_bindings:
                arg = str(b.get("arg_name") or "file_path").strip()
                act = str(b.get("action") or "read").strip().lower()
                if act not in ("read", "write", "exec"):
                    act = "read"
                out.append(FileToolSpec(tool_name, arg, act))  # type: ignore[arg-type]
            return out
        return None

    def collect_tool_arg_accesses(
            self,
            tool_name: str,
            tool_args: dict[str, Any],
    ) -> list[tuple[Path, str, str]]:
        """从 ``tool_args`` 抽取 ``(path, action, "tool_arg")`` 三元组（注册表通道）。"""
        ws = self.workspace_root()
        out: list[tuple[Path, str, str]] = []
        specs = self._specs_for_tool(tool_name)
        if specs:
            for spec in specs:
                raw = tool_args.get(spec.arg_name)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                rp = _resolve_path_str(raw, ws)
                if rp is None:
                    continue
                out.append((rp, spec.action, "tool_arg"))
            return out
        if tool_name in _PATH_TOOLS:
            action: str = "write" if tool_name in _WRITE_PATH_TOOLS else "read"
            for s in _iter_path_strings(tool_name, tool_args):
                rp = _resolve_path_str(s, ws)
                if rp is None:
                    continue
                out.append((rp, action, "tool_arg"))
        return out

    # ----- 评估入口 -----

    def evaluate_accesses(
            self,
            accesses: Iterable[tuple[Path, str, str]],
    ) -> PermissionResult | None:
        """对 ``(Path, action, source)`` 列表逐条判定，按 ``strictest`` 合并并产出 ``FileOperation[]``。"""
        accesses = list(accesses)
        if not accesses:
            return None

        overall = PermissionLevel.ALLOW
        pending: list[FileOperation] = []
        seen_ops: set[tuple[str, str, str]] = set()

        for path, action, source in accesses:
            level = self._check_one(path, action)
            overall = self._strictest(overall, level)
            if level != PermissionLevel.ASK:
                continue
            ps = _posix_str(path)
            key = (action, ps, source)
            if key in seen_ops:
                continue
            seen_ops.add(key)
            verb = _VERB_CN.get(action, action)
            normalized_source = source if source in _VALID_OP_SOURCES else "shlex"
            pending.append(FileOperation(
                action=action,  # type: ignore[arg-type]
                path=ps,
                source=normalized_source,  # type: ignore[arg-type]
                prompt=f"是否允许{verb} {ps}？",
            ))

        if overall == PermissionLevel.ALLOW:
            return None

        hint = pending[0].path if pending else _posix_str(accesses[0][0])
        return PermissionResult(
            permission=overall,
            reason=f"file_guard requires approval (paths outside policy): {hint}" if overall == PermissionLevel.ASK
                   else f"file_guard denied: {hint}",
            matched_rule="file_guard:ask" if overall == PermissionLevel.ASK else "file_guard:deny",
            file_operations=pending or None,
        )

    def evaluate_command_intents(self, intents: Iterable[Any]) -> PermissionResult | None:
        """把 ``CommandIntent[]``（来自 ``command_intent`` L1+L3-Cmd）转成判定结果。"""
        ws = self.workspace_root()
        accesses: list[tuple[Path, str, str]] = []
        for intent in intents:
            paths = getattr(intent, "paths", None) or ()
            action = getattr(intent, "action", None)
            source = getattr(intent, "source", "shlex")
            if action not in ("read", "write", "exec"):
                continue
            for raw in paths:
                if not isinstance(raw, str) or not raw.strip():
                    continue
                rp = _resolve_path_str(raw, ws)
                if rp is None:
                    continue
                accesses.append((rp, action, source))
        return self.evaluate_accesses(accesses)

    def check_external_paths(
            self,
            tool_name: str,
            tool_args: dict[str, Any],
    ) -> PermissionResult | None:
        """向后兼容：仅基于 ``tool_args`` 的注册表通道路径做判定。

        命令串里的路径（shlex / LLM）请显式调用 ``evaluate_command_intents``，
        以便 ``PermissionEngine`` 显式编排 L1 + L3-Cmd。
        """
        accesses = self.collect_tool_arg_accesses(tool_name, tool_args)
        return self.evaluate_accesses(accesses)


# ---------- 持久化 ----------


def _yaml_update_permissions(mutate_fn) -> None:
    from jiuwenclaw.agentserver.permissions.core import get_permission_engine
    from jiuwenclaw.config import (
        _CONFIG_YAML_PATH,
        _load_yaml_round_trip,
        _dump_yaml_round_trip,
    )

    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        data["permissions"] = permissions
    mutate_fn(permissions)
    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
    get_permission_engine().update_config(data.get("permissions", {}))


def _ensure_file_guard_dict(permissions: dict[str, Any]) -> dict[str, Any]:
    fg = permissions.get("file_guard")
    if not isinstance(fg, dict):
        fg = {}
        permissions["file_guard"] = fg
    gm = fg.get("global")
    if not isinstance(gm, dict):
        gm = {}
        fg["global"] = gm
    if "workspace" not in fg or not isinstance(fg["workspace"], dict):
        fg["workspace"] = {"rw_enabled": True, "description": ""}
    if "trusted_exec_directory" not in fg or not isinstance(fg["trusted_exec_directory"], list):
        fg["trusted_exec_directory"] = []
    return fg


def _filter_persistable(operations: list[FileOperation]) -> list[FileOperation]:
    """``source=llm + action=exec`` 不允许永久化，按设计静默降级到 ``allow_once``。"""
    out: list[FileOperation] = []
    for op in operations:
        if op.source == "llm" and op.action == "exec":
            logger.warning(
                "[file_guard] persist.skip llm+exec downgraded to allow_once path=%s",
                op.path,
            )
            continue
        out.append(op)
    return out


def persist_file_operations_allow(operations: list[FileOperation]) -> None:
    """落地用户的 ``allow_always`` 决策到 ``file_guard.global`` / ``trusted_exec_directory``。"""
    if not operations:
        return
    persistable = _filter_persistable(list(operations))
    if not persistable:
        logger.info("[file_guard] persist.noop reason=all_filtered")
        return

    def mutate(permissions: dict[str, Any]) -> None:
        fg = _ensure_file_guard_dict(permissions)
        gm: dict[str, Any] = fg["global"]  # type: ignore[assignment]
        ted: list[Any] = fg["trusted_exec_directory"]  # type: ignore[assignment]

        for op in persistable:
            path_norm = op.path.replace("\\", "/").rstrip("/")
            if op.action == "exec":
                norm_set = {str(x).replace("\\", "/").rstrip("/") for x in ted}
                if path_norm and path_norm not in norm_set:
                    ted.append(path_norm)
                continue
            cur = gm.get(path_norm)
            if not isinstance(cur, dict):
                cur = {}
            if op.action == "read":
                cur = {**cur, "read_enable": True}
            elif op.action == "write":
                cur = {**cur, "write_enable": True}
            gm[path_norm] = cur

    _yaml_update_permissions(mutate)
    logger.info("[file_guard] persist_file_operations_allow count=%s", len(persistable))


def persist_legacy_external_allow_paths(paths: list[str]) -> None:
    """兼容旧 ``persist_external_directory_allow``：写到 ``file_guard.global``。"""
    if not paths:
        return
    ops: list[FileOperation] = []
    for raw in paths:
        path_norm = str(Path(raw.replace("\\", "/")).expanduser()).replace("\\", "/").rstrip("/")
        parent = str(Path(path_norm).parent.as_posix()).rstrip("/") if path_norm else path_norm
        key = parent if parent and parent != "." else path_norm
        ops.append(FileOperation(action="read", path=key, source="tool_arg", prompt=""))
        ops.append(FileOperation(action="write", path=key, source="tool_arg", prompt=""))
    persist_file_operations_allow(ops)


def apply_cli_trusted_to_permissions_dict(permissions: dict[str, Any], dir_norm: str) -> None:
    """供 ``persist_cli_trusted_directory`` 使用：在内存 ``permissions`` 中追加信任目录。"""
    fg = _ensure_file_guard_dict(permissions)
    gm: dict[str, Any] = fg["global"]  # type: ignore[assignment]
    ted: list[Any] = fg["trusted_exec_directory"]  # type: ignore[assignment]
    gm[dir_norm] = {"read_enable": True, "write_enable": True}
    norm_ted = {str(x).replace("\\", "/").rstrip("/") for x in ted}
    if dir_norm not in norm_ted:
        ted.append(dir_norm)


def _looks_like_path_pattern(pattern: str) -> bool:
    """启发式判断 pattern 是否为"文件路径"类（用于加载期校验，宁严勿宽）。"""
    if not pattern:
        return False
    s = pattern.strip()
    if not s:
        return False
    if "/" in s or "\\" in s:
        return True
    if "**" in s:
        return True
    if s.startswith("~") or s.startswith("$") or s.startswith("%"):
        return True
    if len(s) > 1 and s[1] == ":":
        return True
    return False


def report_legacy_path_rules_at_load(permissions: dict[str, Any]) -> list[str]:
    """加载期校验：把 ``rules[*]`` 里残留的 path 类条目挂到 ERROR 日志。

    返回命中条目的 ``id`` 列表，便于上层做集中提示或单测断言。
    Phase-1 不强制阻断启动，仅打 ERROR + 提示用户改写到 ``file_guard.global``，
    避免因"老配置 + 新代码"导致服务直接挂掉。
    """
    rules = permissions.get("rules") if isinstance(permissions, dict) else None
    if not isinstance(rules, list):
        return []

    flagged: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rid = str(rule.get("id") or "").strip()
        tools = rule.get("tools")
        pattern = rule.get("pattern")
        if not isinstance(tools, list) or not isinstance(pattern, str):
            continue
        path_tool_hits = [t for t in tools if isinstance(t, str) and t in _PATH_CLASS_TOOLS]
        if not path_tool_hits:
            continue
        if not _looks_like_path_pattern(pattern):
            continue
        flagged.append(rid)
        cache_key = rid or f"__anon__:{pattern}:{','.join(sorted(path_tool_hits))}"
        if cache_key in _LOAD_ERROR_REPORTED_RULE_IDS:
            continue
        _LOAD_ERROR_REPORTED_RULE_IDS.add(cache_key)
        logger.error(
            "[file_guard] permissions.rules.path_class_legacy id=%s tools=%s pattern=%r "
            "hint=请把该条目改写到 permissions.file_guard.global['<path>'] = "
            "{read_enable: bool, write_enable: bool}（exec 类放 trusted_exec_directory）",
            rid or "<no-id>",
            path_tool_hits,
            pattern,
        )
    return flagged


def list_pending_file_operations_for_tool(
        permissions: dict[str, Any],
        tool_name: str,
        tool_args: dict[str, Any],
) -> list[FileOperation]:
    """重检：返回当前仍处于 ASK 状态的 file_operations（仅 tool_arg 通道，用于持久化前的 sanity check）。"""
    checker = FileGuardChecker(permissions)
    result = checker.check_external_paths(tool_name, tool_args)
    if result is None or not result.file_operations:
        return []
    return list(result.file_operations)
