# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一安全名单的**对外规则判定接口**（供出入管控等执行面调用）。

定位：本模块是规则对外的唯一出口——执行面（宿主 HTTP 出口逐跳校验、沙箱
egress / 文件 ACL 渲染）在动手前调用这里拿当刻裁决，而不是自己去读
``permissions.net_guard`` / ``permissions.file_guard`` 等存储。

契约（定稿见 ``cjh/feature/安全/端侧行为管控能力/规则判定接口对齐稿-2026-09-30.md``）：

- **非交互**：不弹窗、不阻塞、不写盘、**默认不写审计**（逐跳校验会刷爆 JSONL；
  拒绝时由调用方自己记日志）；
- 返回值域 ``allow / ask / deny / none``；``none`` = 名单无表态，**由调用方自己的
  默认档兜**（我们不越权代答）；
- ``*_static`` 是给"没有默认档、只要一个二元结果"的调用方的包装：
  ``ask → deny``（fail-closed）、``none → allow``（无表态不越权拦）、
  **异常/名单损坏 → deny**；
- ``export_*`` 给渲染用：一次性拿全量 allow/deny 清单 + 兜底档 + 内容指纹；
- 域名一律按 **host 字面量**判定（取 URL 的 host、小写、去尾点），**不接受 IP 段**——
  把白名单落到 IP 层会被 DNS 轮换 / DNS rebinding 绕过，判定必须在 host 层做。

本模块**不新增求值逻辑**：薄封装既有 ``evaluate`` / ``resolve_cell`` /
``resolve_default`` / ``SecurityListComposer``。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, NamedTuple

from jiuwenswarm.common.permission_profile import current_permission_profile

from .composer import SecurityListComposer
from .evaluate import evaluate
from .matcher import _exe_name, _hostname, _norm_path_text
from .models import resolve_cell, resolve_default

Action = Literal["allow", "ask", "deny", "none"]
StaticAction = Literal["allow", "deny"]

#: 列表类型的操作键（domain / command 单操作，file_path 三轴）
_PATH_OPS: tuple[str, ...] = ("read", "write", "exec")

_TYPE_LABELS = {"file_path": "文件路径", "domain": "域名", "command": "命令"}
_SOURCE_LABELS = {
    "user": "用户名单",
    "user_approval": "审批记住",
    "builtin": "内置底线",
    "cloud": "云侧下发",
    "default": "兜底档",
}

#: 严格度（多目标合并 / 静态化共用）
_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}


class RulesVerdict(NamedTuple):
    """单次判定结果（``action="none"`` 时其余记录字段为空串）。"""

    action: Action
    list_type: str
    target: str
    op: str
    record_id: str
    pattern: str
    source: str
    reason: str


class DomainExport(NamedTuple):
    """域名规则全量快照（渲染宿主出口 / 沙箱 egress 用）。"""

    stamp: str
    #: 本侧兜底档：``deny`` = 白名单（未列出即拒）；``None`` = 无表态（不代答）
    defaults: StaticAction | None
    allow: list[str]
    deny: list[str]


class PathExport(NamedTuple):
    """路径规则全量快照（渲染文件 ACL 用；只出 read/write 两轴）。"""

    stamp: str
    allow_read: list[str]
    deny_read: list[str]
    allow_write: list[str]
    deny_write: list[str]


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _mode_or_current(mode: str | None) -> str:
    return mode or current_permission_profile()


def _reason(list_type: str, source: str, pattern: str) -> str:
    type_label = _TYPE_LABELS.get(list_type, list_type)
    source_label = _SOURCE_LABELS.get(source, source)
    if source == "default":
        return f"兜底档（{type_label}未列出即拒）"
    return f"命中{source_label}{type_label}规则 {pattern}"


def _verdict(
    list_type: str,
    target: str,
    *,
    op: str,
    mode: str,
    composer: SecurityListComposer,
    session_id: str | None = None,
) -> RulesVerdict:
    result = evaluate(
        list_type, target, op=op, mode=mode, composer=composer, session_id=session_id
    )
    if result is None:
        return RulesVerdict(
            "none", list_type, target, op, "", "", "none",
            f"名单无表态（{_TYPE_LABELS.get(list_type, list_type)}）",
        )
    rec = result.record
    return RulesVerdict(
        result.action,                      # type: ignore[arg-type]
        list_type,
        target,
        op,
        rec.id if rec is not None else "",
        rec.pattern if rec is not None else "",
        result.source,
        _reason(list_type, result.source, rec.pattern if rec is not None else ""),
    )


def _strictest(verdicts: list[RulesVerdict]) -> RulesVerdict:
    """多目标合并：deny > ask > allow > none（与 rail 的处置顺序一致）。"""
    for action in ("deny", "ask", "allow"):
        for verdict in verdicts:
            if verdict.action == action:
                return verdict
    return verdicts[0]


def _static(verdict: RulesVerdict) -> StaticAction:
    """静态化：``ask → deny``；``none → allow``（无表态不越权拦）。"""
    if verdict.action in ("deny", "ask"):
        return "deny"
    return "allow"


def _stamp(payload: dict[str, Any]) -> str:
    """内容指纹（执行面据此判断是否需要重渲染）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _static_or_deny(fn: Any, /, *args: Any, **kwargs: Any) -> StaticAction:
    """执行 ``fn`` 并静态化；异常/名单损坏 → ``deny``（fail-closed）。"""
    try:
        return _static(fn(*args, **kwargs))
    except Exception:  # noqa: BLE001 — 见模块 docstring：异常一律 fail-closed
        import logging

        logging.getLogger(__name__).warning(
            "[security_lists.api] 判定异常，按 fail-closed 拒绝", exc_info=True
        )
        return "deny"


# ---------------------------------------------------------------------------
# 逐次判定
# ---------------------------------------------------------------------------


def check_domain(
    host_or_url: str,
    *,
    mode: str | None = None,
    session_id: str | None = None,
) -> RulesVerdict:
    """判定一个域名（网址或裸 host）：取 URL 的 host、小写、去尾点。"""
    host = _hostname(str(host_or_url or ""))
    if not host:
        return RulesVerdict(
            "none", "domain", str(host_or_url or ""), "*", "", "", "none", "空/非法域名"
        )
    return _verdict(
        "domain", host, op="*", mode=_mode_or_current(mode),
        composer=SecurityListComposer(), session_id=session_id,
    )


def check_path(
    path: str,
    *,
    op: Literal["read", "write", "exec"],
    mode: str | None = None,
    session_id: str | None = None,
) -> RulesVerdict:
    """判定一个文件路径在指定操作（read/write/exec）下的裁决。"""
    if op not in _PATH_OPS:
        raise ValueError(f"op 须为 {_PATH_OPS}：{op!r}")
    target = _norm_path_text(str(path or ""))
    if not target:
        return RulesVerdict("none", "file_path", "", op, "", "", "none", "空路径")
    return _verdict(
        "file_path", target, op=op, mode=_mode_or_current(mode),
        composer=SecurityListComposer(), session_id=session_id,
    )


def check_command(
    exe: str,
    cmdline: str = "",
    *,
    mode: str | None = None,
    session_id: str | None = None,
) -> RulesVerdict:
    """判定一条命令：``exact`` 规则按 exe 名、``glob``/``regex`` 按整行，两者都查。"""
    resolved_mode = _mode_or_current(mode)
    composer = SecurityListComposer()
    targets: list[str] = []
    line = str(cmdline or "").strip()
    if line:
        targets.append(line)
    name = str(exe or "").strip() or (_exe_name(line) if line else None)
    if name and name != line:
        targets.append(name)
    if not targets:
        return RulesVerdict("none", "command", "", "*", "", "", "none", "空命令")
    verdicts = [
        _verdict(
            "command", target, op="*", mode=resolved_mode,
            composer=composer, session_id=session_id,
        )
        for target in targets
    ]
    return _strictest(verdicts)


def check_domain_static(
    host_or_url: str, *, mode: str | None = None, session_id: str | None = None
) -> StaticAction:
    """``check_domain`` 的二元包装（异常/损坏 → deny）。"""
    return _static_or_deny(check_domain, host_or_url, mode=mode, session_id=session_id)


def check_path_static(
    path: str,
    *,
    op: Literal["read", "write", "exec"],
    mode: str | None = None,
    session_id: str | None = None,
) -> StaticAction:
    """``check_path`` 的二元包装（异常/损坏 → deny）。"""
    return _static_or_deny(check_path, path, op=op, mode=mode, session_id=session_id)


# ---------------------------------------------------------------------------
# 全量快照（渲染执行面用）
# ---------------------------------------------------------------------------


def _export_actions(
    list_type: str, mode: str, *, op: str
) -> tuple[list[str], list[str], StaticAction | None]:
    """收集某类型在 ``(mode, op)`` 下的 (allow, deny, defaults)。

    ``ask`` 在静态执行面无交互可问 → 归入 ``deny``（fail-closed）。
    """
    composer = SecurityListComposer()
    allow: list[str] = []
    deny: list[str] = []
    for rec in composer.collect(list_type):
        if not rec.enabled or rec.type != list_type:
            continue
        action = resolve_cell(rec.cells, mode, op)
        if action == "allow":
            allow.append(rec.pattern)
        elif action in ("deny", "ask"):
            deny.append(rec.pattern)
    default_action = resolve_default(composer.defaults(), mode, list_type)
    static_default: StaticAction | None = None
    if default_action is not None:
        static_default = "deny" if default_action in ("deny", "ask") else "allow"
    return sorted(set(allow)), sorted(set(deny)), static_default


def export_domain_rules(*, mode: str | None = None) -> DomainExport:
    """域名规则全量快照（含本侧兜底档与内容指纹）。"""
    resolved_mode = _mode_or_current(mode)
    allow, deny, default_action = _export_actions("domain", resolved_mode, op="*")
    return DomainExport(
        stamp=_stamp({"type": "domain", "mode": resolved_mode,
                      "allow": allow, "deny": deny, "defaults": default_action}),
        defaults=default_action,
        allow=allow,
        deny=deny,
    )


def export_path_rules(*, mode: str | None = None) -> PathExport:
    """路径规则全量快照（read/write 两轴；执行面 ACL 不支持 exec，故意不出）。"""
    resolved_mode = _mode_or_current(mode)
    allow_read, deny_read, _ = _export_actions("file_path", resolved_mode, op="read")
    allow_write, deny_write, _ = _export_actions("file_path", resolved_mode, op="write")
    return PathExport(
        stamp=_stamp({"type": "file_path", "mode": resolved_mode,
                      "allow_read": allow_read, "deny_read": deny_read,
                      "allow_write": allow_write, "deny_write": deny_write}),
        allow_read=allow_read,
        deny_read=deny_read,
        allow_write=allow_write,
        deny_write=deny_write,
    )
