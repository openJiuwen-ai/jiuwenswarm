# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一安全名单 RPC（``security_lists.*`` 六个 WS 方法）。

dispatch 形态仿 :mod:`permissions_config_rpc` / :mod:`sandbox_config_rpc`，
注册进 ``agent_ws_server``。

| 方法 | params | 返回 | 错误 |
| --- | --- | --- | --- |
| security_lists.get | ``{type?, mode?, source?, session_id?}`` | ``{records, builtin, mode, cloud_meta}`` | 500 段损坏 |
| security_lists.upsert | ``{record}`` | ``{record}`` | 409 唯一性冲突（附 existing_id）；400 校验 |
| security_lists.cells.patch | ``{id, set:{"mode.op":action}, unset:["mode.op"]}`` | ``{record}`` | 404/400 |
| security_lists.delete | ``{id}`` | ``{ok}`` | 404 |
| security_lists.cloud.sync | ``{sync_version, synced_at, records[], defaults?}`` | ``{applied}`` | 400 整批拒绝 |
| security_lists.defaults.get / .set | ``{}`` / ``{defaults}`` | ``{defaults}`` | 400 键空间非法 |
| security_lists.migrate | ``{sources?: string[], dry_run?}`` | ``{created, skipped, candidates, sources, records}`` | 400 未知来源 |
| security_lists.audit.query | ``{kind?, since?, limit?}`` | ``{events}`` | — |

**写入路径分离**（可回溯性关键）：upsert/cells.patch/delete 仅操作 user 区
（store 强制 ``source=user``，防伪造）；``user_approval`` 格子无 RPC 创建通道，
只能由审批弹窗"永久/会话记住"经 ``permissions_persist`` 产生。

写方法成功后：① 触发设计 4.4 双端同步（``security_lists_render`` 渲染沙箱副本
→ box-server 指纹重载，best-effort 不影响主路径）；② 写 ``security.list.change``
审计。rail 侧经 get_config stamp 失效下次求值即新名单（热更新链路），无需重载广播。
"""
from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)

_SECURITY_LISTS_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.SECURITY_LISTS_GET,
        ReqMethod.SECURITY_LISTS_UPSERT,
        ReqMethod.SECURITY_LISTS_CELLS_PATCH,
        ReqMethod.SECURITY_LISTS_DELETE,
        ReqMethod.SECURITY_LISTS_CLOUD_SYNC,
        ReqMethod.SECURITY_LISTS_AUDIT_QUERY,
        ReqMethod.SECURITY_LISTS_DEFAULTS_GET,
        ReqMethod.SECURITY_LISTS_DEFAULTS_SET,
        ReqMethod.SECURITY_LISTS_MIGRATE,
    }
)


def get_security_lists_req_methods() -> frozenset[ReqMethod]:
    return _SECURITY_LISTS_METHODS


def _err(request: AgentRequest, message: str, *, code: str = "BAD_REQUEST", **extra: Any) -> AgentResponse:
    payload: dict[str, Any] = {"error": message, "code": code}
    payload.update(extra)
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=False,
        payload=payload,
        metadata=request.metadata,
    )


def _ok(request: AgentRequest, payload: dict[str, Any] | None) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload or {},
        metadata=request.metadata,
    )


# ---------------------------------------------------------------------------
# 卡片视图（user/cloud 聚合记录 + 审批格子视图合入；builtin 单独数组）
# ---------------------------------------------------------------------------

_CARD_SOURCES = ("user", "cloud", "user_approval")
#: 审批格压制严重度（多条审批条目同格时取最严，与合成算法层内语义一致）
_SEVERITY = {"allow": 1, "ask": 2, "deny": 3}


def _cell_view(action: str, source: str, *, scope: str = "global", overridden: dict | None = None) -> dict[str, Any]:
    cell: dict[str, Any] = {"action": action, "source": source}
    if source == "user_approval":
        cell["scope"] = scope
    if overridden:
        cell["overridden"] = overridden
    return cell


def _record_card(rec: Any, *, scope: str = "global") -> dict[str, Any]:
    """SecurityListRecord → 卡片视图（格子带 source 徽标；审批格带 scope）。"""
    return {
        "id": rec.id,
        "type": rec.type,
        "pattern": rec.pattern,
        "match": rec.match,
        "enabled": rec.enabled,
        "note": rec.note,
        "source": rec.source,
        "scope": scope,
        "cells": {
            mode: {op: _cell_view(action, rec.source, scope=scope) for op, action in row.items()}
            for mode, row in rec.cells.items()
        },
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }


def _merge_approval_cells(card: dict[str, Any], approval: Any, *, scope: str) -> None:
    """把一条审批投影的格子合入对应卡片（user_approval > user/cloud 压制标记）。"""
    for mode, row in approval.cells.items():
        target_row = card["cells"].setdefault(mode, {})
        for op, action in row.items():
            existing = target_row.get(op)
            if existing is None:
                target_row[op] = _cell_view(action, "user_approval", scope=scope)
                continue
            if existing["source"] == "user_approval":
                # 多审批条目同格：取最严（deny > ask > allow）
                if _SEVERITY.get(action, 0) > _SEVERITY.get(existing["action"], 0):
                    target_row[op] = _cell_view(action, "user_approval", scope=scope)
                continue
            if existing["action"] == action:
                continue  # 同值不算压制，保留用户格
            # 用户/云格被审批压制 → 徽标"被审批记住规则覆盖"
            target_row[op] = _cell_view(action, "user_approval", scope=scope, overridden=existing)


def _approval_key(rec: Any) -> str:
    return f"{rec.id}|{rec.type}|{rec.pattern}"


def _build_cards(*, session_id: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """卡片全集 + cloud 元信息。

    user 物理记录 → cloud 物理记录（均含 disabled，前端管启用开关）→
    审批投影按 ``(type, pattern, match)`` 合入对应卡片；无对应卡片的审批
    自成卡片置顶（徽标"审批记住"）。``session_id`` 非空时叠加该会话的
    session 级审批格子（标记 ``scope=session`` 仅当前会话）。
    """
    from jiuwenswarm.agents.harness.common.rails.security_lists import store
    from jiuwenswarm.agents.harness.common.rails.security_lists.normalize import (
        project_approvals,
    )

    lists = store.get_security_lists()
    cards: list[dict[str, Any]] = []
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rec in lists["user"]:
        card = _record_card(rec)
        cards.append(card)
        index.setdefault((rec.type, rec.pattern, rec.match), card)
    for rec in lists["cloud"]["records"]:
        card = _record_card(rec)
        cards.append(card)
        index.setdefault((rec.type, rec.pattern, rec.match), card)

    # 磁盘审批（显式注入 permissions，绕开 ContextVar 误判 session 范围）
    #
    # **刻意不传 occupied（让位）**：卡片视图回答的是"用户按了删除之后，到底还会不会被拦"，
    # 而 legacy 段即使被 rail 让位，core 的 FileGuardChecker / NetGuardChecker **仍在读它**
    # （见 store.migrate_legacy_once 文档）。此处若也按让位隐藏，用户会以为"删了就没了"，
    # 实际引擎照拦——那是比重复展示更糟的谎。让位只是 rail 侧的唯一真源，不是全局的。
    from jiuwenswarm.common.config import get_config

    config = get_config()
    disk_perms = config.get("permissions") if isinstance(config, dict) else {}
    disk_approvals = project_approvals(None, permissions=disk_perms if isinstance(disk_perms, dict) else {})
    disk_keys = {_approval_key(r) for r in disk_approvals}

    approvals = list(disk_approvals)
    if session_id:
        for rec in project_approvals(session_id):
            if _approval_key(rec) not in disk_keys:
                approvals.append(rec)

    for approval in approvals:
        scope = "global" if _approval_key(approval) in disk_keys else "session"
        key = (approval.type, approval.pattern, approval.match)
        card = index.get(key)
        if card is None:
            card = _record_card(approval, scope=scope)
            cards.insert(0, card)  # 自成审批卡片置顶
            index[key] = card
            continue
        _merge_approval_cells(card, approval, scope=scope)

    cloud_meta = {
        "sync_version": lists["cloud"]["sync_version"],
        "synced_at": lists["cloud"]["synced_at"],
    }
    return cards, cloud_meta


# ---------------------------------------------------------------------------
# 写后动作：审计 + 沙箱双端同步
# ---------------------------------------------------------------------------


def _audit_change(op: str, **fields: Any) -> None:
    from jiuwenswarm.agents.harness.common.rails.security_lists import audit

    fields.setdefault("scope", "rpc")
    audit.log_event(audit.AUDIT_CHANGE, op=op, **fields)


def _trigger_sandbox_sync() -> None:
    """名单写后双端同步：渲染副本 → 触发 box-server 重载（best-effort）。

    rail 热读走 get_config stamp 失效链路，不受本步成败影响；
    副本内容未变时 runner 指纹比对早退，不会真重启 box-server。
    """
    try:
        from jiuwenswarm.server.security_lists_render import render_sandbox_copy

        render_sandbox_copy()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[security_lists] 沙箱副本渲染失败（rail 热读不受影响）: %s", exc)
        return
    try:
        from jiuwenswarm.server.sandbox_config_rpc import trigger_sandbox_apply

        trigger_sandbox_apply("files")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[security_lists] 触发沙箱重载失败: %s", exc)


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def _parse_cell_key(key: Any) -> tuple[str, str]:
    """``"mode.op"`` → ``(mode, op)``；非法形式抛 ValueError（映射 400）。"""
    mode, dot, op = str(key).partition(".")
    if not dot or not mode or not op:
        raise ValueError(f"格子键须为 'mode.op' 形式: {key!r}")
    return mode, op


def _find_existing_id(list_type: str, pattern: str, match: str) -> str | None:
    """409 冲突时查已存在记录 id（best-effort，段损坏则省略）。"""
    try:
        from jiuwenswarm.agents.harness.common.rails.security_lists import store

        for rec in store.get_security_lists()["user"]:
            if (rec.type, rec.pattern, rec.match) == (list_type, pattern, match):
                return rec.id
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def dispatch_security_lists_request(request: AgentRequest) -> AgentResponse:
    """执行一条 security_lists RPC（与 dispatch_permissions_config_request 同形态）。"""
    from jiuwenswarm.agents.harness.common.rails.security_lists import audit, store
    from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
        ACTIONS,
        ALLOWED_MATCH,
        MODE_KEYS,
        DuplicateRecordError,
        SecurityListsCorruptedError,
        record_from_dict,
        record_to_dict,
    )

    m = request.req_method
    params = request.params if isinstance(request.params, dict) else {}
    tag = m.value if m is not None else ""

    try:
        # ---- get：卡片视图 ----
        if m == ReqMethod.SECURITY_LISTS_GET:
            list_type = params.get("type")
            if list_type is not None and list_type not in ALLOWED_MATCH:
                return _err(request, f"未知名单类型: {list_type!r}")
            source = params.get("source")
            if source is not None and source not in _CARD_SOURCES:
                return _err(request, f"未知卡片来源: {source!r}")
            mode = params.get("mode")
            if mode is not None and mode not in MODE_KEYS:
                return _err(request, f"未知模式: {mode!r}")
            session_id = params.get("session_id")
            session_id = str(session_id) if session_id else None

            cards, cloud_meta = _build_cards(session_id=session_id)
            if list_type is not None:
                cards = [c for c in cards if c["type"] == list_type]
            if source is not None:
                cards = [c for c in cards if c["source"] == source]

            from jiuwenswarm.agents.harness.common.rails.security_lists.normalize import (
                project_builtin,
            )
            from jiuwenswarm.common.permission_profile import current_permission_profile

            builtin = [_record_card(rec) for rec in project_builtin()]
            if list_type is not None:
                builtin = [c for c in builtin if c["type"] == list_type]
            return _ok(
                request,
                {
                    "records": cards,
                    "builtin": builtin,
                    "mode": mode or current_permission_profile(),
                    "cloud_meta": cloud_meta,
                    # v3 兜底档（白名单模式）：随卡片视图一并下发，前端免二次请求
                    "defaults": store.get_defaults(),
                },
            )

        # ---- upsert：user 区整卡片保存 ----
        if m == ReqMethod.SECURITY_LISTS_UPSERT:
            raw = params.get("record")
            if not isinstance(raw, dict):
                return _err(request, "record must be object")
            rec = record_from_dict(raw)  # ValueError → 400
            try:
                stored = store.upsert_record(rec)
            except DuplicateRecordError as exc:
                existing_id = _find_existing_id(rec.type, rec.pattern, rec.match)
                extra: dict[str, Any] = {"existing_id": existing_id} if existing_id else {}
                return _err(request, str(exc), code="CONFLICT", **extra)
            _audit_change("upsert", record_id=stored.id, type=stored.type, pattern=stored.pattern)
            _trigger_sandbox_sync()
            return _ok(request, {"record": record_to_dict(stored)})

        # ---- cells.patch：格子级增改删 ----
        if m == ReqMethod.SECURITY_LISTS_CELLS_PATCH:
            record_id = str(params.get("id") or "").strip()
            if not record_id:
                return _err(request, "id is required")
            raw_set = params.get("set") or {}
            raw_unset = params.get("unset") or []
            if not isinstance(raw_set, dict) or not isinstance(raw_unset, list):
                return _err(request, "set must be object, unset must be list")
            if not raw_set and not raw_unset:
                return _err(request, "set/unset 至少其一")
            set_: dict[tuple[str, str], str] = {}
            for key, action in raw_set.items():
                cell_key = _parse_cell_key(key)  # ValueError → 400
                if not isinstance(action, str) or action not in ACTIONS:
                    return _err(request, f"未知格子值: {action!r}")
                set_[cell_key] = action
            unset = [_parse_cell_key(key) for key in raw_unset]
            try:
                stored = store.patch_cells(record_id, set_=set_, unset=unset)
            except KeyError:
                return _err(request, f"记录不存在: {record_id}", code="NOT_FOUND")
            _audit_change(
                "cells.patch",
                record_id=stored.id,
                set={f"{mode}.{op}": action for (mode, op), action in set_.items()},
                unset=[f"{mode}.{op}" for mode, op in unset],
            )
            _trigger_sandbox_sync()
            return _ok(request, {"record": record_to_dict(stored)})

        # ---- delete：user 区整卡片删除 ----
        if m == ReqMethod.SECURITY_LISTS_DELETE:
            record_id = str(params.get("id") or "").strip()
            if not record_id:
                return _err(request, "id is required")
            if not store.delete_record(record_id):
                return _err(request, f"记录不存在: {record_id}", code="NOT_FOUND")
            _audit_change("delete", record_id=record_id)
            _trigger_sandbox_sync()
            return _ok(request, {"ok": True})

        # ---- cloud.sync：cloud 区整区替换 ----
        if m == ReqMethod.SECURITY_LISTS_CLOUD_SYNC:
            records = params.get("records")
            if not isinstance(records, list):
                return _err(request, "records must be list")
            applied = store.cloud_sync(
                sync_version=str(params.get("sync_version") or ""),
                synced_at=str(params.get("synced_at") or ""),
                records=records,
                # 缺省不下发 → 保留现值（云侧不静默改档位）
                defaults=params.get("defaults"),
            )  # ValueError → 400 整批拒绝
            _audit_change(
                "cloud.sync",
                scope="cloud",
                sync_version=str(params.get("sync_version") or ""),
                applied=applied,
                defaults=params.get("defaults"),
            )
            _trigger_sandbox_sync()
            return _ok(request, {"applied": applied})

        # ---- defaults.get / defaults.set：兜底档（白名单模式） ----
        if m == ReqMethod.SECURITY_LISTS_DEFAULTS_GET:
            return _ok(request, {"defaults": store.get_defaults()})

        if m == ReqMethod.SECURITY_LISTS_DEFAULTS_SET:
            defaults = params.get("defaults")
            if not isinstance(defaults, dict):
                return _err(request, "defaults must be object")
            store.set_defaults(defaults)   # ValueError → 400，且不落盘
            _audit_change("defaults.set", defaults=defaults)
            _trigger_sandbox_sync()
            return _ok(request, {"defaults": store.get_defaults()})

        # ---- migrate：legacy 段（net_guard.urls / file_guard.paths）一次性搬进 user 区 ----
        if m == ReqMethod.SECURITY_LISTS_MIGRATE:
            raw_sources = params.get("sources")
            if raw_sources is None:
                sources = None
            elif isinstance(raw_sources, list) and all(isinstance(s, str) for s in raw_sources):
                sources = tuple(raw_sources)
            else:
                return _err(request, "sources must be list[str]")
            dry_run = bool(params.get("dry_run"))

            kwargs: dict[str, Any] = {"dry_run": dry_run}
            if sources is not None:
                kwargs["sources"] = sources
            result = store.migrate_legacy_once(**kwargs)  # ValueError → 400 未知来源
            if not dry_run:
                _audit_change(
                    "migrate",
                    created=result["created"],
                    skipped=result["skipped"],
                    sources=result["sources"],
                )
                if result["created"]:
                    _trigger_sandbox_sync()
            return _ok(request, {
                "created": result["created"],
                "skipped": result["skipped"],
                "candidates": result["candidates"],
                "sources": result["sources"],
                "records": [record_to_dict(r) for r in result["records"]],
            })

        # ---- audit.query：审计回溯 ----
        if m == ReqMethod.SECURITY_LISTS_AUDIT_QUERY:
            kind = params.get("kind")
            since = params.get("since")
            limit = params.get("limit", 200)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                return _err(request, "limit must be int")
            events = audit.query_events(
                kind=str(kind) if kind else None,
                since=str(since) if since else None,
                limit=limit,
            )
            return _ok(request, {"events": events})

    except DuplicateRecordError as exc:  # cells.patch 等兜底（upsert 已单独带 existing_id）
        return _err(request, str(exc), code="CONFLICT")
    except SecurityListsCorruptedError as exc:
        logger.error("[%s] security_lists 段损坏: %s", tag, exc)
        return _err(request, str(exc), code="INTERNAL_ERROR")
    except ValueError as exc:
        return _err(request, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] %s", tag, exc)
        return _err(request, str(exc), code="INTERNAL_ERROR")

    return _err(request, "unknown security_lists req_method", code="BAD_REQUEST")


__all__ = [
    "dispatch_security_lists_request",
    "get_security_lists_req_methods",
]
