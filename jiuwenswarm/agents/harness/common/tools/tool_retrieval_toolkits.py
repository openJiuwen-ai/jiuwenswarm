"""Static in-code tool category catalogue for progressive ``tool_search``.

The deferred-tool directory is a two-level category tree frozen into this
module (``_TOOL_CATEGORIES``): category path → description + member tool
names. ``tool_search`` serves ``search`` (keyword matching, ``[tool]`` name +
short description + category lines) and ``get_schema`` (the ``{name,
description, schema}`` record
for one tool) by rendering that static table against the live deferred
registry — no on-disk tree index, no LLM taxonomy build, no engine
pagination. Tools missing from the static table (MCP connector tools,
late additions) surface through the connector virtual branches and the
uncategorized block instead of rewriting the table; new static tools are a
code change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import Any, NamedTuple

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard, ToolInfo

logger = logging.getLogger(__name__)

# Domain separator for tool ID generation
_TOOL_ID_DOMAIN = b"openjiuwen.tool-index-tool.v1\0"


class ToolCategory(NamedTuple):
    """One catalogue category: display path, description, member tool names."""

    path: str
    description: str
    tools: tuple[str, ...]


# The static category tree. Category lines are description-only (tool names
# are discovered through tool_search's search); two-level paths render as
# ``scheduling > cron``. Membership lists the stable built-in deferred tools;
# names not present in the live registry are skipped at render time.
_TOOL_CATEGORIES: tuple[ToolCategory, ...] = (
    ToolCategory(
        path="documents",
        description="读取 PDF 文档的文本内容，支持按页与分块读取。",
        tools=("read_pdf",),
    ),
    ToolCategory(
        path="external-agents",
        description=(
            "与外部 ACP 兼容智能体子进程（Codex CLI、Gemini CLI 等）建立会话并收发消息。"
        ),
        tools=("acp_chat",),
    ),
    ToolCategory(
        path="file-system",
        description="列出目录内容。",
        tools=("list_files",),
    ),
    ToolCategory(
        path="goals",
        description=(
            "查询当前会话的持续目标及其执行状态，提交目标执行结果的结构化报告"
            "（继续、完成或受阻）。"
        ),
        tools=("get_current_goal", "submit_goal_report"),
    ),
    ToolCategory(
        path="modes",
        description="在 normal 与 plan 两种会话模式之间切换当前会话。",
        tools=("switch_mode",),
    ),
    ToolCategory(
        path="scheduling > cron",
        description=(
            "cron 定时任务的创建、更新、删除、查询、启停、预览执行时间与手动触发。"
        ),
        tools=(
            "cron",
            "cron_create_job",
            "cron_delete_job",
            "cron_get_job",
            "cron_list_jobs",
            "cron_preview_job",
            "cron_toggle_job",
            "cron_update_job",
        ),
    ),
    ToolCategory(
        path="scheduling > heartbeat",
        description=(
            "绑定当前会话的心跳跟进任务的创建、更新、删除、查询、启停、预览触发时间、"
            "立即运行与取消运行。"
        ),
        tools=(
            "heartbeat_cancel_run",
            "heartbeat_create_job",
            "heartbeat_delete_job",
            "heartbeat_get_job",
            "heartbeat_list_jobs",
            "heartbeat_preview_job",
            "heartbeat_run_now",
            "heartbeat_toggle_job",
            "heartbeat_update_job",
        ),
    ),
    ToolCategory(
        path="sessions",
        description=(
            "跨会话协作：列出同一用户的其他持久化会话，向目标会话异步发送消息并跟踪其状态，"
            "读取指定会话的对话、进展与结果，处理未知状态的跨会话消息，恢复未执行的排队任务。"
        ),
        tools=(
            "session_continue_queued",
            "session_list",
            "session_message_list",
            "session_message_resolve",
            "session_read",
            "session_send_message",
        ),
    ),
    ToolCategory(
        path="skill-management",
        description="网络上技能搜索、安装与本地技能卸载。",
        tools=("install_skill", "search_skill", "uninstall_skill"),
    ),
    ToolCategory(
        path="terminal",
        description=(
            "通过 PowerShell 执行终端命令与脚本（git、npm、docker、python 等），"
            "支持超时与后台运行。"
        ),
        tools=("powershell",),
    ),
)


class _FlatToolEntry(NamedTuple):
    """Name+description pair for uncategorized catalogue lines."""

    name: str
    description: str


class ToolCatalogueSnapshot(NamedTuple):
    """Frozen catalogue view over the live deferred registry.

    ``categories`` carries only categories with at least one live tool, each
    with membership intersected against the live registry; ``flat_entries``
    lists every live deferred tool (the uncategorized fallback and the drift
    baseline). Frozen once at init and re-frozen on the first model call;
    mid-session drift flows through delta attachments, never a re-render.
    """

    categories: tuple[ToolCategory, ...]
    flat_entries: tuple[_FlatToolEntry, ...]


def _tool_index_tool_id(session_scope: str) -> str:
    """Stable per-session tool id for the tool_search LocalFunction."""

    return hashlib.sha256(
        _TOOL_ID_DOMAIN + session_scope.encode("utf-8")
    ).hexdigest()[:16]


_TOOL_SEARCH_CARD_DESCRIPTION = (
    "Deferred-tool catalogue, read-only. search: deferred tools matching "
    "[query], each as name + short description + category. get_schema: the "
    "{name, description, schema} of the tool named in [tool]; results "
    "execute via the fixed tool_call tool. Never executes tools."
)

_SCHEMA_DESC_CHARS = 200

# Direct-call serialization rule appended to every object-typed parameter the
# model sees in get_schema output. The deferred tool is not in the advertised
# tools list, so the call the model emits has no API-level schema anchoring —
# "documentation read in a tool result → native function call" is exactly where
# nested objects get double-serialized into JSON strings (observed: cron's
# ``job`` passed as a string, twice, by both weak and strong models).
_OBJECT_PARAM_ANCHOR = (
    " Must be passed as an inline JSON object literal, never a JSON string."
)


def _anchored_parameters(parameters: Any) -> Any:
    """Annotate object-typed parameters with the direct-call serialization rule.

    Presentation only — the tool's real schema is untouched; the annotation
    lives solely in what ``get_schema`` renders to the model, right where the
    field is read before the call is composed.
    """

    if not isinstance(parameters, dict):
        return parameters
    try:
        anchored = json.loads(json.dumps(parameters))
    except (TypeError, ValueError):
        return parameters

    def _walk(node: dict[str, Any]) -> None:
        properties = node.get("properties")
        if not isinstance(properties, dict):
            return
        for prop in properties.values():
            if not isinstance(prop, dict):
                continue
            if "object" in (prop.get("type") if isinstance(prop.get("type"), list) else (prop.get("type"),)):
                prop["description"] = (
                    str(prop.get("description") or "").rstrip() + _OBJECT_PARAM_ANCHOR
                ).lstrip()
            _walk(prop)

    _walk(anchored)
    return anchored


# Name segments that carry no routing signal: nearly every tool name has
# one ("cron_create_job", "list_files"), so a query word matching them is
# noise, not evidence the tool fits the intent. A name-level match counts
# as strong evidence only when a distinctive segment (or an underscore
# run, or the full name) lines up.
_GENERIC_NAME_SEGMENTS = frozenset(
    {
        "create", "get", "list", "new", "run", "now", "update", "delete",
        "remove", "job", "jobs", "send", "read", "write", "file", "files",
        "tool", "tools", "user", "chat", "message", "msg", "search", "find",
        "show", "print", "name", "time", "text", "data", "info", "id",
    }
)


def _strong_name_hit(token: str, lowered_name: str) -> bool:
    """Whether a query token matching a tool name is distinctive evidence."""

    if len(token) < 3:
        return False
    if token not in lowered_name and lowered_name not in token:
        return False
    if "_" in token:
        # Multi-segment runs ("heartbeat_run", "mcp_amap") are distinctive
        # by construction.
        return True
    if lowered_name in token:
        # The query contains the full tool name — a guessed exact name.
        return True
    segments = lowered_name.split("_")
    return token in segments and token not in _GENERIC_NAME_SEGMENTS


def _compact_schema_description(description: str) -> str:
    """Compact a listing description; full docstrings live in get_schema."""

    compact = " ".join(str(description or "").split())
    if len(compact) <= _SCHEMA_DESC_CHARS:
        return compact
    return compact[: _SCHEMA_DESC_CHARS - 1].rstrip() + "…"


def _tool_search_input_params() -> dict[str, Any]:
    """``tool_search`` card schema: search / get_schema over the catalogue.

    The discovery flow is two-phase to bound tokens: search returns names,
    short descriptions, and category paths only; get_schema returns the full
    schema for the tool the model has already picked.
    """

    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["search", "get_schema"],
                "description": (
                    "search: deferred tools matching [query]. get_schema: "
                    "the schema of the tool named in [tool]."
                ),
            },
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "search only. Intent or capability keywords.",
            },
            "tool": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "get_schema only. One exact tool name.",
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    }


class ToolRetrievalToolkit:
    """Expose one session-local ``tool_search`` structured tool for deferred tools.

    The catalogue is the in-code ``_TOOL_CATEGORIES`` tree rendered against
    the live deferred registry; MCP connector tools stay out of the static
    table and group under their ``mcp_{server}`` virtual branches instead.
    """

    TOOL_NAME = "tool_search"

    def __init__(
        self,
        *,
        deferred_tools_provider: Callable[[], list[ToolInfo]],
        session_scope: str = "default",
        config_base: dict[str, Any] | None = None,
        mcp_servers_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        """Initialize the tool retrieval toolkit.

        Args:
            deferred_tools_provider: Callable that returns current deferred tools
            session_scope: Unique session identifier
            config_base: Application configuration
            mcp_servers_provider: Callable returning ``{server_name: description}``
                for registered MCP servers; the names resolve multi-underscore
                tool prefixes precisely and the (optional, possibly empty)
                descriptions back the connector group lines in the prompt
                catalogue and the connector's virtual-branch category suffix
                in search results
        """
        self._deferred_tools_provider = deferred_tools_provider
        self._session_scope = session_scope
        self._config_base = config_base or {}
        self._mcp_servers_provider = mcp_servers_provider

        # Deferred tools are not frozen at construction: AbilityManager assigns
        # exposure at registration time, which happens after this toolkit is
        # built (during ensure_initialized). The provider is called lazily on
        # every refresh / tool_search so the live, post-assignment set is
        # always used.
        self._tool_by_name: dict[str, ToolInfo] = {}
        self._frozen_catalogue = self._build_catalogue()

    # ─── live registry ──────────────────────────────────────────────────

    def _live_records(self) -> None:
        """Refresh the deferred-tool mapping from the provider."""

        tools = self._deferred_tools_provider() or []
        self._tool_by_name = {tool.name: tool for tool in tools}

    def _build_catalogue(self) -> ToolCatalogueSnapshot:
        """Freeze the static tree against the live registry."""

        self._live_records()
        live = self._tool_by_name
        categories = tuple(
            category._replace(
                tools=tuple(name for name in category.tools if name in live)
            )
            for category in _TOOL_CATEGORIES
            if any(name in live for name in category.tools)
        )
        flat_entries = tuple(
            _FlatToolEntry(name, tool.description or "")
            for name, tool in live.items()
        )
        return ToolCatalogueSnapshot(
            categories=categories, flat_entries=flat_entries
        )

    # ─── public surface ─────────────────────────────────────────────────

    @property
    def session_scope(self) -> str:
        return self._session_scope

    @property
    def frozen_catalogue(self) -> ToolCatalogueSnapshot:
        """Return the frozen catalogue captured at the last refresh."""

        return self._frozen_catalogue

    @property
    def deferred_tool_names(self) -> frozenset[str]:
        """Live deferred-tool names, refreshed from the provider on read.

        Backs the ``tool_call`` execution envelope's authorization: only names
        present in the deferred catalogue may be dispatched through it.
        """

        self._live_records()
        return frozenset(self._tool_by_name)

    def deferred_tool_infos(self) -> dict[str, ToolInfo]:
        """Live deferred ToolInfo mapping, refreshed from the provider on read.

        Backs the prompt rail's catalogue drift detection: names, descriptions,
        and schemas of the current deferred registry, i.e. exactly the set
        ``tool_search`` serves this turn.
        """

        self._live_records()
        return dict(self._tool_by_name)

    def mcp_connector_groups(self) -> dict[str, dict[str, Any]]:
        """Group the live deferred MCP tools by connector server.

        openjiuwen registers MCP tools as ``mcp_{server}_{tool}``; known
        server names (from ``mcp_servers_provider``, longest first) resolve
        multi-underscore server names precisely, with the first segment as
        the fallback. The provider also carries the optional localized
        description (``{"zh": ..., "en": ...}``; a plain string is accepted
        and used for both languages).
        """

        self._live_records()
        descriptions: dict[str, dict[str, str]] = {}
        if self._mcp_servers_provider is not None:
            try:
                raw = self._mcp_servers_provider() or {}
                for name, value in raw.items():
                    key = str(name or "").strip()
                    if not key:
                        continue
                    if isinstance(value, str):
                        text = value.strip()
                        descriptions[key] = {"zh": text, "en": text}
                    elif isinstance(value, dict):
                        descriptions[key] = {
                            "zh": str(value.get("zh") or "").strip(),
                            "en": str(value.get("en") or "").strip(),
                        }
            except Exception:
                logger.debug(
                    "[ToolRetrievalToolkit] mcp server descriptions read failed",
                    exc_info=True,
                )
        known_servers = sorted(descriptions, key=len, reverse=True)
        groups: dict[str, list[str]] = {}
        for name in self._tool_by_name:
            if not name.startswith("mcp_"):
                continue
            rest = name[len("mcp_"):]
            server = None
            for candidate in known_servers:
                prefix = f"{candidate}_"
                if rest.startswith(prefix) and len(rest) > len(prefix):
                    server = candidate
                    break
            if server is None:
                first, separator, tool = rest.partition("_")
                if not separator or not tool:
                    continue  # "mcp_x" alone is not server-qualified
                server = first
            groups.setdefault(server, []).append(name)
        return {
            server: {
                "tools": tuple(sorted(names, key=str.casefold)),
                "description_zh": descriptions.get(server, {}).get("zh", ""),
                "description_en": descriptions.get(server, {}).get("en", ""),
            }
            for server, names in groups.items()
        }

    def refresh_snapshot(self) -> None:
        """Re-read deferred tools and re-freeze the catalogue.

        Called by ``ToolRetrievalPromptRail.init`` and again on the first
        ``before_model_call`` (the registry is complete only then), so the
        snapshot reflects the live deferred set rather than the empty
        pre-init state.
        """

        self._frozen_catalogue = self._build_catalogue()

    # ─── tool_search ────────────────────────────────────────────────────

    async def tool_search(self, **kwargs: Any) -> str:
        """Serve one catalogue operation from the static category tree.

        Token-bounding two-phase flow:

        - ``search`` with ``query=<keywords>``: deferred tools whose name,
          description, or category matches — name + short description +
          category per line, no schemas, no pagination. A miss is rendered
          as an explicit "no deferred tool matches" answer, never an error,
          so the model can conclude absence in one round instead of walking
          the catalogue category by category.
        - ``get_schema`` with ``tool=<name>``: the full parameter schema
          for exactly the tool the model picked, ready for the fixed
          ``tool_call``.
        """

        # Fields from retired card contracts, popped defensively so long
        # sessions holding an older prompt snapshot degrade instead of
        # erroring: call_goal (UI-display-only), cursor (engine pagination —
        # never offered), tools (retired array spelling of ``tool``; a
        # direct caller's list degrades to its first name), category (the
        # retired list operation's browse key — reused as a search query),
        # limit (retired result-count knob — search serves a fixed top 5).
        kwargs.pop("call_goal", None)
        kwargs.pop("cursor", None)
        kwargs.pop("limit", None)
        legacy_category = str(kwargs.pop("category", "") or "").strip()
        legacy_tools = kwargs.pop("tools", None)
        operation = str(kwargs.get("operation") or "").strip().lower()
        tool_name = str(kwargs.pop("tool", "") or "").strip()
        if not tool_name and isinstance(legacy_tools, list) and legacy_tools:
            tool_name = str(legacy_tools[0]).strip()
        query = str(kwargs.pop("query", "") or "").strip()

        if operation == "get_schema":
            return self._render_schema(tool_name)
        if operation in ("search", ""):
            # Empty operation (direct callers only — the card requires it)
            # degrades to search; a guessed exact name also lands here
            # instead of a get_schema hard error.
            if not query and tool_name:
                query = tool_name
            return self._render_search(query)
        if operation == "list":
            # Retired operation: degrade to search — the stale prompt's
            # category name matches the same category line; a bare root
            # list falls back to the category overview.
            if not query:
                query = legacy_category
            if query:
                return self._render_search(query)
            return self._render_root_listing()

        raise ValueError(
            f'tool_search supports {"search", "get_schema"}, not {operation!r}; '
            'e.g. {"operation":"search","query":"cron 定时任务"} or '
            '{"operation":"get_schema","tool":"cron_create_job"}'
        )

    # ─── keyword search ─────────────────────────────────────────────────

    _SEARCH_RESULT_LIMIT = 5

    @staticmethod
    def _search_tokens(query: str) -> list[str]:
        """Split a query into lowercase word / CJK-run tokens, deduped."""

        tokens = re.findall(
            r"[a-z0-9_]+|[一-鿿]+", str(query or "").strip().casefold()
        )
        seen: set[str] = set()
        unique: list[str] = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                unique.append(token)
        return unique

    def _render_search(self, query: str) -> str:
        """Match deferred tools by keyword; a miss is the answer, not an error.

        Scoring: name containment (either direction — covers guessed exact
        names) beats description hits beats category hits (the category text
        feeds the score only). Result lines are ``[tool]`` name + short
        description + category suffix, schema-free — the full schema stays
        behind get_schema; at most ``_SEARCH_RESULT_LIMIT`` tools are served.
        """

        self._live_records()
        tokens = self._search_tokens(query)
        if not tokens:
            return self._render_root_listing()

        categories = self._live_categories()
        category_text = {
            category.path: f"{category.path} {category.description}".casefold()
            for category in categories
        }
        name_to_category = {
            name: category.path
            for category in categories
            for name in category.tools
        }
        for server, group in sorted(self.mcp_connector_groups().items()):
            branch = f"mcp_{server}"
            description = str(
                group.get("description_zh") or group.get("description_en") or ""
            ).strip()
            if description:
                category_text[branch] = f"{branch} {description}".casefold()
            for name in group.get("tools") or ():
                if name in self._tool_by_name:
                    name_to_category.setdefault(name, branch)

        scored_tools: list[tuple[int, bool, str]] = []
        for name, tool in self._tool_by_name.items():
            lowered_name = name.casefold()
            description = str(getattr(tool, "description", "") or "").casefold()
            branch = name_to_category.get(name)
            branch_text = category_text.get(branch, "") if branch else ""
            score = 0
            strong_hit = False
            desc_hits = 0
            branch_hits = 0
            for token in tokens:
                if len(token) >= 3 and (
                    token in lowered_name or lowered_name in token
                ):
                    score += 30
                    if _strong_name_hit(token, lowered_name):
                        strong_hit = True
                elif token in description:
                    desc_hits += 1
                elif branch_text and token in branch_text:
                    branch_hits += 1
            # Baseline parity: a single keyword hit in the description or
            # category text counts (BM25 also scores any single overlap
            # positively). Noise tolerance comes from ranking (name hits
            # outrank) and the weak-match absence license, not from a
            # token-count threshold.
            if desc_hits:
                score += 10 * desc_hits
            if branch_hits:
                score += 5 * branch_hits
            if score:
                scored_tools.append((score, strong_hit, name))

        lines = [f'## Deferred tools matching "{query}"', ""]
        for _, _, name in sorted(
            scored_tools, key=lambda item: (-item[0], item[2])
        )[: self._SEARCH_RESULT_LIMIT]:
            branch = name_to_category.get(name)
            suffix = f" (category: {branch})" if branch else ""
            lines.append(
                f"- [tool] {name}: "
                f"{_compact_schema_description(self._tool_by_name[name].description)}"
                f"{suffix}"
            )
        if not scored_tools:
            lines.extend(
                [
                    "",
                    "No deferred tool matches the query — the deferred tools "
                    "do not provide this capability. Conclude and use "
                    "another route (a direct tool or a skill) instead of "
                    "rephrasing the query or enumerating categories.",
                ]
            )
        elif not any(strong_hit for _, strong_hit, _ in scored_tools):
            # Weak keyword matches only: no distinctive tool-name token
            # lines up with the query, so the matches are likely noise.
            # State the absence license anyway — otherwise the model keeps
            # rephrasing the query hunting for the clean "no match" answer
            # (observed: pinchbench task_image_gen, 5 searches before
            # giving up).
            lines.extend(
                [
                    "",
                    "Only weak keyword matches — no tool name fits the query. "
                    "Unless one of the above is what you meant, the deferred "
                    "tools do not provide this capability: use another route "
                    "instead of rephrasing the query.",
                ]
            )
        return "\n".join(lines)

    # ─── listing renderers ──────────────────────────────────────────────

    def _live_categories(self) -> tuple[ToolCategory, ...]:
        """Static categories whose membership intersects the live registry."""

        live = self._tool_by_name
        return tuple(
            category._replace(
                tools=tuple(name for name in category.tools if name in live)
            )
            for category in _TOOL_CATEGORIES
            if any(name in live for name in category.tools)
        )

    def _uncategorized_names(self) -> tuple[str, ...]:
        """Live deferred names no static category claims (connectors aside)."""

        covered = {
            name
            for category in _TOOL_CATEGORIES
            for name in category.tools
        }
        return tuple(
            sorted(
                (
                    name
                    for name in self._tool_by_name
                    if name not in covered and not name.startswith("mcp_")
                ),
                key=str.casefold,
            )
        )

    def _render_root_listing(self) -> str:
        """Category overview — the defensive fallback for a bare list call."""

        lines = ["## Tool categories", ""]
        lines.extend(
            f"- [category] {category.path}: {category.description}"
            for category in self._live_categories()
        )
        for server, group in sorted(self.mcp_connector_groups().items()):
            description = str(
                group.get("description_zh") or group.get("description_en") or ""
            ).strip()
            if not description:
                description = f"Tools from MCP connector {server}."
            lines.append(f"- [category] mcp_{server}: {description}")
        uncategorized = self._uncategorized_names()
        if uncategorized:
            lines.append("")
            lines.extend(
                f"- [tool] {name}: "
                f"{_compact_schema_description(self._tool_by_name[name].description)}"
                for name in uncategorized
            )
        if len(lines) == 2:
            # No categories, no connectors, no stray tools: empty registry.
            return "## Tool categories\n\nNo deferred tools available."
        return "\n".join(lines)

    # ─── schema rendering ───────────────────────────────────────────────

    def _render_schema(self, tool_name: str) -> str:
        """Return the get_schema JSON record for one deferred tool."""

        if not tool_name:
            raise ValueError(
                'get_schema requires "tool": the exact tool name whose schema you need, '
                'e.g. {"operation":"get_schema","tool":"cron_create_job"}'
            )
        self._live_records()
        if tool_name not in self._tool_by_name:
            raise ValueError(
                f"get_schema: '{tool_name}' is not in the deferred catalogue. "
                "Use search (operation=search, query=<keywords>) to find the "
                "exact name; if nothing matches, the deferred tools do not "
                "provide that capability."
            )
        schemas = self._schemas_for_mentioned(
            "", cap=None, parse_names=False, requested=[tool_name]
        )
        record = schemas[0] if schemas else {
            "name": tool_name,
            "description": "",
            "parameters": {},
        }
        return json.dumps(
            {
                "operation": "get_schema",
                "name": record["name"],
                "description": record["description"],
                "schema": record["parameters"],
                "usage": (
                    "Execute via the fixed `tool_call` tool: name=<exact tool "
                    "name>, args=<this schema's parameters object>. "
                    "object-typed parameters must be JSON object literals inside "
                    "args, never JSON strings; keep field names and casing "
                    "exactly as in the schema."
                ),
            },
            ensure_ascii=False,
        )

    def _schemas_for_mentioned(
        self,
        content: str,
        *,
        cap: int | None = 12,
        parse_names: bool = True,
        requested: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve tool names into full-schema descriptors.

        ``requested`` (the get_schema call's ``tool`` argument, wrapped in a
        list) is authoritative — no text parsing, no cap, the model
        explicitly picked these.
        """

        if requested:
            names = [str(name) for name in requested if str(name) in self._tool_by_name]
        elif parse_names:
            names = [
                name for name in self._tool_by_name
                if f"[tool] {name} " in content or f"`{name}`" in content
            ]
        else:
            names = []
        if not names:
            return []
        schemas: list[dict[str, Any]] = []
        for name in sorted(set(names)):
            tool = self._tool_by_name.get(name)
            if tool is None:
                continue
            parameters = tool.parameters
            if hasattr(parameters, "model_json_schema"):
                parameters = parameters.model_json_schema()
            schemas.append(
                {
                    # Full description, verbatim: the usage rules embedded in a
                    # tool's description (cron-expression constraints, fields
                    # that must be omitted, delivery-channel policy) are what
                    # the model needs to fill args correctly — the old
                    # one-shot tool_search served them untruncated and its
                    # calls succeeded.
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": _anchored_parameters(parameters),
                }
            )
        if cap is not None and not (0 < len(schemas) <= cap):
            return []
        return schemas

    # ─── card ───────────────────────────────────────────────────────────

    def get_tools(self) -> list[LocalFunction]:
        """Return the session-local ``tool_search`` structured tool."""

        card = ToolCard(
            id=_tool_index_tool_id(self._session_scope),
            name=self.TOOL_NAME,
            description=_TOOL_SEARCH_CARD_DESCRIPTION,
            input_params=_tool_search_input_params(),
        )
        return [LocalFunction(card=card, func=self.tool_search)]


__all__ = [
    "ToolRetrievalToolkit",
    "ToolCategory",
    "ToolCatalogueSnapshot",
]
