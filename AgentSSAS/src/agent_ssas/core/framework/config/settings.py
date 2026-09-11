# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 配置模块。

从 jiuwenswarm 的 config.yaml 的 ssas 段加载配置,
环境变量覆盖(SSAS_ 前缀),注入到各模块。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class AgentSSASMode(str, Enum):
    """SSAS 运行模式枚举。

    INPROCESS: 进程内库模式,零网络延迟。
    HTTP: 独立 HTTP 服务模式。
    """

    INPROCESS = "inprocess"
    HTTP = "http"


@dataclass
class AgentSSASConfig:
    """AgentSSAS 子系统配置。

    从 config.yaml 的 ssas 段加载,环境变量覆盖。
    注入到各模块(接入适配、数据预处理、流水线、
    数据建模、威胁分析、存储、呈现、检测模块管理器)。
    """

    # 全局开关
    enabled: bool = True

    # 运行模式
    mode: AgentSSASMode = AgentSSASMode.INPROCESS

    # HTTP 模式
    http_endpoint: str = "http://localhost:8443"
    http_timeout: float = 5.0
    http_token: str | None = None
    http_host: str = "0.0.0.0"
    http_port: int = 8443

    # 存储(遵循 JIUWENSWARM_HOME 约定)
    # None 时按 SSAS_HOME → JIUWENSWARM_DATA_DIR → JIUWENSWARM_HOME → ~/.jiuwenswarm 顺序解析
    ssas_home: str | None = None
    storage_backend: str = "sqlite"
    event_ttl_days: int = 30
    alert_ttl_days: int = 90

    # Rail 配置
    rail_priority: int = 80
    enable_exception_hooks: bool = True
    risk_report_threshold: str = "low"  # safe / low / medium / high / critical

    # auth 模式超时(秒):auth 订阅者超过此时间未返回时,按模块默认策略处理
    auth_timeout: float = 2.0

    # AgentSSASSecurityRail 决策策略模式名
    # observe_only: 仅观察,全部放行(默认,避免二次阻断)
    # active_protection: 按风险等级处理(critical→阻断, high/medium/low→告警)
    # 策略定义在 decision_policies.yaml 中
    decision_policy: str = "observe_only"

    # 各模块插件专有配置(预留扩展)
    modules: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _resolve_ssas_home(self) -> Path:
        """解析存储根目录。

        按 SSAS_HOME → JIUWENSWARM_DATA_DIR → JIUWENSWARM_HOME → ~/.jiuwenswarm
        顺序解析,返回第一个非空环境变量对应的路径,否则回退到用户主目录。
        """
        home = os.environ.get("SSAS_HOME")
        if home:
            return Path(home)
        home = os.environ.get("JIUWENSWARM_DATA_DIR")
        if home:
            return Path(home)
        home = os.environ.get("JIUWENSWARM_HOME")
        if home:
            return Path(home)
        return Path.home() / ".jiuwenswarm"

    @property
    def storage_path(self) -> str:
        """存储路径,从 ssas_home 推导。

        ssas_home 为 None 时自动解析环境变量。
        返回 ssas_home / "ssas" 的字符串形式。
        """
        home = self.ssas_home or self._resolve_ssas_home()
        return str(Path(home) / "ssas")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AgentSSASConfig:
        """从 dict 加载配置。

        用于从 config.yaml 的 ssas 段加载配置。
        仅识别已知字段,忽略未知字段。mode 字段自动转换为 AgentSSASMode 枚举。
        加载后应用 SSAS_ 前缀环境变量覆盖。

        Args:
            data: 配置字典,对应 config.yaml 的 ssas 段内容。

        Returns:
            AgentSSASConfig 实例。

        Raises:
            TypeError: data 不是 dict 类型时。
        """
        if data is None:
            config = cls()
            config._apply_env_overrides()
            return config
        if not isinstance(data, dict):
            raise TypeError(f"data 必须为 dict,实际类型: {type(data).__name__}")

        # 已知字段集合
        known_fields = {
            "enabled",
            "mode",
            "http_endpoint",
            "http_timeout",
            "http_token",
            "http_host",
            "http_port",
            "ssas_home",
            "storage_backend",
            "event_ttl_days",
            "alert_ttl_days",
            "rail_priority",
            "enable_exception_hooks",
            "risk_report_threshold",
            "auth_timeout",
            "decision_policy",
            "modules",
        }

        # 构造构造参数,仅取已知字段
        kwargs: dict[str, Any] = {}
        for key in known_fields:
            if key in data:
                kwargs[key] = data[key]

        # mode 字段转换为 AgentSSASMode 枚举
        if "mode" in kwargs:
            mode_value = kwargs["mode"]
            if isinstance(mode_value, str):
                # 兼容字符串形式的 mode
                try:
                    kwargs["mode"] = AgentSSASMode(mode_value)
                except ValueError:
                    # 未知 mode 值,保留原值让 dataclass 校验
                    kwargs["mode"] = mode_value
            elif not isinstance(mode_value, AgentSSASMode):
                # 既不是字符串也不是 AgentSSASMode,保留原值让 dataclass 校验
                kwargs["mode"] = mode_value

        config = cls(**kwargs)
        config._apply_env_overrides()
        return config

    def _apply_env_overrides(self) -> None:
        """应用 SSAS_ 前缀环境变量覆盖配置。

        环境变量优先级高于 config.yaml 中的配置值。
        支持 SSAS_ 前缀 + 字段名(大写)格式,如 SSAS_MODE、SSAS_ENABLED。
        存储路径相关环境变量(SSAS_HOME 等)不带 SSAS_ 前缀,由 _resolve_ssas_home 处理。
        """
        # bool 字段
        _BOOL_FIELDS = {
            "SSAS_ENABLED": "enabled",
            "SSAS_ENABLE_EXCEPTION_HOOKS": "enable_exception_hooks",
        }
        for env_key, field_name in _BOOL_FIELDS.items():
            val = os.environ.get(env_key)
            if val is not None:
                setattr(self, field_name, val.lower() == "true")

        # int 字段
        _INT_FIELDS = {
            "SSAS_RAIL_PRIORITY": "rail_priority",
            "SSAS_HTTP_PORT": "http_port",
            "SSAS_EVENT_TTL_DAYS": "event_ttl_days",
            "SSAS_ALERT_TTL_DAYS": "alert_ttl_days",
        }
        for env_key, field_name in _INT_FIELDS.items():
            val = os.environ.get(env_key)
            if val is not None:
                try:
                    setattr(self, field_name, int(val))
                except ValueError:
                    pass

        # float 字段
        _FLOAT_FIELDS = {
            "SSAS_HTTP_TIMEOUT": "http_timeout",
            "SSAS_AUTH_TIMEOUT": "auth_timeout",
        }
        for env_key, field_name in _FLOAT_FIELDS.items():
            val = os.environ.get(env_key)
            if val is not None:
                try:
                    setattr(self, field_name, float(val))
                except ValueError:
                    pass

        # SSAS_MODE 特殊处理(枚举)
        mode_val = os.environ.get("SSAS_MODE")
        if mode_val:
            try:
                self.mode = AgentSSASMode(mode_val)
            except ValueError:
                pass

        # 字符串字段
        _STR_FIELDS = {
            "SSAS_HTTP_ENDPOINT": "http_endpoint",
            "SSAS_HTTP_HOST": "http_host",
            "SSAS_HTTP_TOKEN": "http_token",
            "SSAS_HOME": "ssas_home",
            "SSAS_DECISION_POLICY": "decision_policy",
            "SSAS_RISK_REPORT_THRESHOLD": "risk_report_threshold",
        }
        for env_key, field_name in _STR_FIELDS.items():
            val = os.environ.get(env_key)
            if val is not None:
                setattr(self, field_name, val)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentSSASConfig:
        """从 config.yaml 文件加载配置。

        读取 YAML 文件,提取 ssas 段,调用 from_dict 构造配置。
        from_dict 内部会应用 SSAS_ 前缀环境变量覆盖。

        Args:
            path: config.yaml 文件路径。

        Returns:
            AgentSSASConfig 实例。

        Raises:
            FileNotFoundError: 文件不存在时。
            yaml.YAMLError: YAML 解析失败时。
            TypeError: ssas 段不是 dict 时。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # 提取 ssas 段,如果文件中没有 ssas 段则使用默认配置
        if not isinstance(data, dict):
            raise TypeError(f"配置文件根节点必须为 dict,实际类型: {type(data).__name__}")

        ssas_section = data.get("ssas", {})
        return cls.from_dict(ssas_section)
