# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-connection harness choice and the one chat.send it may rewrite."""

from __future__ import annotations

from collections.abc import Mapping

UI_PACKAGE_ID = "sample.harness-workbench"
ACTION_ID = "harness.next-task-mode"
MODES = {
    "normal": "",
    "claude": "harness-claude-expert",
    "codex": "harness-codex-expert",
}
_TEMPLATE_MODES = {template: mode for mode, template in MODES.items() if template}


class HarnessNewTaskChoice:
    """Remember one browser connection's harness choice.

    ``session.create`` arms the next ``chat.send`` whose session has not already
    sent a message on this connection. A selection that names an already sent
    session is ignored. A selection with no session updates the next new task.
    History and metadata requests do not count as speaking.
    """

    def __init__(self) -> None:
        self._mode: dict[str, str] = {}
        self._armed: set[str] = set()
        self._seen: dict[str, set[str]] = {}
        self._new_session: dict[str, str] = {}

    def observe(self, method: str, params: object, connection_id: str) -> dict | None:
        """Record the frame and return replacement params only for an armed first send."""
        if not connection_id:
            return None
        if method == "webui.dispatch":
            self.remember_selection(params, connection_id)
            return None
        if method == "session.create":
            self._armed.add(connection_id)
            session_id = _session_id(params)
            if session_id:
                self._new_session[connection_id] = session_id
            return None
        if method != "chat.send":
            return None
        session_id = _session_id(params)
        if connection_id not in self._armed:
            self._remember_session(params, connection_id)
            return None
        if not session_id or session_id in self._seen.get(connection_id, set()):
            return None
        self._armed.discard(connection_id)
        self._seen.setdefault(connection_id, set()).add(session_id)
        return _with_template(params, self._mode.get(connection_id, "normal"))

    def drop(self, connection_id: str) -> None:
        """Forget a browser connection after it disconnects."""
        self._mode.pop(connection_id, None)
        self._armed.discard(connection_id)
        self._seen.pop(connection_id, None)
        self._new_session.pop(connection_id, None)

    def is_new_task_session(self, connection_id: str, session_id: str) -> bool:
        """Return whether this session can still take the next-task preview."""
        if not session_id or session_id in self._seen.get(connection_id, set()):
            return False
        created = self._new_session.get(connection_id, "")
        return bool(created) and created == session_id

    def selected_mode(self, connection_id: str) -> str:
        """Return the pending mode for a browser connection."""
        return self._mode.get(connection_id, "normal")

    def remember_selection(self, params: object, connection_id: str) -> str | None:
        """Store a valid selector event and return the newly selected mode."""
        if not isinstance(params, Mapping) or params.get("event") not in ("ui.click", "ui.select"):
            return None
        target = params.get("target")
        payload = params.get("payload")
        if not isinstance(target, Mapping):
            return None
        package_id = target.get("package_id", "")
        if package_id not in ("", UI_PACKAGE_ID):
            return None
        if not isinstance(payload, Mapping) or payload.get("action_id") != ACTION_ID:
            return None
        session_id = _session_id(target) or _session_id(params)
        if session_id and session_id in self._seen.get(connection_id, set()):
            return None
        value = _choice_value(payload)
        if value in MODES:
            self._mode[connection_id] = value
            return value
        return None

    def _remember_session(self, params: object, connection_id: str) -> None:
        session_id = _session_id(params)
        if session_id:
            self._seen.setdefault(connection_id, set()).add(session_id)


def _choice_value(payload: Mapping) -> str:
    args = payload.get("args")
    if isinstance(args, Mapping):
        value = args.get("value")
        if isinstance(value, str) and value in MODES:
            return value
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ""
    value = data.get("value")
    if value not in MODES:
        value = data.get("key")
    if isinstance(value, str) and value in MODES:
        return value
    return ""


def _session_id(params: object) -> str:
    if not isinstance(params, Mapping):
        return ""
    value = params.get("session_id")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _with_template(params: object, mode: str) -> dict:
    updated = dict(params) if isinstance(params, Mapping) else {}
    if "agent_template_name" in updated:
        return updated
    template = MODES.get(mode, "")
    if template:
        updated["agent_template_name"] = template
    return updated


def harness_mode(params: object) -> str | None:
    """Return the Harness mode explicitly carried by a chat request."""
    if not isinstance(params, Mapping) or "agent_template_name" not in params:
        return None
    template = params.get("agent_template_name")
    if not isinstance(template, str):
        return "normal"
    return _TEMPLATE_MODES.get(template.strip(), "normal")
