# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Task tools - wraps TaskMemoryService as @tool decorated functions."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from jiuwenswarm.agents.harness.common.tools import (
    AddMemoryRequest,
    JSONFileConnector,
    TaskMemoryService,
    ce_config as _ce_config,
    tool,
)

from jiuwenswarm.common.local_env_config import read_env
from jiuwenswarm.common.utils import get_agent_workspace_dir

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


@dataclass(frozen=True)
class TaskMemoryConfigFingerprint:
    """Lightweight, hashable key for the service cache pool."""
    api_key: str = ""
    api_base: str = ""
    embedding_model: str = ""
    llm_model: str = ""
    retrieval_algo: str = ""
    summary_algo: str = ""

    @classmethod
    def from_resolved(cls, resolved: "TaskMemoryResolvedConfig") -> "TaskMemoryConfigFingerprint":
        return cls(
            api_key=resolved.api_key,
            api_base=resolved.api_base,
            embedding_model=resolved.embedding_model,
            llm_model=resolved.llm_model,
            retrieval_algo=resolved.retrieval_algo,
            summary_algo=resolved.summary_algo,
        )


@dataclass(frozen=True)
class TaskMemoryResolvedConfig:
    """Fully resolved configuration for building a TaskMemoryService."""
    llm_model: str = ""
    embedding_model: str = ""
    api_key: str = ""
    api_base: str = ""
    model_provider: str = ""
    retrieval_algo: str = ""
    summary_algo: str = ""

    def fingerprint(self) -> TaskMemoryConfigFingerprint:
        return TaskMemoryConfigFingerprint.from_resolved(self)


# Path for persisting task_add entries
def _get_task_data_path() -> str:
    return str(get_agent_workspace_dir() / "task-data.json")


_connector = JSONFileConnector(indent=2)

_SERVICE_CACHE: dict[TaskMemoryConfigFingerprint, Any] = {}
_SERVICE_CACHE_MAX = 32


def task_memory_service_cache_size() -> int:
    """Return the current number of cached TaskMemoryService instances."""
    return len(_SERVICE_CACHE)


def _first_non_empty(*values: Optional[str]) -> str:
    """Return the first non-empty, non-None string among *values*."""
    for v in values:
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def resolve_task_memory_config(cfg=None) -> TaskMemoryResolvedConfig:
    """Resolve task-memory configuration from yaml + overlay-aware env.

    Uses ``read_env`` instead of ``os.getenv`` so that per-agent tip/overlay
    values are respected.
    """
    from jiuwenswarm.common.config import get_config

    if cfg is None:
        cfg = get_config()

    react_cfg = cfg.get("react", {})
    model_client = react_cfg.get("model_client_config", {})
    embed_cfg = cfg.get("embed", {})
    models_default = cfg.get("models", {}).get("default", {}).get("model_client_config", {})
    task_memory_cfg = cfg.get("task_memory", {})

    llm_model = _first_non_empty(
        task_memory_cfg.get("llm_model"),
        read_env("TASK_MEMORY_LLM_MODEL"),
        read_env("MODEL_NAME"),
        react_cfg.get("model_name"),
    )
    embedding_model = _first_non_empty(
        task_memory_cfg.get("embedding_model"),
        read_env("TASK_MEMORY_EMBED_MODEL"),
        embed_cfg.get("embed_model"),
        read_env("EMBED_MODEL"),
        read_env("EMBEDDING_MODEL"),
    )
    api_key = _first_non_empty(
        task_memory_cfg.get("api_key"),
        read_env("TASK_MEMORY_API_KEY"),
        embed_cfg.get("embed_api_key"),
        read_env("EMBED_API_KEY"),
        read_env("API_KEY"),
        model_client.get("api_key"),
        models_default.get("api_key"),
    )
    api_base = _first_non_empty(
        task_memory_cfg.get("api_base"),
        read_env("TASK_MEMORY_API_BASE"),
        embed_cfg.get("embed_base_url"),
        read_env("EMBED_API_BASE"),
        read_env("API_BASE"),
        model_client.get("api_base"),
        models_default.get("api_base"),
    )
    model_provider = _first_non_empty(
        task_memory_cfg.get("model_provider"),
        read_env("TASK_MEMORY_MODEL_PROVIDER"),
        model_client.get("client_provider"),
        models_default.get("client_provider"),
        read_env("MODEL_PROVIDER"),
    )
    retrieval_algo = _first_non_empty(
        task_memory_cfg.get("retrieval_algo"),
        read_env("TASK_MEMORY_RETRIEVAL_ALGO"),
    )
    summary_algo = _first_non_empty(
        task_memory_cfg.get("summary_algo"),
        read_env("TASK_MEMORY_SUMMARY_ALGO"),
    )

    return TaskMemoryResolvedConfig(
        llm_model=llm_model,
        embedding_model=embedding_model,
        api_key=api_key,
        api_base=api_base,
        model_provider=model_provider,
        retrieval_algo=retrieval_algo,
        summary_algo=summary_algo,
    )


def task_memory_config_fingerprint(cfg=None) -> TaskMemoryConfigFingerprint:
    """Return the fingerprint for the current (or given) task-memory config."""
    resolved = resolve_task_memory_config(cfg)
    return resolved.fingerprint()


def clear_task_memory_service() -> None:
    """Clear the entire service cache pool."""
    _SERVICE_CACHE.clear()
    logger.debug("[Experience] Service cache pool cleared")


def _evict_service_cache_if_needed() -> None:
    """Evict the oldest entry when the cache exceeds ``_SERVICE_CACHE_MAX``."""
    while len(_SERVICE_CACHE) > _SERVICE_CACHE_MAX:
        oldest_key = next(iter(_SERVICE_CACHE))
        _SERVICE_CACHE.pop(oldest_key, None)
        logger.debug("[Experience] Evicted service cache entry (pool size=%d)", len(_SERVICE_CACHE))


def _build_task_memory_service(resolved: TaskMemoryResolvedConfig, fp: TaskMemoryConfigFingerprint) -> Any:
    """Create a TaskMemoryService instance from resolved config (no env mutation)."""
    kwargs: Dict[str, Any] = dict(
        llm_model=resolved.llm_model,
        embedding_model=resolved.embedding_model,
        api_key=resolved.api_key,
    )
    if resolved.api_base:
        kwargs["api_base"] = resolved.api_base
    if resolved.retrieval_algo:
        kwargs["retrieval_algo"] = resolved.retrieval_algo
    if resolved.summary_algo:
        kwargs["summary_algo"] = resolved.summary_algo

    svc = TaskMemoryService(**kwargs)
    _SERVICE_CACHE[fp] = svc
    _evict_service_cache_if_needed()
    logger.info(
        "[Experience] TaskMemoryService created & cached (llm=%s, embed=%s, pool=%d)",
        resolved.llm_model, resolved.embedding_model, len(_SERVICE_CACHE),
    )
    return svc


def _apply_ce_defaults(resolved: Optional[TaskMemoryResolvedConfig] = None) -> None:
    """Seed the context_evolver config from the resolved config, falling back to env."""
    try:
        if resolved is None:
            resolved = resolve_task_memory_config()

        mappings = {
            "API_KEY": resolved.api_key,
            "API_BASE": resolved.api_base,
            "MODEL_NAME": resolved.llm_model,
            "MODEL_PROVIDER": resolved.model_provider,
            "EMBEDDING_MODEL": resolved.embedding_model,
        }

        for key, value in mappings.items():
            if value and not str(value).startswith("${"):  # skip unresolved placeholders
                _ce_config.set_value(key, str(value))
    except Exception as exc:
        logger.debug("[Experience] Could not apply CE defaults: %s", exc)


def _is_task_memory_enabled() -> bool:
    """Check if task memory is enabled via config or environment."""
    from jiuwenswarm.agents.harness.common.memory.config import get_task_memory_config

    task_memory_cfg = get_task_memory_config()
    return bool(task_memory_cfg.get("enabled", False))


def get_task_memory_service(cfg=None):
    """Return a cached TaskMemoryService keyed by config fingerprint.

    If the fingerprint matches an existing entry, return it; otherwise build,
    cache, and return a new instance.
    """
    resolved = resolve_task_memory_config(cfg)
    fp = resolved.fingerprint()

    # Check cache first
    svc = _SERVICE_CACHE.get(fp)
    if svc is not None:
        return svc

    if TaskMemoryService is None:
        logger.warning("[Experience] TaskMemoryService not available")
        return None

    if not resolved.api_key:
        logger.warning("[Experience] No API key found; task tools will be disabled")
        return None
    if not resolved.llm_model:
        logger.warning("[Experience] No LLM model configured; task tools will be disabled")
        return None
    if not resolved.embedding_model:
        logger.warning("[Experience] No embedding model configured; task tools will be disabled")
        return None

    _apply_ce_defaults(resolved)

    try:
        return _build_task_memory_service(resolved, fp)
    except Exception as exc:
        logger.error("[Experience] Failed to initialize TaskMemoryService: %s", exc)
        return None


@tool(
    name="experience_retrieve",
    description=(
            "检索与当前任务相关的历史记忆与经验教训。"
        "每个任务开始时都应调用，以检查是否有先例可参考。"
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

    svc = get_task_memory_service()
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
        "记录当前任务的关键发现、规则或洞察，并整合为可复用记忆。"
        "在最终回复前调用一次——既保存新条目，也汇总迄今所学。"
        "所有字段放在 params 对象内："
        "{content, section, when_to_use, title, description, query, label, tools_used}。"
        "tools_used 为描述本轮各工具调用结果的对象列表，"
        "例如 tools_used=[{\"tool\": \"web_search\", \"status\": \"success\"}, "
        "{\"tool\": \"write_memory\", \"status\": \"failed\", \"error\": \"permission denied\", "
        "\"note\": \"fell back to in-chat reply\"}]。"
        "务必记录失败的工具调用——这些是最有价值的学习信号。"
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
    svc = get_task_memory_service()
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
        "清空 task-data.json 中存储的全部任务记忆。"
        "仅当用户明确要求清除所有已存储知识时调用，且须先确认。"
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
