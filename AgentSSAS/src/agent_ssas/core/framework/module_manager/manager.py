# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 检测模块管理器。

管理检测模块的完整生命周期:扫描、加载、注册、订阅管理、存储管理。
初始化时扫描 detection_modules/ 目录,加载所有模块的配置和插件。
流水线执行时提供订阅查询接口。

订阅模式(notify / auth):
检测模块订阅事件时,通过后缀语法区分订阅模式:
- "tool_input"(默认 notify 模式):异步检测,report_event 不等待结果。
- "tool_input:auth"(auth 模式):同步检测,report_event 必须等待结果。

聚合事件订阅展开:
聚合事件类型(one_toolcall_event / one_llmcall_event / one_interaction_event)
按关联的基础事件展开为带模式的子订阅。auth 模式作用在结束事件上,
起始事件始终为 notify。
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent_ssas.core.framework.config import AgentSSASConfig
from agent_ssas.core.framework.storage.module_store import ModuleStorageManager

logger = logging.getLogger(__name__)

# 订阅模式常量
MODE_NOTIFY = "notify"
MODE_AUTH = "auth"

# 聚合事件类型 → 关联的基础事件类型(有序:起始在前,结束在后)
_AGGREGATE_EVENT_MAP: dict[str, list[str]] = {
    "one_toolcall_event": ["tool_input", "tool_output"],
    "one_llmcall_event": ["llm_input", "llm_output", "tool_input", "tool_output"],
    "one_interaction_event": [
        "invoke_start", "invoke_end",
        "llm_input", "llm_output",
        "tool_input", "tool_output",
    ],
}

# 聚合事件的结束事件(auth 模式作用在此事件上)
_AGGREGATE_END_EVENT: dict[str, str] = {
    "one_toolcall_event": "tool_output",
    "one_llmcall_event": "llm_output",
    "one_interaction_event": "invoke_end",
}


@dataclass
class Subscription:
    """单条订阅记录。

    一条订阅记录表示某检测模块以指定模式订阅了某个基础事件类型。

    Attributes:
        module_name: 检测模块名。
        event_type: 基础事件类型(聚合事件展开后的基础事件)。
        mode: 订阅模式,"notify" 或 "auth"。
    """

    module_name: str
    event_type: str
    mode: str = MODE_NOTIFY


@dataclass
class DetectionModule:
    """检测模块实例。

    一个检测模块 = 1 个数据建模插件 + 1 个威胁分析插件 + 1 个存储管理器。
    由 DetectionModuleManager 在初始化时加载构建,供流水线模块查询和调用。

    Attributes:
        name: 检测模块名(唯一标识,与 module.yaml 的 name 字段对应)。
        config: 完整的 module.yaml 解析结果,包含 display_name、enabled、
            event_version、subscribed_events、modeler、analyzer 等字段。
        modeler: 数据建模插件实例,实现 DataModelerPlugin 协议。
        analyzer: 威胁分析插件实例,实现 ThreatAnalyzerPlugin 协议。
        storage: 该模块的独立存储管理器,包含 process.db 和 result.db。
    """

    name: str
    config: dict
    modeler: Any  # DataModelerPlugin 实例
    analyzer: Any  # ThreatAnalyzerPlugin 实例
    storage: ModuleStorageManager


class DetectionModuleManager:
    """检测模块管理器。

    管理检测模块的完整生命周期:扫描、加载、注册、订阅管理、存储管理。
    初始化时扫描 detection_modules/ 目录,加载所有模块的配置和插件。
    流水线执行时提供订阅查询接口,供流水线模块查询"哪些模块订阅了该事件"。

    订阅表结构:
    - _subscriptions: event_type → list[Subscription],存储每个基础事件类型的订阅列表。
    - _subscriber_counts: event_type → int,引用计数,标识事件类型是否有人订阅。
        聚合事件订阅会提升关联基础事件的引用计数。
    """

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化检测模块管理器。

        仅初始化内部数据结构,不执行扫描加载。实际加载由 initialize() 触发。

        Args:
            config: AgentSSAS 子系统配置,注入到管理器供读取存储路径等配置。
        """
        self._config = config
        # module_name → DetectionModule
        self._modules: dict[str, DetectionModule] = {}
        # event_type → list[Subscription]
        self._subscriptions: dict[str, list[Subscription]] = {}
        # event_type → int(引用计数,含聚合展开的基础事件)
        self._subscriber_counts: dict[str, int] = {}
        # module_name → ModuleStorageManager
        self._storages: dict[str, ModuleStorageManager] = {}

    async def initialize(self) -> None:
        """初始化:扫描、加载、注册所有检测模块。

        扫描 detection_modules/ 目录,遍历每个子目录,读取 module.yaml 配置,
        动态导入并实例化建模插件和分析插件,构建订阅表,创建存储管理器。
        单个模块加载失败时记录异常并跳过,不影响其他模块的加载。
        目录不存在时直接返回,视为无检测模块。
        """
        modules_dir = self._get_modules_dir()
        if not modules_dir.exists() or not modules_dir.is_dir():
            logger.info("检测模块目录不存在或不是目录: %s", modules_dir)
            return

        for module_dir in modules_dir.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("_"):
                continue
            # common 目录是通用组件目录,不是检测模块,跳过
            if module_dir.name == "common":
                continue
            module_yaml = module_dir / "module.yaml"
            if not module_yaml.exists():
                continue
            try:
                await self._load_module(module_dir, module_yaml)
            except Exception:
                logger.exception(
                    "加载检测模块失败: module_dir=%s", module_dir
                )

        # 校验 config.modules 中是否有未匹配的模块名(拼写错误或不存在的模块)
        if self._config.modules:
            loaded_names = set(self._modules.keys())
            for configured_name in self._config.modules:
                if configured_name not in loaded_names:
                    logger.warning(
                        "config.yaml 中配置的模块名 '%s' 未找到对应的检测模块,"
                        "请检查拼写是否正确",
                        configured_name,
                    )

    async def close(self) -> None:
        """关闭所有检测模块的存储连接。

        依次关闭各模块的 ModuleStorageManager(process.db / result.db)。
        单个模块关闭失败时记录异常并继续,不影响其他模块。
        供接入适配层在 backend.close() 时调用。
        """
        for name, storage in self._storages.items():
            try:
                await storage.close()
            except Exception:
                logger.exception("关闭检测模块存储失败: module=%s", name)

    async def cleanup_all(
        self, event_ttl_days: int, alert_ttl_days: int
    ) -> None:
        """按 TTL 清理所有检测模块存储的过期数据。

        遍历已加载模块的 ModuleStorageManager 执行清理:
        process.db 按 event_ttl_days,result.db 按 alert_ttl_days。
        单个模块清理失败时记录异常并继续,不影响其他模块。
        供接入适配层在 TTL 清理时机(启动 + 周期触发)调用。

        Args:
            event_ttl_days: 过程数据保留天数(<= 0 禁用)。
            alert_ttl_days: 结果数据保留天数(<= 0 禁用)。
        """
        for name, storage in self._storages.items():
            try:
                await storage.cleanup(event_ttl_days, alert_ttl_days)
            except Exception:
                logger.exception("清理检测模块存储失败: module=%s", name)

    def _get_modules_dir(self) -> Path:
        """获取 detection_modules 目录路径。

        从 agent_ssas 包安装路径推导,返回 agent_ssas/detection_modules 目录。
        优先使用 __path__ 属性(支持普通包和命名空间包),回退到 __file__。

        Returns:
            detection_modules 目录的 Path 对象。

        Raises:
            RuntimeError: 无法定位 agent_ssas 包路径时。
        """
        import agent_ssas.core as _core

        paths = getattr(_core, "__path__", None)
        if paths:
            return Path(next(iter(paths))) / "detection_modules"
        file = getattr(_core, "__file__", None)
        if file:
            return Path(file).parent / "detection_modules"
        raise RuntimeError("无法定位 agent_ssas 包路径")

    async def _load_module(self, module_dir: Path, module_yaml: Path) -> None:
        """加载单个检测模块。

        读取并解析 module.yaml 配置,检查 enabled 开关,
        动态导入并实例化建模插件和分析插件,创建存储管理器,
        注册到 _modules、_storages,并根据 subscribed_events 构建订阅表。

        Args:
            module_dir: 检测模块目录路径(如 .../detection_modules/test_detection/)。
            module_yaml: module.yaml 配置文件路径。

        Raises:
            ValueError: module.yaml 格式错误(缺少必需字段、缺少插件)时。
            ImportError: 插件类无法在框架预置目录或模块目录中找到时。
        """
        config = self._parse_module_yaml(module_yaml)
        # 支持 config.yaml 的 ssas.modules 段覆盖 module.yaml 的 enabled 字段
        module_name = config.get("name", "")
        if module_name in self._config.modules:
            module_override = self._config.modules[module_name]
            if isinstance(module_override, dict):
                if "enabled" in module_override:
                    config["enabled"] = module_override["enabled"]
        if not config.get("enabled", True):
            logger.info("检测模块未启用,跳过: %s", config.get("name"))
            return

        # 动态导入并实例化插件
        modeler = self._instantiate_plugin(config["modeler"], module_dir)
        analyzer = self._instantiate_plugin(config["analyzer"], module_dir)

        # 创建存储管理器
        ssas_home = Path(self._config.ssas_home) if self._config.ssas_home else None
        storage = ModuleStorageManager(config["name"], ssas_home)

        module = DetectionModule(
            name=config["name"],
            config=config,
            modeler=modeler,
            analyzer=analyzer,
            storage=storage,
        )
        self._modules[config["name"]] = module
        self._storages[config["name"]] = storage

        # 注册订阅(含聚合事件展开)
        subscribed_events = config.get("subscribed_events", ["*"])
        for event_spec in subscribed_events:
            self._register_subscription(config["name"], event_spec)

        logger.info(
            "检测模块加载完成: name=%s, events=%s",
            config["name"],
            config.get("subscribed_events", ["*"]),
        )

    def _register_subscription(self, module_name: str, event_spec: str) -> None:
        """注册单条订阅。

        解析事件规格字符串(含模式后缀),处理聚合事件展开,
        将展开后的基础事件订阅加入订阅表并递增引用计数。

        Args:
            module_name: 检测模块名。
            event_spec: 事件规格字符串,如 "tool_input"、"tool_input:auth"、
                "one_toolcall_event"、"one_toolcall_event:auth"。
        """
        # 解析事件规格:event_type[:mode]
        parts = event_spec.split(":", 1)
        event_type = parts[0]
        mode = parts[1] if len(parts) > 1 and parts[1] else MODE_NOTIFY

        # 通配符 * 直接注册
        if event_type == "*":
            self._add_subscription(Subscription(
                module_name=module_name,
                event_type="*",
                mode=MODE_NOTIFY,
            ))
            return

        # 聚合事件展开
        if event_type in _AGGREGATE_EVENT_MAP:
            base_events = _AGGREGATE_EVENT_MAP[event_type]
            end_event = _AGGREGATE_END_EVENT[event_type]
            for base_event in base_events:
                # auth 模式作用在结束事件上,其余为 notify
                base_mode = mode if base_event == end_event else MODE_NOTIFY
                self._add_subscription(Subscription(
                    module_name=module_name,
                    event_type=base_event,
                    mode=base_mode,
                ))
            return

        # 基础事件直接注册
        self._add_subscription(Subscription(
            module_name=module_name,
            event_type=event_type,
            mode=mode,
        ))

    def _add_subscription(self, sub: Subscription) -> None:
        """添加订阅记录到订阅表并递增引用计数。

        Args:
            sub: 订阅记录。
        """
        if sub.event_type not in self._subscriptions:
            self._subscriptions[sub.event_type] = []
        self._subscriptions[sub.event_type].append(sub)
        self._subscriber_counts[sub.event_type] = (
            self._subscriber_counts.get(sub.event_type, 0) + 1
        )

    def _parse_module_yaml(self, module_yaml: Path) -> dict[str, Any]:
        """解析 module.yaml 配置。

        读取 YAML 文件,校验必需字段,将 plugins 列表拆分为
        modeler(data_modeling 类型)和 analyzer(threat_analysis 类型)两个配置。

        Args:
            module_yaml: module.yaml 文件路径。

        Returns:
            解析后的配置字典,包含 name、display_name、enabled、event_version、
            subscribed_events、modeler、analyzer 字段。

        Raises:
            ValueError: YAML 根节点不是 dict、缺少 name 字段、
                缺少 data_modeling 或 threat_analysis 插件时。
            yaml.YAMLError: YAML 解析失败时。
        """
        with open(module_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(
                f"module.yaml 根节点必须为 dict: {module_yaml}"
            )

        # 校验必需字段
        name = data.get("name")
        if not name:
            raise ValueError(f"module.yaml 缺少 name 字段: {module_yaml}")

        # 处理 plugins 列表,按 type 拆分为 modeler 和 analyzer
        plugins = data.get("plugins", [])
        if not isinstance(plugins, list):
            raise ValueError(
                f"module.yaml 的 plugins 字段必须为 list: {module_yaml}"
            )

        modeler_config: dict[str, Any] | None = None
        analyzer_config: dict[str, Any] | None = None
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            ptype = plugin.get("type")
            if ptype == "data_modeling":
                modeler_config = plugin
            elif ptype == "threat_analysis":
                analyzer_config = plugin

        if not modeler_config:
            raise ValueError(
                f"module.yaml 缺少 data_modeling 插件: {module_yaml}"
            )
        if not analyzer_config:
            raise ValueError(
                f"module.yaml 缺少 threat_analysis 插件: {module_yaml}"
            )

        return {
            "name": name,
            "display_name": data.get("display_name", name),
            "enabled": data.get("enabled", True),
            "event_version": data.get("event_version", "1.0"),
            "subscribed_events": data.get("subscribed_events", ["*"]),
            "auth_timeout_policy": data.get("auth_timeout_policy", "allow"),
            "analytic_type_id": data.get("analytic_type_id", 0),
            "modeler": modeler_config,
            "analyzer": analyzer_config,
        }

    def _instantiate_plugin(
        self, plugin_config: dict[str, Any], module_dir: Path
    ) -> Any:
        """动态导入并实例化插件。

        先检查框架预置插件目录,再检查模块目录。
        框架预置插件位于 agent_ssas.core.framework.data_modeler.plugins 或
        agent_ssas.core.framework.threat_analyzer.plugins。
        检测模块专属插件位于 agent_ssas.core.detection_modules.<module_name>/ 下,
        文件名由类名转 snake_case 推导(如 SecurityRailAnalyzer → security_rail_analyzer)。

        实例化时优先尝试传入插件专有配置 dict(plugin_config["config"]),
        若构造函数不接受参数则回退到无参实例化,兼容现有空白插件。

        Args:
            plugin_config: 插件配置字典,包含 type、name、model_type/
                expected_model_type、config 等字段。
            module_dir: 检测模块目录路径,用于推导模块专属插件的导入路径。

        Returns:
            插件实例。

        Raises:
            ValueError: 插件配置缺少 name 或 type 字段、type 值无效时。
            ImportError: 插件类在框架预置目录和模块目录中均未找到时。
        """
        plugin_name = plugin_config.get("name")
        if not plugin_name:
            raise ValueError("插件配置缺少 name 字段")

        plugin_type = plugin_config.get("type")
        if plugin_type not in ("data_modeling", "threat_analysis"):
            raise ValueError(
                f"插件类型无效: {plugin_type},应为 data_modeling 或 threat_analysis"
            )

        # 框架预置插件模块
        if plugin_type == "data_modeling":
            framework_module = "agent_ssas.core.framework.data_modeler.plugins"
        else:
            framework_module = "agent_ssas.core.framework.threat_analyzer.plugins"

        cls = None

        # 先检查框架预置插件
        try:
            mod = importlib.import_module(framework_module)
            cls = getattr(mod, plugin_name, None)
        except ImportError:
            pass

        # 再检查模块目录
        if cls is None:
            module_name = module_dir.name
            file_name = self._to_snake_case(plugin_name)
            module_path = f"agent_ssas.core.detection_modules.{module_name}.{file_name}"
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, plugin_name, None)
            except ImportError:
                pass

        if cls is None:
            raise ImportError(
                f"无法找到插件: name={plugin_name}, type={plugin_type}"
            )

        # 实例化:优先传入插件专有配置,回退到无参实例化
        plugin_cfg = plugin_config.get("config", {})
        try:
            return cls(plugin_cfg)
        except TypeError:
            return cls()

    @staticmethod
    def _to_snake_case(name: str) -> str:
        """将 PascalCase 转换为 snake_case。

        用于根据插件类名推导文件名。例如:
        SecurityRailAnalyzer → security_rail_analyzer
        BlankDataModeler → blank_data_modeler

        Args:
            name: PascalCase 格式的类名。

        Returns:
            snake_case 格式的文件名。
        """
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    def get_subscribers_with_mode(self, event_type: str) -> list[tuple[str, str]]:
        """查询订阅了指定事件类型的检测模块列表(含模式)。

        返回直接订阅该事件类型的模块列表(含模式),加上通过通配符 "*" 订阅
        所有事件的模块列表。结果去重并保持插入顺序,避免同一模块重复出现。

        Args:
            event_type: 事件类型(如 "tool_input"、"invoke_start")。

        Returns:
            [(module_name, mode), ...] 列表。无订阅者时返回空列表。
            通配符订阅者的模式始终为 notify。
        """
        result: list[tuple[str, str]] = []
        seen: set[str] = set()

        # 直接订阅
        for sub in self._subscriptions.get(event_type, []):
            if sub.module_name not in seen:
                seen.add(sub.module_name)
                result.append((sub.module_name, sub.mode))

        # 通配 * 匹配所有事件(模式始终为 notify)
        for sub in self._subscriptions.get("*", []):
            if sub.module_name not in seen:
                seen.add(sub.module_name)
                result.append((sub.module_name, MODE_NOTIFY))

        return result

    def get_subscribers(self, event_type: str) -> list[str]:
        """查询订阅了指定事件类型的检测模块名列表。

        兼容方法,返回模块名列表(不含模式)。模式信息通过
        get_subscribers_with_mode 获取。

        Args:
            event_type: 类型(如 "tool_input"、"one_interaction_event")。

        Returns:
            订阅该事件类型的检测模块名列表。无订阅者时返回空列表。
        """
        return [name for name, _ in self.get_subscribers_with_mode(event_type)]

    def has_subscribers(self, event_type: str) -> bool:
        """判断指定事件类型是否有订阅者。

        基于引用计数判断。聚合事件订阅会提升关联基础事件的引用计数,
        因此即使某基础事件未被直接订阅,但聚合事件订阅了,也算有订阅者。

        Args:
            event_type: 事件类型。

        Returns:
            有订阅者返回 True,否则 False。
        """
        count = self._subscriber_counts.get(event_type, 0)
        if count > 0:
            return True
        # 通配符 * 订阅所有事件
        return self._subscriber_counts.get("*", 0) > 0

    def get_module(self, module_name: str) -> DetectionModule | None:
        """获取检测模块实例。

        Args:
            module_name: 检测模块名。

        Returns:
            DetectionModule 实例;模块未加载时返回 None。
        """
        return self._modules.get(module_name)

    def get_storage(self, module_name: str) -> ModuleStorageManager | None:
        """获取检测模块的存储管理器。

        Args:
            module_name: 检测模块名。

        Returns:
            该模块的 ModuleStorageManager 实例;模块未加载时返回 None。
        """
        return self._storages.get(module_name)
