# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Policy file reader for loading security policies from YAML files.

Shared by SandboxManager and ProxyManager to avoid duplicate policy loading logic.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import yaml

from jiuwenbox.bundled_configs import base_policy_path
from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.policy_engine import PolicyEngine, read_policy_text

configure_logging()
logger = logging.getLogger(__name__)

JIUWENBOX_POLICY_PATH_ENV = "JIUWENBOX_POLICY_PATH"


def _resolve_tool_paths(policy: SecurityPolicy) -> SecurityPolicy:
    """Windows 下自动检测 tool_paths 空字段, 用 sys.executable 反推.

    基底 windows-policy.yaml 随 wheel 打包, tool_paths. 
    这里在 load_policy 内存合并后, 对空的 tool_paths
    字段做运行时检测填充: agent-server 进程用 OfficeAce 预制 python 跑 →
    sys.executable 推出 tools 目录.

    只填空字段: 基底/副本显式配的值不覆盖. 检测到的路径校验 os.path.isdir,
    无效则跳过该字段 (降级到留空, 由后续依赖系统 PATH 的逻辑兜底). 不落盘
    (符合 load_policy 不生成合并文件的机制).

    非 win32 直接返回原 policy (tool_paths 仅 Windows 用).
    """
    if sys.platform != "win32":
        return policy
    try:
        fs = policy.windows.filesystem
        tp = fs.tool_paths
    except AttributeError:
        return policy
    filled: dict[str, str] = {}

    # python_dir: sys.executable 所在目录
    if not (tp.python_dir or "").strip():
        try:
            py_dir = str(Path(sys.executable).parent.resolve())
            if Path(py_dir, "python.exe").is_file():
                filled["python_dir"] = py_dir
        except OSError:
            pass

    # node_dir: 从 python_dir 往上找 tools/node (OfficeAce 结构: tools/python + tools/node).
    if not (tp.node_dir or "").strip() and filled.get("python_dir"):
        py_dir = Path(filled["python_dir"])
        # python_dir 形如 <root>/tools/python → node 在 <root>/tools/node.
        # P2-41: 限定只查 py_dir.parent, 不遍历到根 (OfficeAce 标准结构).
        for ancestor in (py_dir.parent,):
            cand = ancestor / "node"
            if (cand / "node.exe").is_file():
                filled["node_dir"] = str(cand)
                break

    # git_dir / bash_path: OfficeAce 包未必带 git, 从 PATH 检测; 检测不到留空.
    if not (tp.git_dir or "").strip():
        git_exe = shutil.which("git")
        if git_exe:
            # git.exe 多在 <git_root>/cmd 或 <git_root>/bin 或 mingw64/bin;
            # git_dir 期望是安装根 (含 usr/bin/bash.exe).
            git_path = Path(git_exe).resolve()
            for ancestor in (git_path.parent, *git_path.parents):
                if (ancestor / "usr" / "bin" / "bash.exe").is_file():
                    filled["git_dir"] = str(ancestor)
                    if not (tp.bash_path or "").strip():
                        filled["bash_path"] = str(ancestor / "usr" / "bin" / "bash.exe")
                    break

    if not filled:
        return policy
    # 用 model_copy 更新 (SecurityPolicy/ToolPaths 是 pydantic model, 不可变约束下用 copy).
    new_tp = tp.model_copy(update=filled)
    new_fs = fs.model_copy(update={"tool_paths": new_tp})
    new_windows = policy.windows.model_copy(update={"filesystem": new_fs})
    logger.info(
        "tool_paths 自动检测填充: %s (python_dir=via sys.executable, git_dir=via PATH)",
        ", ".join(f"{k}={v}" for k, v in filled.items()),
    )
    return policy.model_copy(update={"windows": new_windows})

# Top-level YAML keys that don't represent sandbox-related configuration.
# A policy file whose effective sandbox-config keys are empty (i.e. its
# top-level key set is a subset of ``_META_KEYS | {"inference_privacy_proxies"}``)
# is treated as proxy-only and the server skips sandbox initialisation.
_META_KEYS: frozenset[str] = frozenset({"version", "name"})
_PROXY_ONLY_ALLOWED_KEYS: frozenset[str] = _META_KEYS | {"inference_privacy_proxies"}


class PolicyReader:
    """Reads security policy from YAML files."""

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        policy_path: Path | None = None,
    ) -> None:
        self.policy_engine = policy_engine or PolicyEngine()
        if policy_path is not None:
            self.policy_path = Path(policy_path)
            self._policy_source = "constructor"
        else:
            self.policy_path = self._resolve_policy_path()
            if os.environ.get(JIUWENBOX_POLICY_PATH_ENV):
                self._policy_source = JIUWENBOX_POLICY_PATH_ENV
            else:
                self._policy_source = "bundled default"
        self._log_resolved_policy_path()

    def _log_resolved_policy_path(self) -> None:
        try:
            resolved = self.policy_path.resolve()
        except OSError:
            resolved = self.policy_path
        if resolved.exists():
            logger.info(
                "Loading security policy from %s (%s)",
                resolved,
                self._policy_source,
            )
        else:
            logger.warning(
                "Security policy file not found at %s (%s); "
                "will fall back to SecurityPolicy defaults on load",
                resolved,
                self._policy_source,
            )

    @staticmethod
    def _resolve_policy_path() -> Path:
        """副本 (user_config) 路径: ``JIUWENBOX_POLICY_PATH`` env 指向 workspace 下
        的稀疏用户副本; 未设则回落打包基底 (退化为"只读基底"行为, 兼容旧用法).
        """
        env_path = os.environ.get(JIUWENBOX_POLICY_PATH_ENV)
        if env_path:
            return Path(env_path).expanduser()
        # 未配副本: 回落基底 (兼容旧用法, 退化为只读基底, 无 user_config 合并).
        return base_policy_path()

    def load_policy(self) -> SecurityPolicy:
        """读基底 (框架 default, 打包随 wheel) + 副本 (用户 user_config) 合并.

        - 基底: ``base_policy_path()`` (windows-policy.yaml / default-policy.yaml),
          随 wheel 升级, 提供 default 值; 热更新场景新字段经此生效.
        - 副本: ``self.policy_path`` (``JIUWENBOX_POLICY_PATH`` env 指向 workspace 下
          稀疏 user_config, 只存用户可配字段, 用 policy 字段名 e.g.
          ``windows.filesystem.allow_read`` / ``windows.network.egress.allowed_domains``).
          副本不存在 → 只读基底 (退化为无 user_config).
        - 合并: ``policy_engine.merge_policy(基底, 副本)`` — dict 深合并, list 追加去重
          (用户白名单叠加基底必需集, 不丢); 不生成合并文件 (与 jiuwenclaw config.yaml
          template+override 机制对齐, 但用 list 追加语义而非替换).
        """
        base_path = base_policy_path()
        try:
            base_data = yaml.safe_load(read_policy_text(base_path)) or {}
        except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
            # OSError = 文件不可读/不存在; yaml.YAMLError = 语法错误 (如双引号内
            # 非法 \U 转义). 基底随 wheel 打包一般不坏, 但兜底回落 SecurityPolicy
            # 默认值, 不阻断启动 (与 is_proxy_only 的 except 范式一致).
            logger.warning(
                "Base policy %s unreadable (%s); falling back to SecurityPolicy defaults",
                base_path, exc,
            )
            base_data = {}
        if not isinstance(base_data, dict):
            base_data = {}
        base_policy = SecurityPolicy.model_validate(base_data)

        # 无副本 / 副本路径等于基底 (未配 env) → 直接用基底.
        if not self.policy_path.exists() or (
            self.policy_path.resolve() == base_path.resolve()
        ):
            return _resolve_tool_paths(base_policy)

        # 有副本: 合并基底 + 副本 (副本用户配置叠加基底; list 追加, dict 深合并).
        try:
            override_data = yaml.safe_load(read_policy_text(self.policy_path)) or {}
        except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
            # OSError = 副本不可读; yaml.YAMLError = 副本语法错误 (如双引号内 Windows 路径 C:\Users\... 的 \U 被当转义).
            # 副本是用户可写文件易被手改坏. 回落基底而非抛异常 (否则抛到 app.py 外层致 win_proxy 不启动 + 沙箱用空 policy, P1-14).
            # WS 接口写的副本经 safe_dump 不会触发 (plain scalar 不解析 \U).
            logger.warning(
                "User policy copy %s unreadable (%s); using base only "
                "(若手改过副本, 检查 YAML 语法: 双引号内 Windows 路径反斜杠需用 "
                "单引号或正斜杠, 如 'C:\\Users\\...' 或 C:/Users/...)",
                self.policy_path, exc,
            )
            return _resolve_tool_paths(base_policy)
        if not isinstance(override_data, dict) or not override_data:
            return _resolve_tool_paths(base_policy)

        return _resolve_tool_paths(
            self.policy_engine.merge_policy(base_policy, override_data)
        )

    def load_policy_from_file(self, path: Path) -> SecurityPolicy:
        """从单文件加载 (不合并, 用于 per-sandbox policy 文件)."""
        return self.policy_engine.load_policy_from_file(path)

    def is_proxy_only(self) -> bool:
        """Return True iff the YAML file only configures the inference proxy.

        "Proxy-only" means the operator wants jiuwenbox to act purely as an
        inference privacy router: the YAML's top-level keys are limited to
        :data:`_PROXY_ONLY_ALLOWED_KEYS` and the proxy listener is actually
        enabled (``listen_port > 0``). When this is the case the server skips
        the sandbox subsystem entirely (no ``ProcessRuntime``, no idle
        reaper, no zombie reaper) and only runs the proxy lifecycle.
        """
        if not self.policy_path.exists():
            return False
        try:
            data = yaml.safe_load(read_policy_text(self.policy_path))
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        top_keys = set(data.keys())
        if not top_keys.issubset(_PROXY_ONLY_ALLOWED_KEYS):
            return False
        proxy_section = data.get("inference_privacy_proxies")
        if not isinstance(proxy_section, dict):
            return False
        try:
            port = int(proxy_section.get("listen_port", 0) or 0)
        except (TypeError, ValueError):
            return False
        return port > 0
