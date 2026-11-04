# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""openjiwen / openjiuwen_runtime 日志引导（gateway 自包含版本）。

与母仓 ``jiuwenswarm.common.utils.configure_*`` 语义一致；gateway 包不依赖
jiuwenswarm，独立安装时同样把第三方日志钉到 infra 区，避免相对 cwd 落盘。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from gateway.common.paths import get_config_dir, get_logs_dir

logger = logging.getLogger(__name__)


def configure_openjiuwen_runtime_logging() -> None:
    """把 openjiuwen_runtime foundation 文件日志钉到 infra/logs/runtime。

    SDK 自带 logging.yaml 的 file handler filename 相对进程 cwd 解析，须在
    首次 ``get_logger()`` 之前调用；入口进程应在模块顶部调用。
    失败仅告警，不抛出。
    """
    try:
        from openjiuwen_runtime.foundation.log import setup_logging

        setup_logging(
            log_file=str(get_logs_dir() / "runtime" / "openjiuwen_runtime.log")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("openjiuwen runtime logging bootstrap failed: %s", exc)


def configure_openjiuwen_core_logging(*, suppress_level: Optional[int] = None) -> bool:
    """把 openjiwen core LogManager 日志钉到 infra/logs/core。

    默认 ``log_path`` 相对进程 cwd 解析，须在任何 ``LogManager.get_logger()``
    之前调用：``infra/config/logging.yaml`` 存在则整份加载，否则按备用 dict
    配置（jiuwen.log 与 interface/、performance/ 子目录均相对 log_path 落盘）。
    失败仅告警，不抛出。

    Args:
        suppress_level: 用户未自管 ``logging.yaml`` 时，将全部 openjiwen core
            logger 级别压至该值；``None`` 表示不压制。

    Returns:
        True 表示已应用用户自管的 ``logging.yaml``；yaml 缺失、加载失败
        回退内置配置或整体失败时为 False。
    """
    user_managed = False
    _logging_yaml = get_config_dir() / "logging.yaml"
    if _logging_yaml.exists():
        try:
            from openjiuwen.core.common.logging.log_config import configure_log

            configure_log(str(_logging_yaml))
            user_managed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "openjiwen core logging: failed to load %s; using built-in config: %s",
                _logging_yaml,
                exc,
            )
    if not user_managed:
        try:
            from openjiuwen.core.common.logging.log_config import configure_log_config

            # 本引导先于 openjiwen 导入执行：reset 后的业务 logger 均在之后才创建，
            # 压制意图必须进全局配置快照（level 供 reset 后新建 logger 继承），
            # 仅 set_level 存量 logger 压不住后续创建的。
            configure_log_config({
                "backend": "default",
                "level": logging.getLevelName(
                    suppress_level if suppress_level is not None else logging.INFO
                ),
                "log_path": f"{get_logs_dir() / 'core'}/",
                "log_file": "jiuwen.log",
                "output": ["console", "file"],
                "structured_output_format": "json",
                "interface_log_file": "interface/jiuwen_interface.log",
                "prompt_builder_interface_log_file": "interface/jiuwen_prompt_builder_interface.log",
                "performance_log_file": "performance/jiuwen_performance.log",
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("openjiwen core logging bootstrap failed: %s", exc)
        if suppress_level is not None:
            from openjiuwen.core.common.logging import LogManager

            for core_logger in LogManager.get_all_loggers().values():
                core_logger.set_level(suppress_level)
    return user_managed
