"""Static in-code tool category catalogue for progressive ``tool_search``.

The deferred-tool directory is a flat category table frozen into this
module (``_TOOL_CATEGORIES``): category name → description + member tool
names. ``tool_search`` serves ``list`` (category navigation, names + short
descriptions) and ``get_schema`` (the ``{name, description, schema}`` record
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
from collections.abc import Callable
from typing import Any, NamedTuple

from openjiuwen.core.context_engine.processor.forked.compressor.recall.bm25 import (
    tokenize as _bm25_tokenize,
)
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard, ToolInfo
from openjiuwen.harness.tools.tool_discovery.bm25 import BM25ToolIndex

logger = logging.getLogger(__name__)

# Domain separator for tool ID generation
_TOOL_ID_DOMAIN = b"openjiuwen.tool-index-tool.v1\0"


class ToolCategory(NamedTuple):
    """One catalogue category: display path, description, member tool names."""

    path: str
    description: str
    tools: tuple[str, ...]


# The static category table — flat names, no hierarchy. Category lines are
# description-only (tool names are discovered through tool_search's list).
# Membership lists the stable built-in deferred tools; names not present in
# the live registry are skipped at render time.
_TOOL_CATEGORIES: tuple[ToolCategory, ...] = (
    ToolCategory(
        path="coding-memory",
        description=(
            "读写项目编码记忆（coding_memory/ 下的 markdown）：跨会话沉淀"
            "代码经验、事实与决策，支持分页读取与精确字符串替换。"
        ),
        tools=("coding_memory_edit", "coding_memory_read", "coding_memory_write"),
    ),
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
        path="image-processing",
        description="使用视觉模型处理图片：OCR 文字提取与图片问答。",
        tools=("image_ocr", "visual_question_answering"),
    ),
    ToolCategory(
        path="audio-processing",
        description="使用音频模型处理音频：语音转写、音频问答与元数据提取。",
        tools=(
            "audio_metadata",
            "audio_question_answering",
            "audio_transcription",
        ),
    ),
    ToolCategory(
        path="video-understanding",
        description="使用视频理解模型分析视频内容并回答相关问题。",
        tools=("video_understanding",),
    ),
    ToolCategory(
        path="image-generation",
        description="文生图：按文字提示词生成图片并保存到工作区。",
        tools=("generate_image", "generate_visual"),
    ),
    ToolCategory(
        path="video-generation",
        description="文生视频：提交视频生成任务，按任务号轮询进度并下载结果。",
        tools=("generate_video", "check_video_status"),
    ),
    ToolCategory(
        path="lsp",
        description="通过语言服务器（LSP）获取代码智能：定义、引用、诊断等。",
        tools=("lsp",),
    ),
    ToolCategory(
        path="modes",
        description="在 normal 与 plan 两种会话模式之间切换当前会话。",
        tools=("switch_mode",),
    ),
    ToolCategory(
        path="cron",
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
        path="heartbeat",
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
        path="skill-evolution",
        description=(
            "本地技能与经验的演进：经用户确认创建演进评审、启动受限评审并接受"
            "已审查的变更提案，以及演进经验的查询、读取与整理。"
        ),
        tools=(
            "evolve_review_task",
            "evolve_skill_experiences",
            "list_skill_experiences",
            "prepare_skill_evolution",
            "read_skill_experiences",
            "simplify_skill_experiences",
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
    ToolCategory(
        path="user-todos",
        description="管理用户个人待办与日程：任务、会议、提醒、计划。",
        tools=("user_todos",),
    ),
    ToolCategory(
        path="worktree",
        description=(
            "创建或恢复隔离的 git worktree 并切换当前会话工作目录；"
            "退出时还原到原目录。"
        ),
        tools=("enter_worktree", "exit_worktree"),
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
    "Deferred-tool catalogue, read-only. list: tools under [category]; "
    "add [keyword] to BM25-search within the category or, with no "
    "category, across all tools. "
    "get_schema: the {name, description, schema} of the tool named in "
    "[tool]; results execute via the fixed tool_call tool. "
    "Never executes tools."
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


def _compact_schema_description(description: str) -> str:
    """Compact a listing description; full docstrings live in get_schema."""

    compact = " ".join(str(description or "").split())
    if len(compact) <= _SCHEMA_DESC_CHARS:
        return compact
    return compact[: _SCHEMA_DESC_CHARS - 1].rstrip() + "…"


def _tool_search_input_params() -> dict[str, Any]:
    """``tool_search`` card schema: list / get_schema over the category table.

    The discovery flow is two-phase to bound tokens: list returns names and
    short descriptions only; get_schema returns the full schema for the tool
    the model has already picked.
    """

    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list", "get_schema"],
                "description": (
                    "list: tools under [category], optionally filtered by "
                    "[keyword]. get_schema: the schema of the tool named "
                    "in [tool]."
                ),
            },
            "category": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Exact category name. Omit only for a keyword-only "
                    "global search."
                ),
            },
            "keyword": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    "list only. BM25 keyword search: within [category], or "
                    "across all tools when category is omitted."
                ),
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


def _category_key(path: str) -> tuple[str, ...]:
    """Normalize a category name to a casefolded single-segment tuple."""

    return (str(path or "").strip().casefold(),)


class ToolRetrievalToolkit:
    """Expose one session-local ``tool_search`` structured tool for deferred tools.

    The catalogue is the in-code ``_TOOL_CATEGORIES`` table rendered against
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
                catalogue and the connector ``list`` rendering
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
        """Freeze the static table against the live registry.

        A category line is a routing signal: it pays for itself only when
        there is a membership to route to. Categories with exactly one live
        member collapse — the category line is dropped and the member falls
        through to the flat ``[tool]`` block (name + short description,
        get_schema directly), saving a list round trip that would reveal a
        single name the prompt could have stated outright.
        """

        self._live_records()
        live = self._tool_by_name
        categories = tuple(
            category._replace(
                tools=tuple(name for name in category.tools if name in live)
            )
            for category in _TOOL_CATEGORIES
            if sum(1 for name in category.tools if name in live) >= 2
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
        """Serve one catalogue operation from the static category table.

        Token-bounding two-phase flow:

        - ``list``: one category's member tool names + short descriptions
          — served whole, no schemas, no pagination. ``keyword``
          BM25-searches within the category, or across every tool when
          category is omitted; bare ``list`` (neither field) errors — the
          category catalogue already sits in the system prompt.
        - ``get_schema`` with ``tool=<name>``: the full parameter schema
          for exactly the tool the model picked, ready for the fixed
          ``tool_call``.
        """

        # Fields from retired card contracts, popped defensively so long
        # sessions holding an older prompt snapshot degrade instead of
        # erroring: call_goal (UI-display-only), cursor (engine pagination —
        # never offered), query (retired keyword filter), tools (retired
        # array spelling of ``tool``; a direct caller's list degrades to its
        # first name).
        kwargs.pop("call_goal", None)
        kwargs.pop("cursor", None)
        had_retired_query = "query" in kwargs
        kwargs.pop("query", None)
        legacy_tools = kwargs.pop("tools", None)
        operation = str(kwargs.get("operation") or "").strip().lower()
        tool_name = str(kwargs.pop("tool", "") or "").strip()
        if not tool_name and isinstance(legacy_tools, list) and legacy_tools:
            tool_name = str(legacy_tools[0]).strip()

        # ``keyword`` (list only) is normalized leniently: a stale
        # snapshot's array spelling joins into one string, and anything
        # empty after the strip degrades to an unfiltered listing.
        raw_keyword = kwargs.pop("keyword", None)
        if isinstance(raw_keyword, (list, tuple)):
            raw_keyword = " ".join(str(item) for item in raw_keyword)
        keyword = str(raw_keyword or "").strip()

        if operation == "get_schema":
            return self._render_schema(tool_name)
        if operation in ("list", "search", ""):
            # "search" aliases list for prompts frozen before search was
            # retired from the card; an empty operation (direct callers only
            # — the card requires it) degrades to a plain listing too.
            category = str(kwargs.get("category") or "").strip()
            if not category and not keyword:
                # Bare list (and a stale keyword-era snapshot whose ``query``
                # was dropped) teaches the valid call shapes; a bare legacy
                # "search" degrades to the root category listing instead.
                if operation == "list" or had_retired_query:
                    raise ValueError(
                        'list needs "category" or "keyword": pick a category '
                        "from the catalogue in your system prompt, or pass "
                        'keyword for a global search. e.g. '
                        '{"operation":"list","category":"cron"} or '
                        '{"operation":"list","keyword":"图片"}'
                    )
                return self._render_root_listing()
            return self._render_listing(category, keyword=keyword)

        raise ValueError(
            f'tool_search supports {"list", "get_schema"}, not {operation!r}; '
            'e.g. {"operation":"list","category":"cron"} or '
            '{"operation":"get_schema","tool":"cron_create_job"}'
        )

    # ─── listing renderers ──────────────────────────────────────────────

    def _render_listing(self, category: str, keyword: str = "") -> str:
        self._live_records()

        if not category:
            if keyword:
                return self._render_keyword_search_listing(keyword)
            # The category catalogue is already in the system prompt; a
            # bare list would only re-dump it. Teach the call shape.
            raise ValueError(
                'list needs "category" or "keyword": pick a category from '
                "the catalogue in your system prompt, or pass keyword for "
                'a global search. e.g. {"operation":"list","category":'
                '"cron"} or {"operation":"list","keyword":"图片"}'
            )

        connector_server = self._match_connector_category(category)
        if connector_server is not None:
            # Connector groups live only as virtual branches; serve their
            # list here so the advertised category stays navigable.
            return self._render_connector_listing(connector_server, keyword=keyword)
        return self._render_category_listing(category, keyword=keyword)

    def _render_root_listing(self) -> str:
        """Root category listing for retired bare ``search`` spellings.

        Mirrors the prompt directory's collapse semantics — multi-member
        categories render ``[category]`` lines, single-member ones fold
        into ``[tool]`` lines — plus the MCP connector virtual branches
        (connecters never fold: the server description carries its own
        routing signal). Bare ``list`` never gets here: the catalogue
        already sits in the system prompt, so re-dumping it would only
        waste a call.
        """

        lines = [
            "## Tool categories",
            "完整目录见 system prompt；本列表仅为旧拼写降级渲染。",
            "",
        ]
        for category in self._live_categories():
            description = " ".join((category.description or "").split())
            if len(category.tools) >= 2:
                lines.append(f"- [category] {category.path}: {description}")
                continue
            name = category.tools[0]
            tool = self._tool_by_name.get(name)
            tool_description = (tool.description if tool else "") or ""
            lines.append(
                f"- [tool] {name}: "
                f"{_compact_schema_description(tool_description)}"
            )
        for server, group in self.mcp_connector_groups().items():
            description = str(
                group.get("description_zh")
                or group.get("description_en")
                or ""
            ).strip() or f"Tools from MCP connector {server}."
            lines.append(f"- [category] mcp_{server}: {description}")
        return "\n".join(lines)

    def _rank_tools_by_query(
        self, names: tuple[str, ...] | list[str], keyword: str
    ) -> list[str]:
        """BM25-rank tools against a keyword, engine-index semantics.

        ``BM25ToolIndex.search`` brings the ranking (exact-name first),
        the score>0 threshold, and the tiebreaks; the corpus is one
        category or the live registry, so the index is rebuilt per call.
        Results sharing only single-character tokens with the query are
        dropped: the shared tokenizer emits CJK unigrams, and a
        unigram-only overlap (query 图片生成 vs doc 完成/发生) is noise
        BM25's positive IDF cannot zero out.
        """

        documents = [
            self._tool_by_name[name] for name in names if name in self._tool_by_name
        ]
        if not documents:
            return []
        ranked = BM25ToolIndex.build(documents).search(keyword, limit=len(documents))
        multi_char = {
            token for token in _bm25_tokenize(keyword) if len(token) > 1
        }
        return [
            tool.name
            for tool in ranked
            if multi_char
            & set(_bm25_tokenize(f"{tool.name or ''} {tool.description or ''}"))
        ]

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

    def _render_keyword_search_listing(self, keyword: str) -> str:
        """BM25-search every live deferred tool (root + keyword).

        Covers categorized, uncategorized, and MCP connector tools alike —
        names are sorted before indexing so the rank tiebreaks stay
        deterministic regardless of registry order.
        """

        names = sorted(self._tool_by_name, key=str.casefold)
        ranked = self._rank_tools_by_query(names, keyword)
        header = f'## Tools (keyword: "{keyword}")'
        if not ranked:
            return (
                f"{header}\n\n"
                f'No tools match keyword "{keyword}" (0 of {len(names)})'
            )
        lines = [header, ""]
        lines.extend(
            f"- [tool] {name}: "
            f"{_compact_schema_description(self._tool_by_name[name].description)}"
            for name in ranked
        )
        return "\n".join(lines)

    def _render_category_listing(self, category: str, keyword: str = "") -> str:
        """Render one category's member tools.

        A name match lists the category's tools, BM25-ranked against
        ``keyword`` when given; anything else raises with the valid names
        so the model can retry without probing.
        """

        requested = _category_key(category)
        matches = [
            candidate
            for candidate in self._live_categories()
            if _category_key(candidate.path) == requested
        ]
        if matches:
            matched = matches[0]
            names: tuple[str, ...] | list[str] = matched.tools
            header = f"## Tools (category: {matched.path})"
            if keyword:
                names = self._rank_tools_by_query(matched.tools, keyword)
                if not names:
                    return (
                        f'## Tools (category: {matched.path}, keyword: "{keyword}")'
                        f"\n\n"
                        f'No tools match keyword "{keyword}" '
                        f"(0 of {len(matched.tools)})"
                    )
                header = f'## Tools (category: {matched.path}, keyword: "{keyword}")'
            lines = [header, ""]
            lines.extend(
                f"- [tool] {name}: "
                f"{_compact_schema_description(self._tool_by_name[name].description)}"
                for name in names
            )
            return "\n".join(lines)

        raise ValueError(
            f"list: {category!r} is not a catalogue category. Valid: "
            f"{', '.join(self._valid_category_names())}. "
            "Or omit category and pass keyword for a global search."
        )

    def _valid_category_names(self) -> list[str]:
        names = [category.path for category in self._live_categories()]
        for server in self.mcp_connector_groups():
            names.append(f"mcp_{server}")
        return names

    def _match_connector_category(self, category: str) -> str | None:
        """Resolve a list category against the MCP connector groups."""

        category = category.strip()
        if not category:
            return None
        for server in self.mcp_connector_groups():
            if category in (f"mcp_{server}", server):
                return server
        return None

    def _render_connector_listing(self, server: str, keyword: str = "") -> str:
        """Render one connector group in the standard list format."""

        group = self.mcp_connector_groups().get(server) or {}
        member_names = tuple(
            name for name in group.get("tools") or () if name in self._tool_by_name
        )
        header = f"## Tools (MCP connector: {server})"
        if keyword:
            ranked = self._rank_tools_by_query(member_names, keyword)
            if not ranked:
                return (
                    f'## Tools (MCP connector: {server}, keyword: "{keyword}")'
                    f"\n\n"
                    f'No tools match keyword "{keyword}" '
                    f"(0 of {len(member_names)})"
                )
            member_names = tuple(ranked)
            header = f'## Tools (MCP connector: {server}, keyword: "{keyword}")'
        lines = [header]
        description = str(
            group.get("description_zh") or group.get("description_en") or ""
        ).strip()
        if description:
            lines.extend(["", description])
        lines.append("")
        lines.extend(
            f"- [tool] {name}: {self._tool_by_name[name].description or ''}"
            for name in member_names
        )
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
                "Use list to find the exact name."
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
