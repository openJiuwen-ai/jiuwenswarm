# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""NPU 硬件监控：执行 npu-smi 并解析为结构化状态。

仅供 web 查询（hardware.npu.status），无独立管理器、无驻留进程；
机器无 npu-smi（非昇腾环境）时返回 available=False 优雅降级。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_NPU_SMI_TIMEOUT_SEC = 10

# npu-smi 的常见绝对路径（昇腾驱动默认安装位置），按优先级探测
_NPU_SMI_CANDIDATES = (
    "/usr/local/Ascend/driver/tools/npu-smi",
    "/usr/local/bin/npu-smi",
    "/usr/bin/npu-smi",
)


def _npu_smi_path() -> str | None:
    """解析 npu-smi 的绝对路径；未安装时返回 None。"""
    for path in _NPU_SMI_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # 兜底：经 PATH 解析为绝对路径后再执行，不直接依赖 PATH 查找
    return shutil.which("npu-smi")

# npu-smi info 数据行：| <id> <name...> | <health/bus-id> | <数值组...> |
# 表头第一段为 "NPU     Name"（非数字开头），借此区分数据行。
_BUS_ID_RE = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-9A-Fa-f]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _split_row(line: str) -> list[str] | None:
    """把 `| a | b | c |` 形式的行拆成三段；非表格行返回 None。"""
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    parts = [p.strip() for p in stripped.strip("|").split("|")]
    return parts if len(parts) == 3 else None


def _numbers(text: str) -> list[float]:
    return [float(m) for m in _NUMBER_RE.findall(text)]


def parse_npu_smi_info(output: str) -> list[dict[str, Any]]:
    """解析 `npu-smi info` 表格输出为 NPU 状态列表。

    每组数据为一个 NPU 行（第二段是健康状态）后跟若干 Chip 行
    （第二段是 Bus-Id）；NPU 级利用率/显存取第一颗 Chip 的值。
    解析不到的字段为 None，不因单行格式异常中断整体解析。
    """
    npus: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in output.splitlines():
        parts = _split_row(line)
        if parts is None:
            continue
        first, second, third = parts
        if not first or not first.split()[0].isdigit():
            continue  # 表头（NPU/Chip 标题行）与分隔行
        if second.isdigit():
            continue  # 进程表行（第二段为进程 id），健康状态不会是纯数字

        if _BUS_ID_RE.match(second):
            # Chip 行（无 HBM 列，如 310P）：0  0 | 0000:C1:00.0 | 12  2620 / 65536
            # Chip 行（有 HBM 列，如 910B）：0    | 0000:C1:00.0 | 0  0 / 0  3546 / 65536
            if current is None:
                continue
            tokens = first.split()
            values = _numbers(third)
            memory_used = values[1] if len(values) > 1 else None
            memory_total = values[2] if len(values) > 2 else None
            hbm_used = values[3] if len(values) > 3 else None
            hbm_total = values[4] if len(values) > 4 else None
            # 910 等训练卡的 Memory-Usage(DDR) 列恒为 0 / 0，真实显存在 HBM-Usage 列
            if not memory_total and hbm_total:
                memory_used, memory_total = hbm_used, hbm_total
            chip = {
                "chip_id": int(tokens[0]),
                "device": tokens[1] if len(tokens) > 1 else None,
                "bus_id": second,
                "aicore_percent": values[0] if len(values) > 0 else None,
                "memory_used_mb": memory_used,
                "memory_total_mb": memory_total,
            }
            current["chips"].append(chip)
            if current["aicore_percent"] is None:
                current["aicore_percent"] = chip["aicore_percent"]
                current["memory_used_mb"] = chip["memory_used_mb"]
                current["memory_total_mb"] = chip["memory_total_mb"]
                current["bus_id"] = chip["bus_id"]
            continue

        # NPU 行：0       910B3    | OK | 92.5    42    0 / 0
        tokens = first.split()
        values = _numbers(third)
        current = {
            "id": int(tokens[0]),
            "name": tokens[1] if len(tokens) > 1 else None,
            "health": second or None,
            "power_w": values[0] if len(values) > 0 else None,
            "temp_c": values[1] if len(values) > 1 else None,
            "hugepages_used": int(values[2]) if len(values) > 2 else None,
            "hugepages_total": int(values[3]) if len(values) > 3 else None,
            "bus_id": None,
            "aicore_percent": None,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "chips": [],
        }
        npus.append(current)

    return npus


def query_npu_status() -> dict[str, Any]:
    """执行 npu-smi info 并返回结构化状态（同步，调用方负责线程切换）。"""
    npu_smi = _npu_smi_path()
    if npu_smi is None:
        return {"available": False, "npus": [],
                "reason": "npu-smi 命令不存在（非昇腾 NPU 环境或未安装驱动工具）"}
    try:
        result = subprocess.run(
            [npu_smi, "info"],
            timeout=_NPU_SMI_TIMEOUT_SEC,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        return {"available": False, "npus": [],
                "reason": "npu-smi 命令不存在（非昇腾 NPU 环境或未安装驱动工具）"}
    except subprocess.TimeoutExpired:
        return {"available": False, "npus": [],
                "reason": f"npu-smi 执行超时（{_NPU_SMI_TIMEOUT_SEC} 秒）"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "npus": [], "reason": f"npu-smi 执行失败：{exc}"}

    if result.returncode != 0:
        output = (result.stdout or "").strip()[-300:]
        return {"available": False, "npus": [],
                "reason": f"npu-smi 返回错误（exit {result.returncode}）：{output}"}

    npus = parse_npu_smi_info(result.stdout or "")
    if not npus:
        return {"available": False, "npus": [], "reason": "未从 npu-smi 输出中解析到 NPU 设备"}
    return {"available": True, "npus": npus, "reason": ""}


async def handle_hardware_npu_status(params: dict) -> dict:
    """web 通道 ``hardware.npu.status`` 的 handler（只读）。"""
    return await asyncio.to_thread(query_npu_status)


__all__ = ["parse_npu_smi_info", "query_npu_status", "handle_hardware_npu_status"]
