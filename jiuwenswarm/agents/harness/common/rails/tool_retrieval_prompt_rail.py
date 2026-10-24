"""Prompt boundary for progressive ``tool_search`` deferred-tool disclosure.

Mirrors the skill-side ``SkillRetrievalPromptRail`` disclosure model: the
system prompt shows only the tool category catalogue (static in-code
categories plus MCP connector virtual branches), NOT a flat dump of every
deferred tool's name+description. The full inventory is discovered on
demand through ``tool_search``.

Three responsibilities each turn:
1. Inject the disclosure section (categories with their descriptions) into
   the system prompt, built from ``ToolRetrievalToolkit.frozen_catalogue`` —
   the static category table frozen against the live deferred registry.
2. Keep the frozen catalogue honest: tools registered/removed after the
   freeze are reported as delta/snapshot attachments, mirroring openjiuwen
   ``ProgressiveToolRail._sync_deferred_tool_attachment``.
3. Filter ``inputs.tools`` so only DIRECT tools (plus our ``tool_search``)
   reach the model; DEFERRED tools stay hidden until discovered.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Dict, Optional, Set

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import ToolCard, ToolExposure
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptAttachmentKind, PromptSection
from openjiuwen.harness.prompts.sections.progressive_tool_rail import (
    render_deferred_tool_catalog_delta,
    render_deferred_tool_catalog_snapshot,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.progressive_tool_rail import _NESTED_TOOL_CALL_KEY
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.tool_discovery.bm25 import BM25ToolIndex
from openjiuwen.harness.tools.tool_discovery.tool_call import ToolCallTool

from jiuwenswarm.agents.harness.common.tools.tool_retrieval_toolkits import (
    ToolCategory,
    ToolCatalogueSnapshot,
    ToolRetrievalToolkit,
)
from jiuwenswarm.common.config import is_tool_retrieval_enabled

logger = logging.getLogger(__name__)

_TOOL_SEARCH_TOOL_NAME = "tool_search"
_MAX_ENTRY_DESC_CHARS = 120

# Deferred-catalogue drift detection (mirrors openjiuwen ProgressiveToolRail's
# attachment-sync state/metadata contract, namespaced to this rail).
_CATALOG_STATE_KEY = "__tool_retrieval_deferred_catalog__"
_CATALOG_ATTACHMENT_SECTION = "tool_retrieval_deferred_tools"
_CATALOG_ATTACHMENT_SOURCE = "tool_retrieval_prompt_rail"
_CATALOG_METADATA_TOOLS = "catalog_tools"
_CATALOG_METADATA_FINGERPRINTS = "catalog_fingerprints"
_CATALOG_SNAPSHOT_INTERVAL = 10
_STATE_INITIAL_TOOLS = "initial_tools"
_STATE_INITIAL_FINGERPRINTS = "initial_fingerprints"
_STATE_TOOLS = "tools"
_STATE_FINGERPRINTS = "fingerprints"

_DISCLOSURE_INTRO_CN = (
    "当前启用了渐进式工具加载。工具分为两类：\n"
    "- direct 工具：在顶层 tools 中，可以直接调用。\n"
    "- deferred 工具：已注册且可用，但不在顶层 tools 中，需先通过 `tool_search` 发现。\n"
    "\n"
    "## tool_search 使用规则\n"
    "1. 先判断用户真正想完成的操作；用户未点名执行方式且 direct 工具明确支持时，"
    "可直接调用。\n"
    "2. 用户点名了具体工具、技术或执行方式（如“用 PowerShell”“用终端”）时，以用户"
    "点名为准：即使 direct 工具（如 bash）技术上也能完成，也必须使用对应的 deferred "
    "工具；目录中没有时向用户说明，不得用 direct 工具擅自替代。\n"
    "3. 目录每行已标注类型，按标注导航：`[category]` 行是分类，要看其下工具名单，调用 "
    "`tool_search`（operation=list，category=该行名称）；`[tool]` 行已直列工具名与简述，"
    "直接调用 `tool_search`（operation=get_schema，tool=名称），不要 list。分类名就在行"
    "上，永远不要 list 根分类。"
    "不能猜测 deferred 工具名称，也不要将就使用不合适的工具。list 只返回名称与简短"
    "描述，不含 schema。\n"
    "4. 确定工具后，用 get_schema（tool=准确工具名）获取该工具的 "
    "{name, description, schema}，schema 即执行参数结构。\n"
    "5. get_schema 之后，下一轮通过固定的 `tool_call` 执行：把准确工具名称放入 `name`，"
    "把符合 schema 的参数放入 `args`。不要直接调用 deferred 工具名称。\n"
    "6. `tool_search` 只用于发现工具，永不执行工具；不要用 `bash` 或其他 direct 工具"
    "替代或执行搜索到的工具。\n"
    "\n"
    "下方目录为 session 初始快照；会话中的新增、修改、删除以「deferred 工具目录更新」"
    "附件通知，新增工具的名称同样直列，直接 get_schema。"
)
_DISCLOSURE_INTRO_EN = (
    "Progressive tool loading is enabled. Tools fall into two classes:\n"
    "- direct tools: listed in the top-level tools; call them directly.\n"
    "- deferred tools: registered and available but not in the top-level tools; "
    "discover them via `tool_search` first.\n"
    "\n"
    "## tool_search usage rules\n"
    "1. Identify the operation the user actually wants; if a direct tool clearly "
    "supports it and the user named no specific tool or execution method, call "
    "that tool directly.\n"
    "2. When the user explicitly names a tool, technology, or execution method "
    "(e.g. \"use PowerShell\", \"use the terminal\"), that naming takes "
    "precedence: even if a direct tool (such as bash) could technically do it, "
    "you must use the matching deferred tool; if the catalogue has no match, "
    "tell the user instead of silently substituting a direct tool.\n"
    "3. Every catalogue line carries a type mark — navigate by it: a "
    "`[category]` line is a category; to see its tool names, call "
    "`tool_search` (operation=list, category=<that line's name>). A `[tool]` "
    "line already lists the tool's name and short description — go straight "
    "to `tool_search` (operation=get_schema, tool=<name>), do not list. The "
    "category name is right on the line, so "
    "never list the root. Do not guess deferred tool names and do not settle "
    "for an ill-fitting tool. list returns names and short descriptions only, "
    "no schema.\n"
    "4. Once you know the tool, fetch its {name, description, schema} record "
    "with get_schema (tool=<one exact name>); `schema` is the execution args "
    "structure.\n"
    "5. After get_schema, execute via the fixed `tool_call` tool in the next "
    "turn: put the exact tool name in `name` and the schema-compatible "
    "arguments in `args`. Do not call deferred tool names directly.\n"
    "6. `tool_search` only discovers tools, it never executes them; do not use "
    "`bash` or another direct tool to substitute for or execute a discovered tool.\n"
    "\n"
    "The catalogue below is the session's initial snapshot; mid-session "
    "additions, changes, and removals arrive as \"Deferred-tool directory "
    "update\" attachment notices — an added tool's name is listed there too, "
    "so get_schema it directly."
)


class ToolRetrievalPromptRail(DeepAgentRail):
    """Own the progressive tool disclosure prompt + tool filtering."""

    priority = 80
    SECTION_NAME = "tool_retrieval"
    SECTION_PRIORITY = 75

    def __init__(
        self,
        *,
        toolkit: ToolRetrievalToolkit | None = None,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._toolkit = toolkit
        self._config_base = config_base or {}
        self._session_enabled = is_tool_retrieval_enabled(self._config_base)
        self._agent: Any = None
        self.system_prompt_builder = None
        self._frozen_snapshot: ToolCatalogueSnapshot | None = None
        self._frozen_tool_names: frozenset[str] = frozenset()
        # Frozen MCP connector groups (server → {tools, description}): one
        # catalogue line per connector — their permanent virtual branch.
        self._frozen_connector_groups: Dict[str, Dict[str, Any]] = {}
        # Set once the catalogue has been re-frozen on the first model call;
        # after that the section stays byte-stable and drift flows through
        # delta attachments only.
        self._catalogue_finalized = False
        # Cards this rail registered into AbilityManager (the ``tool_call``
        # envelope); uninit removes exactly these.
        self._owned_tool_cards: Dict[str, ToolCard] = {}

    def init(self, agent: Any) -> None:
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        # Freeze the disclosure catalogue at session start — same contract as
        # SkillRetrievalPromptRail. init() runs during ensure_initialized(),
        # after AbilityManager resolved exposure, so refresh first to pick up
        # the live deferred set (the toolkit was constructed pre-init with an
        # empty snapshot).
        try:
            self._freeze_disclosure_catalogue()
        except Exception:
            logger.debug(
                "[ToolRetrievalPromptRail] snapshot freeze failed",
                exc_info=True,
            )

        self._register_execution_envelope(agent)

    def _freeze_disclosure_catalogue(self) -> None:
        """(Re-)freeze the disclosure catalogue from the live registry.

        Runs once at init and once more on the first ``before_model_call``:
        MCP connector tools register asynchronously and can land after
        ``ensure_initialized``, so the static directory is captured at the
        last moment before the first prompt render — the same timing
        openjiuwen's rail gets via ``before_invoke``.
        """

        toolkit = self._toolkit
        if toolkit is None:
            return
        toolkit.refresh_snapshot()
        self._frozen_snapshot = toolkit.frozen_catalogue
        self._frozen_tool_names = frozenset(toolkit.deferred_tool_names)
        try:
            self._frozen_connector_groups = toolkit.mcp_connector_groups()
        except Exception:
            logger.debug(
                "[ToolRetrievalPromptRail] connector group freeze failed",
                exc_info=True,
            )
            self._frozen_connector_groups = {}
        logger.info(
            "[ToolRetrievalPromptRail] freeze catalogue | tools=%d | categories=%d | connectors=%d",
            len(self._frozen_tool_names),
            len(self._frozen_snapshot.categories),
            len(self._frozen_connector_groups),
        )

    def _register_execution_envelope(self, agent: Any) -> None:
        """Register the advertised ``tool_call`` wrapper for catalogue tools.

        A native call straight to an unadvertised deferred tool has no
        API-level schema anchoring, and nested-object parameters get
        double-serialized into JSON strings during free-form transcription
        from the get_schema result (observed 6/6 identical retries on cron's
        ``job`` with in-result anchoring notes ignored). Deferred tools stay
        out of the advertised tools list; instead the fixed envelope — whose
        ``args: object`` slot anchors the payload — is the one advertised, the
        same contract openjiuwen's ProgressiveToolRail used and the model
        demonstrably calls correctly.
        """

        ability_manager = getattr(agent, "ability_manager", None)
        add_ability = getattr(ability_manager, "add_ability", None)
        if not callable(add_ability):
            return
        try:
            language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
            envelope = ToolCallTool(
                call_tool=self._call_deferred_tool,
                language=language,
            )
            # Meta tools are always directly callable; mark explicitly so a
            # progressive registration policy cannot hide the wrapper.
            envelope.card.exposure = ToolExposure.DIRECT
            envelope.card.set_exposure_declared(True)
            result = add_ability(envelope.card, envelope)
            if getattr(result, "added", False):
                self._owned_tool_cards[envelope.card.name] = envelope.card
                logger.info(
                    "[ToolRetrievalPromptRail] tool_call envelope registered"
                )
            else:
                logger.debug(
                    "[ToolRetrievalPromptRail] tool_call envelope not added "
                    "(name already registered)"
                )
        except Exception:
            logger.warning(
                "[ToolRetrievalPromptRail] tool_call envelope registration failed",
                exc_info=True,
            )

    async def _call_deferred_tool(
        self,
        name: str,
        args: Dict[str, Any],
        session: Any,
        callback_context: Any,
    ) -> ToolOutput:
        """Execute one catalogue deferred tool through AbilityManager.

        Mirrors openjiuwen ProgressiveToolRail._call_discovered_tool: resolve
        the target, authorize it against the live deferred catalogue, then
        dispatch through the normal AbilityManager execution path so target
        tool rails, permissions, timeouts, and error handling remain active.
        """

        target_name = str(name or "").strip()
        if not target_name:
            return ToolOutput(
                success=False, error="tool_call requires a non-empty tool name."
            )

        toolkit = self._toolkit
        if toolkit is None or target_name not in toolkit.deferred_tool_names:
            return ToolOutput(
                success=False,
                error=(
                    f"Deferred tool '{target_name}' must come from the "
                    "tool_search catalogue before it can be called."
                ),
            )

        agent = getattr(callback_context, "agent", None) or self._agent
        ability_manager = getattr(agent, "ability_manager", None)
        card = ability_manager.get(target_name) if ability_manager is not None else None
        if card is None:
            return ToolOutput(
                success=False,
                error=f"Deferred tool '{target_name}' is no longer registered.",
            )
        if isinstance(card, ToolCard) and (
            getattr(card, "exposure", ToolExposure.DIRECT) != ToolExposure.DEFERRED
        ):
            return ToolOutput(
                success=False,
                error=f"Tool '{target_name}' is not a deferred catalogue tool.",
            )

        outer_call = getattr(getattr(callback_context, "inputs", None), "tool_call", None)
        outer_call_id = str(getattr(outer_call, "id", "tool_call") or "tool_call")
        target_call = ToolCall(
            id=f"{outer_call_id}:target",
            type="function",
            name=target_name,
            arguments=json.dumps(args or {}, ensure_ascii=False),
        )

        extra = getattr(callback_context, "extra", None)
        if isinstance(extra, dict):
            extra[_NESTED_TOOL_CALL_KEY] = True
        try:
            results = await ability_manager.execute(
                ctx=callback_context,
                tool_call=target_call,
                session=session,
                parallel_tool_calls=False,
            )
        except ToolInterruptException:
            raise
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        finally:
            if isinstance(extra, dict):
                extra.pop(_NESTED_TOOL_CALL_KEY, None)

        if not results:
            return ToolOutput(
                success=False,
                error=f"Deferred tool '{target_name}' returned no execution result.",
            )
        target_result, target_message = results[0]
        if isinstance(target_result, ToolInterruptException):
            raise target_result
        target_success = getattr(target_result, "success", None)
        if target_success is False:
            return ToolOutput(
                success=False,
                error=str(
                    getattr(target_result, "error", None)
                    or getattr(target_message, "content", None)
                    or target_result
                ),
            )
        return ToolOutput(
            success=True,
            data={"name": target_name, "result": target_result},
        )

    def uninit(self, agent: Any) -> None:
        # Remove exactly the cards this rail registered; skip names another
        # owner re-registered after us (tearing those down would unregister
        # their live instance).
        ability_manager = getattr(agent, "ability_manager", None)
        remove_ability = getattr(ability_manager, "remove_ability", None)
        if callable(remove_ability):
            get = getattr(ability_manager, "get", None)
            for tool_name, tool_card in list(self._owned_tool_cards.items()):
                if callable(get) and get(tool_name) is not tool_card:
                    continue
                try:
                    remove_ability(tool_name)
                except Exception as exc:
                    logger.warning(
                        "[ToolRetrievalPromptRail] failed to remove '%s': %s",
                        tool_name,
                        exc,
                    )
        self._owned_tool_cards.clear()

        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None
        self._agent = None
        self._frozen_snapshot = None
        self._frozen_tool_names = frozenset()
        self._frozen_connector_groups = {}
        self._catalogue_finalized = False

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if not self._session_enabled:
            return
        agent = getattr(ctx, "agent", None) or self._agent
        if agent is not None and self.system_prompt_builder is None:
            self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

        if not self._catalogue_finalized:
            # One re-freeze at the last moment before the first render: MCP
            # connector tools register after ensure_initialized and must be
            # named in the static directory, not only in tool_search results.
            self._catalogue_finalized = True
            try:
                self._freeze_disclosure_catalogue()
            except Exception:
                logger.debug(
                    "[ToolRetrievalPromptRail] first-render re-freeze failed",
                    exc_info=True,
                )

        language = "cn"
        if self.system_prompt_builder is not None:
            language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
            english = str(language).lower().startswith("en")
            snapshot = self._frozen_snapshot
            appendix = self._build_disclosure_appendix(english, snapshot)
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=self.SECTION_NAME,
                    content={language: appendix},
                    priority=self.SECTION_PRIORITY,
                )
            )

        await self._sync_deferred_tool_catalog(ctx, language=str(language))
        self._filter_deferred_from_inputs(ctx)

    def _build_disclosure_appendix(
        self,
        english: bool,
        snapshot: ToolCatalogueSnapshot | None,
    ) -> str:
        """Render the progressive disclosure section: categories + names.

        Mirrors ``SkillRetrievalPromptRail._build_candidate_appendix``: only
        the category catalogue is shown, never a full flat dump. Each category
        line carries only its description — tool names are discovered
        through tool_search. MCP connector tools get one virtual branch line
        per connector; tools no category line routes to (static-table misses)
        are listed with name+description in an uncategorized block (bare
        names carry no routing signal: the model would have to probe each
        one with get_schema). The listing is frozen at init; mid-session
        drift is reported through delta attachments, not by rewriting this
        section.
        """

        lines = ["## Deferred Tools" if english else "## Deferred 工具（渐进式披露）"]
        lines.extend(["", _DISCLOSURE_INTRO_EN if english else _DISCLOSURE_INTRO_CN])

        if snapshot is None:
            return "\n".join(lines)

        categories = snapshot.categories
        if categories:
            lines.extend(
                [
                    "",
                    "Categories (virtual groups):" if english else "分类（虚拟目录）：",
                    *_render_categories(categories),
                ]
            )
        covered = {name for category in categories for name in category.tools}
        connector_lines, connector_names = self._render_connector_lines(
            english, covered
        )
        lines.extend(connector_lines)
        uncovered = self._uncovered_tool_names(connector_names)
        if uncovered:
            # No category line routes to these tools, so a bare name
            # list would force one get_schema probe per opaque name:
            # list them with compact descriptions instead — the same
            # entry primitive the uncategorized block uses, bounded by
            # compaction.
            lines.extend(
                [
                    "",
                    "Direct get_schema tools:" if english else "直接 get_schema 工具：",
                    *_render_entries(self._uncovered_flat_entries(uncovered)),
                ]
            )
        if not categories and not connector_lines and not uncovered:
            lines.extend(
                [
                    "",
                    "No available tools."
                    if english
                    else "当前没有可用工具。",
                ]
            )

        return "\n".join(lines)

    def _render_connector_lines(
        self,
        english: bool,
        covered: Set[str],
    ) -> tuple[list[str], Set[str]]:
        """One catalogue line per MCP connector: its permanent virtual branch.

        Connector tools are dynamic and never enter the static category
        table; the branch groups them under ``mcp_{server}`` — a category
        ``tool_search``'s list resolves — with the manifest's localized
        description (generic fallback otherwise). Tools already covered by a
        category are skipped defensively, so each connector's tools are
        claimed exactly once for coverage.
        """

        lines: list[str] = []
        shown: Set[str] = set()
        for server, group in sorted(self._frozen_connector_groups.items()):
            names = tuple(
                name for name in group.get("tools", ()) if name not in covered
            )
            if not names:
                continue
            if english:
                description = str(
                    group.get("description_en") or group.get("description_zh") or ""
                ).strip()
            else:
                description = str(
                    group.get("description_zh") or group.get("description_en") or ""
                ).strip()
            if not description:
                description = (
                    f"Tools from MCP connector {server}."
                    if english
                    else f"MCP 连接器 {server} 提供的工具。"
                )
            lines.append(f"- [category] `mcp_{server}`: {_compact_text(description)}")
            shown.update(names)
        return lines, shown

    def _uncovered_tool_names(
        self,
        extra_covered: Set[str] | None = None,
    ) -> tuple[str, ...]:
        """Frozen deferred names no category/connector line covers."""

        snapshot = self._frozen_snapshot
        covered = {
            name
            for category in (snapshot.categories if snapshot else ())
            for name in category.tools
        }
        if extra_covered:
            covered |= extra_covered
        return tuple(sorted(self._frozen_tool_names - covered, key=str.casefold))

    def _uncovered_flat_entries(self, uncovered: tuple[str, ...]) -> tuple:
        """Frozen name+description entries for the uncovered tool names.

        The frozen flat inventory — captured at the same freeze as the
        catalogue — supplies the compact descriptions; uncovered order is
        preserved so the block stays deterministic.
        """

        snapshot = self._frozen_snapshot
        descriptions = {
            entry.name: entry.description
            for entry in (snapshot.flat_entries if snapshot else ())
        }
        return tuple(
            (name, descriptions.get(name, ""))
            for name in uncovered
        )

    def _filter_deferred_from_inputs(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not isinstance(tools, list):
            return
        # DIRECT tools plus our meta tools (tool_call is registered with
        # exposure=DIRECT so it is already inside direct_names). Deferred
        # tools stay hidden from the advertised list; the model executes them
        # through the advertised tool_call envelope instead.
        direct_names = self._get_direct_tool_names(self._agent)
        direct_names.add(_TOOL_SEARCH_TOOL_NAME)
        filtered = [
            tool for tool in tools
            if str(getattr(tool, "name", "") or "") in direct_names
        ]
        inputs.tools = filtered

    @staticmethod
    def _get_direct_tool_names(agent: Any) -> Set[str]:
        ability_manager = getattr(agent, "ability_manager", None)
        list_abilities = getattr(ability_manager, "list", None)
        if not callable(list_abilities):
            return set()
        try:
            abilities = list_abilities() or []
        except Exception:
            return set()
        names: Set[str] = set()
        for card in abilities:
            name = str(getattr(card, "name", "") or "")
            exposure = getattr(card, "exposure", ToolExposure.DIRECT)
            if name and exposure == ToolExposure.DIRECT:
                names.add(name)
        return names

    # ─── deferred-catalogue drift sync (mirrors ProgressiveToolRail) ─────

    async def _sync_deferred_tool_catalog(
        self,
        ctx: AgentCallbackContext,
        *,
        language: str = "cn",
    ) -> None:
        """Publish deferred-catalogue changes (added/updated/removed).

        The frozen prompt section is the session's initial directory and is
        never rewritten. Each turn the live deferred registry is diffed
        against the last materialized directory; drift is reported as a delta
        attachment, switching to a full snapshot after a language change,
        context-compaction history loss, or ``_CATALOG_SNAPSHOT_INTERVAL``
        consecutive deltas — the same contract openjiuwen's
        ``ProgressiveToolRail._sync_deferred_tool_attachment`` keeps.
        """

        session = getattr(ctx, "session", None)
        agent = getattr(ctx, "agent", None) or self._agent
        manager = getattr(agent, "prompt_attachment_manager", None)
        if session is None or manager is None:
            return
        try:
            writer = manager.bind_context(ctx)
        except Exception:
            logger.debug(
                "[ToolRetrievalPromptRail] attachment context bind failed",
                exc_info=True,
            )
            return
        session_id = getattr(writer, "session_id", None)
        if not session_id:
            return

        current_tools, current_fingerprints = self._collect_live_catalog()
        state = session.get_state(_CATALOG_STATE_KEY)
        if not isinstance(state, dict):
            state = {}

        initial_tools = _normalize_catalog(state.get(_STATE_INITIAL_TOOLS))
        if initial_tools is None:
            # Seed from the frozen directory — the catalogue the model
            # actually sees — so a registration racing the freeze is reported
            # as an addition instead of being swallowed into the baseline.
            initial_tools = self._frozen_catalog_descriptions() or dict(
                current_tools
            )
        initial_fingerprints = _normalize_catalog(
            state.get(_STATE_INITIAL_FINGERPRINTS)
        )
        if initial_fingerprints is None:
            initial_fingerprints = dict(current_fingerprints)
        previous_tools = _normalize_catalog(state.get(_STATE_TOOLS)) or {}
        previous_fingerprints = (
            _normalize_catalog(state.get(_STATE_FINGERPRINTS)) or {}
        )
        initialized = bool(state.get("initialized"))
        previous_language = str(state.get("language") or "")
        version = max(1, _to_int(state.get("version"), 1))
        delta_count = max(0, _to_int(state.get("delta_count"), 0))

        # Session state is persistent, but a hot reload or adapter recreation
        # can hand the rail a fresh Session object while the attachment
        # manager still holds the latest materialized directory. Recover that
        # directory as the diff baseline; otherwise a removal/addition would
        # be re-emitted (or missed) with a new catalog version every turn.
        existing = await self._existing_catalog_attachment(manager, session_id)
        metadata = getattr(existing, "metadata", None) if existing is not None else None
        if isinstance(metadata, dict):
            attachment_tools = _normalize_catalog(
                metadata.get(_CATALOG_METADATA_TOOLS)
            )
            if attachment_tools is not None:
                previous_tools = attachment_tools
                initialized = True
                attachment_fingerprints = _normalize_catalog(
                    metadata.get(_CATALOG_METADATA_FINGERPRINTS)
                )
                if attachment_fingerprints is not None:
                    previous_fingerprints = attachment_fingerprints
                attachment_language = str(metadata.get("catalog_language") or "")
                if attachment_language:
                    previous_language = attachment_language
                attachment_version = max(
                    0, _to_int(metadata.get("catalog_version"), 0)
                )
                if attachment_version:
                    version = max(version, attachment_version)
                attachment_delta_count = max(
                    0, _to_int(metadata.get("delta_count"), 0)
                )
                if attachment_delta_count:
                    delta_count = attachment_delta_count

        baseline_tools = previous_tools if initialized else initial_tools
        baseline_fingerprints = (
            previous_fingerprints if initialized else initial_fingerprints
        )
        added = {
            name: description
            for name, description in current_tools.items()
            if name not in baseline_tools
        }
        updated: Dict[str, str] = {}
        for name, description in current_tools.items():
            if name not in baseline_tools:
                continue
            if baseline_tools[name] != description:
                updated[name] = description
                continue
            # Fingerprints are compared only when both sides carry one: the
            # frozen baseline has descriptions but no fingerprints, and a
            # failed fingerprint on either side must not read as a change.
            baseline_fp = baseline_fingerprints.get(name)
            current_fp = current_fingerprints.get(name)
            if (
                baseline_fp is not None
                and current_fp is not None
                and baseline_fp != current_fp
            ):
                updated[name] = description
        removed = sorted(name for name in baseline_tools if name not in current_tools)
        changed = bool(added or updated or removed)
        next_version = version + 1 if changed else version

        if not initialized:
            # The frozen prompt section is the baseline. If registration
            # changed between startup and the first model call, report that
            # difference as the first attachment; otherwise emit nothing.
            if not changed:
                self._write_catalog_state(
                    session,
                    state,
                    version=version,
                    delta_count=0,
                    language=language,
                    initial_tools=initial_tools,
                    initial_fingerprints=initial_fingerprints,
                    tools=current_tools,
                    fingerprints=current_fingerprints,
                )
                return
            version = next_version
            next_delta_count = 1
            mode = "delta"
            content = render_deferred_tool_catalog_delta(
                added,
                updated,
                removed,
                language=language,
                version=version,
            )
        else:
            force_snapshot = (
                not self._has_attachment_history(manager, ctx, session_id)
                or previous_language != language
                or delta_count >= _CATALOG_SNAPSHOT_INTERVAL
            )
            if not force_snapshot and not changed:
                return
            version = next_version
            if force_snapshot:
                content = render_deferred_tool_catalog_snapshot(
                    current_tools,
                    language=language,
                    version=version,
                )
                mode = "snapshot"
                next_delta_count = 0
            else:
                content = render_deferred_tool_catalog_delta(
                    added,
                    updated,
                    removed,
                    language=language,
                    version=version,
                )
                mode = "delta"
                next_delta_count = delta_count + 1

        try:
            await writer.add_section(
                section=_CATALOG_ATTACHMENT_SECTION,
                content=content,
                kind=PromptAttachmentKind.TOOL,
                source=_CATALOG_ATTACHMENT_SOURCE,
                priority=75,
                metadata={
                    "catalog_version": version,
                    "catalog_mode": mode,
                    "catalog_language": str(language),
                    "delta_count": next_delta_count,
                    _CATALOG_METADATA_TOOLS: dict(current_tools),
                    _CATALOG_METADATA_FINGERPRINTS: dict(current_fingerprints),
                },
                content_kind="text/markdown",
            )
        except Exception:
            logger.warning(
                "[ToolRetrievalPromptRail] deferred-catalogue attachment failed",
                exc_info=True,
            )
            return

        self._write_catalog_state(
            session,
            state,
            version=version,
            delta_count=next_delta_count,
            language=language,
            initial_tools=initial_tools,
            initial_fingerprints=initial_fingerprints,
            tools=current_tools,
            fingerprints=current_fingerprints,
        )

    def _frozen_catalog_descriptions(self) -> Dict[str, str]:
        """Return the frozen directory as ``name → compacted description``.

        The sync's initial baseline must be the catalogue the model actually
        saw in the prompt section, which was frozen from the toolkit's live
        records — not whatever the registry happens to hold at first sync.
        """

        snapshot = self._frozen_snapshot
        return {
            entry.name: _compact_description(str(entry.description or ""))
            for entry in (snapshot.flat_entries if snapshot else ())
        }

    def _collect_live_catalog(self) -> tuple[Dict[str, str], Dict[str, str]]:
        """Read the live deferred directory and its schema fingerprints.

        Descriptions are compacted to the prompt budget (the attachment lists
        the same short form the catalogue section shows); fingerprints come
        from ``BM25ToolIndex.tool_fingerprint`` so in-place schema edits that
        do not touch the compacted description still count as an update.
        """

        toolkit = self._toolkit
        if toolkit is None:
            return {}, {}
        try:
            infos = toolkit.deferred_tool_infos()
        except Exception:
            logger.warning(
                "[ToolRetrievalPromptRail] live catalogue refresh failed",
                exc_info=True,
            )
            return {}, {}
        descriptions = {
            str(name): _compact_description(str(getattr(info, "description", "") or ""))
            for name, info in infos.items()
            if str(name)
        }
        fingerprints: Dict[str, str] = {}
        for name, info in infos.items():
            try:
                fingerprints[str(name)] = BM25ToolIndex.tool_fingerprint(info)
            except Exception:
                logger.debug(
                    "[ToolRetrievalPromptRail] fingerprint failed for '%s'",
                    name,
                    exc_info=True,
                )
        return (
            dict(sorted(descriptions.items())),
            dict(sorted(fingerprints.items())),
        )

    @staticmethod
    def _write_catalog_state(
        session: Any,
        state: Dict[str, Any],
        *,
        version: int,
        delta_count: int,
        language: str,
        initial_tools: Dict[str, str],
        initial_fingerprints: Dict[str, str],
        tools: Dict[str, str],
        fingerprints: Dict[str, str],
    ) -> None:
        session.update_state(
            {
                _CATALOG_STATE_KEY: {
                    **state,
                    "initialized": True,
                    "version": version,
                    "delta_count": delta_count,
                    "language": str(language),
                    _STATE_INITIAL_TOOLS: dict(initial_tools),
                    _STATE_INITIAL_FINGERPRINTS: dict(initial_fingerprints),
                    _STATE_TOOLS: dict(tools),
                    _STATE_FINGERPRINTS: dict(fingerprints),
                }
            }
        )

    @staticmethod
    async def _existing_catalog_attachment(
        manager: Any,
        session_id: str,
    ) -> Any:
        """Read the currently materialized deferred-catalogue attachment."""

        list_by_filter = getattr(manager, "list_by_filter", None)
        if not callable(list_by_filter):
            return None
        try:
            result = list_by_filter(
                session_id=session_id,
                section=_CATALOG_ATTACHMENT_SECTION,
                source=_CATALOG_ATTACHMENT_SOURCE,
            )
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, (list, tuple)) and result:
                return result[0]
        except Exception:
            logger.debug(
                "[ToolRetrievalPromptRail] deferred-catalogue attachment "
                "recovery failed",
                exc_info=True,
            )
        return None

    @staticmethod
    def _has_attachment_history(
        manager: Any,
        ctx: AgentCallbackContext,
        session_id: str,
    ) -> bool:
        """Return whether the context still holds the attachment snapshot.

        After context compaction or recreation the historical snapshot is
        gone; the next update must then be a full snapshot so the model does
        not receive an orphaned delta.
        """

        has_history = getattr(manager, "has_history_snapshot", None)
        context = getattr(ctx, "context", None)
        if not callable(has_history) or context is None:
            return False
        try:
            return bool(has_history(context, session_id))
        except Exception:
            return False


def _compact_text(value: str) -> str:
    return " ".join(str(value or "").replace("`", "\\`").split())


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_catalog(value: Any) -> Optional[Dict[str, str]]:
    """Normalize a serialized deferred-tool directory.

    ``None`` means no recoverable catalogue was supplied. An empty dict is a
    valid catalogue (an empty deferred registry) and must not be confused
    with missing state.
    """

    if value is None or not isinstance(value, dict):
        return None
    return dict(
        sorted(
            {
                str(name): str(description or "")
                for name, description in value.items()
                if str(name).strip()
            }.items()
        )
    )


def _compact_description(description: str) -> str:
    """Compact a tool description to the prompt budget.

    Mirrors openjiuwen's ``_compact_tool_description``: keep the first
    sentence or cap to ``_MAX_ENTRY_DESC_CHARS`` so long tool docs (powershell,
    cron) don't dominate the prompt.
    """

    compact = _compact_text(description)
    if len(compact) <= _MAX_ENTRY_DESC_CHARS:
        return compact
    sentence_endings = ("。", ".", "！", "!", "？", "?")
    first_ending = min(
        (
            index
            for index, char in enumerate(compact)
            if char in sentence_endings and index > 0
        ),
        default=-1,
    )
    if 0 < first_ending < _MAX_ENTRY_DESC_CHARS:
        return compact[: first_ending + 1]
    return compact[: _MAX_ENTRY_DESC_CHARS - 1].rstrip() + "…"


def _render_categories(categories: tuple[ToolCategory, ...]) -> list[str]:
    """Render description-only category lines, type-marked ``[category]``.

    Lines carry only the description — tool names are discovered through
    tool_search, not listed here. The ``[category]``/``[tool]`` marks mirror
    tool_search's list output so rule 3 can navigate by mark: category ->
    list, tool -> get_schema.
    """

    return [
        f"- [category] `{_compact_text(category.path)}`: "
        f"{_compact_text(category.description)}"
        for category in categories
    ]


def _render_entries(entries: tuple[tuple[str, str], ...]) -> list[str]:
    return [
        f"- [tool] `{_compact_text(name)}`: {_compact_description(description)}"
        for name, description in entries
    ]
