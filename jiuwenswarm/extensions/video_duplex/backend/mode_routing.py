"""Resolve voice execution from authenticated session metadata, not model arguments."""

from jiuwenswarm.runtime.request import resolve_agent_request_mode
from jiuwenswarm.common.mode_matrix import deprecate_mode, is_code_profile_mode, is_single_agent_mode


def execution_target(metadata: dict) -> dict:
    work_mode = str(metadata.get("work_mode") or "")
    mode = str(metadata.get("mode") or "agent")
    if work_mode == "design" or mode == "team.design":
        mode, work_mode = "team.design", "design"
    else:
        _, _, mode = resolve_agent_request_mode(mode, work_mode=work_mode)
        mode = deprecate_mode(mode)
        if not is_single_agent_mode(mode):
            raise ValueError("Full-duplex supports single-agent Work/Code chats and Design projects")
        work_mode = "code" if is_code_profile_mode(mode) else "work"
    return {
        "mode": mode, "work_mode": work_mode,
        **{key: metadata[key] for key in ("project_id", "project_dir", "model_name", "model_selection")
           if metadata.get(key)},
    }


async def resolve_execution_target(client, owner: str, scope: str) -> dict:
    if not scope.startswith("task-duplex:"):
        return {"mode": "agent", "work_mode": "work"}
    from jiuwenswarm.common.schema.message import ReqMethod
    from .task_adapter import task_agent_query

    session = scope.removeprefix("task-duplex:")
    metadata = await task_agent_query(client, ReqMethod.SESSION_GET_METADATA,
                                     {"session_id": session}, session, owner)
    if not metadata or str(metadata.get("user_id") or "").strip() != owner:
        raise ValueError("Conversation is unavailable to this user")
    return execution_target(metadata)
