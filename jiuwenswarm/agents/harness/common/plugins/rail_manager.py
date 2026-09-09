# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Rail Extension Manager - 管理用户自定义的 Rail 扩展."""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import importlib
import importlib.util
import json
import logging
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from jiuwenswarm.common.utils import get_agent_workspace_dir, get_multi_tenant_user_workspace_dir

logger = logging.getLogger(__name__)


@dataclass
class RailExtension:
    """Rail 扩展信息."""

    name: str  # 扩展名称 (文件夹名称)
    class_name: str = "CustomRail"  # Rail 类名 (从 rail.py 中提取)
    enabled: bool = True  # 是否启用
    description: str = ""  # 描述
    priority: int = 50  # 优先级

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "class_name": self.class_name,
            "enabled": self.enabled,
            "description": self.description,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RailExtension:
        return cls(
            name=data["name"],
            class_name=data.get("class_name", "CustomRail"),
            enabled=data.get("enabled", True),
            description=data.get("description", ""),
            priority=data.get("priority", 50),
        )


class RailManager:
    """Rail 扩展管理器（按租户 ``(service_id, agent_id, workspace_key)`` 分桶）."""

    def __init__(
        self,
        *,
        service_id: str = "default",
        agent_id: str = "default",
        workspace_key: str = "default",
    ):
        """初始化 Rail 管理器."""
        self.service_id = (service_id or "default").strip() or "default"
        self.agent_id = (agent_id or "default").strip() or "default"
        self.workspace_key = (workspace_key or "default").strip() or "default"
        self._extensions: dict[str, RailExtension] = {}

        self.extensions_dir.mkdir(parents=True, exist_ok=True)
        self._load_config()

        self._registered_rails: set[str] = set()
        self._agent_instance: Any = None
        self._rail_instances: dict[str, Any] = {}

        logger.info(
            "[RailManager] 初始化完成 tenant=(%s,%s,%s) 扩展目录: %s",
            self.service_id,
            self.agent_id,
            self.workspace_key,
            self.extensions_dir,
        )

    def dispose(self) -> None:
        """Drop in-memory rail state when the tenant is evicted from the pool."""
        self._rail_instances.clear()
        self._extensions.clear()
        self._registered_rails.clear()
        self._agent_instance = None

    def _resolve_extensions_dir(self) -> Path:
        import os
        if is_enterprise():
            workspace = get_multi_tenant_user_workspace_dir(self.workspace_key)
            return workspace / "agent" / "jiuwenclaw_workspace" / "extensions"
        return get_agent_workspace_dir() / "extensions"

    @property
    def extensions_dir(self) -> Path:
        return self._resolve_extensions_dir()

    @property
    def config_file(self) -> Path:
        return self._resolve_extensions_dir() / "extensions_config.json"

    def _load_config(self) -> None:
        """从配置文件加载扩展信息."""
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._extensions = {
                        name: RailExtension.from_dict(ext_data)
                        for name, ext_data in data.items()
                    }
                logger.info("[RailManager] 加载了 %d 个扩展配置", len(self._extensions))
            except Exception as e:
                logger.error("[RailManager] 加载配置文件失败: %s", e)
                self._extensions = {}
        else:
            self._extensions = {}

    def _save_config(self) -> None:
        """保存扩展信息到配置文件."""
        try:
            data = {
                name: ext.to_dict()
                for name, ext in self._extensions.items()
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug("[RailManager] 保存配置文件成功")
        except Exception as e:
            logger.error("[RailManager] 保存配置文件失败: %s", e)
            raise

    def list_extensions(self) -> List[dict]:
        """获取所有扩展列表."""
        return [ext.to_dict() for ext in self._extensions.values()]

    def import_extension(self, folder_path: str) -> dict:
        """导入一个新的 Rail 扩展（文件夹结构）.

        Args:
            folder_path: 扩展文件夹路径

        Returns:
            导入的扩展信息

        Raises:
            ValueError: 文件夹名称无效或结构不符合要求
            Exception: 其他错误
        """
        source_path = Path(folder_path)
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"文件夹不存在或不是目录: {folder_path}")

        # 获取文件夹名称
        name = source_path.name

        # 验证文件夹名称是否为有效的英文标识符
        if not name.isidentifier() or not name.isascii():
            raise ValueError(f"文件夹名称 '{name}' 必须是有效的英文标识符")

        # 检查是否已存在
        if name in self._extensions:
            raise ValueError(f"扩展 '{name}' 已存在")

        # 验证文件夹结构：必须包含 rail.py
        plugin_file = source_path / "rail.py"
        if not plugin_file.exists():
            raise ValueError(f"扩展文件夹必须包含 rail.py 文件")

        # 读取并验证 rail.py 内容
        try:
            with open(plugin_file, "r", encoding="utf-8") as f:
                plugin_content = f.read()
            self._validate_rail_file(plugin_content, name)
        except Exception as e:
            logger.error("[RailManager] rail.py 验证失败: %s", e)
            raise ValueError("rail.py 验证失败") from e

        # 复制整个文件夹到扩展目录
        dest_path = self.extensions_dir / name
        try:
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(source_path, dest_path)
            logger.info("[RailManager] 复制文件夹成功: %s -> %s", source_path, dest_path)
        except Exception as e:
            logger.error("[RailManager] 复制文件夹失败: %s", e)
            raise

        # 创建扩展记录
        class_name = self._extract_class_name(plugin_content, name)
        description = self._extract_description(plugin_content)
        priority = self._extract_priority(plugin_content)

        extension = RailExtension(
            name=name,
            class_name=class_name,
            enabled=False,
            description=description,
            priority=priority,
        )

        self._extensions[name] = extension
        self._save_config()

        logger.info("[RailManager] 导入扩展成功: %s", name)
        return extension.to_dict()

    @staticmethod
    def _validate_rail_file(file_str: str, name: str) -> None:
        """验证 Rail 文件内容是否有效.

        Args:
            file_str: 文件内容字符串
            name: 扩展名称

        Raises:
            ValueError: 文件内容无效
        """
        # 简单验证：文件中必须包含继承自 DeepAgentRail 或 AgentRail 的类
        required_patterns = ["DeepAgentRail", "AgentRail"]
        has_required_import = any(pattern in file_str for pattern in required_patterns)

        if not has_required_import:
            raise ValueError("文件必须包含对 DeepAgentRail 或 AgentRail 的导入")

        # 验证语法
        try:
            compile(file_str, f"{name}.py", "exec")
        except SyntaxError as e:
            logger.error("[RailManager] rail.py 验证失败: %s", e)
            raise ValueError("语法错误") from e

    @staticmethod
    def _extract_class_name(file_str: str, default_name: str) -> str:
        """从文件内容中提取 Rail 类名.

        Args:
            file_str: 文件内容字符串
            default_name: 默认类名 (使用扩展名的首字母大写形式)

        Returns:
            提取到的类名
        """
        # 尝试匹配 "class XXXRail(DeepAgentRail):" 或 "class XXXRail(AgentRail):"
        import re

        pattern = r"class\s+(\w+Rail)\s*\(\s*(DeepAgentRail|AgentRail)\s*\)"
        matches = re.findall(pattern, file_str)
        if matches:
            return matches[0][0]

        # 默认使用扩展名 + "Rail"
        return default_name.capitalize() + "Rail"

    @staticmethod
    def _extract_description(file_str: str) -> str:
        """从文件内容中提取描述信息.

        Args:
            file_str: 文件内容字符串

        Returns:
            提取到的描述
        """
        import re

        # 尝试匹配类文档字符串
        pattern = r'class\s+\w+Rail[^:]*:\s*"""([^"]*?)"""'
        match = re.search(pattern, file_str)
        if match:
            return match.group(1).strip()

        return ""

    @staticmethod
    def _extract_priority(file_str: str) -> int:
        """从文件内容中提取优先级.

        Args:
            file_str: 文件内容字符串

        Returns:
            提取到的优先级
        """
        import re

        # 尝试匹配 priority: int = XX
        pattern = r'priority\s*:\s*int\s*=\s*(\d+)'
        match = re.search(pattern, file_str)
        if match:
            return int(match.group(1))

        return 50  # 默认优先级

    def get_registered_rail_names(self) -> set[str]:
        """获取所有已注册的 rail 扩展名称集合.

        Returns:
            已注册的 rail 名称集合的副本
        """
        return self._registered_rails.copy()

    def delete_extension(self, name: str) -> bool:
        """删除一个扩展（整个文件夹）.

        Args:
            name: 扩展名称

        Returns:
            是否删除成功

        Raises:
            ValueError: 扩展不存在
        """
        if name not in self._extensions:
            raise ValueError(f"扩展 '{name}' 不存在")

        # 如果扩展已注册，从已注册集合中移除
        if name in self._registered_rails:
            self._registered_rails.discard(name)
            logger.info("[RailManager] 扩展 '%s' 从已注册集合中移除", name)

        # 清除缓存的实例
        if name in self._rail_instances:
            del self._rail_instances[name]
            logger.info("[RailManager] 扩展 '%s' 的缓存实例已清除", name)

        # 删除整个文件夹
        folder_path = self.extensions_dir / name
        if folder_path.exists():
            try:
                if folder_path.is_dir():
                    shutil.rmtree(folder_path)
                else:
                    folder_path.unlink()
            except Exception as e:
                logger.error("[RailManager] 删除文件夹失败: %s", e)
                raise

        # 删除扩展记录
        del self._extensions[name]
        self._save_config()

        logger.info("[RailManager] 删除扩展成功: %s", name)
        return True

    def toggle_extension(self, name: str, enabled: bool) -> dict:
        """切换扩展的启用状态（仅更新配置文件）.

        Args:
            name: 扩展名称
            enabled: 是否启用

        Returns:
            更新后的扩展信息

        Raises:
            ValueError: 扩展不存在
        """
        if name not in self._extensions:
            raise ValueError(f"扩展 '{name}' 不存在")

        self._extensions[name].enabled = enabled
        self._save_config()

        logger.info("[RailManager] 切换扩展状态（配置文件）: %s -> %s", name, enabled)
        return self._extensions[name].to_dict()

    def set_agent_instance(self, agent_instance: Any) -> None:
        """设置 DeepAgent 实例，用于热更新 rail."""
        self._agent_instance = agent_instance
        logger.info("[RailManager] DeepAgent 实例已设置")

    async def hot_reload_rail(self, name: str, enabled: bool) -> None:
        """热更新 rail：根据 enabled 状态注册或注销 rail 实例.

        Args:
            name: 扩展名称
            enabled: 是否启用

        Raises:
            ValueError: 扩展不存在或未设置 agent 实例
        """
        if name not in self._extensions:
            raise ValueError(f"扩展 '{name}' 不存在")

        if self._agent_instance is None:
            raise ValueError("DeepAgent 实例未设置，请先调用 set_agent_instance()")

        if enabled:
            # 开启：注册 rail
            if name in self._registered_rails:
                logger.warning("[RailManager] 扩展 '%s' 已注册，跳过", name)
                return

            try:
                rail_instance = self.load_rail_instance_without_enabled_check(name)
                await self._agent_instance.register_rail(rail_instance)
                self._registered_rails.add(name)
                logger.info("[RailManager] 成功注册 rail 扩展: %s", name)
            except Exception as e:
                logger.error("[RailManager] 注册 rail 扩展失败: %s, 错误: %s", name, e)
                raise
        else:
            # 关闭：注销 rail
            if name not in self._registered_rails:
                logger.warning("[RailManager] 扩展 %s 未注册，跳过", name)
                return

            try:
                rail_instance = self.load_rail_instance_without_enabled_check(name)
                await self._agent_instance.unregister_rail(rail_instance)
                self._registered_rails.discard(name)
                logger.info("[RailManager] 成功注销 rail 扩展: %s", name)
            except Exception as e:
                logger.error("[RailManager] 注销 rail 扩展失败: %s, 错误: %s", name, e)
                raise

    def is_rail_registered(self, name: str) -> bool:
        """检查 rail 是否已注册."""
        return name in self._registered_rails

    def get_extensions(self) -> List[dict]:
        """获取所有扩展列表."""
        return [ext.to_dict() for ext in self._extensions.values()]

    def load_rail_instance(self, name: str) -> Any:
        """动态加载并实例化 Rail（需要扩展已启用）.

        Args:
            name: 扩展名称

        Returns:
            Rail 实例

        Raises:
            ValueError: 扩展不存在或未启用
            Exception: 加载失败
        """
        if name not in self._extensions:
            raise ValueError(f"扩展 '{name}' 不存在")

        extension = self._extensions[name]
        if not extension.enabled:
            raise ValueError(f"扩展 '{name}' 未启用")

        return self._load_rail_instance_impl(name)

    def load_rail_instance_without_enabled_check(self, name: str) -> Any:
        """动态加载并实例化 Rail（不检查启用状态，用于热更新）.

        Args:
            name: 扩展名称

        Returns:
            Rail 实例

        Raises:
            ValueError: 扩展不存在
            Exception: 加载失败
        """
        if name not in self._extensions:
            raise ValueError(f"扩展 '{name}' 不存在")

        return self._load_rail_instance_impl(name)

    def _load_rail_class(self, name: str) -> type:
        """加载 Rail 类（不实例化，不缓存）."""
        extension = self._extensions[name]

        folder_path = self.extensions_dir / name
        plugin_file = folder_path / "rail.py"
        if not plugin_file.exists():
            raise ValueError(f"扩展插件文件 '{name}/rail.py' 不存在")

        try:
            module: Any
            if (folder_path / "__init__.py").exists():
                package_name = f"jiuwenswarm_rail_extension_{name}"
                package_spec = importlib.util.spec_from_file_location(
                    package_name,
                    folder_path / "__init__.py",
                    submodule_search_locations=[str(folder_path)],
                )
                if package_spec is None or package_spec.loader is None:
                    raise ValueError(f"无法加载包规范: {name}")

                package_module = importlib.util.module_from_spec(package_spec)
                sys.modules[package_name] = package_module
                package_spec.loader.exec_module(package_module)

                module_name = f"{package_name}.rail"
                rail_spec = importlib.util.spec_from_file_location(module_name, plugin_file)
                if rail_spec is None or rail_spec.loader is None:
                    raise ValueError(f"无法加载 Rail 模块: {name}")

                module = importlib.util.module_from_spec(rail_spec)
                sys.modules[module_name] = module
                rail_spec.loader.exec_module(module)
            else:
                spec = importlib.util.spec_from_file_location(
                    f"rail_extension_{name}", plugin_file
                )
                if spec is None or spec.loader is None:
                    raise ValueError(f"无法加载模块规范: {name}")

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

            rail_class = getattr(module, extension.class_name, None)
            if rail_class is None:
                raise ValueError(f"模块中未找到类: {extension.class_name}")

            return rail_class
        except ImportError as e:
            if "attempted relative import with no known parent package" in str(e):
                raise ValueError(
                    f"扩展 '{name}' 使用了相对导入但缺少 __init__.py 文件。"
                    f"请确保扩展文件夹中包含 __init__.py 文件以支持相对导入。"
                ) from e
            raise
        except Exception as e:
            logger.error("[RailManager] 加载 Rail 类失败: %s, 错误: %s", name, e)
            raise

    def _load_rail_instance_impl(self, name: str) -> Any:
        """加载 rail 实例的实现（缓存机制，确保主 agent 的 rail 只实例化一次）."""
        if name in self._rail_instances:
            logger.debug("[RailManager] 返回缓存的 Rail 实例: %s", name)
            return self._rail_instances[name]

        rail_class = self._load_rail_class(name)
        rail_instance = rail_class()
        self._rail_instances[name] = rail_instance
        logger.info("[RailManager] 加载并缓存 Rail 实例成功: %s", name)
        return rail_instance

    def create_fresh_rail_instance(self, name: str) -> Any:
        """为 team 子 agent 创建独立的 rail 实例（不使用缓存，每次返回新实例）.

        Args:
            name: 扩展名称

        Returns:
            新的 Rail 实例

        Raises:
            ValueError: 扩展不存在
            Exception: 加载失败
        """
        if name not in self._extensions:
            raise ValueError(f"扩展 '{name}' 不存在")

        rail_class = self._load_rail_class(name)
        rail_instance = rail_class()
        logger.debug("[RailManager] 创建新 Rail 实例（team 专用）: %s -> %s", name, rail_instance)
        return rail_instance


def get_rail_manager(scope: Any) -> RailManager:
    """获取租户级 Rail 管理器。

    ``scope`` must be a ``RuntimeScopeKey`` or ``(service_id, agent_id)`` tuple.
    For the default tenant pass ``RuntimeScopeKey.from_ids("default", "default")``.
    """
    if scope is None:
        raise TypeError(
            "get_rail_manager(scope) requires a non-None scope; "
            "for default tenant pass RuntimeScopeKey.from_ids('default', 'default')"
        )
    return RailManagerPool.get_or_create(scope)


class RailManagerPool:
    """Process-level pool of per-tenant RailManager instances."""

    _managers: dict[tuple[str, str, str], RailManager] = {}
    _lock = threading.Lock()

    @classmethod
    def _normalize_tenant(cls, scope: Any) -> tuple[str, str, str]:
        if scope is None:
            raise TypeError(
                "RailManagerPool requires a non-None scope; "
                "for default tenant pass RuntimeScopeKey.from_ids('default', 'default')"
            )
        tenant = getattr(scope, "tenant", None)
        if callable(tenant):
            parts = tenant()
            sid = parts[0] if len(parts) > 0 else "default"
            aid = parts[1] if len(parts) > 1 else "default"
            wk = parts[2] if len(parts) > 2 else getattr(scope, "workspace_key", "default")
            return (
                (str(sid or "default").strip() or "default"),
                (str(aid or "default").strip() or "default"),
                (str(wk or "default").strip() or "default"),
            )
        if isinstance(scope, (tuple, list)) and len(scope) >= 2:
            wk = scope[2] if len(scope) >= 3 else "default"
            return (
                (str(scope[0] or "default").strip() or "default"),
                (str(scope[1] or "default").strip() or "default"),
                (str(wk or "default").strip() or "default"),
            )
        sid = getattr(scope, "service_id", None)
        aid = getattr(scope, "agent_id", None)
        wk = getattr(scope, "workspace_key", None)
        if sid is not None or aid is not None or wk is not None:
            return (
                (str(sid or "default").strip() or "default"),
                (str(aid or "default").strip() or "default"),
                (str(wk or "default").strip() or "default"),
            )
        raise TypeError(
            f"unsupported rail manager scope type: {type(scope)!r}; "
            "expected RuntimeScopeKey or (service_id, agent_id[, workspace_key])"
        )

    @classmethod
    def get_or_create(cls, scope: Any) -> RailManager:
        key = cls._normalize_tenant(scope)
        with cls._lock:
            mgr = cls._managers.get(key)
            if mgr is None:
                mgr = RailManager(service_id=key[0], agent_id=key[1], workspace_key=key[2])
                cls._managers[key] = mgr
            return mgr

    @classmethod
    def remove(cls, service_id: str, agent_id: str, workspace_key: str = "default") -> bool:
        """Evict one tenant RailManager from the process pool."""
        key = (
            (str(service_id or "default").strip() or "default"),
            (str(agent_id or "default").strip() or "default"),
            (str(workspace_key or "default").strip() or "default"),
        )
        with cls._lock:
            mgr = cls._managers.pop(key, None)
        if mgr is None:
            return False
        try:
            mgr.dispose()
        except Exception:
            logger.warning(
                "[RailManagerPool] dispose failed tenant=(%s,%s,%s)",
                key[0],
                key[1],
                key[2],
                exc_info=True,
            )
        return True

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._lock:
            cls._managers.clear()


__all__ = [
    "RailExtension",
    "RailManager",
    "RailManagerPool",
    "get_rail_manager",
]
