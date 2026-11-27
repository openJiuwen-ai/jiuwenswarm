from __future__ import annotations

from typing import Any

from jiuwenclaw.version import __version__


def build_acp_initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": 1,
        "agentInfo": {
            "name": "jiuwenclaw",
            "title": "JiuwenClaw",
            "version": __version__,
        },
        "agentCapabilities": {
            "loadSession": False,
            "promptCapabilities": {
                "image": False,
                "audio": False,
                "embeddedContext": False,
            },
            "sessionCapabilities": {
                "list": {},
            },
            "mcpCapabilities": {
                "http": False,
                "sse": False,
            },
        },
        "authMethods": [],
    }


def build_acp_session_new_result(session_id: str) -> dict[str, Any]:
    return {
        "sessionId": str(session_id or "").strip(),
        "configOptions": [],
    }


def build_acp_session_list_result(session_ids: list[str]) -> dict[str, Any]:
    normalized = []
    seen: set[str] = set()
    for session_id in session_ids:
        sid = str(session_id or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        normalized.append({"sessionId": sid})
    return {"sessions": normalized}


def build_acp_prompt_result(
    *,
    stop_reason: str,
    user_message_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"stopReason": stop_reason}
    if isinstance(user_message_id, str) and user_message_id.strip():
        result["userMessageId"] = user_message_id.strip()
    return result


__all__ = [
    "build_acp_initialize_result",
    "build_acp_prompt_result",
    "build_acp_session_list_result",
    "build_acp_session_new_result",
]
