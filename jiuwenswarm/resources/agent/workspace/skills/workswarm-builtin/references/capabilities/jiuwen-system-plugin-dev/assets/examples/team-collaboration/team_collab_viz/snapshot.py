"""Snapshot assembly and every derived metric, as pure functions.

This module never imports host code: host objects are normalised into plain
dicts by the ``to_*`` helpers before anything else happens, so no host-owned
object (``TaskInfo``/``MemberInfo``/``Ops`` view) is retained past the call
that produced it (contract I4).

Everything downstream of ``build_snapshot`` is deterministic and side-effect
free, which keeps the whole contract surface unit-testable without a host.
"""

from __future__ import annotations

import copy
import heapq
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "1.1"
GLOBAL_SCOPE_KEY = "global"

TERMINAL_TASK_STATUSES = ("completed", "cancelled")
SETTLED_MEMBER_STATUSES = frozenset({"paused", "stopped", "shut_down", "dormant"})
TEAM_LIFECYCLE_EVENTS = frozenset({"team_created", "team_standby", "team_cleaned"})

MEMBER_STATUS_LABELS: dict[str, str] = {
    "unstarted": "未启动",
    "starting": "启动中",
    "ready": "就绪",
    "busy": "执行中",
    "paused": "已暂停",
    "stopped": "已停止",
    "restarting": "重启中",
    "shutdown_requested": "正在关闭",
    "shut_down": "已关闭",
    "dormant": "休眠",
    "error": "异常",
}

EXECUTION_STATUS_LABELS: dict[str, str] = {
    "idle": "空闲",
    "starting": "启动中",
    "running": "运行中",
    "cancel_requested": "请求取消",
    "cancelling": "取消中",
    "cancelled": "已取消",
    "completing": "收尾中",
    "completed": "已完成",
    "failed": "失败",
    "timed_out": "超时",
}

TASK_STATUS_LABELS: dict[str, str] = {
    "pending": "待开始",
    "blocked": "阻塞",
    "planning": "计划中",
    "in_progress": "进行中",
    "in_review": "待验收",
    "completed": "已完成",
    "cancelled": "已取消",
}

TEAM_ROLE_LABELS: dict[str, str] = {
    "leader": "队长",
    "teammate": "成员",
    "human_agent": "人类代理",
    "passive_human": "人类席位",
    "bridge_agent": "外部桥接成员",
    "worker": "执行工人",
    "external_cli": "外部 CLI 成员",
}

TEAM_STATUS_LABELS: dict[str, str] = {
    "running": "运行中",
    "standby": "待机",
    "cleaned": "已结束",
    "completed": "已完成",
    "history": "历史记录",
}


def now_ms() -> int:
    """Current wall clock in epoch milliseconds (the snapshot time unit)."""
    return int(time.time() * 1000)


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing host object."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _label(labels: Mapping[str, str], value: Any) -> tuple[str, bool]:
    """Return ``(chinese_label, unknown)``; unknown values fall back to the raw value."""
    if isinstance(value, str) and value in labels:
        return labels[value], False
    return ("" if value is None else str(value)), True


def to_team_dict(info: Any) -> dict[str, Any]:
    """Normalise ``TeamInfo`` (or a dict) into the plain team record."""
    return {
        "team_name": str(_field(info, "team_name") or ""),
        "display_name": str(_field(info, "display_name") or ""),
        "leader_member_name": str(_field(info, "leader_member_name") or ""),
        "desc": _field(info, "desc"),
        "created": _field(info, "created"),
    }


def to_member_dict(info: Any) -> dict[str, Any]:
    """Normalise ``MemberInfo`` (or a dict) into the plain member record."""
    return {
        "member_name": str(_field(info, "member_name") or ""),
        "display_name": _field(info, "display_name"),
        "desc": _field(info, "desc"),
        "role": str(_field(info, "role") or ""),
        "status": str(_field(info, "status") or ""),
        "execution_status": _field(info, "execution_status"),
        "mode": str(_field(info, "mode") or ""),
        "cli_agent": _field(info, "cli_agent"),
    }


def to_task_dict(info: Any) -> dict[str, Any]:
    """Normalise ``TaskInfo`` (or a dict) into the plain task record."""
    return {
        "task_id": str(_field(info, "task_id") or ""),
        "title": str(_field(info, "title") or ""),
        "content": str(_field(info, "content") or ""),
        "status": str(_field(info, "status") or ""),
        "assignee": _field(info, "assignee"),
        "updated_at": _field(info, "updated_at"),
    }


def to_dependency_map(rows_by_task: Mapping[str, Sequence[Any]]) -> dict[str, list[tuple[str, bool]]]:
    """Collect ``(upstream_id, resolved)`` pairs keyed by the dependent task."""
    collected: dict[str, list[tuple[str, bool]]] = {}
    for task_id, rows in rows_by_task.items():
        pairs: list[tuple[str, bool]] = []
        for row in rows or ():
            upstream = _field(row, "depends_on_task_id")
            if not isinstance(upstream, str) or not upstream:
                continue
            pairs.append((upstream, bool(_field(row, "resolved"))))
        collected[str(task_id)] = pairs
    return collected


def progress_of(statuses: Iterable[str]) -> dict[str, Any]:
    """Five-bucket counters plus the completion percentage (§2.6)."""
    counts = {
        "completed": 0,
        "cancelled": 0,
        "in_review": 0,
        "blocked": 0,
        "pending": 0,
        "in_progress": 0,
    }
    total = 0
    for status in statuses:
        total += 1
        if status == "completed":
            counts["completed"] += 1
        elif status == "cancelled":
            counts["cancelled"] += 1
        elif status == "in_review":
            counts["in_review"] += 1
        elif status == "blocked":
            counts["blocked"] += 1
        elif status in ("in_progress", "planning"):
            counts["in_progress"] += 1
        else:
            counts["pending"] += 1
    denominator = total - counts["cancelled"]
    percent = round(counts["completed"] / denominator * 100, 1) if denominator > 0 else None
    return {"total": total, **counts, "percent": percent}


def _compute_depths(
    task_ids: Sequence[str],
    blocked_by: Mapping[str, Sequence[str]],
) -> tuple[dict[str, int], bool]:
    """Longest upstream chain length per task, tolerating cycles (§2.5)."""
    depth: dict[str, int] = {}
    state: dict[str, int] = {}
    has_cycle = False
    for start in task_ids:
        if state.get(start) == 2:
            continue
        state[start] = 1
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, index = stack.pop()
            upstreams = list(blocked_by.get(node, ()))
            if index < len(upstreams):
                stack.append((node, index + 1))
                upstream = upstreams[index]
                if state.get(upstream) == 1:
                    has_cycle = True
                    continue
                if state.get(upstream) == 2:
                    continue
                state[upstream] = 1
                stack.append((upstream, 0))
                continue
            best = -1
            for upstream in upstreams:
                known = depth.get(upstream)
                if known is not None and known > best:
                    best = known
            depth[node] = 0 if best < 0 else best + 1
            state[node] = 2
    return depth, has_cycle


def _topological_order(
    task_ids: Sequence[str],
    blocked_by: Mapping[str, Sequence[str]],
    blocks: Mapping[str, Sequence[str]],
) -> list[str]:
    """Stable Kahn order; leftover cycle members are appended sorted by id."""
    indegree = {task_id: len(blocked_by.get(task_id, ())) for task_id in task_ids}
    ready = [task_id for task_id in task_ids if indegree[task_id] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        order.append(current)
        for downstream in blocks.get(current, ()):
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                heapq.heappush(ready, downstream)
    if len(order) < len(task_ids):
        seen = set(order)
        order.extend(sorted(task_id for task_id in task_ids if task_id not in seen))
    return order


def build_graph(
    tasks: Sequence[Mapping[str, Any]],
    dependencies: Mapping[str, Sequence[tuple[str, bool]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive edges/layers/order plus the per-task dependency view.

    Returns ``(graph, per_task)`` where ``per_task`` carries ``blocked_by``,
    ``blocks``, ``unresolved_blocked_by``, ``depth`` and ``flags``.
    """
    task_ids = sorted(str(task["task_id"]) for task in tasks)
    known = set(task_ids)
    blocked_by: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    unresolved: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    resolved_by_edge: dict[tuple[str, str], bool] = {}
    for task_id in task_ids:
        pairs: dict[str, bool] = {}
        for upstream, resolved in dependencies.get(task_id, ()):
            if upstream not in known or upstream == task_id:
                continue
            pairs[upstream] = bool(resolved) or pairs.get(upstream, False)
        blocked_by[task_id] = sorted(pairs)
        unresolved[task_id] = sorted(upstream for upstream, resolved in pairs.items() if not resolved)
        for upstream, resolved in pairs.items():
            resolved_by_edge[(upstream, task_id)] = resolved

    blocks: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    for task_id in task_ids:
        for upstream in blocked_by[task_id]:
            blocks[upstream].append(task_id)
    for task_id in task_ids:
        blocks[task_id] = sorted(set(blocks[task_id]))

    depth, has_cycle = _compute_depths(task_ids, blocked_by)
    depth = {task_id: depth.get(task_id, 0) for task_id in task_ids}

    edges = [
        {"from": upstream, "to": task_id, "resolved": resolved}
        for (upstream, task_id), resolved in sorted(resolved_by_edge.items())
    ]
    layers: list[list[str]] = []
    for task_id in task_ids:
        level = depth[task_id]
        while len(layers) <= level:
            layers.append([])
        layers[level].append(task_id)
    for layer in layers:
        layer.sort()

    graph = {
        "edges": edges,
        "layers": layers,
        "node_order": _topological_order(task_ids, blocked_by, blocks),
        "has_cycle": has_cycle,
    }
    per_task = {
        task_id: {
            "blocked_by": blocked_by[task_id],
            "blocks": blocks[task_id],
            "unresolved_blocked_by": unresolved[task_id],
            "depth": depth[task_id],
            "flags": {
                "is_root": not blocked_by[task_id],
                "is_join": len(blocked_by[task_id]) >= 2,
                "is_parallel_branch": len(blocks[task_id]) >= 2,
                "is_isolated": not blocked_by[task_id] and not blocks[task_id],
            },
        }
        for task_id in task_ids
    }
    return graph, per_task


def team_status(
    *,
    team_present: bool,
    lifecycle_event: str | None,
    has_lifecycle_fact: bool,
    tasks: Sequence[Mapping[str, Any]],
    members: Sequence[Mapping[str, Any]],
) -> tuple[str, str, bool]:
    """§2.9 status decision: ``(status, status_source, unknown_enum)``."""
    if not team_present:
        return "", "", False
    if has_lifecycle_fact and lifecycle_event == "team_cleaned":
        return "cleaned", "event", False
    if not has_lifecycle_fact:
        return "history", "history", False
    if lifecycle_event == "team_standby":
        return "standby", "event", False
    all_tasks_terminal = bool(tasks) and all(task["status"] in TERMINAL_TASK_STATUSES for task in tasks)
    members_settled = all(member["status"] in SETTLED_MEMBER_STATUSES for member in members)
    if all_tasks_terminal and members_settled:
        return "completed", "derived", False
    return "running", "event", False


def _member_block(
    member: Mapping[str, Any],
    tasks_by_assignee: Mapping[str, Sequence[Mapping[str, Any]]],
    captain_name: str | None,
) -> dict[str, Any]:
    status_label, status_unknown = _label(MEMBER_STATUS_LABELS, member["status"])
    execution = member["execution_status"]
    execution_label, execution_unknown = (None, False)
    if execution is not None:
        execution_label, execution_unknown = _label(EXECUTION_STATUS_LABELS, execution)
    role = member["role"]
    role_label, role_unknown = _label(TEAM_ROLE_LABELS, role)
    # §2.4.0: the captain is whoever ``TeamInfo.leader_member_name`` points at —
    # the host may report ``role == "teammate"`` for every member, so ``role``
    # is only a fallback when that name matches nobody (empty / exited leader).
    # ``role``/``role_label`` describe the role type and stay untouched.
    is_leader = captain_name is not None and member["member_name"] == captain_name
    owned = list(tasks_by_assignee.get(member["member_name"], ()))
    active = [task for task in owned if task["status"] not in TERMINAL_TASK_STATUSES]
    current: dict[str, Any] | None = None
    if active:
        chosen = min(
            active,
            key=lambda task: (-(task["updated_at"] if isinstance(task["updated_at"], int) else -1), task["task_id"]),
        )
        chosen_label, _ = _label(TASK_STATUS_LABELS, chosen["status"])
        current = {
            "task_id": chosen["task_id"],
            "title": chosen["title"],
            "status": chosen["status"],
            "status_label": chosen_label,
        }
    block: dict[str, Any] = {
        "member_name": member["member_name"],
        "display_name": member["display_name"] if member["display_name"] else member["member_name"],
        "desc": member["desc"],
        "role": role,
        "role_label": role_label,
        "role_group": "captain" if is_leader else "member",
        "is_leader": is_leader,
        "status": member["status"],
        "status_label": status_label,
        "execution_status": execution,
        "execution_label": execution_label,
        "mode": member["mode"],
        "cli_agent": member["cli_agent"],
        "task_progress": progress_of(task["status"] for task in owned),
        "active_task_count": len(active),
        "current_task": current,
    }
    if status_unknown or execution_unknown or role_unknown:
        block["unknown_enum"] = True
    return block


def _task_block(
    task: Mapping[str, Any],
    per_task: Mapping[str, Mapping[str, Any]],
    display_names: Mapping[str, Any],
) -> dict[str, Any]:
    status_label, status_unknown = _label(TASK_STATUS_LABELS, task["status"])
    assignee_raw = task["assignee"]
    assignee: dict[str, Any] | None = None
    if isinstance(assignee_raw, str) and assignee_raw:
        assignee = {
            "member_name": assignee_raw,
            "display_name": display_names.get(assignee_raw),
        }
    derived = per_task[task["task_id"]]
    block: dict[str, Any] = {
        "task_id": task["task_id"],
        "title": task["title"],
        "content": task["content"],
        "status": task["status"],
        "status_label": status_label,
        "assignee": assignee,
        "blocked_by": list(derived["blocked_by"]),
        "blocks": list(derived["blocks"]),
        "unresolved_blocked_by": list(derived["unresolved_blocked_by"]),
        "updated_at": task["updated_at"],
        "depth": derived["depth"],
        "flags": dict(derived["flags"]),
    }
    if status_unknown:
        block["unknown_enum"] = True
    return block


def build_snapshot(
    *,
    scope_kind: str,
    scope_key: str,
    team: Any,
    members: Sequence[Any],
    tasks: Sequence[Any],
    dependencies: Mapping[str, Sequence[Any]],
    session_map_source: str,
    last_event_at: int | None,
    last_success_at: int | None,
    stale: bool,
    stale_reason: str | None,
    lifecycle_event: str | None,
    has_lifecycle_fact: bool,
    served_at: int | None = None,
) -> dict[str, Any]:
    """Build one complete snapshot payload (§2) from plain host reads."""
    team_dict = to_team_dict(team) if team is not None else None
    member_dicts = [to_member_dict(member) for member in members]
    task_dicts = [to_task_dict(task) for task in tasks]
    task_dicts.sort(key=lambda task: task["task_id"])
    dependency_map = to_dependency_map(dependencies)

    graph, per_task = build_graph(task_dicts, dependency_map)
    display_names = {member["member_name"]: member["display_name"] for member in member_dicts}

    tasks_by_assignee: dict[str, list[dict[str, Any]]] = {}
    for task in task_dicts:
        assignee = task["assignee"]
        if isinstance(assignee, str) and assignee:
            tasks_by_assignee.setdefault(assignee, []).append(task)

    # §2.4.0 deterministic convergence: at most one captain.
    # (1) ``leader_member_name`` is authoritative when it matches a live member —
    #     any other member flagged ``role == "leader"`` must NOT be marked then.
    # (2) ``role`` is only consulted when (1) matches nobody (empty / exited
    #     leader); with several such members the lexicographically smallest name
    #     wins so the outcome stays stable across snapshots.
    leader_name = (team_dict["leader_member_name"] if team_dict is not None else "") or ""
    captain_name: str | None = None
    if leader_name and any(member["member_name"] == leader_name for member in member_dicts):
        captain_name = leader_name
    else:
        fallback = sorted(
            member["member_name"] for member in member_dicts if member["role"] == "leader"
        )
        captain_name = fallback[0] if fallback else None
    member_blocks = [
        _member_block(member, tasks_by_assignee, captain_name) for member in member_dicts
    ]
    member_blocks.sort(key=lambda member: (0 if member["is_leader"] else 1, member["member_name"]))
    task_blocks = [_task_block(task, per_task, display_names) for task in task_dicts]

    status, status_source, _ = team_status(
        team_present=team_dict is not None,
        lifecycle_event=lifecycle_event,
        has_lifecycle_fact=has_lifecycle_fact,
        tasks=task_dicts,
        members=member_dicts,
    )
    team_block: dict[str, Any] | None = None
    if team_dict is not None:
        team_block = {
            "team_name": team_dict["team_name"],
            "display_name": team_dict["display_name"],
            "leader_member_name": team_dict["leader_member_name"],
            "desc": team_dict["desc"],
            "created_at": team_dict["created"] if isinstance(team_dict["created"], int) else 0,
            "status": status,
            "status_label": TEAM_STATUS_LABELS.get(status, status),
            "status_source": status_source,
            "progress": progress_of(task["status"] for task in task_dicts),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "generated_at": last_success_at if last_success_at is not None else (served_at or now_ms()),
        "served_at": served_at if served_at is not None else now_ms(),
        "stale": bool(stale),
        "stale_reason": stale_reason,
        "scope": {"kind": scope_kind, "key": scope_key},
        "availability": {
            "session_map_source": session_map_source,
            "team_readable": team_dict is not None,
            "last_event_at": last_event_at,
            "last_success_at": last_success_at,
        },
        "team": team_block,
        "members": member_blocks,
        "tasks": task_blocks,
        "graph": graph,
        "limits": {"members_total": len(member_blocks), "tasks_total": len(task_blocks)},
    }


def empty_snapshot(*, scope_kind: str, scope_key: str, reason: str = "no_session_map") -> dict[str, Any]:
    """§2.8 payload for a scope with no locatable team (never fabricate a team)."""
    return build_snapshot(
        scope_kind=scope_kind,
        scope_key=scope_key,
        team=None,
        members=(),
        tasks=(),
        dependencies={},
        session_map_source="none",
        last_event_at=None,
        last_success_at=None,
        stale=True,
        stale_reason=reason,
        lifecycle_event=None,
        has_lifecycle_fact=False,
    )


@dataclass
class _Record:
    """Last successful content plus the flags that ride alongside it.

    ``snapshot`` is **only** replaced by a successful re-read (§5.2.1 D2). A
    degraded read flips ``stale``/``stale_reason`` here instead of rewriting the
    payload, so the data fields stay byte-identical to the last success (D1) and
    ``generated_at`` keeps the last success time (D3).
    """

    snapshot: dict[str, Any]
    revision: int
    stale: bool = False
    stale_reason: str | None = None


class SnapshotCache:
    """In-memory snapshot store shared by event handlers and HTTP threads.

    ``revision`` is monotonic per scope and only bumps when new content is
    published, so long-poll waiters can key off it alone (§6).
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._records: dict[str, _Record] = {}
        self._published = 0

    def publish(self, key: str, snapshot: dict[str, Any]) -> int:
        """Store ``snapshot`` as the new content for ``key`` and wake waiters."""
        with self._condition:
            previous = self._records.get(key)
            revision = 1 if previous is None else previous.revision + 1
            snapshot["revision"] = revision
            self._records[key] = _Record(
                snapshot=snapshot,
                revision=revision,
                stale=bool(snapshot.get("stale")),
                stale_reason=snapshot.get("stale_reason"),
            )
            self._published += 1
            self._condition.notify_all()
            return revision

    def mark_stale(self, key: str, reason: str) -> int | None:
        """Flag the cached snapshot stale *without* touching its data (§5.2.1).

        ``False → True`` bumps ``revision`` exactly once so a long poll can hand
        the page the expiry badge; repeating the same failure keeps the revision
        stable so the page cannot spin (D5). The cached payload is never
        rewritten (D1/D2) and ``generated_at`` therefore keeps the last success
        time (D3). Returns the resulting revision, or ``None`` when this scope
        has never succeeded (the caller then publishes a cold-start §2.8 body).
        """
        with self._condition:
            record = self._records.get(key)
            if record is None:
                return None
            record.stale_reason = reason
            if record.stale:
                return record.revision
            record.stale = True
            record.revision += 1
            self._published += 1
            self._condition.notify_all()
            return record.revision

    def ensure(self, key: str, factory: Any) -> dict[str, Any]:
        """Return the cached snapshot, creating a §2.8 placeholder when absent."""
        with self._condition:
            record = self._records.get(key)
            if record is not None:
                return record.snapshot
            snapshot = factory()
            snapshot["revision"] = 1
            self._records[key] = _Record(
                snapshot=snapshot,
                revision=1,
                stale=True,
                stale_reason=snapshot.get("stale_reason") or "cold_start",
            )
            return snapshot

    def get(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """Return ``(snapshot, revision)``; ``(None, 0)`` when nothing cached."""
        with self._condition:
            record = self._records.get(key)
            if record is None:
                return None, 0
            return record.snapshot, record.revision

    def view(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """Return a private response-ready copy with the live stale flags (D1/D4).

        ``served_at`` is stamped here — every response (fresh, degraded or
        ``unchanged``) carries the moment it was served, while ``generated_at``
        still tells when the data itself was read.
        """
        with self._condition:
            record = self._records.get(key)
            if record is None:
                return None, 0
            payload = copy.deepcopy(record.snapshot)
            payload["revision"] = record.revision
            payload["served_at"] = now_ms()
            if record.stale:
                payload["stale"] = True
                payload["stale_reason"] = record.stale_reason or "ops_error"
            else:
                payload["stale"] = False
                payload["stale_reason"] = None
            return payload, record.revision

    def revision_of(self, key: str) -> int:
        """Current revision for ``key`` (0 when unknown)."""
        with self._condition:
            record = self._records.get(key)
            return 0 if record is None else record.revision

    def wait_change(self, key: str, since: int, timeout: float, should_stop: Any = None) -> int | None:
        """Block until ``key`` publishes past ``since``; returns the new revision."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                record = self._records.get(key)
                if record is not None and record.revision != since:
                    return record.revision
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if should_stop is not None and should_stop():
                    return None
                self._condition.wait(min(remaining, 0.5))

    def scope_count(self) -> int:
        """Number of scopes currently cached (``/health.snapshot.scopes``)."""
        with self._condition:
            return len(self._records)

    def summary(self) -> dict[str, Any]:
        """Compact health view of the newest cached snapshot."""
        with self._condition:
            newest = None
            for record in self._records.values():
                if newest is None or record.revision > newest.revision:
                    newest = record
            if newest is None:
                return {
                    "revision": 0,
                    "generated_at": None,
                    "stale": True,
                    "session_map_source": "none",
                    "scopes": 0,
                }
            snapshot = newest.snapshot
            availability = snapshot.get("availability") or {}
            return {
                "revision": newest.revision,
                "generated_at": snapshot.get("generated_at"),
                "stale": newest.stale,
                "stale_reason": newest.stale_reason if newest.stale else None,
                "session_map_source": availability.get("session_map_source", "none"),
                "scopes": len(self._records),
            }

    def drop(self, key: str) -> None:
        """Forget one scope (used when a session map is invalidated)."""
        with self._condition:
            self._records.pop(key, None)

    def clear(self) -> None:
        """Forget every scope (kept for tests and reload hygiene)."""
        with self._condition:
            self._records.clear()
            self._condition.notify_all()
