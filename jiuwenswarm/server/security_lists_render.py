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

**为何渲染前要先做"会不会丢条目"的检查**：副本有**两个独立写入者**——本模块，
以及 ``sandbox_policy_render``（沙箱面板 / ``sandbox.files.set`` / ``.network.set`` /
``/add-dir`` / FileGuard sync）。本模块是**整段替换**六列表，数据只来自
``security_lists``；只要副本里存在名单不知道的条目，替换就会把它们**静默抹掉**，
还会触发 box-server 重载——用户的沙箱 ACL/egress 就真的没了。

实测过的丢失路径：用户先在沙箱面板配了 ACL → 启动时的一次性迁移已经跑过（标记存在），
于是这些新条目**不会**再被导入名单 → 之后用户在安全中心改任意一条规则 →
本模块渲染 → 沙箱面板配的 ACL 消失。

因此这里加一道**安全护栏**：若本次渲染会丢掉副本里已有的条目，就**不渲染**，
WARNING + 审计留痕（``security.list.render.skipped``）。取舍是明确的：

- 代价：名单里的增删暂时下发不到沙箱（**护栏 rail 不受影响**，它直读名单），
  其中包括"从名单里删除"暂时无法同步到沙箱；
- 收益：不会静默毁掉用户的沙箱策略。

正解是**写面收敛**（沙箱面板改写 ``security_lists``，副本不再有未纳管条目），
届时这道护栏自然失效、可以删掉。
"""
from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.rails.security_lists import audit, store
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


def _copy_lists(data: Any, spr: Any) -> dict[str, list[str]]:
    """副本六列表现值，用**与写入相同的归一化**（保证比较口径一致，不产生假差异）。"""
    win = data.get("windows") if isinstance(data, dict) else None
    win = win if isinstance(win, dict) else {}
    fs = win.get("filesystem") if isinstance(win.get("filesystem"), dict) else {}
    net = win.get("network") if isinstance(win.get("network"), dict) else {}
    egress = net.get("egress") if isinstance(net.get("egress"), dict) else {}
    return {
        "allow_read": spr._norm_file_paths(fs.get("allow_read") or []),  # noqa: SLF001
        "allow_write": spr._norm_file_paths(fs.get("allow_write") or []),  # noqa: SLF001
        "deny_read": spr._norm_file_paths(fs.get("deny_read") or []),  # noqa: SLF001
        "deny_write": spr._norm_file_paths(fs.get("deny_write") or []),  # noqa: SLF001
        "allowed_domains": spr._norm_domains(egress.get("allowed_domains") or []),  # noqa: SLF001
        "blocked_domains": spr._norm_domains(egress.get("blocked_domains") or []),  # noqa: SLF001
    }


def render_sandbox_copy(*, mode: str | None = None) -> dict[str, Any]:
    """把名单渲染进运行时副本用户段（保留 ``disable_all``），返回各类条数。

    复用 ``sandbox_policy_render`` 的副本读写与校验（原子写 + 非法条目跳过）。
    调用方负责触发 box-server 重载；本函数异常原样上抛（调用方 best-effort 捕获）。

    若本次渲染会丢掉副本里已有、而名单不知道的条目 → **不渲染**（见模块 docstring），
    返回里带 ``skipped=True``，各计数为**副本当前**条数（不是名单条数）。
    """
    from jiuwenswarm.server import sandbox_policy_render as spr

    wanted = collect_sandbox_lists(mode=mode)
    data = spr._load_copy()  # noqa: SLF001 - 同 server 包内复用副本读写
    current = _copy_lists(data, spr)
    dropped = {
        key: [v for v in current[key] if v not in set(wanted[key])]
        for key in _LIST_KEYS
    }
    lost = {key: values for key, values in dropped.items() if values}
    if lost:
        logger.warning(
            "security_lists → 沙箱副本渲染已跳过：会丢弃副本里未纳管的条目 %s。"
            "这些条目来自沙箱面板 / sandbox.files|network.set / FileGuard sync，"
            "覆盖会静默清空用户策略；请改用写面收敛（沙箱面板写 security_lists）",
            lost,
        )
        audit.log_event(
            audit.AUDIT_RENDER_SKIPPED,
            reason="would_drop_unmanaged_entries",
            mode=mode or "",
            dropped=lost,
        )
        counts: dict[str, Any] = {key: len(current[key]) for key in _LIST_KEYS}
        counts["skipped"] = True
        return counts

    fs = data["windows"]["filesystem"]
    fs["allow_read"] = spr._norm_file_paths(wanted["allow_read"])  # noqa: SLF001
    fs["allow_write"] = spr._norm_file_paths(wanted["allow_write"])  # noqa: SLF001
    fs["deny_read"] = spr._norm_file_paths(wanted["deny_read"])  # noqa: SLF001
    fs["deny_write"] = spr._norm_file_paths(wanted["deny_write"])  # noqa: SLF001
    egress = data["windows"]["network"]["egress"]
    egress["allowed_domains"] = spr._norm_domains(wanted["allowed_domains"])  # noqa: SLF001
    egress["blocked_domains"] = spr._norm_domains(wanted["blocked_domains"])  # noqa: SLF001
    spr._save_copy(data)  # noqa: SLF001
    rendered: dict[str, Any] = {key: len(wanted[key]) for key in _LIST_KEYS}
    logger.info("security_lists → 沙箱副本渲染完成: %s", rendered)
    return rendered


__all__ = [
    "collect_sandbox_lists",
    "render_sandbox_copy",
]
