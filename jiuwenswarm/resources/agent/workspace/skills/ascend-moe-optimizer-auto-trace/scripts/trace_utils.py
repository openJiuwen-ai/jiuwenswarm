#!/usr/bin/env python3
"""
Profiling 数据工具集：从 base.h 读取宏配置、核类型映射、profiling tensor 拆分保存。
"""

import logging
import os
import re
from typing import Callable, List, Optional

import torch

logger = logging.getLogger(__name__)

# Lazy state for mapping helpers (avoid pylint "protected member" on function objects)
_m1c2v_offset: Optional[int] = None
_sequence_prefix: Optional[List[int]] = None


def _ensure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")


def get_define_value_from_file(filepath: str, macro_name: str):
    path = os.path.normpath(os.path.expanduser(filepath))
    if not os.path.isfile(path):
        return None
    pattern = re.compile(
        rf"^\s*#\s*define\s+{re.escape(macro_name)}\s+([^\\/].*?)(?:\s*//.*|$)",
        re.IGNORECASE,
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("//") or line.startswith("/*"):
                    continue
                m = pattern.match(line)
                if m:
                    expr = m.group(1).strip().strip("()")
                    return eval(expr, {"__builtins__": {}, "True": True, "False": False})
    except Exception as exc:
        logger.debug("could not parse macro from %s: %s", path, exc)
    return None


def _script_dir() -> str:
    d = os.path.dirname(__file__)
    return d if d else os.getcwd()


def get_define_value_from_base(macro_name: str, base_h_path: Optional[str] = None):
    if base_h_path and os.path.isfile(base_h_path):
        return get_define_value_from_file(base_h_path, macro_name)
    # Fallback path is UMDK-example layout; other operators MUST pass base_h_path to save_profiling_data.
    candidates = [
        os.path.join(
            _script_dir(),
            "../../../src/cam/comm_operator/ascend_kernels/fused_deep_moe/op_kernel",
            "fused_deep_moe_base.h",
        ),
    ]
    for c in candidates:
        v = get_define_value_from_file(c, macro_name)
        if v is not None:
            return v
    return None


def get_prof_size_per_core(base_h_path: Optional[str] = None) -> int:
    v = get_define_value_from_base("PROF_SIZE_PER_CORE", base_h_path)
    return int(v) if v is not None else 2048


def get_enable_moe_profiling(base_h_path: Optional[str] = None) -> bool:
    v = get_define_value_from_base("ENABLE_MOE_PROFILING", base_h_path)
    return False if v is None else v != 0


def get_core_num_list() -> List[int]:
    use_1c2v = os.environ.get("MOE_USE_1C2V", "0")
    if use_1c2v == "1":
        return [24, 24, 24]
    return [24, 48]


def mapping_with_1c2v(gid: int, idx: int) -> int:
    global _m1c2v_offset
    if _m1c2v_offset is None:
        _m1c2v_offset = get_core_num_list()[0]
    if gid == 0:
        return idx
    elif gid == 1:
        return _m1c2v_offset + 2 * idx
    elif gid == 2:
        return _m1c2v_offset + 2 * idx + 1
    raise ValueError(f"Invalid group id {gid}")


def mapping_with_sequence(gid: int, idx: int) -> int:
    global _sequence_prefix
    if _sequence_prefix is None:
        cnl = get_core_num_list()
        _sequence_prefix = [sum(cnl[:pos]) for pos in range(len(cnl))]
    return _sequence_prefix[gid] + idx


def group_by_mapping(
    profiling: torch.Tensor,
    group_sizes: List[int],
    mapping_func: Callable[[int, int], int] = mapping_with_sequence,
) -> List[torch.Tensor]:
    total_cores = profiling.size(0)
    groups = []
    for gid, size in enumerate(group_sizes):
        indices = [mapping_func(gid, i) for i in range(size)]
        if max(indices) >= total_cores or min(indices) < 0:
            raise ValueError(f"Mapping out of range: group {gid} indices {indices}")
        groups.append(profiling[indices])
    return groups


def save_profiling_data(
    profiling_raw: torch.Tensor,
    rank_id: int,
    output_dir: str = "profiling_data",
    base_h_path: Optional[str] = None,
):
    if not get_enable_moe_profiling(base_h_path):
        return

    _ensure_logging()
    core_num_list = get_core_num_list()
    prof_size = get_prof_size_per_core(base_h_path)
    total_cores = sum(core_num_list)
    required_len = total_cores * prof_size

    profiling = profiling_raw.view(torch.int64).flatten()[:required_len].view(total_cores, prof_size)
    group_map = mapping_with_1c2v if len(core_num_list) == 3 else mapping_with_sequence
    split_tensors = group_by_mapping(profiling, core_num_list, group_map)

    base = _script_dir()
    out_dir = output_dir if os.path.isabs(output_dir) else os.path.join(base, output_dir)
    os.makedirs(out_dir, exist_ok=True)

    out_path = os.path.join(out_dir, f"rank{rank_id:03d}.pt")
    torch.save(split_tensors, out_path)
    logger.info("Saved: %s (%d core types)", out_path, len(split_tensors))
