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

**单一写入者不变式**：本模块是运行时副本六列表的**唯一**写入者。沙箱面板的
``sandbox.files.set`` / ``sandbox.network.set`` 已收敛为写 ``security_lists``
（经 :func:`store.replace_records_by_origin`），再回到本模块渲染——所以副本里
不会再有"名单不知道的条目"。

> 历史：收敛之前副本有两个独立写入者，本模块的整段替换会把面板配的 ACL **静默
> 抹掉**（实测复现过）。当时加过一道"会丢条目就不渲染"的拦截护栏；但收敛之后
> 那道护栏会**反过来死锁**——面板删掉一条路径时副本里还留着它，护栏会判定
> "会丢条目"从而拒绝渲染，删除永远不生效。故降级为**告警不拦截**
> （``security.list.render.dropped``）。若日志里出现它，说明又有别的写入者
> 在改副本，应当排查而不是放行。
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

    若本次渲染会丢掉副本里已有、而名单不知道的条目 → **照常渲染**（本模块是副本的
    唯一写入者，正常不该出现），但 WARNING + 审计留痕 ``security.list.render.dropped``——
    出现即说明又有别的写入者在改副本，应排查。
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
            "security_lists → 沙箱副本渲染丢弃了副本里未纳管的条目 %s。"
            "本模块应是副本六列表的唯一写入者，出现这条说明另有写入者，请排查",
            lost,
        )
        audit.log_event(
            audit.AUDIT_RENDER_DROPPED,
            reason="unmanaged_entries_overwritten",
            mode=mode or "",
            dropped=lost,
        )

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
