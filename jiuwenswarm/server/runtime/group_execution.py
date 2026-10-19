"""Durable group execution cards; core remains the task scheduler."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time

from openjiuwen.agent_teams.team_workspace.frontmatter import atomic_write

from jiuwenswarm.server.runtime.group_service import GroupError, group_lock, group_path, read_group
from jiuwenswarm.server.runtime.session import lifecycle

logger = logging.getLogger(__name__)
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted", "timed_out"})


class GroupExecutions:
    def __init__(self, group_id: str):
        self.group_id = group_id
        self.path = group_path(group_id).with_name("executions.json")

    def read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise GroupError("GROUP_CORRUPT", "invalid execution state") from exc
        if not isinstance(records, dict):
            raise GroupError("GROUP_CORRUPT", "invalid execution state")
        for record in records.values():
            if "definition_id" in record:
                record.setdefault("id", record.pop("definition_id"))
            if "member_kind" in record:
                record["member_kind"] = {"expert": "agent_template", "expert_team": "agent_group"}.get(
                    record["member_kind"], record["member_kind"]
                )
            record.setdefault("reply_id", record["execution_id"])
            if record["execution_kind"] == "team" and not record.get("run_key") and not record.get("team_id"):
                record["execution_kind"] = "agent"
            record["presentation"] = "card"
        return records

    def get(self, execution_id: str) -> dict:
        if not isinstance(execution_id, str) or not execution_id:
            raise GroupError("BAD_REQUEST", "execution_id must be nonempty text")
        record = self.read().get(execution_id)
        if record is None:
            raise GroupError("NOT_FOUND", "execution not found")
        return copy.deepcopy(record)

    def active(self, member_name: str | None) -> dict | None:
        for record in self.read().values():
            if record["member_name"] == member_name and record["status"] not in TERMINAL:
                return copy.deepcopy(record)
        return None

    def save(self, records: dict) -> None:
        with lifecycle.resource_lock("session", self.group_id):
            lifecycle.guard(self.group_id)
            group = read_group(self.group_id)
            if group is None or group["status"] == "deleting":
                raise GroupError("GROUP_BUSY", "group is unavailable")
            atomic_write(self.path, json.dumps(records, ensure_ascii=False))

    async def reconcile(self) -> None:
        """Release orphaned executions without rerunning work or disturbing live owners."""
        changed = []
        async with group_lock(self.group_id):
            records = self.read()
            for record in records.values():
                if record["status"] in TERMINAL:
                    continue
                owner = record.get("owner")
                if owner is None:
                    from jiuwenswarm.server.runtime.group_service import resolve_group_backend

                    group = read_group(self.group_id)
                    if group is None:
                        return
                    if await resolve_group_backend(group) is not None:
                        continue
                else:
                    try:
                        if lifecycle.runtime_owner_alive(owner):
                            continue
                    except lifecycle.LifecycleError:
                        # Lack of process access is not proof that a worker has died.
                        continue
                record.update(status="interrupted", phase="owner_lost", error_code="EXECUTION_OWNER_LOST")
                record["revision"] += 1
                record["updated_at"] = time.time()
                changed.append(copy.deepcopy(record))
            if changed:
                self.save(records)
        for record in changed:
            await self.publish(record, "finished")

    async def fail_pending(self, execution_ids: list[str], phase: str, *, include_accepted=False) -> None:
        """Finish only this request's undelivered work; other members keep running."""
        changed = []
        async with group_lock(self.group_id):
            records = self.read()
            for execution_id in execution_ids:
                record = records.get(execution_id)
                if record is None or record["status"] != "queued":
                    continue
                if record.get("delivery_status") == "accepted" and not include_accepted:
                    continue
                record.update(status="failed", phase=phase, error_code="TASK_NOT_DELIVERED")
                record["revision"] += 1
                record["updated_at"] = time.time()
                changed.append(copy.deepcopy(record))
            if changed:
                self.save(records)
        for record in changed:
            await self.publish(record, "finished")

    async def accept_delivery(self, client_message_id: str) -> None:
        request_key = hashlib.sha256(client_message_id.encode()).hexdigest()
        for record in self.read().values():
            if record.get("request_key") == request_key:
                await self.update(record["execution_id"], delivery_status="accepted")

    async def publish(self, record: dict, action: str) -> None:
        from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

        group = read_group(self.group_id)
        if group is None or group["status"] == "deleting":
            return
        payload = {
            "event_type": f"group.execution.{action}",
            "group_id": self.group_id,
            **copy.deepcopy(public_execution(record)),
        }
        try:
            await WebSocketGatewayPushTransport().send_push({
                "request_id": f'{record["execution_id"]}:{record["revision"]}',
                "session_id": self.group_id,
                "channel_id": group["channel_id"],
                "payload": payload,
                "is_complete": False,
            })
        except Exception:
            # The persisted snapshot is authoritative when delivery is unavailable.
            logger.warning("Group execution push failed group=%s", self.group_id, exc_info=True)

    async def create(self, members: list[str], task: str, token: str) -> list[dict]:
        if not isinstance(task, str) or not task.strip():
            raise GroupError("BAD_REQUEST", "task must be nonempty text")
        if len(task) > 20000:
            raise GroupError("BAD_REQUEST", "task exceeds 20000 characters")
        if not isinstance(token, str) or not token.strip():
            raise GroupError("BAD_REQUEST", "client_request_id must be nonempty text")
        if len(token) > 256:
            raise GroupError("BAD_REQUEST", "client_request_id exceeds 256 characters")
        if not isinstance(members, list) or not members:
            raise GroupError("BAD_REQUEST", "task requires member names")
        if any(not isinstance(name, str) for name in members):
            raise GroupError("BAD_REQUEST", "task members must be strings")
        if len(set(members)) != len(members):
            raise GroupError("BAD_REQUEST", "duplicate task member")
        await self.reconcile()
        result = []
        async with group_lock(self.group_id):
            group = read_group(self.group_id)
            if group is None:
                raise GroupError("NOT_FOUND", "group not found")
            roster = {member["member_name"]: member for member in group["members"]}
            records = self.read()
            request_key = hashlib.sha256(token.encode()).hexdigest()
            for record in records.values():
                if record.get("request_key") != request_key:
                    continue
                if record["task"] != task or record["request_members"] != sorted(members):
                    raise GroupError("CONFLICT", "request ID was used for another task or member set")
            for name in members:
                if name not in roster:
                    raise GroupError("BAD_REQUEST", "task target is not a group expert")
                compiled = group["compiled"][name]["member"]
                if compiled.get("external_cli") is not None:
                    raise GroupError("UNSUPPORTED_RUNTIME", "CLI task cards require a runtime lifecycle adapter")
                key = hashlib.sha256(json.dumps([self.group_id, token, name]).encode()).hexdigest()
                existing = records.get(key)
                if existing is not None:
                    if existing["task"] != task:
                        raise GroupError("CONFLICT", "request ID was used for another task")
                    result.append(copy.deepcopy(existing))
                    continue
                if self.active(name) is not None:
                    raise GroupError("MEMBER_BUSY", "member already has an active execution")
                now = time.time()
                record = {
                    "execution_id": key,
                    "reply_id": key,
                    "presentation": "card",
                    "owner": lifecycle.current_runtime_owner(),
                    "delivery_status": "pending",
                    "member_kind": roster[name]["kind"],
                    "id": roster[name]["id"],
                    "display_name": roster[name].get("display_name") or name,
                    "request_key": request_key,
                    "request_members": sorted(members),
                    "execution_kind": "agent",
                    "member_name": name,
                    "title": task[:200],
                    "task": task,
                    "status": "queued",
                    "phase": "queued",
                    "progress": None,
                    "team_id": None,
                    "organization_id": None,
                    "root_task_id": None,
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                    "result": None,
                    "attachments": [],
                    "message_id": None,
                }
                records[key] = record
                result.append(copy.deepcopy(record))
            self.save(records)
        for record in result:
            await self.publish(record, "created")
        return result

    async def update(self, execution_id: str, **changes) -> dict:
        async with group_lock(self.group_id):
            records = self.read()
            if execution_id not in records:
                raise GroupError("NOT_FOUND", "execution not found")
            record = records[execution_id]
            if record["status"] in TERMINAL:
                return copy.deepcopy(record)
            if all(record.get(key) == value for key, value in changes.items()):
                return copy.deepcopy(record)
            record.update(changes)
            record["revision"] += 1
            record["updated_at"] = time.time()
            self.save(records)
            result = copy.deepcopy(record)
        action = "finished" if result["status"] in TERMINAL else "updated"
        await self.publish(result, action)
        return result

    async def finish(self, execution_id: str, backend, result: str, attachments: list) -> dict:
        if not isinstance(result, str) or not result.strip():
            raise GroupError("BAD_REQUEST", "result must be nonempty")
        if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
            raise GroupError("BAD_REQUEST", "attachments must be JSON objects")
        # Publication has a separate cross-process lock; state updates still use the group lock.
        lock_key = hashlib.sha256(json.dumps([self.group_id, execution_id]).encode()).hexdigest()
        async with group_lock(f"group-result-{lock_key}"):
            record = self.get(execution_id)
            if record["member_name"] != backend.member_name:
                raise GroupError("NOT_FOUND", "execution not found")
            if record["status"] == "completed":
                if (record["result"], record["attachments"]) != (result, attachments):
                    raise GroupError("CONFLICT", "execution already has another result")
                return record
            if record["status"] in TERMINAL:
                raise GroupError("CONFLICT", "execution is no longer active")
            if record["result"] is not None:
                if (record["result"], record["attachments"]) != (result, attachments):
                    raise GroupError("CONFLICT", "execution already has another result")
            await self.update(execution_id, phase="publishing", result=result, attachments=attachments)
            async with group_lock(self.group_id):
                records = self.read()
                record = records[execution_id]
                if record["status"] in TERMINAL:
                    raise GroupError("CONFLICT", "execution is no longer active")
                group = read_group(self.group_id)
                if group is None or group["status"] == "deleting":
                    raise GroupError("GROUP_BUSY", "group is unavailable")
                lifecycle.guard(self.group_id)
                posted = await backend.append_group_message(
                    backend.member_name, result, attachments=attachments,
                    client_message_id=f"execution-result-{execution_id}", mentions=[],
                )
                record.update(status="completed", phase="completed", message_id=posted.message.message_id)
                record["revision"] += 1
                record["updated_at"] = time.time()
                self.save(records)
                completed = copy.deepcopy(record)
            await self.publish(completed, "finished")
            return completed

    async def task_event(self, execution_id: str, payload: dict) -> None:
        """Count authoritative core task states, never model-written percentages."""
        event = payload.get("event")
        if payload.get("event_type") != "team.task" or not isinstance(event, dict):
            return
        task_id, status = event.get("task_id"), event.get("status")
        if status is None:
            states = {
                "team.task.created": "pending", "team.task.claimed": "in_progress",
                "team.task.started": "in_progress", "team.task.completed": "completed",
                "team.task.cancelled": "cancelled",
            }
            status = states.get(event.get("type"))
        if not task_id or not status:
            return
        async with group_lock(self.group_id):
            records = self.read()
            if execution_id not in records:
                raise GroupError("NOT_FOUND", "execution not found")
            record = records[execution_id]
            if record["status"] in TERMINAL:
                return
            tasks = dict(record.get("tasks", {}))
            details = copy.deepcopy(record.get("task_details", {}))
            detail = dict(details.get(str(task_id), {}))
            for field in ("task_id", "title", "content", "member_id", "team_id", "type"):
                if field in event:
                    detail[field] = event[field]
            detail["status"] = str(status)
            if tasks.get(str(task_id)) == str(status) and details.get(str(task_id)) == detail:
                return
            tasks[str(task_id)] = str(status)
            details[str(task_id)] = detail
            completed = sum(value == "completed" for value in tasks.values())
            record.update(
                tasks=tasks, task_details=details, phase="executing",
                progress={"completed": completed, "total": len(tasks)},
            )
            record["revision"] += 1
            record["updated_at"] = time.time()
            self.save(records)
            result = copy.deepcopy(record)
        await self.publish(result, "updated")


def public_execution(record: dict) -> dict:
    internal_fields = {"request_key", "request_members", "run_key", "owner"}
    result = {}
    for key, value in record.items():
        if key not in internal_fields:
            result[key] = value
    return result


def hide_group_internal_event(session_id: str, event: dict) -> bool:
    if event.get("event_type") not in {
        "chat.delta", "chat.reasoning", "chat.final", "chat.tool_call", "chat.tool_result",
    }:
        return False
    return group_path(session_id).is_file()


async def project_group_stream_event(session_id: str, event: dict) -> dict | None:
    """Only persisted public conversation messages cross the group chat boundary."""
    if hide_group_internal_event(session_id, event):
        return None
    event_type = event.get("event_type")
    if event_type not in {
        "team.task", "team.message", "team.member", "team.group_message.accepted", "team.error", "team.interact.failed",
    }:
        return event
    group = read_group(session_id)
    if group is None:
        return event
    if event_type == "team.group_message.accepted":
        client_id = event.get("client_message_id") or event.get("message", {}).get("client_message_id")
        if isinstance(client_id, str):
            await GroupExecutions(session_id).accept_delivery(client_id)
        return event
    if event_type in {"team.error", "team.interact.failed"}:
        store = GroupExecutions(session_id)
        await store.fail_pending(
            list(store.read()), "delivery_failed", include_accepted=event_type == "team.error",
        )
        return event
    if event_type == "team.member":
        inner = event.get("event") or {}
        status = inner.get("new_status")
        if inner.get("type") == "team.member.execution_changed" and status in {"failed", "cancelled", "timed_out"}:
            store = GroupExecutions(session_id)
            active = store.active(inner.get("member_id"))
            if active is not None:
                await store.update(active["execution_id"], status=status, phase=status)
        return event
    if event_type == "team.task":
        inner = event.get("event") or {}
        member_name = inner.get("member_id")
        if member_name:
            store = GroupExecutions(session_id)
            active = store.active(member_name)
            if active is not None:
                await store.task_event(active["execution_id"], event)
        return None
    if event_type != "team.message":
        return event
    inner = event.get("event") or {}
    message_id = inner.get("message_id")
    if not message_id:
        return None
    from openjiuwen.agent_teams.context import set_session_id, reset_session_id
    from openjiuwen.agent_teams.group_chat.handler import group_metadata, conversation_message
    from jiuwenswarm.server.runtime.group_service import resolve_group_backend

    backend = await resolve_group_backend(group)
    if backend is None:
        return None
    token = set_session_id(session_id)
    try:
        row = await backend.db.message.get_message(message_id)
        if row is None or not group_metadata(row):
            return None
        message = conversation_message(row, session_id).model_dump(mode="json")
        from jiuwenswarm.server.runtime.group_reply import correlate_public_reply

        correlation = correlate_public_reply(session_id, message)
        return {
            "event_type": "group.message", "group_id": session_id,
            "message": message, **correlation,
        }
    finally:
        reset_session_id(token)


async def project_organization_progress(session_id: str, root_team_id: str, progress: dict) -> None:
    """Project core organization milestones without fabricating a scheduler or percentage."""
    group = read_group(session_id)
    if group is None or group["team_name"] != root_team_id:
        return
    root_task_id = progress.get("root_task_id")
    phase = progress.get("phase")
    if not isinstance(root_task_id, str) or not isinstance(phase, str):
        return
    key = hashlib.sha256(json.dumps([session_id, "organization", root_team_id, root_task_id]).encode()).hexdigest()
    store = GroupExecutions(session_id)
    created = False
    async with group_lock(session_id):
        records = store.read()
        if key not in records:
            now = time.time()
            records[key] = {
                "execution_id": key, "execution_kind": "organization", "member_name": None,
                "presentation": "card", "owner": lifecycle.current_runtime_owner(),
                "team_id": root_team_id, "root_task_id": root_task_id,
                "organization_id": progress.get("organization_id"),
                "title": progress.get("title") or "Organization task", "task": None,
                "status": "queued" if phase == "queued" else "running", "phase": phase, "progress": None,
                "revision": 1, "created_at": now, "updated_at": now,
                "result": None, "attachments": [], "message_id": None,
            }
            store.save(records)
            created = True
        record = copy.deepcopy(records[key])
    if created:
        await store.publish(record, "created")
    if phase == "queued" and not created:
        return
    status = "running"
    if phase == "queued":
        status = "queued"
    elif phase in {"summary_completed", "root_completed"}:
        status = "completed"
    elif phase == "failed":
        status = "failed"
    await store.update(key, status=status, phase=phase)
