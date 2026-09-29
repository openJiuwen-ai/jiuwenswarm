# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一名单读取归一（投影，全部只读）。

把四处既有数据源投影为 :class:`SecurityListRecord` 卡片视图：

- :func:`project_builtin`：agent-core 包内 ``builtin_rules.yaml``
  （命令规则/敏感路径/网络 deny）→ ``source=builtin``、``cells["*"]``；
- :func:`project_approvals`：``permissions.approval_overrides``（command 类）
  + ``file_guard.paths``（config ∪ 当前会话 overlay）→ ``source=user_approval``；
  条目 ``mode`` 字段 → 对应模式格，无 ``mode`` → 通用格（模式无关）；
- :func:`project_sandbox_runtime_copy`：windows-policy 用户副本 → ``source=user``
  （M1 迁移前的过渡投影；迁移清空副本后自然返回空）。

物理存储不动（引擎契约），此处只读。审批/副本单条畸形 → 跳过 + 告警
（与引擎既有宽容语义一致）；YAML 整体不可读 → 异常上抛（rail fail-closed）。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Mapping

import yaml

from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
    get_permissions_with_session_overlay,
)

from .models import (
    ACTIONS,
    MODE_KEYS,
    SecurityListRecord,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


def _mode_key(value: Any) -> str:
    """审批条目 mode 字段 → 模式格键；缺失/非法 → 通用格（存量=全局）。"""
    mode = str(value or "").strip()
    return mode if mode in MODE_KEYS else "*"


def _action(value: Any) -> str | None:
    action = str(value or "").strip().lower()
    return action if action in ACTIONS else None


# ---------------------------------------------------------------------------
# builtin（agent-core 包内基线）
# ---------------------------------------------------------------------------


def project_builtin() -> list[SecurityListRecord]:
    """agent-core ``builtin_rules.yaml`` → builtin 投影（每次 collect 实时读）。"""
    from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import (
        get_package_builtin_rules_path,
    )

    path = Path(get_package_builtin_rules_path())
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return []
    out: list[SecurityListRecord] = []

    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        pattern = str(rule.get("pattern") or "").strip()
        action = _action(rule.get("action"))
        if not pattern or action is None:
            continue
        if pattern.startswith("re:"):
            match, pat = "regex", pattern[3:]
        else:
            match, pat = "glob", pattern
        out.append(SecurityListRecord(
            id=str(rule.get("id") or ""),
            type="command",
            pattern=pat,
            match=match,
            note=str(rule.get("description") or ""),
            cells={"*": {"*": action}},
            source="builtin",
        ))

    for sp in data.get("sensitive_paths") or []:
        if not isinstance(sp, dict):
            continue
        spath = str(sp.get("path") or "").strip()
        action = _action(sp.get("action"))
        if not spath or action is None:
            continue
        match = str(sp.get("match") or "glob").strip().lower()
        if match not in ("glob", "prefix"):
            match = "glob"
        out.append(SecurityListRecord(
            id=str(sp.get("id") or ""),
            type="file_path",
            pattern=spath,
            match=match,
            note=str(sp.get("description") or ""),
            # file_guard 内置底线默认写入三轴
            cells={"*": {"read": action, "write": action, "exec": action}},
            source="builtin",
        ))

    net_urls = data.get("net_urls")
    if isinstance(net_urls, Mapping):
        for host, value in net_urls.items():
            action = _action(value)
            domain = str(host or "").strip().lower()
            if not domain or action is None:
                continue
            out.append(SecurityListRecord(
                id=f"builtin_net_{domain}",
                type="domain",
                pattern=domain,
                match="exact",
                cells={"*": {"*": action}},
                source="builtin",
            ))
    return out


# ---------------------------------------------------------------------------
# 审批（永久记住 ∪ 会话记住）
# ---------------------------------------------------------------------------


def project_approvals(
    session_id: str | None = None,
    *,
    permissions: Mapping[str, Any] | None = None,
) -> list[SecurityListRecord]:
    """``approval_overrides`` + ``file_guard.paths`` → user_approval 投影。

    ``permissions`` 可注入（测试/复用既有快照）；缺省走
    :func:`get_permissions_with_session_overlay`（磁盘 ∪ 当前会话 overlay）。
    """
    perms: Mapping[str, Any] = (
        permissions
        if permissions is not None
        else get_permissions_with_session_overlay(session_id=session_id)
    )
    if not isinstance(perms, Mapping):
        return []
    out: list[SecurityListRecord] = []

    for entry in perms.get("approval_overrides") or []:
        if not isinstance(entry, dict):
            continue
        # path 类引擎已忽略（路径放行只认 file_guard）；未知 match_type 跳过
        match_type = str(entry.get("match_type") or "").strip().lower()
        if match_type != "command":
            continue
        pattern = str(entry.get("pattern") or "").strip()
        action = _action(entry.get("action"))
        if not pattern or action is None:
            continue
        out.append(SecurityListRecord(
            id=str(entry.get("id") or ""),
            type="command",
            pattern=pattern,
            match="glob",
            cells={_mode_key(entry.get("mode")): {"*": action}},
            created_at=str(entry.get("created_at") or ""),
            source="user_approval",
        ))

    file_guard = perms.get("file_guard")
    paths = file_guard.get("paths") if isinstance(file_guard, Mapping) else None
    for entry in paths or []:
        if not isinstance(entry, dict):
            continue
        fpath = str(entry.get("path") or "").strip()
        if not fpath:
            continue
        row: dict[str, str] = {}
        for axis in ("read", "write", "exec"):
            action = _action(entry.get(axis))
            if action is not None:
                row[axis] = action
        if not row:
            continue
        match = str(entry.get("match") or "prefix").strip().lower()
        if match not in ("glob", "prefix"):
            match = "prefix"
        entry_id = str(entry.get("id") or "") or (
            "fg_" + hashlib.sha1(fpath.encode("utf-8")).hexdigest()[:8]
        )
        out.append(SecurityListRecord(
            id=entry_id,
            type="file_path",
            pattern=fpath,
            match=match,
            cells={_mode_key(entry.get("mode")): row},
            created_at=str(entry.get("created_at") or ""),
            source="user_approval",
        ))
    return out


# ---------------------------------------------------------------------------
# 沙箱副本（过渡投影）
# ---------------------------------------------------------------------------


def project_sandbox_runtime_copy(copy_path: str | Path | None = None) -> list[SecurityListRecord]:
    """windows-policy 用户副本 → user 投影（M1 迁移后副本被清空，自然返回空）。

    与物理 user 记录同源同形；若迁移已落记录但副本清空失败，可能与物理记录
    重复——求值对重复同语义记录天然安全（同 verdict），不做去重。
    """
    from . import store as _store  # 同包内部复用副本解析，避免环导入

    path = Path(copy_path) if copy_path is not None else _store._default_copy_path()
    if not path.is_file():
        return []
    try:
        copy = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("[security_lists] 读沙箱副本失败（投影跳过）: %s", exc)
        return []
    if not isinstance(copy, dict):
        return []
    return _store._build_migrated_records(copy, utc_now_iso())
