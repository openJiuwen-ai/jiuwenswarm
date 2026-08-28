# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build the same Single Coding Agent the JiuwenSwarm UI uses (no server).

Root DeepAgent is assembled like ``JiuwenSwarmCodeAdapter.create_instance``:
``build_code_system_prompt`` + explore/plan + ``code_agent``, with the Code
Graph profile attached to ``code_agent`` only.

Eval-only deviations (documented, not product behavior):
- no permission / plan-approval / ask-user interrupts
- ``enable_read_image_multimodal=False``
- no MCP / cron / browser
- ``enable_task_loop=False`` (single-query fix)
- extra ``EvalTraceRail`` for timings and intermediate payloads
- optional ``hide_grep`` ablation
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_EVAL_DIR = Path(__file__).resolve().parent
_JIUWEN_ROOT = _EVAL_DIR.parents[1]
for _path in (_EVAL_DIR, _JIUWEN_ROOT):
    text = str(_path)
    if text not in sys.path:
        sys.path.insert(0, text)

from eval_env import (  # noqa: E402
    load_eval_dotenv,
    require_code_graph,
)

from openjiuwen.core.foundation.llm import init_model  # noqa: E402
from openjiuwen.core.foundation.llm.model import Model  # noqa: E402
from openjiuwen.core.runner.runner import Runner  # noqa: E402
from openjiuwen.core.single_agent.schema.agent_card import AgentCard  # noqa: E402
from openjiuwen.harness.deep_agent import DeepAgent  # noqa: E402
from openjiuwen.harness.factory import create_deep_agent  # noqa: E402
from openjiuwen.harness.rails.base import DeepAgentRail  # noqa: E402
from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail  # noqa: E402
from openjiuwen.harness.subagents.code_agent import build_code_agent_config  # noqa: E402
from openjiuwen.harness.subagents.explore_agent import build_explore_agent_config  # noqa: E402
from openjiuwen.harness.subagents.plan_agent import build_plan_agent_config  # noqa: E402
from openjiuwen.harness.workspace.workspace import Workspace  # noqa: E402

from jiuwenswarm.server.runtime.agent_adapter.code_graph_flags import (  # noqa: E402
    PROFILE_GRAPH,
    PROFILE_OFF,
    CodeGraphFlags,
    apply_code_graph_profile,
    resolve_code_graph_flags,
    resolve_profile,
)
from trajectory import EvalTrace  # noqa: E402

try:
    from openjiuwen.core.retrieval.code_graph.models import CodeGraphConfig
except ImportError:  # original agent-core has no Code Graph engine
    CodeGraphConfig = None  # type: ignore[misc, assignment]

try:
    from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
        build_code_system_prompt,
    )
except ImportError:  # running without the jiuwenswarm package on PYTHONPATH
    build_code_system_prompt = None


def load_product_config() -> dict[str, Any]:
    """Prefer the live JiuwenSwarm config (same file as the UI), else repo yaml."""
    try:
        from jiuwenswarm.common.config import get_config

        loaded = get_config()
        if isinstance(loaded, dict) and loaded:
            return deepcopy(loaded)
    except Exception:
        pass
    path = _JIUWEN_ROOT / "jiuwenswarm" / "resources" / "config.yaml"
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("PyYAML is required to load jiuwenswarm/resources/config.yaml") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _react_config(config_base: dict[str, Any]) -> dict[str, Any]:
    react = config_base.get("react")
    return react if isinstance(react, dict) else {}


def subagent_enabled(config_base: dict[str, Any], name: str, default: bool = False) -> bool:
    spec = (_react_config(config_base).get("subagents") or {}).get(name)
    if not isinstance(spec, dict):
        return default
    if default:
        return spec.get("enabled", default) is not False
    return spec.get("enabled") is True


def config_dir_name(*, profile: str = "off", prefix: str = "") -> str:
    """Folder name that states the profile, e.g. ``cfg_b__graph``."""
    resolved = (profile or "off").strip().lower()
    head = f"cfg_{prefix}" if prefix else "cfg_b"
    tag = "graph-off" if resolved == "off" else resolved
    return f"{head}__{tag}"


def cfg_paths(run_root: Path, name: str) -> dict[str, Path]:
    root = run_root.expanduser().resolve() / name
    return {
        "root": root,
        "raw": root / "raw",
        "logs": root / "logs",
        "workspaces": root / "workspaces",
        "code_graph_cache": root / "code_graph_cache",
        "config_json": root / "config.json",
    }


def isolate_eval_logs(log_dir: Path) -> Path:
    """Point openjiuwen file logs at this config's ``logs/`` directory."""
    from openjiuwen.core.common.logging.default.constant import DEFAULT_INNER_LOG_CONFIG
    from openjiuwen.core.common.logging.log_config import configure_log_config

    target = log_dir.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    snapshot = deepcopy(DEFAULT_INNER_LOG_CONFIG)
    snapshot["log_path"] = str(target)
    configure_log_config(snapshot)
    return target


def write_run_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body.setdefault("written_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def model_env_snapshot() -> dict[str, str]:
    return {
        "MODEL_PROVIDER": os.getenv("MODEL_PROVIDER", ""),
        "MODEL_NAME": os.getenv("MODEL_NAME", ""),
        "MODEL_MAX_TOKENS": os.getenv("MODEL_MAX_TOKENS", ""),
        "API_BASE": os.getenv("API_BASE", ""),
    }


def build_model_from_env() -> Model:
    load_eval_dotenv()
    api_key = os.getenv("API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "API_KEY is required. Also set API_BASE, MODEL_NAME, MODEL_PROVIDER "
            "(same variables as jiuwenswarm/resources/config.yaml)."
        )
    return init_model(
        provider=os.getenv("MODEL_PROVIDER", "OpenAI"),
        model_name=os.getenv("MODEL_NAME", "gpt-4.1"),
        api_key=api_key,
        api_base=os.getenv("API_BASE", "https://api.openai.com/v1"),
        timeout=float(os.getenv("MODEL_TIMEOUT", "180")),
        temperature=float(os.getenv("MODEL_TEMPERATURE", "0.2")),
        top_p=float(os.getenv("MODEL_TOP_P", "0.9")),
        max_tokens=int(os.getenv("MODEL_MAX_TOKENS", "0")) or None,
    )


HIDDEN_SEARCH_TOOLS = ("grep", "glob")
# ContextBench graph ablation: Root must dispatch. Retrieval lives on code_agent.
# Hiding grep alone is not enough — Root still has bash/read_file and will
# finish the locate exam without ever calling find_code_symbols.
CONTEXTBENCH_ROOT_HIDDEN_TOOLS = (
    "grep",
    "glob",
    "read_file",
    "list_files",
    "bash",
    "edit_file",
    "write_file",
)
# Graph tools live on Root. Keep read_file as fallback.
CONTEXTBENCH_FIND_HIDDEN_TOOLS = (
    "grep",
    "glob",
    "bash",
    "edit_file",
    "write_file",
    "task_tool",
)
# code_agent keeps read_file + graph. bash would recreate the run01 leak.
# Locate exam: also withhold edit/write so it cannot patch.
CONTEXTBENCH_CODE_HIDDEN_TOOLS = (
    "grep",
    "glob",
    "bash",
    "edit_file",
    "write_file",
)


class EvalHideGrepRail(DeepAgentRail):
    """Drop named search tools after SysOperation + Code Graph rails."""

    priority = 98

    def __init__(self, names: tuple[str, ...] = HIDDEN_SEARCH_TOOLS) -> None:
        super().__init__()
        self.names = names

    def init(self, agent: Any) -> None:
        super().init(agent)
        _remove_named_tools(agent, self.names)


def _remove_named_tools(agent: Any, names: tuple[str, ...]) -> None:
    manager = getattr(agent, "ability_manager", None)
    if manager is None:
        return
    for name in names:
        try:
            if manager.get(name) is None:
                continue
            manager.remove_ability(name)
        except Exception:  # noqa: BLE001 — eval ablation must not fail the run
            continue


def _system_prompt() -> str:
    if build_code_system_prompt is not None:
        return build_code_system_prompt()
    return (
        "You are a coding agent. Use tools instead of guessing file contents. "
        "Read source files before editing."
    )


def _deep_agent_config_fields() -> set[str]:
    from dataclasses import fields, is_dataclass

    from openjiuwen.harness.schema.config import DeepAgentConfig

    if is_dataclass(DeepAgentConfig):
        return {item.name for item in fields(DeepAgentConfig)}
    return set(getattr(DeepAgentConfig, "model_fields", {}) or ())


def _graph_config(
    config_base: dict[str, Any], work: Path, cache_dir: str | Path | None
) -> Any:
    if CodeGraphConfig is None:
        return None
    raw = (
        config_base.get("code_graph")
        if isinstance(config_base.get("code_graph"), dict)
        else {}
    )
    resolved_cache = cache_dir or raw.get("cache_dir") or (work / ".code_graph_cache")
    return CodeGraphConfig(
        cache_dir=str(Path(resolved_cache).expanduser().resolve()),
        max_files=int(
            raw.get("max_files") or os.getenv("CODE_GRAPH_MAX_FILES", "50000")
        ),
        max_index_size_mb=int(
            raw.get("max_index_size_mb")
            or os.getenv("CODE_GRAPH_MAX_INDEX_SIZE_MB", "1024")
        ),
        query_timeout_seconds=float(
            raw.get("query_timeout_seconds")
            or os.getenv("CODE_GRAPH_QUERY_TIMEOUT", "10")
        ),
    )


def _code_agent_profile_kwargs(
    flags: CodeGraphFlags,
    graph_config: Any,
    *,
    inject_builtin_plan_agents: bool = True,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "code_graph_profile": flags.profile,
        "code_graph_prompt_mode": "locate",
    }
    if flags.enabled:
        kwargs["code_graph_config"] = graph_config
    if not inject_builtin_plan_agents:
        kwargs["inject_builtin_plan_agents"] = False
    return kwargs


def _subagent_factory_kwargs() -> dict[str, Any]:
    """Sub-agent kwargs; only code_agent gets a profile."""
    return {
        "auto_create_workspace": False,
        "enable_read_image_multimodal": False,
    }


@dataclass
class CodingAgentHandle:
    agent: DeepAgent
    trace: EvalTrace
    repo_root: Path
    workspace: Path
    flags: CodeGraphFlags
    config_base: dict[str, Any]

    @property
    def recorder(self):
        return self.trace.recorder


def create_coding_agent(
    repo_root: str | Path,
    *,
    model: Model | None = None,
    workspace: str | Path | None = None,
    language: str = "en",
    max_iterations: int = 40,
    enable_code_subagent: bool | None = None,
    enable_explore: bool = True,
    enable_plan: bool = True,
    profile: str | None = None,
    hide_grep: bool = False,
    hide_bash: bool = False,
    hide_edit: bool = False,
    cache_dir: str | Path | None = None,
    config_base: dict[str, Any] | None = None,
    code_agent_system_prompt: str | None = None,
) -> CodingAgentHandle:
    """Create the UI Single Coding Agent with an in-memory profile overlay.

    ``off`` is the original product agent (no graph). ``graph`` gives
    ``code_agent`` the find_* tools. ContextBench uses locate-exam prompts;
    the product TUI uses the product prompt.
    """
    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repo does not exist: {repo}")
    work = Path(workspace).expanduser().resolve() if workspace else repo
    work.mkdir(parents=True, exist_ok=True)

    resolved_profile = resolve_profile(profile)
    if resolved_profile == PROFILE_GRAPH:
        require_code_graph()

    product = apply_code_graph_profile(
        config_base if isinstance(config_base, dict) else load_product_config(),
        resolved_profile,
    )
    flags = resolve_code_graph_flags(product)
    graph_config = _graph_config(product, work, cache_dir)
    model = model or build_model_from_env()
    trace = EvalTrace(
        repo_root=str(repo),
        flags={
            "profile": flags.profile,
        },
    )
    capture = trace.make_rail()
    graph_kwargs = _subagent_factory_kwargs()

    def _rails(*items: Any, extra_hide: tuple[str, ...] = ()) -> list[Any]:
        rails = [item for item in items if item is not None]
        hidden: tuple[str, ...] = ()
        if hide_grep or hide_bash or hide_edit:
            hidden = tuple(
                dict.fromkeys(
                    (HIDDEN_SEARCH_TOOLS if hide_grep else ())
                    + (("bash",) if hide_bash else ())
                    + (("edit_file", "write_file") if hide_edit else ())
                )
            )
        hidden = tuple(dict.fromkeys(hidden + extra_hide))
        if hidden:
            rails.append(EvalHideGrepRail(hidden))
        return rails

    workspace_obj = Workspace(root_path=str(repo), language=language)
    subagents: list[Any] = []
    if enable_explore:
        spec = build_explore_agent_config(
            model=model,
            workspace=str(repo),
            language=language,
            max_iterations=min(25, max_iterations),
            rails=_rails(SysOperationRail(read_only=True), trace.make_rail()),
        )
        spec.factory_kwargs = dict(graph_kwargs)
        subagents.append(spec)
    if enable_plan:
        spec = build_plan_agent_config(
            model=model,
            workspace=str(repo),
            language=language,
            max_iterations=min(25, max_iterations),
            rails=_rails(SysOperationRail(read_only=True), trace.make_rail()),
        )
        spec.factory_kwargs = dict(graph_kwargs)
        subagents.append(spec)
    use_code_subagent = (
        enable_code_subagent
        if enable_code_subagent is not None
        else subagent_enabled(product, "code_agent", default=flags.enabled)
    )
    graph_enabled = flags.enabled
    attach_on_root = graph_enabled and not bool(use_code_subagent)
    graph_on_code_agent = bool(use_code_subagent) and graph_enabled
    if use_code_subagent:
        # Locate exam: hide bash/grep on CA whenever it owns graph tools.
        # Leaving bash (run12 baseline+CA) let a subagent `git checkout` and
        # poison the shared worktree for the next instance.
        code_hide = CONTEXTBENCH_CODE_HIDDEN_TOOLS if graph_on_code_agent else ()
        spec = build_code_agent_config(
            model,
            workspace=str(repo),
            language=language,
            max_iterations=max_iterations,
            system_prompt=code_agent_system_prompt,
            rails=_rails(
                SysOperationRail(),
                trace.make_rail(),
                extra_hide=code_hide,
            ),
            **_code_agent_profile_kwargs(
                flags,
                graph_config,
                inject_builtin_plan_agents=not graph_on_code_agent,
            ),
        )
        spec.factory_kwargs = {**graph_kwargs, **(spec.factory_kwargs or {})}
        subagents.append(spec)

    create_kwargs: dict[str, Any] = {
        "model": model,
        "card": AgentCard(
            name="coding_agent",
            id=f"coding-agent-{uuid.uuid4().hex[:8]}",
            description="Standalone coding agent for eval and local tests",
        ),
        "system_prompt": _system_prompt(),
        "subagents": subagents,
        "rails": _rails(
            SysOperationRail(),
            capture,
        ),
        "workspace": workspace_obj,
        "language": language,
        "enable_task_loop": False,
        "enable_task_planning": False,
        "max_iterations": max_iterations,
        "restrict_to_work_dir": True,
        "add_general_purpose_agent": False,
        "enable_security_rail": True,
        "enable_read_image_multimodal": False,
        "auto_create_workspace": False,
    }
    names = _deep_agent_config_fields()
    if attach_on_root:
        if "code_graph_config" in names:
            create_kwargs["code_graph_config"] = graph_config
        try:
            from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail
        except ImportError:
            CodeGraphProfileRail = None  # type: ignore[misc, assignment]
        if CodeGraphProfileRail is not None:
            create_kwargs["rails"] = _rails(
                SysOperationRail(),
                capture,
                CodeGraphProfileRail(
                    flags.profile,
                    config=graph_config,
                    prompt_mode="locate",
                ),
            )
    import inspect

    allowed = set(inspect.signature(create_deep_agent).parameters) | names
    allowed.discard("config_kwargs")
    agent = create_deep_agent(
        **{key: value for key, value in create_kwargs.items() if key in allowed}
    )
    return CodingAgentHandle(
        agent=agent,
        trace=trace,
        repo_root=repo,
        workspace=work,
        flags=flags,
        config_base=product,
    )


def hide_agent_tools(agent: DeepAgent, names: tuple[str, ...] | list[str]) -> list[str]:
    """Remove named tools after rails have registered. Keeps edit/write/bash."""
    manager = getattr(agent, "ability_manager", None)
    if manager is None:
        return []
    hidden: list[str] = []
    for name in names:
        try:
            if manager.get(name) is None:
                continue
            manager.remove_ability(name)
            hidden.append(str(name))
        except Exception:  # noqa: BLE001 — eval ablation must not fail the run
            continue
    return hidden


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or getattr(item, "content", "") or ""))
        return "\n".join(part for part in parts if part)
    if content is None:
        return str(getattr(message, "text", "") or "")
    return str(content)


def collect_agent_message_texts(agent: Any) -> list[str]:
    """Every stored message, so PATCH_CONTEXT is not lost in a Root summary."""
    texts: list[str] = []
    getters = []
    get_ctx = getattr(agent, "get_current_context", None)
    if callable(get_ctx):
        getters.append(get_ctx)
    react = getattr(agent, "_react_agent", None)
    if react is not None:
        get_react = getattr(react, "get_current_context", None)
        if callable(get_react):
            getters.append(get_react)
    for getter in getters:
        try:
            context = getter()
            messages = context.get_messages() if context is not None else []
        except Exception:  # noqa: BLE001 — best-effort scan
            continue
        for message in messages or []:
            text = _message_text(message)
            if text:
                texts.append(text)
    return texts


async def invoke_coding_agent(
    handle: CodingAgentHandle,
    query: str,
    *,
    hide_tools_named: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    await handle.agent.ensure_initialized()
    if hide_tools_named:
        hide_agent_tools(handle.agent, hide_tools_named)
    state = getattr(handle.agent, "_code_graph_run_state", None)
    if state is not None:
        state.request.query = query
    result = await Runner.run_agent(handle.agent, {"query": query})
    payload = result if isinstance(result, dict) else {"output": str(result)}
    agents = [handle.agent, *list(getattr(handle.trace, "agents", None) or [])]
    texts: list[str] = []
    seen: set[int] = set()
    for agent in agents:
        if agent is None or id(agent) in seen:
            continue
        seen.add(id(agent))
        texts.extend(collect_agent_message_texts(agent))
    payload["message_texts"] = texts
    patch_block = ""
    best_rank = 0
    for agent in agents:
        try:
            block = system_patch_context(agent)
        except Exception:  # noqa: BLE001 — subagent state shapes vary
            continue
        if not block:
            continue
        state = getattr(agent, "_code_graph_run_state", None)
        selected = list(getattr(state, "selected", None) or []) if state is not None else []
        rank = 2 if selected else 1
        if rank > best_rank:
            patch_block = block
            best_rank = rank
            if rank == 2:
                break
    if patch_block:
        payload["patch_context"] = patch_block
        texts = list(payload["message_texts"])
        texts.append(patch_block)
        payload["message_texts"] = texts
        output = str(payload.get("output") or "")
        if "<PATCH_CONTEXT>" not in output or not output.strip().endswith("</PATCH_CONTEXT>"):
            payload["output"] = (output.rstrip() + "\n\n" + patch_block) if output.strip() else patch_block
    return payload


def _as_payload_map(raw: Any) -> dict[str, dict[str, Any]]:
    """Coerce graph evidence to dict; inner ReAct state sometimes stores a list."""
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if isinstance(raw, list):
        mapped: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            key = str(item.get("symbol_id") or item.get("evidence_id") or index)
            mapped[key] = item
        return mapped
    return {}


def system_patch_context(agent: Any) -> str:
    """Format PATCH_CONTEXT from submitted/selected spans. Empty until submit."""
    try:
        from openjiuwen.harness.tools.code_graph.patch_context import (
            format_patch_context,
            normalize_submit_locations,
        )
    except ImportError:
        return ""
    state = getattr(agent, "_code_graph_run_state", None)
    if state is None:
        return ""
    selected = list(getattr(state, "selected", None) or [])
    evidence = _as_payload_map(getattr(state, "read_evidence", None))
    candidates = _as_payload_map(getattr(state, "candidates", None))
    if not selected:
        # Do not invent a PATCH from the last read_symbol: that scores a
        # max-iteration wander as a declared location. submit/select only.
        return ""
    normalized, blockers = normalize_submit_locations(
        selected,
        read_evidence=evidence,
        candidates=candidates,
    )
    if blockers:
        return format_patch_context(normalized)
    return format_patch_context(normalized or selected)


def list_agent_tools(agent: DeepAgent) -> list[str]:
    manager = getattr(agent, "ability_manager", None)
    if manager is None or not hasattr(manager, "list"):
        return []
    names: list[str] = []
    for item in manager.list() or []:
        card = getattr(item, "card", None)
        name = getattr(card, "name", None) or getattr(item, "name", None)
        if name:
            names.append(str(name))
    return sorted(set(names))


def list_subagent_names(agent: DeepAgent) -> list[str]:
    config = getattr(agent, "deep_config", None)
    specs = getattr(config, "subagents", None) or []
    names: list[str] = []
    for spec in specs:
        card = getattr(spec, "agent_card", None)
        name = (
            getattr(card, "name", None)
            if card is not None
            else getattr(spec, "name", None)
        )
        if name:
            names.append(str(name))
    return names


def subagent_graph_profiles(agent: DeepAgent) -> dict[str, str]:
    """Code Graph profile each hung subagent was created with.

    Used by the runner to assert that only ``code_agent`` has a live profile: a
    graph tool anywhere else means the flags leaked.
    """
    config = getattr(agent, "deep_config", None)
    specs = getattr(config, "subagents", None) or []
    profiles: dict[str, str] = {}
    for spec in specs:
        card = getattr(spec, "agent_card", None)
        name = (
            getattr(card, "name", None)
            if card is not None
            else getattr(spec, "name", None)
        )
        if not name:
            continue
        kwargs = getattr(spec, "factory_kwargs", None) or {}
        profile = str(kwargs.get("code_graph_profile") or PROFILE_OFF)
        profiles[str(name)] = profile
    return profiles
