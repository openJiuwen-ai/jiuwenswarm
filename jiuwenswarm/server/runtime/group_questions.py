"""Persist group questions and resume the native ask-user interaction owner."""

import copy
import hashlib
import json
import logging
import time
from weakref import WeakValueDictionary

from openjiuwen.agent_teams.context import set_session_id, reset_session_id
from openjiuwen.agent_teams.team_workspace.frontmatter import atomic_write

from jiuwenswarm.agents.harness.common.rails.ask_user_contract import build_ask_user_interactive_input
from jiuwenswarm.server.runtime.group_service import GroupError, group_lock, group_path, read_group
from jiuwenswarm.server.runtime.session import lifecycle

logger = logging.getLogger(__name__)
_targets = WeakValueDictionary()


def register_question_target(group_id: str, session_id: str, member_name: str, agent) -> str:
    key = hashlib.sha256(json.dumps([group_id, session_id, member_name]).encode()).hexdigest()
    _targets[key] = agent
    return key


class GroupQuestions:
    def __init__(self, group_id: str):
        self.group_id = group_id
        self.path = group_path(group_id).with_name("questions.json")

    def read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise GroupError("GROUP_CORRUPT", "invalid group questions") from exc
        if not isinstance(records, dict):
            raise GroupError("GROUP_CORRUPT", "invalid group questions")
        return records

    def save(self, records: dict) -> None:
        with lifecycle.resource_lock("session", self.group_id):
            lifecycle.guard(self.group_id)
            group = read_group(self.group_id)
            if group is None or group["status"] == "deleting":
                raise GroupError("GROUP_BUSY", "group is unavailable")
            atomic_write(self.path, json.dumps(records, ensure_ascii=False))

    def pending(self, member_name: str | None = None) -> list[dict]:
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions, TERMINAL

        executions = GroupExecutions(self.group_id).read()
        result = []
        for record in self.read().values():
            if record["status"] != "pending":
                continue
            if record["target"] not in _targets:
                continue
            if member_name is not None and record["member_name"] != member_name:
                continue
            execution_id = record.get("execution_id")
            if execution_id and executions.get(execution_id, {}).get("status") in TERMINAL:
                continue
            result.append(self.public(record))
        return result

    @staticmethod
    def public(record: dict) -> dict:
        return {key: copy.deepcopy(record[key]) for key in (
            "request_id", "questions", "source", "member_name", "execution_id", "created_at",
        )}

    async def publish(self, payload: dict) -> None:
        from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

        group = read_group(self.group_id)
        if group is None or group["status"] == "deleting":
            return
        try:
            await WebSocketGatewayPushTransport().send_push({
                "request_id": payload["request_id"], "session_id": self.group_id,
                "channel_id": group["channel_id"], "is_complete": False,
                "payload": {**payload, "group_id": self.group_id, "session_id": self.group_id},
            })
        except Exception:
            logger.warning("Group question push failed group=%s", self.group_id, exc_info=True)

    async def ask(self, payload: dict, target: str, member_name: str, execution_id: str | None) -> None:
        native_id = payload["request_id"]
        request_id = hashlib.sha256(json.dumps([self.group_id, target, execution_id, native_id]).encode()).hexdigest()
        async with group_lock(self.group_id):
            records = self.read()
            previous = records.get(request_id)
            if previous is not None and previous["status"] == "answered":
                return
            record = {
                "request_id": request_id, "native_request_id": native_id,
                "questions": payload["questions"], "source": "ask_user_interrupt",
                "target": target, "member_name": member_name, "execution_id": execution_id,
                "created_at": previous["created_at"] if previous else time.time(), "status": "pending",
            }
            records[request_id] = record
            self.save(records)
        await self.publish({"event_type": "chat.ask_user_question", **self.public(record)})

    async def answer(self, request_id: str, answers: list) -> dict:
        if not isinstance(request_id, str) or not request_id.strip():
            raise GroupError("BAD_REQUEST", "request_id is required")
        if not isinstance(answers, list) or not answers:
            raise GroupError("BAD_REQUEST", "answers must be a nonempty array")
        for answer in answers:
            if not isinstance(answer, dict):
                raise GroupError("BAD_REQUEST", "answers must contain objects")
            options = answer.get("selected_options", [])
            if not isinstance(options, list) or any(not isinstance(value, str) for value in options):
                raise GroupError("BAD_REQUEST", "selected_options must contain text")
            if not isinstance(answer.get("custom_input", ""), str):
                raise GroupError("BAD_REQUEST", "custom_input must be text")
        lock_id = hashlib.sha256(json.dumps([self.group_id, request_id]).encode()).hexdigest()
        async with group_lock(f"question-{lock_id}"):
            record = self.read().get(request_id)
            if record is None:
                raise GroupError("NOT_FOUND", "question not found")
            if record["status"] == "answered":
                if record["answers"] != answers:
                    raise GroupError("CONFLICT", "question already has another answer")
                return {"request_id": request_id, "accepted": True}
            target = _targets.get(record["target"])
            if target is None:
                raise GroupError("NOT_RUNNING", "question owner is no longer running")
            if not any(item["request_id"] == request_id for item in self.pending()):
                raise GroupError("CONFLICT", "question execution has ended")
            question_texts = {item["question"] for item in record["questions"]}
            for answer in answers:
                if answer.get("question") not in question_texts:
                    raise GroupError("BAD_REQUEST", "answer does not match a pending question")
            native_id = record["native_request_id"]
            query = build_ask_user_interactive_input(native_id, answers)
            if not query.user_inputs[native_id]["answers"]:
                raise GroupError("BAD_REQUEST", "answer must contain nonempty text")
            from jiuwenswarm.server.runtime.group_execution import GroupExecutions

            execution_id = record.get("execution_id")
            execution = GroupExecutions(self.group_id).get(execution_id) if execution_id else {}
            session_id = execution.get("team_session_id") or self.group_id
            token = set_session_id(session_id)
            try:
                await target.send(query)
            finally:
                reset_session_id(token)
            async with group_lock(self.group_id):
                records = self.read()
                records[request_id].update(status="answered", answers=answers)
                self.save(records)
        await self.publish({"event_type": "chat.ask_user_answer", "request_id": request_id})
        return {"request_id": request_id, "accepted": True}
