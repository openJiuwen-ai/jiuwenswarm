# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Task tools - wraps TaskMemoryService as @tool decorated functions."""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from jiuwenclaw.agentserver.tools import (
    AddMemoryRequest,
    JSONFileConnector,
    TaskMemoryService,
    ce_config as _ce_config,
    tool,
)

from jiuwenclaw.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)


@dataclass
class TaskAddParams:
    """Encapsulates all parameters for the task_add operation."""
    content: str
    section: str = "general"
    when_to_use: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    query: Optional[str] = None
    label: Optional[str] = None
    tools_used: Optional[List[Dict[str, Any]]] = None


# Path for persisting task_add entries
def _get_task_data_path() -> str:
    return str(get_user_workspace_dir() / "agent" / "workspace" / "task-data.json")


_connector = JSONFileConnector(indent=2)

_service: Optional[Any] = None  # TaskMemoryService instance


def _apply_ce_defaults() -> None:
    """Seed the context_evolver config from the UI config (config.yaml), falling back to env vars."""
    try:
        from jiuwenclaw.config import get_config

        cfg = get_config()
        react_cfg = cfg.get("react", {})
        model_client = react_cfg.get("model_client_config", {})
        embed_cfg = cfg.get("embed", {})
        models_default = cfg.get("models", {}).get("default", {}).get("model_client_config", {})

        # Resolve each value: UI config → env var → empty (no hardcoded fallback)
        mappings = {
            "API_KEY": (
                model_client.get("api_key")
                or models_default.get("api_key")
                or embed_cfg.get("embed_api_key")
                or os.getenv("API_KEY", "")
            ),
            "API_BASE": (
                model_client.get("api_base")
                or models_default.get("api_base")
                or embed_cfg.get("embed_base_url")
                or os.getenv("API_BASE", "")
            ),
            "MODEL_NAME": (
                react_cfg.get("model_name")
                or os.getenv("MODEL_NAME", "")
            ),
            "MODEL_PROVIDER": (
                model_client.get("client_provider")
                or models_default.get("client_provider")
                or os.getenv("MODEL_PROVIDER", "")
            ),
            "EMBEDDING_MODEL": (
                embed_cfg.get("embed_model")
                or os.getenv("EMBEDDING_MODEL")
                or os.getenv("EMBED_MODEL")
                or cfg.get("task_memory", {}).get("embedding_model", "text-embedding-3-small")
            ),
        }

        for key, value in mappings.items():
            if value and not str(value).startswith("${"):  # skip unresolved placeholders
                _ce_config.set_value(key, str(value))
    except Exception as exc:
        logger.debug("[Experience] Could not apply CE defaults: %s", exc)


def _is_task_memory_enabled() -> bool:
    """Check if task memory is enabled via config or environment."""
    from jiuwenclaw.config import get_config
    cfg = get_config()
    task_memory_cfg = cfg.get("task_memory", {})
    return bool(task_memory_cfg.get("enabled", False))


def _get_service():
    """Lazily initialize and return the TaskMemoryService singleton."""
    global _service
    if _service is not None:
        return _service

    if TaskMemoryService is None:
        logger.warning("[Experience] TaskMemoryService not available")
        return None

    _apply_ce_defaults()

    from jiuwenclaw.config import get_config
    cfg = get_config()
    task_memory_cfg = cfg.get("task_memory", {})
    embed_cfg = cfg.get("embed", {})

    llm_model = (
        task_memory_cfg.get("llm_model")
        or os.getenv("TASK_MEMORY_LLM_MODEL")
        or os.getenv("MODEL_NAME")
    )
    embedding_model = (
        task_memory_cfg.get("embedding_model")
        or os.getenv("TASK_MEMORY_EMBED_MODEL")
        or embed_cfg.get("embed_model")
        or os.getenv("EMBED_MODEL")
        or os.getenv("EMBEDDING_MODEL")
    )
    api_key = (
        task_memory_cfg.get("api_key")
        or os.getenv("TASK_MEMORY_API_KEY")
        or embed_cfg.get("embed_api_key")
        or os.getenv("EMBED_API_KEY")
        or os.getenv("API_KEY")
    )
    api_base = (
        task_memory_cfg.get("api_base")
        or os.getenv("TASK_MEMORY_API_BASE")
        or embed_cfg.get("embed_base_url")
        or os.getenv("EMBED_API_BASE")
        or os.getenv("API_BASE")
    )
    retrieval_algo = task_memory_cfg.get("retrieval_algo") or os.getenv("TASK_MEMORY_RETRIEVAL_ALGO")
    summary_algo = task_memory_cfg.get("summary_algo") or os.getenv("TASK_MEMORY_SUMMARY_ALGO")

    if not api_key:
        logger.warning("[Experience] No API key found; task tools will be disabled")
        return None
    if not llm_model:
        logger.warning("[Experience] No LLM model configured; task tools will be disabled")
        return None
    if not embedding_model:
        logger.warning("[Experience] No embedding model configured; task tools will be disabled")
        return None

    try:
        kwargs: Dict[str, Any] = dict(
            llm_model=llm_model,
            embedding_model=embedding_model,
            api_key=api_key,
        )
        if api_base:
            kwargs["api_key"] = api_key  # already set, just making sure
            # Pass via environment since TaskMemoryService reads from its own config
            # We set them explicitly via kwargs instead
        if retrieval_algo:
            kwargs["retrieval_algo"] = retrieval_algo
        if summary_algo:
            kwargs["summary_algo"] = summary_algo

        # TaskMemoryService reads API_BASE from its config; patch via kwarg if supported
        # The constructor accepts no api_base param, so we pass via the wrappers internally.
        # Temporarily set env vars so the wrappers pick them up.
        _orig_base = os.environ.get("API_BASE")
        if api_base:
            os.environ["API_BASE"] = api_base

        _service = TaskMemoryService(**kwargs)

        if api_base and _orig_base is None:
            del os.environ["API_BASE"]
        elif api_base and _orig_base is not None:
            os.environ["API_BASE"] = _orig_base

        logger.info("[Experience] TaskMemoryService initialized (llm=%s, embed=%s)", llm_model, embedding_model)
    except Exception as exc:
        logger.error("[Experience] Failed to initialize TaskMemoryService: %s", exc)
        _service = None

    return _service


@tool(
    name="experience_retrieve",
    description=(
        "Retrieve relevant past memories and lessons for the current task. "
        "Call this at the start of every task to check for prior experience."
    ),
)
async def experience_retrieve(
    query: str,
) -> Dict[str, Any]:
    """Retrieve task memory relevant to a query.

    Args:
        query: The task or question to search memory for.

    Returns:
        Dictionary with memory_string and retrieved_memory list.
    """
    
    logger.info("[Exp] experience_retrieve called: query=%s", query[:80])

    # Load persisted entries from task-data.json
    persisted_memories: List[Dict[str, Any]] = []
    persisted_lines: List[str] = []
    try:
        if _connector.exists(_get_task_data_path()):
            data = _connector.load_from_file(_get_task_data_path())
            for entry in data.get("entries", []):
                mem = {
                    "id": entry.get("memory_id", ""),
                    "section": entry.get("section", "general"),
                    "content": entry.get("content", ""),
                    "added_at": entry.get("added_at", ""),
                }
                persisted_memories.append(mem)
                persisted_lines.append(
                    f"[{mem['id']}] section={mem['section']}\nContent: {mem['content']}"
                )
            logger.info(
                "[Experience] experience_retrieve: loaded %d entries from task-data.json",
                len(persisted_memories)
                )
    except Exception as load_exc:
        logger.warning("[Experience] experience_retrieve: failed to load task-data.json: %s", load_exc)

    svc = _get_service()
    if svc is None:
        logger.info("[Experience] experience_retrieve: service disabled — returning persisted only")
        memory_string = "\n\n".join(persisted_lines)
        return {
            "status": "persisted_only",
            "memory_string": memory_string,
            "retrieved_memory": persisted_memories,
        }
    try:
        result = await svc.retrieve(user_id="main", query=query)
        # Merge persisted entries with service results
        svc_memories = result.get("retrieved_memory", [])
        svc_string = result.get("memory_string", "")
        merged_memories = persisted_memories + svc_memories
        merged_string = "\n\n".join(filter(None, ["\n\n".join(persisted_lines), svc_string]))
        count = len(merged_memories)
        logger.info(
            "[Experience] experience_retrieve done: %d memories (%d persisted + %d from service)",
            count, len(persisted_memories), len(svc_memories),
        )
        result["retrieved_memory"] = merged_memories
        result["memory_string"] = merged_string
        return result
    except Exception as exc:
        logger.error("[Experience] experience_retrieve failed: %s", exc)
        logger.info("[Experience] experience_retrieve error: %s", exc)
        return {"status": "error", "error": str(exc), "memory_string": "", "retrieved_memory": []}


def _format_trajectory_feedback(entry: Dict[str, Any]) -> str:
    """Build a feedback string for a trajectory entry, including tool outcomes."""
    parts = [f"section={entry.get('section', 'general')}"]
    tools = entry.get("tools_used")
    if tools:
        for t in tools:
            if isinstance(t, dict):
                name = t.get("tool", "unknown")
                status = t.get("status", "unknown")
                error = t.get("error", "")
                note = t.get("note", "")
                line = f"{name}:{status}"
                if error:
                    line += f"({error})"
                if note:
                    line += f"[{note}]"
                parts.append(line)
            else:
                parts.append(str(t))
    return "; ".join(parts)


@tool(
    name="experience_learn",
    description=(
        "Record a key finding, rule, or insight from the current task and consolidate it into "
        "reusable memory. Call this once before the final reply — it both saves the new entry "
        "and summarizes everything learned so far. "
        "Pass all fields inside a `params` object: "
        "{content, section, when_to_use, title, description, query, label, tools_used}. "
        "Include tools_used as a list of objects describing each tool call outcome this turn, "
        "e.g. tools_used=[{\"tool\": \"web_search\", \"status\": \"success\"}, "
        "{\"tool\": \"write_memory\", \"status\": \"failed\", \"error\": \"permission denied\", "
        "\"note\": \"fell back to in-chat reply\"}]. "
        "Always record failed tool calls — these are the most valuable learning signals."
    ),
)
async def experience_learn(params: TaskAddParams, matts: str = "none") -> Dict[str, Any]:
    """Record and consolidate a task finding into reusable memory.

    Args:
        params: TaskAddParams containing the new entry to record.
        matts: MaTTS summarization mode (default: 'none').

    Returns:
        Dictionary with status, memory_id, and consolidated memory list.
    """
    # The @tool framework may deliver params as a plain dict — convert to dataclass.
    if isinstance(params, dict):
        valid = TaskAddParams.__dataclass_fields__
        params = TaskAddParams(**{k: v for k, v in params.items() if k in valid})

    logger.info(
        "[Exp] experience_learn called: section=%s, content=%s",
        params.section, params.content[:120],
    )
    svc = _get_service()
    memory_id: Optional[str] = None

    # Step 1: add_memory via service (if available)
    if svc is not None:
        try:
            content_for_service = params.content
            if params.tools_used:
                tool_lines = []
                for t in params.tools_used:
                    if isinstance(t, dict):
                        status = t.get("status", "unknown")
                        name = t.get("tool", "unknown")
                        error = t.get("error", "")
                        note = t.get("note", "")
                        line = f"  - {name}: {status}"
                        if error:
                            line += f" | error: {error}"
                        if note:
                            line += f" | note: {note}"
                        tool_lines.append(line)
                    else:
                        tool_lines.append(f"  - {t}: unknown")
                content_for_service += "\n\nTool outcomes:\n" + "\n".join(tool_lines)
            request = AddMemoryRequest(
                content=content_for_service,
                query=params.query,
                when_to_use=params.when_to_use,
                title=params.title,
                description=params.description,
                section=params.section,
                label=params.label,
            )
            add_result = await svc.add_memory(user_id="main", request=request)
            memory_id = add_result.get("memory_id")
            logger.info("[Experience] experience_learn: add_memory done: memory_id=%s", memory_id)
        except Exception as exc:
            logger.error("[Experience] experience_learn: add_memory failed: %s", exc)

    # Step 2: persist new entry to task-data.json
    try:
        existing = (
            _connector.load_from_file(_get_task_data_path())
            if _connector.exists(_get_task_data_path())
            else {"entries": []}
        )
        entry: Dict[str, Any] = {
            "content": params.content,
            "section": params.section,
            "memory_id": memory_id,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        if params.when_to_use is not None:
            entry["when_to_use"] = params.when_to_use
        if params.title is not None:
            entry["title"] = params.title
        if params.description is not None:
            entry["description"] = params.description
        if params.query is not None:
            entry["query"] = params.query
        if params.label is not None:
            entry["label"] = params.label
        if params.tools_used is not None:
            entry["tools_used"] = params.tools_used
        existing.setdefault("entries", []).append(entry)
        _connector.save_to_file(_get_task_data_path(), existing)
    except Exception as persist_exc:
        logger.warning(
            "[Experience] experience_learn: failed to persist to task-data.json: %s", persist_exc,
        )

    if svc is None:
        logger.info("[Experience] experience_learn: service disabled — entry persisted only")
        return {"status": "persisted_only", "memory_id": None}

    # Step 3: summarize all entries in task-data.json
    raw_entries: List[Dict[str, Any]] = []
    try:
        if _connector.exists(_get_task_data_path()):
            data = _connector.load_from_file(_get_task_data_path())
            raw_entries = data.get("entries", [])
    except Exception as load_exc:
        logger.warning("[Experience] experience_learn: failed to reload task-data.json: %s", load_exc)

    if not raw_entries:
        return {"status": "ok", "memory_id": memory_id, "memory": [], "summary": ""}

    query = params.query or params.content
    trajectories = [
        {
            "query": e.get("content", ""),
            "response": e.get("content", ""),
            "feedback": _format_trajectory_feedback(e),
        }
        for e in raw_entries
    ]
    fallback_summary = "\n".join(
        f"[{e.get('section', 'general')}] {e.get('content', '')}" for e in raw_entries
    )

    try:
        result = await svc.summarize(
            user_id="main", matts=matts, query=query, trajectories=trajectories,
        )
        logger.info("[Experience] experience_learn: summarize done: status=%s", result.get("status"))
        memories = result.get("memory", [])
        if memories:
            try:
                summarized_entries = [
                    {
                        "content": mem.get("content", ""),
                        "section": mem.get("section", "general"),
                        "memory_id": mem.get("id", ""),
                        "added_at": datetime.now(timezone.utc).isoformat(),
                        "source": "experience_learn",
                        "query": query,
                    }
                    for mem in memories
                ]
                existing = (
                    _connector.load_from_file(_get_task_data_path())
                    if _connector.exists(_get_task_data_path())
                    else {"entries": []}
                )
                existing_ids = {
                    e.get("memory_id") for e in existing.get("entries", []) if e.get("memory_id")
                }
                added = 0
                for s_entry in summarized_entries:
                    if s_entry.get("memory_id") not in existing_ids:
                        existing.setdefault("entries", []).append(s_entry)
                        existing_ids.add(s_entry.get("memory_id"))
                        added += 1
                _connector.save_to_file(_get_task_data_path(), existing)
                logger.info(
                    "[Experience] experience_learn: merged %d summarized entries (total=%d)",
                    added, len(existing.get("entries", [])),
                )
            except Exception as persist_exc:
                logger.warning(
                    "[Experience] experience_learn: failed to persist summarized entries: %s",
                    persist_exc,
                )
        result["memory_id"] = memory_id
        return result
    except Exception as exc:
        logger.error("[Experience] experience_learn: summarize failed: %s", exc)
        return {
            "status": "persisted_only",
            "memory_id": memory_id,
            "memory": raw_entries,
            "summary": fallback_summary,
        }



@tool(
    name="experience_clear",
    description=(
        "Wipe all stored task memory from task-data.json. "
        "ONLY call this when the user explicitly asks to clear all stored knowledge. Always confirm first."
    ),
)
async def experience_clear() -> Dict[str, Any]:
    """Clear all entries from task-data.json.

    Returns:
        Dictionary with status message.
    """
    try:
        _connector.save_to_file(_get_task_data_path(), {"entries": []})
        logger.info("[Experience] experience_clear: task-data.json wiped")
        return {"status": "success", "message": "task-data.json cleared"}
    except Exception as exc:
        logger.error("[Experience] experience_clear failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def get_task_tools() -> List:
    """Return the list of task tool functions."""
    return [
        experience_retrieve,
        experience_learn,
        experience_clear,
    ]
