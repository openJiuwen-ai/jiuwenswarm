# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Rail Extension Manager - 管理用户自定义的 Rail 扩展."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import shutil
import sys
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from jiuwenswarm.common.utils import get_agent_workspace_dir

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
    """Rail 扩展管理器."""

    _instance = None
    _extensions_dir: Path
    _config_file: Path
    _extensions: dict[str, RailExtension] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化 Rail 管理器."""
        if hasattr(self, "_initialized"):
            return

        self._extensions_dir = get_agent_workspace_dir() / "extensions"
        self._config_file = self._extensions_dir / "extensions_config.json"

        # 确保目录存在
        self._extensions_dir.mkdir(parents=True, exist_ok=True)

        # 加载配置
        self._load_config()

        # 跟踪已注册的rail扩展名称
        self._registered_rails: set[str] = set()
        # DeepAgent 实例引用，用于 register/unregister
        self._agent_instance: Any = None
        # 注册状态必须按 DeepAgent 实例隔离。agent 模式会先创建模板 Agent，
        # 再为每个 session 创建独立 Agent；若只用全局名称集合，后者会被误判为
        # “已注册”而跳过。弱引用避免 session Agent 回收后被管理器持有。
        self._agent_rail_instances: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
            weakref.WeakKeyDictionary()
        )
        # Rail class 与实例分开缓存：不同 Agent 需要独立实例，但扩展模块只应加载一次。
        self._rail_classes: dict[str, type] = {}
        self._rail_class_lock = threading.RLock()
        # 缓存已加载的 rail 实例，确保同一个 rail 只实例化一次
        self._rail_instances: dict[str, Any] = {}
        # 串行化注册、注销与删除，避免删除过程中又有新 Agent 注册同一扩展。
        self._lifecycle_lock = asyncio.Lock()

        self._initialized = True
        logger.info("[RailManager] 初始化完成，扩展目录: %s", self._extensions_dir)

    def _load_config(self) -> None:
        """从配置文件加载扩展信息."""
        if self._config_file.exists():
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
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
            data = {name: ext.to_dict() for name, ext in self._extensions.items()}
            with open(self._config_file, "w", encoding="utf-8") as f:
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
        dest_path = self._extensions_dir / name
        try:
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(source_path, dest_path)
            logger.info(
                "[RailManager] 复制文件夹成功: %s -> %s", source_path, dest_path
            )
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
        pattern = r"priority\s*:\s*int\s*=\s*(\d+)"
        match = re.search(pattern, file_str)
        if match:
            return int(match.group(1))

        return 50  # 默认优先级

    def get_registered_rail_names(self) -> set[str]:
        """获取所有已注册的 rail 扩展名称集合.

        Returns:
            已注册的 rail 名称集合的副本
        """
        names = {
            name
            for rails_by_name in self._agent_rail_instances.values()
            for name in rails_by_name
        }
        self._registered_rails = names
        return names.copy()

    async def delete_extension(self, name: str) -> bool:
        """删除一个扩展（整个文件夹）.

        删除磁盘文件和配置前，会先从所有仍存活的 Agent 注销对应 Rail。
        任一注销失败时保留扩展与失败 Agent 的实例记录，便于重试。

        Args:
            name: 扩展名称

        Returns:
            是否删除成功

        Raises:
            ValueError: 扩展不存在
            RuntimeError: 扩展无法从一个或多个 Agent 注销
        """
        async with self._lifecycle_lock:
            if name not in self._extensions:
                raise ValueError(f"扩展 '{name}' 不存在")
            await self._unregister_extension_from_agents(name)

            self._registered_rails.discard(name)
            self.invalidate_rail_cache(name)

            # 删除整个文件夹
            folder_path = self._extensions_dir / name
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

    async def _unregister_extension_from_agents(self, name: str) -> None:
        """从所有活跃 Agent 注销扩展，且仅在成功后移除实例记录."""
        registrations = [
            (agent, rails_by_name[name])
            for agent, rails_by_name in list(self._agent_rail_instances.items())
            if name in rails_by_name
        ]
        failures: list[Exception] = []

        for agent, rail_instance in registrations:
            try:
                await agent.unregister_rail(rail_instance)
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)
                logger.error(
                    "[RailManager] 从 Agent 注销扩展失败: %s, agent_id=%s, 错误: %s",
                    name,
                    id(agent),
                    exc,
                )
                continue

            rails_by_name = self._agent_rail_instances.get(agent)
            if rails_by_name is not None and rails_by_name.get(name) is rail_instance:
                del rails_by_name[name]

        self.get_registered_rail_names()
        if failures:
            raise RuntimeError(
                f"扩展 '{name}' 无法从 {len(failures)} 个 Agent 注销，已保留扩展以便重试"
            ) from failures[0]

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

    async def hot_reload_rail(
        self,
        name: str,
        enabled: bool,
        *,
        agent_instance: Any | None = None,
    ) -> None:
        """热更新 rail：根据 enabled 状态注册或注销 rail 实例.

        Args:
            name: 扩展名称
            enabled: 是否启用
            agent_instance: 显式目标 DeepAgent。省略时兼容使用
                ``set_agent_instance()`` 设置的实例。并发创建 session Agent
                时应显式传入，避免目标串线。

        Raises:
            ValueError: 扩展不存在或未设置 agent 实例
        """
        target_agent = (
            agent_instance if agent_instance is not None else self._agent_instance
        )
        if target_agent is None:
            raise ValueError("DeepAgent 实例未设置，请先调用 set_agent_instance()")

        async with self._lifecycle_lock:
            if name not in self._extensions:
                raise ValueError(f"扩展 '{name}' 不存在")
            await self._hot_reload_rail_for_agent(name, enabled, target_agent)

    async def _hot_reload_rail_for_agent(
        self,
        name: str,
        enabled: bool,
        target_agent: Any,
    ) -> None:
        """在生命周期锁内为单个 Agent 注册或注销 Rail."""
        rails_by_name = self._agent_rail_instances.setdefault(target_agent, {})

        if enabled:
            # 开启：注册 rail
            if name in rails_by_name:
                logger.warning(
                    "[RailManager] 扩展 '%s' 已在当前 Agent 注册，跳过", name
                )
                return

            try:
                # Rail 可持有会话上下文、工具注册状态等实例字段，不能跨 Agent 复用。
                rail_instance = self.create_fresh_rail_instance(name)
                await target_agent.register_rail(rail_instance)
                rails_by_name[name] = rail_instance
                self._registered_rails.add(name)
                logger.info("[RailManager] 成功注册 rail 扩展: %s", name)
            except Exception as e:
                logger.error("[RailManager] 注册 rail 扩展失败: %s, 错误: %s", name, e)
                raise
        else:
            # 关闭：注销 rail
            if name not in rails_by_name:
                logger.warning("[RailManager] 扩展 %s 未在当前 Agent 注册，跳过", name)
                return

            try:
                rail_instance = rails_by_name[name]
                await target_agent.unregister_rail(rail_instance)
                del rails_by_name[name]
                if not any(
                    name in registered
                    for registered in self._agent_rail_instances.values()
                ):
                    self._registered_rails.discard(name)
                logger.info("[RailManager] 成功注销 rail 扩展: %s", name)
            except Exception as e:
                logger.error("[RailManager] 注销 rail 扩展失败: %s, 错误: %s", name, e)
                raise

    def is_rail_registered(
        self,
        name: str,
        *,
        agent_instance: Any | None = None,
    ) -> bool:
        """检查 rail 是否已注册."""
        if agent_instance is not None:
            return name in self._agent_rail_instances.get(agent_instance, {})
        return name in self.get_registered_rail_names()

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

    def invalidate_rail_cache(self, name: str) -> None:
        """清除扩展的 class、主实例与动态模块缓存，供真正代码重载使用."""
        with self._rail_class_lock:
            class_removed = self._rail_classes.pop(name, None) is not None
            instance_removed = self._rail_instances.pop(name, None) is not None

            module_roots = (
                f"jiuwenswarm_rail_extension_{name}",
                f"rail_extension_{name}",
            )
            removed_modules = 0
            for module_name in tuple(sys.modules):
                if any(
                    module_name == root or module_name.startswith(f"{root}.")
                    for root in module_roots
                ):
                    sys.modules.pop(module_name, None)
                    removed_modules += 1

        if class_removed or instance_removed or removed_modules:
            logger.info(
                "[RailManager] 扩展 '%s' 缓存已清除: class=%s, instance=%s, modules=%d",
                name,
                class_removed,
                instance_removed,
                removed_modules,
            )

    def _load_rail_class(self, name: str) -> type:
        """加载并缓存 Rail 类，避免创建独立实例时重复执行扩展模块."""
        with self._rail_class_lock:
            if name in self._rail_classes:
                logger.debug("[RailManager] 返回缓存的 Rail 类: %s", name)
                return self._rail_classes[name]

            try:
                rail_class = self._load_rail_class_uncached(name)
            except Exception:
                # 避免失败的包导入在 sys.modules 中留下半初始化模块，阻塞后续重试。
                self.invalidate_rail_cache(name)
                raise
            self._rail_classes[name] = rail_class
            logger.info("[RailManager] 加载并缓存 Rail 类成功: %s", name)
            return rail_class

    def _load_rail_class_uncached(self, name: str) -> type:
        """从扩展目录执行模块并返回 Rail 类，不读取或写入 class 缓存."""
        extension = self._extensions[name]

        folder_path = self._extensions_dir / name
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
                module = sys.modules.get(module_name)
                if module is None:
                    rail_spec = importlib.util.spec_from_file_location(
                        module_name,
                        plugin_file,
                    )
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
        """从缓存的 Rail class 创建独立实例（不复用实例）.

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
        logger.debug("[RailManager] 创建独立 Rail 实例: %s -> %s", name, rail_instance)
        return rail_instance


def get_rail_manager() -> RailManager:
    """获取 Rail 管理器单例."""
    return RailManager()
