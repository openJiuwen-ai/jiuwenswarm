# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists → 沙箱运行时副本渲染（设计文档 4.4 双端同步）。

迁移后 security_lists 是用户文件/域名名单的**唯一事实源**；本模块把
user/cloud 记录按**当前模式**做格子解析（resolve_cell 回退链）后映射为
``windows-policy.runtime.yaml`` 用户段：

- file_path：``read=deny`` → ``deny_read``；``write=deny`` → ``deny_write``；
  ``allow`` 同理映射 ``allow_read``/``allow_write``；
  ``ask`` 无沙箱语义不渲染；``exec`` 轴沙箱不管控不渲染；
  同对象跨记录冲突 **deny 优先**（从 allow 列表剔除）。
- domain：``deny`` → ``egress.blocked_domains``；``allow`` → ``egress.allowed_domains``。
- ``network.disable_all`` 总开关不碰（``sandbox.network.set`` 独立通道）。

渲染后由调用方触发 box-server 重载（指纹变更才真正重启，见
``sandbox_config_rpc._apply_sandbox_change`` / ``JiuwenBoxRunner.ensure_running``）。
"""
from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.rails.security_lists import store
from jiuwenswarm.agents.harness.common.rails.security_lists.models import resolve_cell

logger = logging.getLogger(__name__)

#: 副本六列表键（filesystem 四轴 + egress 两轴）
_LIST_KEYS: tuple[str, ...] = (
    "allow_read",
    "allow_write",
    "deny_read",
    "deny_write",
    "allowed_domains",
    "blocked_domains",
)


def _current_mode() -> str:
    """当前生效模式（渲染视角）；读取异常回退 default（fail-safe）。"""
    from jiuwenswarm.common.permission_profile import current_permission_profile

    try:
        return current_permission_profile()
    except Exception:  # noqa: BLE001
        logger.warning("[security_lists] 读取当前模式失败，按 default 渲染", exc_info=True)
        return "default"


def collect_sandbox_lists(*, mode: str | None = None) -> dict[str, list[str]]:
    """user/cloud 记录 → 副本六列表（纯函数，不写盘）。

    ``mode`` 缺省=当前生效模式（决定格子解析取值）。
    """
    mode = mode or _current_mode()
    lists = store.get_security_lists()
    out: dict[str, list[str]] = {key: [] for key in _LIST_KEYS}

    def _add(key: str, value: str) -> None:
        if value not in out[key]:
            out[key].append(value)

    for rec in [*lists["user"], *lists["cloud"]["records"]]:
        if not rec.enabled:
            continue
        if rec.type == "file_path":
            read_v = resolve_cell(rec.cells, mode, "read")
            write_v = resolve_cell(rec.cells, mode, "write")
            if read_v == "deny":
                _add("deny_read", rec.pattern)
            elif read_v == "allow":
                _add("allow_read", rec.pattern)
            if write_v == "deny":
                _add("deny_write", rec.pattern)
            elif write_v == "allow":
                _add("allow_write", rec.pattern)
        elif rec.type == "domain":
            value = resolve_cell(rec.cells, mode, "*")
            if value == "deny":
                _add("blocked_domains", rec.pattern.lower())
            elif value == "allow":
                _add("allowed_domains", rec.pattern.lower())

    # deny 优先：同对象经不同记录同时落 allow/deny → 从 allow 剔除
    for allow_key, deny_key in (
        ("allow_read", "deny_read"),
        ("allow_write", "deny_write"),
        ("allowed_domains", "blocked_domains"),
    ):
        denied = set(out[deny_key])
        out[allow_key] = [v for v in out[allow_key] if v not in denied]
    return out


def render_sandbox_copy(*, mode: str | None = None) -> dict[str, int]:
    """把名单渲染进运行时副本用户段（保留 ``disable_all``），返回各类条数。

    复用 ``sandbox_policy_render`` 的副本读写与校验（原子写 + 非法条目跳过）。
    调用方负责触发 box-server 重载；本函数异常原样上抛（调用方 best-effort 捕获）。
    """
    from jiuwenswarm.server import sandbox_policy_render as spr

    wanted = collect_sandbox_lists(mode=mode)
    data = spr._load_copy()  # noqa: SLF001 - 同 server 包内复用副本读写
    fs = data["windows"]["filesystem"]
    fs["allow_read"] = spr._norm_file_paths(wanted["allow_read"])  # noqa: SLF001
    fs["allow_write"] = spr._norm_file_paths(wanted["allow_write"])  # noqa: SLF001
    fs["deny_read"] = spr._norm_file_paths(wanted["deny_read"])  # noqa: SLF001
    fs["deny_write"] = spr._norm_file_paths(wanted["deny_write"])  # noqa: SLF001
    egress = data["windows"]["network"]["egress"]
    egress["allowed_domains"] = spr._norm_domains(wanted["allowed_domains"])  # noqa: SLF001
    egress["blocked_domains"] = spr._norm_domains(wanted["blocked_domains"])  # noqa: SLF001
    spr._save_copy(data)  # noqa: SLF001
    counts = {key: len(wanted[key]) for key in _LIST_KEYS}
    logger.info("security_lists → 沙箱副本渲染完成: %s", counts)
    return counts


__all__ = [
    "collect_sandbox_lists",
    "render_sandbox_copy",
]
