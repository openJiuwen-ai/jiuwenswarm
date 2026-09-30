# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""名单 domain 规则回流到 core ``net_guard`` 强制点（S2）。

**为什么需要**：规则归属收敛到本名单之后（设计 §4.4），名单里写的域名 deny 必须
在**宿主 HTTP 出口**（P3，逐跳 3xx 校验）也生效。rail 只看得到 fetch 工具的
参数，脚本里的 ``requests`` / ``urllib`` 出站它够不到——那正是 P3 存在的理由。

**为什么不需要改 agent-core**：三处强制点的权限都由本仓组装的 dict 喂进去——

- Pipeline C / P1：``PermissionInterruptRail`` 用我们传入的 config 造
  ``PermissionEngine``，引擎自己会跑 ``prepare_permissions_for_engine``；
- P3：我们自己的 ``publish_host_exit_policy_from_config()`` 调
  ``publish_host_exit_policy(prepare_permissions_for_engine(perms))``。

``prepare_permissions_for_engine`` 是作用在 dict 上的纯函数，core 里也已经有现成的
外部规则合并机制（``merge_package_net_urls``，同为"取严"语义）。所以做法就是：
**在把 permissions 交出去之前，把名单的 domain 规则并进 ``net_guard.urls``**。

语义映射（与 rail、与 ``api.check_*_static`` 保持一致）：

===========================  ==========================================
名单记录                      注入 ``net_guard.urls`` 的键
===========================  ==========================================
``exact: example.com``       ``example.com`` **和** ``*.example.com``
``wildcard: *.example.com``  ``*.example.com``
动作 ``allow`` / ``deny``     原样（同键冲突取严，deny 胜）
动作 ``ask``                  ``deny``（出口层无人可问 → fail-closed）
===========================  ==========================================

``exact`` 展开成两条是必需的：名单的 ``exact`` 是「裸域 + 子域」，而 net_guard 的
裸 pattern 走全串 wildcard、只命中裸域——只注入一条会让 ``sub.example.com`` 在
P3 层漏掉，形成"护栏拦得住、宿主出口漏"的不一致。

**只认通用格** ``cells["*"]["*"]``：P3 是进程级全局策略，没有权限档位概念。硬套
某个档位的取值会把"仅默认档禁止"放大成"所有档位禁止"。

**绝不新建、绝不启用 net_guard 段**：``net_guard`` 缺失或 ``enabled: false`` 时
原样返回——那是他们侧控制"这个强制点开不开"的开关，替他们打开等于行为突变。
（同理，``security_lists.defaults`` **不下沉**到无模式维度的 ``net_guard.defaults``。）

段损坏 → 原样返回 + ERROR 告警：rail 那一侧已经 fail-closed 拒绝所有工具调用，
这里再叠加放大没有意义。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from .models import resolve_cell

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"allow", "deny"})


def _parse_action(value: Any) -> str | None:
    """对齐 core ``net_guard._parse_action``：接受 ``str`` 或 ``{"action"|"fetch": str}``。"""
    raw: Any = value
    if isinstance(value, Mapping):
        raw = value.get("action") if value.get("action") is not None else value.get("fetch")
    if not isinstance(raw, str) or not raw.strip():
        return None
    action = raw.strip().lower()
    return action if action in _VALID_ACTIONS else None


def _stricter(left: str, right: str) -> str:
    """同键取严：回流只可能更紧，绝不把既有 deny 放宽成 allow。"""
    return "deny" if "deny" in (left, right) else "allow"


def _net_guard_keys(rec: Any) -> tuple[str, ...]:
    """名单 domain 记录 → net_guard 键（``exact`` 展开裸域 + 子域两条）。"""
    pattern = str(rec.pattern or "").strip().lower().rstrip(".")
    if not pattern:
        return ()
    if rec.match == "exact":
        return (pattern, f"*.{pattern}")
    if rec.match == "wildcard":
        return (pattern,)
    return ()


def _domain_rule_pairs(lists: Mapping[str, Any]) -> dict[str, str]:
    """名单 ``user`` / ``cloud`` 的启用 domain 记录 → ``{net_guard 键: 动作}``。

    只取物理记录（user/cloud）：``builtin`` 由 core ``merge_package_net_urls`` 自己
    并入同一份 ``builtin_rules.yaml::net_urls``；``net_guard`` 投影本来就是这段的
    回读，再注入一次是空转、还会把已让位的条目拉回来。
    """
    cloud = lists.get("cloud") if isinstance(lists.get("cloud"), Mapping) else {}
    records = list(lists.get("user") or []) + list(cloud.get("records") or [])

    out: dict[str, str] = {}
    for rec in records:
        if rec.type != "domain" or not rec.enabled:
            continue
        action = resolve_cell(rec.cells, "*", "*")
        if action is None:
            continue
        if action == "ask":
            action = "deny"          # 出口层只有 block / pass，没有用户可问
        for key in _net_guard_keys(rec):
            prev = out.get(key)
            out[key] = action if prev is None else _stricter(prev, action)
    return out


def merge_domain_rules_into_net_guard(
    permissions: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把名单 domain 规则并入 ``permissions.net_guard.urls``，返回**新** dict。

    调用方共享的 config 对象不被原地修改（``net_guard`` 子段复制后替换）。
    不动 ``net_guard.defaults`` / ``enabled`` / ``enforce_host_exit``。
    """
    perms: dict[str, Any] = dict(permissions) if isinstance(permissions, Mapping) else {}
    ng = perms.get("net_guard")
    if not isinstance(ng, Mapping) or not ng.get("enabled"):
        return perms

    try:
        from . import store

        lists = store.get_security_lists()
    except Exception as exc:  # noqa: BLE001 — 含 SecurityListsCorruptedError
        logger.error(
            "[security_lists] 名单读取失败，domain 规则未回流到 net_guard（P3 保持原状）: %s",
            exc,
        )
        return perms

    rules = _domain_rule_pairs(lists)
    if not rules:
        return perms

    raw_urls = ng.get("urls")
    urls: dict[str, Any] = dict(raw_urls) if isinstance(raw_urls, Mapping) else {}
    for key, action in rules.items():
        prev = _parse_action(urls.get(key)) if key in urls else None
        urls[key] = action if prev is None else _stricter(prev, action)

    merged = dict(ng)
    merged["urls"] = urls
    perms["net_guard"] = merged
    logger.debug(
        "[security_lists] domain 规则回流 net_guard: %d 条（urls %d → %d）",
        len(rules),
        len(raw_urls) if isinstance(raw_urls, Mapping) else 0,
        len(urls),
    )
    return perms


def permissions_for_enforcement(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """``config`` → 强制点用 permissions（含名单 domain 规则回流）。

    ``config`` 缺省/非映射 → 空 dict（**不回读全局 config**，保持与调用点原语义一致）。
    """
    perms = config.get("permissions") if isinstance(config, Mapping) else None
    return merge_domain_rules_into_net_guard(perms)


__all__ = [
    "merge_domain_rules_into_net_guard",
    "permissions_for_enforcement",
]
