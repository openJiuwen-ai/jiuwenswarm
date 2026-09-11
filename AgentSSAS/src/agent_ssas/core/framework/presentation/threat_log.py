# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 威胁日志呈现插件。

AgentSSASThreatLog 接收简化格式的威胁分析报告,结合报告中的
aux_ids、event_node 等信息,调用 ocsf.build_ocsf_report 构建完整的
OCSF Detection Finding 格式报告,落盘存储为 JSON 文件。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.presentation.ocsf import build_ocsf_report


class AgentSSASThreatLog:
    """威胁日志呈现插件。

    输入简化格式的威胁分析报告,结合事件数据构建完整的
    OCSF Detection Finding 格式报告,落盘存储。

    0.1 版本简化实现:trace_id 关联查询暂不实际查询存储模块,
    related_events 传空列表,后续版本可接入存储模块完成关联查询。
    """

    name = "AgentSSASThreatLog"

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化威胁日志呈现插件。

        从 config 的 storage_path 推导输出目录,在
        `<storage_path>/reports/threat_log` 下落盘存储报告 JSON 文件。

        Args:
            config: AgentSSASConfig 配置实例,用于解析存储根目录。
        """
        self._config = config
        # 输出目录:<storage_path>/reports/threat_log
        self._output_dir = Path(config.storage_path) / "reports" / "threat_log"
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def render(self, report: dict) -> None:
        """构建 OCSF 格式报告并落盘。

        从 report 中获取 aux_ids(包含 trace_id、session_id 等)和
        event_node,调用 build_ocsf_report 构建完整的 OCSF 格式报告,
        落盘存储为 JSON 文件。

        Args:
            report: 威胁分析报告(简化格式),包含风险检测结果
                及 aux_ids(trace_id、session_id 等关联字段)、
                event_node 等信息。

        Raises:
            TypeError: report 不是 dict 类型时。
            ValueError: report 为空 dict 时。
        """
        if not isinstance(report, dict):
            raise TypeError(
                f"report 必须为 dict,实际类型: {type(report).__name__}"
            )
        if not report:
            raise ValueError("report 不能为空 dict")

        # 构建完整的 OCSF 格式报告
        ocsf_event = self._to_ocsf(report)

        # 落盘存储,文件名包含 trace_id、module_name 和本地时区可读时间
        # (YYYYMMDD_HHMMSS_mmm,冒号替换为下划线保证 Windows 文件名合法,
        # 毫秒保留避免同秒覆盖)
        aux_ids = report.get("aux_ids", {})
        trace_id = aux_ids.get("trace_id", "")
        safe_trace_id = trace_id or "notrace"
        module_name = report.get("module_name", "unknown")
        now = time.time()
        # DTZ006: 刻意使用本地时区(便于运维直接查看),非 UTC
        now_dt = datetime.fromtimestamp(now)  # noqa: DTZ006
        time_text = now_dt.strftime("%Y%m%d_%H%M%S") + f"_{int(now * 1000) % 1000:03d}"
        file_path = (
            self._output_dir
            / f"threat_{safe_trace_id}_{module_name}_{time_text}.json"
        )
        await self._write_json(file_path, ocsf_event)

    def _to_ocsf(self, report: dict) -> dict:
        """构建 OCSF Detection Finding 格式。

        委托 ocsf.build_ocsf_report 完成实际构建,此方法保留
        作为插件的扩展点,子类可覆写以定制格式。

        Args:
            report: 威胁分析报告(简化格式),包含 aux_ids、event_node、
                has_risk、risk_level、module_name、title、description 等。

        Returns:
            OCSF Detection Finding 格式 dict。
        """
        return build_ocsf_report(report)

    async def _write_json(self, file_path: Path, data: dict) -> None:
        """异步写入 JSON 文件。

        通过 asyncio.to_thread 将同步文件 I/O 操作放到线程中执行,
        避免阻塞事件循环。写入时使用 ensure_ascii=False 以支持中文,
        indent=2 保证可读性。

        Args:
            file_path: 目标 JSON 文件路径。
            data: 待写入的 dict 数据。
        """
        await asyncio.to_thread(self._write_json_sync, file_path, data)

    @staticmethod
    def _write_json_sync(file_path: Path, data: dict) -> None:
        """同步写入 JSON 文件(供 _write_json 通过线程调用)。

        使用 utf-8 编码写入,ensure_ascii=False 支持中文,
        indent=2 保证可读性。

        Args:
            file_path: 目标 JSON 文件路径。
            data: 待写入的 dict 数据。
        """
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
