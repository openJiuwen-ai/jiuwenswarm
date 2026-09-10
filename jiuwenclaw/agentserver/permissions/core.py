# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""权限引擎 - 核心权限控制模块。

Phase-1 编排：

1. 加载 / 热更新 ``permissions`` 配置。
2. ``check_permission`` 按设计文档第 2 节的 Guard 管线评估：
   - 工具档位（``allow`` / ``deny`` / ``guard``）短路 DENY/ALLOW；``guard`` 进入 Guard 管线。
   - 子线 A：``evaluate_tiered_policy_detailed``（命令 / 参数规则）。**已注册的「路径类」读写工具**
     （非 shell）在管线 A **仅解析整工具 DENY**；非 DENY 时不产出 allow/guard 等档位，
     **跳过**管线 A 的其余档位与 ``rules`` / subcommand，实质判定完全由管线 B（``file_guard``）承担。
   - 子线 B：``FileGuardChecker.evaluate_accesses`` + ``evaluate_command_intents``
     （三轴文件路径判定）。
    - 通过 ``strictest`` 合并档位与 ``file_guard`` 结果（路径类工具在非 DENY 时仅由 B 侧抬升降）。
      Shell 工具（``bash`` / ``mcp_exec_command`` / ``create_terminal``）在管线 A 得到显式
      ``ALLOW`` 时**跳过**管线 B，``file_guard`` 不再抬升。
    - ``file_operations`` 透传到 ``PermissionResult`` 供审批卡渲染。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.permissions.checker import PERMISSION_ENABLED_CHANNELS, ToolPermissionLog
from jiuwenclaw.agentserver.permissions.command_intent import (
    CommandIntent,
    LlmUsageCallback,
    collect_command_intents,
    is_command_intent_enabled,
)
from jiuwenclaw.agentserver.permissions.file_guard import (
    FileGuardChecker,
    classify_tool_file_action_kind,
    report_legacy_path_rules_at_load,
)
from jiuwenclaw.agentserver.permissions.models import (
    FileOperation,
    PermissionLevel,
    PermissionResult,
    SubcommandPermissionResult,
)
from jiuwenclaw.agentserver.permissions.security_guard import (
    check_kia_file,
    detect_rms_file,
    extract_file_path_from_tool_args,
)
from jiuwenclaw.agentserver.permissions.shell_tools import is_shell_permission_tool
from jiuwenclaw.agentserver.permissions.tiered_policy import (
    evaluate_tiered_policy_detailed,
    evaluate_tiered_policy_path_tool_pipeline_a,
    get_builtin_security_rules,
    strictest as tiered_policy_strictest,
)

logger = logging.getLogger(__name__)


def _to_bool(value: Any) -> bool:
    """将配置值转为布尔；字符串 "false"/"0"/"no" 视为 False。

    ``resolve_env_vars`` 用 ``re.sub`` 替换环境变量，结果始终为字符串，
    因此 ``${PERMISSIONS_ENABLED:-true}`` 解析后得到 ``"false"`` 而非 ``False``。
    若不做转换，Python 的 ``bool("false") == True`` 会导致开关失效。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


class PermissionEngine:
    """Phase-1 权限引擎。"""

    def __init__(
        self,
        config: dict | None = None,
        llm: Any = None,
        model_name: str | None = None,
    ):
        self.config = config or {}
        self._enabled = _to_bool(self.config.get("enabled", True))
        self._llm = llm
        self._model_name = model_name
        self._file_guard = FileGuardChecker(self.config)
        report_legacy_path_rules_at_load(self.config)

    # ---------- 配置 ----------

    def update_config(self, config: dict):
        """热更新配置。"""
        self.config = config
        self._enabled = _to_bool(config.get("enabled", True))
        self._file_guard = FileGuardChecker(config)
        report_legacy_path_rules_at_load(self.config)

    def update_llm(self, llm: Any, model_name: str | None) -> None:
        """供 ``PermissionInterruptRail`` 等热更新模型；用于 L3-Cmd LLM 调用。"""
        self._llm = llm
        self._model_name = model_name

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def file_guard(self) -> FileGuardChecker:
        return self._file_guard

    @property
    def llm(self) -> Any:
        """供外部（如 ``PermissionInterruptRail`` 诊断日志）只读获取已绑定的 LLM 客户端。

        通过 ``@property`` 暴露公共访问器、避免外部直接读 ``_llm`` 私有字段，
        保持 ``update_llm`` 仍是唯一的写入入口。
        """
        return self._llm

    @property
    def model_name(self) -> str | None:
        """与 ``llm`` 配套的模型名 getter；只读、不可绕过 ``update_llm`` 写入。"""
        return self._model_name

    # ---------- 同步直查（不发起 LLM；仅工具档位 + 子线 A + 子线 B 的工具参数通道） ----------

    def check_tool_permission_directly(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        channel_id: str = "web",
    ) -> tuple[PermissionLevel | None, str | None]:
        """直接检查工具权限，不受 enabled 开关和 channel 限制。"""
        return self.evaluate_global_policy_directly(tool_name, tool_args, channel_id)

    def evaluate_global_policy_directly(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        channel_id: str = "web",
        *,
        include_external_directory: bool = True,
    ) -> tuple[PermissionLevel | None, str | None]:
        """直接评估全局权限，不受 enabled/channel 短路影响。

        ``include_external_directory`` 名字保留向后兼容，实际现在控制是否参与 ``file_guard``
        子线 B（仅注册表通道，不发 LLM）。
        """
        permission, matched_rule, _, _ = self.evaluate_global_policy_with_details(
            tool_name,
            tool_args,
            channel_id,
            include_external_directory=include_external_directory,
        )
        return permission, matched_rule

    @staticmethod
    def _is_registered_path_tool_non_shell(tool_name: str) -> bool:
        """已注册（或兜底集合）路径类工具且非 shell：管线 A 只做 DENY 短路。"""
        return (
            classify_tool_file_action_kind(tool_name) is not None
            and not is_shell_permission_tool(tool_name)
        )

    def _evaluate_tier_for_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> tuple[PermissionLevel | None, str | None, list[tuple[str, PermissionLevel, str]] | None]:
        """子线 A：路径类工具仅 ``evaluate_tiered_policy_path_tool_pipeline_a``（仅 DENY），其余完整 tiered。"""
        if PermissionEngine._is_registered_path_tool_non_shell(tool_name):
            tier_perm, tier_rule = evaluate_tiered_policy_path_tool_pipeline_a(self.config, tool_name)
            return tier_perm, tier_rule, None
        tp, tr, subs = evaluate_tiered_policy_detailed(self.config, tool_name, tool_args)
        return tp, tr, subs

    def evaluate_global_policy_with_details(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        channel_id: str = "web",
        *,
        include_external_directory: bool = True,
        extra_intents: list[CommandIntent] | None = None,
    ) -> tuple[
        PermissionLevel | None,
        str | None,
        list[SubcommandPermissionResult] | None,
        list[FileOperation] | None,
    ]:
        """同步直查的细节版本：返回 ``(level, rule, subcommand_results, file_operations)``。

        - ``include_external_directory`` 等价于"是否合并 file_guard 子线 B"。
        - ``extra_intents`` 由调用方提前异步算好（如 ``check_permission`` 内的 L1+L3-Cmd），
          这里做去重合并；不会自己发 LLM。
        """
        if not isinstance(tool_args, dict):
            logger.warning(
                "[PermissionEngine] direct tool_args is not a dict (type=%s), using {}",
                type(tool_args).__name__,
            )
            tool_args = {}

        tier_perm, tier_rule, raw_subs = self._evaluate_tier_for_tool(tool_name, tool_args)
        permission = tier_perm
        matched_rule = tier_rule
        subcommand_results: list[SubcommandPermissionResult] | None = None
        if raw_subs is not None:
            subcommand_results = [
                SubcommandPermissionResult(text=text, permission=lvl, matched_rule=rule)
                for text, lvl, rule in raw_subs
            ]

        file_operations: list[FileOperation] | None = None
        shell_explicit_allow = (
            permission == PermissionLevel.ALLOW
            and is_shell_permission_tool(tool_name)
        )
        merged_file_guard = bool(
            include_external_directory
            and permission != PermissionLevel.DENY
            and not shell_explicit_allow
        )
        if merged_file_guard:
            fg_result = self._evaluate_file_guard(tool_name, tool_args, extra_intents)
            if fg_result is not None:
                if permission is None:
                    permission = fg_result.permission
                    matched_rule = fg_result.matched_rule
                else:
                    merged = tiered_policy_strictest(permission, fg_result.permission)
                    if merged != permission:
                        permission = merged
                        matched_rule = (
                            f"{matched_rule}|{fg_result.matched_rule}"
                            if matched_rule
                            else fg_result.matched_rule
                        )
                    elif fg_result.permission == permission and fg_result.matched_rule:
                        matched_rule = (
                            f"{matched_rule}|{fg_result.matched_rule}"
                            if matched_rule
                            else fg_result.matched_rule
                        )
                if fg_result.file_operations:
                    file_operations = list(fg_result.file_operations)
            elif permission is None:
                permission = PermissionLevel.ALLOW

        return permission, matched_rule, subcommand_results, file_operations

    # ---------- file_guard 调度 ----------

    def _collect_file_guard_accesses(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        extra_intents: list[CommandIntent] | None,
    ) -> list[tuple[Path, str, str]]:
        """合并工具参数通道与命令意图通道的路径访问列表（与子线 B 全量判定同源）。"""
        accesses = self._file_guard.collect_tool_arg_accesses(tool_name, tool_args)
        ws = self._file_guard.workspace_root()
        if extra_intents:
            for intent in extra_intents:
                action = getattr(intent, "action", None)
                if action not in ("read", "write", "exec"):
                    continue
                source = getattr(intent, "source", "shlex")
                for raw in getattr(intent, "paths", ()) or ():
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    try:
                        p = Path(raw)
                        if not p.is_absolute():
                            p = (ws / p).resolve()
                        else:
                            p = p.resolve()
                    except (OSError, RuntimeError):
                        continue
                    accesses.append((p, action, source))
        return accesses

    def _evaluate_file_guard(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        extra_intents: list[CommandIntent] | None,
    ) -> PermissionResult | None:
        """合并工具参数通道 + 命令意图通道，得到一份 file_guard 结果。"""
        accesses = self._collect_file_guard_accesses(tool_name, tool_args, extra_intents)
        return self._file_guard.evaluate_accesses(accesses)

    # ---------- 异步主入口 ----------

    async def check_permission(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        channel_id: str = "web",
        session_id: str | None = None,
        usage_callback: LlmUsageCallback | None = None,
    ) -> PermissionResult:
        """检查工具调用权限（按 Guard 管线编排）。"""
        logger.info(
            "[PermissionEngine] permission.check.start tool=%s channel=%s enabled=%s",
            tool_name, channel_id, self._enabled,
        )

        if not isinstance(tool_args, dict):
            logger.warning(
                "[PermissionEngine] tool_args is not a dict (type=%s), using {}",
                type(tool_args).__name__,
            )
            tool_args = {}

        if not self._enabled:
            builtin_deny = self._evaluate_builtin_deny_only(tool_name, tool_args)
            if builtin_deny is not None:
                logger.warning(ToolPermissionLog(
                    tool=tool_name,
                    decision="DENY",
                    source="system",
                    rule=builtin_deny,
                    channel=channel_id or "empty",
                    session_id=session_id or "empty",
                ).to_json(), extra={'component': 'permissions'})
                return PermissionResult(
                    permission=PermissionLevel.DENY,
                    matched_rule=builtin_deny,
                    reason=self._get_reason(PermissionLevel.DENY, tool_name, builtin_deny),
                )
            logger.info(ToolPermissionLog(
                tool="N/A",
                decision="SKIP",
                source="system",
                rule="system_disabled",
                channel=channel_id or "empty",
                session_id=session_id or "empty",
            ).to_json(), extra={'component': 'permissions'})
            return PermissionResult(
                permission=PermissionLevel.ALLOW,
                reason="Permission system is disabled",
            )

        normalized_channel = (channel_id or "").strip() or "web"
        if normalized_channel not in PERMISSION_ENABLED_CHANNELS:
            logger.info(ToolPermissionLog(
                tool="N/A",
                decision="SKIP",
                source="system",
                rule="channel_filter",
                channel=normalized_channel,
                session_id=session_id or "empty",
            ).to_json(), extra={'component': 'permissions'})
            return PermissionResult(
                permission=PermissionLevel.ALLOW,
                reason=f"Skipped for channel: {normalized_channel}",
            )

        # L1 + L3-Cmd 命令意图（仅对 shell / code 类工具有意义；其他工具返回空）
        extra_intents: list[CommandIntent] = []
        if is_command_intent_enabled(self.config):
            try:
                extra_intents = await collect_command_intents(
                    tool_name,
                    tool_args,
                    self._file_guard.workspace_root(),
                    self.config,
                    llm=self._llm,
                    model_name=self._model_name,
                    usage_callback=usage_callback,
                )
            except Exception:  # noqa: BLE001 — never let intent extraction crash policy
                logger.warning(
                    "[PermissionEngine] command_intent.collect_failed tool=%s",
                    tool_name,
                    exc_info=True,
                )
                extra_intents = []

        permission, matched_rule, subcommand_results, file_operations = (
            self.evaluate_global_policy_with_details(
                tool_name,
                tool_args,
                channel_id,
                include_external_directory=True,
                extra_intents=extra_intents or None,
            )
        )

        if permission is None:
            permission = PermissionLevel.ASK
            matched_rule = matched_rule or "tiered_policy:fallback(no_baseline)"

        # ── Security gate: KIA + RMS check for file-read tools ──
        # Runs on any non-DENY result (ALLOW or ASK), so workspace-external
        # paths that trigger ASK still go through KIA+RMS before the user
        # can approve. Closing the gap where read_file (FileSystemRail)
        # bypasses the guards in acp_output_tools.read_text_file.
        # Order: KIA first (ICPM), then RMS (local byte check) — same as all
        # other scenarios (upload, RAG index, read_text_file).
        #
        # Extended to check bash commands: LLMs often use bash to read files
        # directly (e.g., `python -c "open('E:\\123\\file.pptx').read()"`).
        # extra_intents contains file paths extracted from shell commands via
        # CommandIntent parsing (L1+L3-Cmd).
        if permission != PermissionLevel.DENY:
            # Collect all file paths to check:
            # 1. Direct tool args (read_file, read_text_file)
            # 2. Paths from bash/shell commands (via extra_intents)
            file_paths_to_check: list[str] = []

            direct_path = extract_file_path_from_tool_args(tool_name, tool_args)
            if direct_path:
                file_paths_to_check.append(direct_path)

            # Extract paths from bash command intents
            if extra_intents:
                for intent in extra_intents:
                    action = getattr(intent, "action", None)
                    if action not in ("read", "write", "exec"):
                        continue
                    intent_paths = getattr(intent, "paths", None)
                    if intent_paths:
                        for p in intent_paths:
                            if isinstance(p, str) and p.strip():
                                file_paths_to_check.append(p.strip())

            # Check each path: KIA first, then RMS
            for file_path in file_paths_to_check:
                # KIA: ICPM path-based check (degrade-to-allow on error)
                try:
                    if await check_kia_file(file_path):
                        permission = PermissionLevel.DENY
                        matched_rule = "security_guard:kia"
                        break  # Stop checking on first DENY
                except Exception:
                    pass  # ICPM error — degrade to allow

                # RMS: local byte detection (only if KIA didn't deny)
                if permission != PermissionLevel.DENY:
                    rms_reason = detect_rms_file(file_path)
                    if rms_reason:
                        permission = PermissionLevel.DENY
                        matched_rule = "security_guard:rms"
                        break  # Stop checking on first DENY

        external_paths = [op.path for op in file_operations] if file_operations else None

        logger.info(
            "[PermissionEngine] permission.policy.result tool=%s permission=%s matched_rule=%s "
            "subcommand_results=%s file_operations=%s",
            tool_name,
            permission.value, matched_rule,
            [(item.text, item.permission.value) for item in subcommand_results]
            if subcommand_results else [],
            [(op.action, op.path, op.source) for op in file_operations]
            if file_operations else [],
        )

        result = PermissionResult(
            permission=permission,
            matched_rule=matched_rule,
            reason=self._get_reason(permission, tool_name, matched_rule),
            risk=None,
            external_paths=external_paths,
            subcommand_results=subcommand_results,
            file_operations=file_operations,
        )

        logger.info(
            "[PermissionEngine] permission.check.final tool=%s channel=%s permission=%s matched_rule=%s "
            "external_paths=%s",
            tool_name,
            channel_id,
            permission.value,
            matched_rule,
            external_paths or [],
        )
        return result

    @staticmethod
    def _evaluate_builtin_deny_only(
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> str | None:
        """Evaluate builtin dangerous-command deny regardless of enabled toggle.

        Use an empty policy config so only builtin rules can participate.
        """
        permission, matched_rule, _ = evaluate_tiered_policy_detailed({}, tool_name, tool_args)
        if permission != PermissionLevel.DENY:
            return None
        if not isinstance(matched_rule, str):
            return None
        if not matched_rule.startswith("tiered_policy:whole_command_deny:"):
            return None
        if "builtin[" not in matched_rule:
            return None
        return matched_rule

    # ---------- 辅助 ----------

    @staticmethod
    def _get_reason(
        permission: PermissionLevel, tool_name: str, matched_rule: str
    ) -> str:
        if permission == PermissionLevel.ALLOW:
            return f"Allowed by rule: {matched_rule}"
        if permission == PermissionLevel.DENY:
            if matched_rule == "security_guard:kia":
                return "文件包含涉密内容(KIA)，禁止读取"
            if matched_rule == "security_guard:rms":
                return "文件为RMS加密文档，禁止读取"
            builtin_reason = PermissionEngine._format_builtin_deny_reason(matched_rule)
            if builtin_reason is not None:
                return builtin_reason
            return f"Denied by rule: {matched_rule}"
        return f"Approval required for {tool_name} (rule: {matched_rule})"

    @staticmethod
    def _format_builtin_deny_reason(matched_rule: str | None) -> str | None:
        if not isinstance(matched_rule, str):
            return None
        if "tiered_policy:whole_command_deny:" not in matched_rule:
            return None
        builtin_ids = re.findall(r"builtin\[([^\]]+)\]", matched_rule)
        if not builtin_ids:
            return None

        descriptions_by_id: dict[str, str] = {}
        for rule in get_builtin_security_rules():
            if not isinstance(rule, dict):
                continue
            rid = str(rule.get("id") or "").strip()
            if not rid:
                continue
            desc = str(rule.get("description") or "").strip()
            if desc:
                descriptions_by_id[rid] = desc

        details = []
        for rid in sorted(set(builtin_ids)):
            desc = descriptions_by_id.get(rid)
            details.append(f"{rid}: {desc}" if desc else rid)
        return f"Denied by builtin dangerous rules: {'; '.join(details)}"


# ----- 全局单例 -----
_permission_engine: PermissionEngine | None = None


def init_permission_engine(config: dict | None = None) -> PermissionEngine:
    """初始化全局权限引擎。"""
    global _permission_engine
    if _permission_engine is None:
        _permission_engine = PermissionEngine(config)
    if config is not None:
        _permission_engine.update_config(config)
    return _permission_engine


def get_permission_engine() -> PermissionEngine:
    """获取全局权限引擎实例（懒初始化）。"""
    global _permission_engine
    if _permission_engine is None:
        _permission_engine = PermissionEngine()
    return _permission_engine


def set_permission_engine(engine: PermissionEngine):
    """替换全局权限引擎（测试用）。"""
    global _permission_engine
    _permission_engine = engine
