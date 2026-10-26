"""Flash-only model-written queries, candidate selection and native loading."""

from __future__ import annotations

import json
import asyncio
import logging
import hashlib
import time
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from .catalog import CatalogChanged, directory_id
from .config import SelectionSettings
from .execution import SelectionBusy, SelectionTimedOut
from .diagnostics import emit, selection_trace
from .query import retrieval_text
from .explicit import explicit_request, explicit_task_text, resolve_names
from .tool import SkillSearchInput, SkillSearchTool, SYSTEM_GUIDANCE
from .presentation import candidate_view
from .prompt import without_catalog
from .request_state import RequestState, SearchCacheKey

logger = logging.getLogger(__name__)


class SkillSelectionRail(DeepAgentRail):
    # Execute after SkillUseRail (95), tree retrieval (94), progressive tools (80),
    # and runtime prompt (5), so none can reintroduce a full catalog afterwards.
    priority = 4
    SECTION = "flash.selected_skill"
    STATE = "flash.skill_selection"
    EXPLICIT = STATE + ".explicit"
    REQUEST = STATE + ".request"
    FALLBACK = STATE + ".fallback"
    REUSE = STATE + ".reuse"
    TOOL_NAMES = (SkillSearchTool.TOOL_NAME,)
    HIDDEN_SECTIONS = ("skills",)
    HIDDEN_TOOLS = frozenset(
        {
            "list_skill",
            "list_skills",
        }
    )

    def __init__(
        self,
        *,
        config_provider: Callable[[], dict],
        skill_rail_provider: Callable[[], Any],
    ) -> None:
        super().__init__()
        self._config_provider = config_provider
        self._skill_rail_provider = skill_rail_provider
        self._agent = None
        self._service = None
        self._configuration = None
        self._last_error: str | None = None
        self._logged_enabled = None
        self._tool = None
        self._native_generation = None
        self._native_refresh_lock = asyncio.Lock()
        from .metrics import MetricsBridge
        self._metrics = MetricsBridge()
        from .loading import SkillSelectionLoadRail
        self.load_rail = SkillSelectionLoadRail(self)

    @property
    def config(self):
        """Read live configuration for cooperating selection components."""
        return self._config_provider()

    @property
    def native(self):
        """Return the current native authority for Skill permissions and loading."""
        return self._skill_rail_provider()

    @property
    def service(self):
        """Expose the current retrieval service without allowing replacement."""
        return self._service

    def init(self, agent: Any) -> None:
        self._agent = agent
        try:
            self.refresh()
        except Exception:
            logger.warning(
                "[SkillSelection] preparation unavailable; default skill handling retained",
                exc_info=True,
            )

    def uninit(self, agent: Any) -> None:
        self._sync_tool(False)
        builder = getattr(agent, "system_prompt_builder", None)
        if builder is not None:
            builder.remove_section(self.SECTION)
        self._agent = None
        self._service = None
        self._configuration = None
        self._native_generation = None

    def refresh(self, *, force: bool = False) -> SelectionSettings:
        settings = SelectionSettings.from_config(self._config_provider())
        disabled = (self._config_provider().get('react') or {}).get('disabled_tools', [])
        if isinstance(disabled, str):
            disabled = [s.strip() for s in disabled.split(',')]
        if SkillSearchTool.TOOL_NAME in (disabled or []):
            settings = SelectionSettings()
        if self._logged_enabled != settings.enabled:
            logger.info(
                "[SkillSelection] mode=%s scheme=bm25-main-llm",
                "enabled" if settings.enabled else "disabled; Flash default",
            )
            self._logged_enabled = settings.enabled
        if not settings.enabled:
            self._sync_tool(False)
            self._service = None
            self._configuration = None
            self._native_generation = None
            return settings
        native = self._skill_rail_provider()
        roots = (
            tuple(
                Path(root).resolve()
                for root in native.normalize_skill_dirs(native.skills_dir)
            )
            if native
            else ()
        )
        if not roots:
            raise RuntimeError("Flash SkillUseRail is unavailable")
        key = (tuple(map(str, roots)), settings.identity())
        if key != self._configuration or getattr(self._service, "retired", False):
            from .service import get_service

            self._service = get_service(roots, settings)
            self._configuration = key
        if force:
            self._service.refresh(force=True)
        self._sync_tool(True)
        return settings

    @staticmethod
    def _latest_content(ctx: Any):
        for message in reversed(getattr(ctx.inputs, "messages", None) or []):
            role = (
                message.get("role")
                if isinstance(message, dict)
                else getattr(message, "role", None)
            )
            if role not in ("user", "human"):
                continue
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", "")
            )
            if isinstance(content, (str, list)):
                return content
        return ""

    def _sync_tool(self, enabled):
        if self._agent is None:
            return
        manager = self._agent.ability_manager
        if enabled and self._tool is None:
            self._tool = SkillSearchTool(self.handle_action)
            manager.add_ability(self._tool.card, self._tool)
        elif not enabled and self._tool is not None:
            manager.remove_ability(SkillSearchTool.TOOL_NAME)
            self._tool = None

    async def _expose_tools(self, ctx, *, selection_stage=None):
        if self.STATE + '.tools' not in ctx.extra:
            ctx.extra[self.STATE + '.tools'] = ctx.inputs.tools
        if selection_stage is not None:
            original = ctx.inputs.tools or []
            # Only narrow the tool set after the model requested Skill search
            # and received candidates. Before that, ordinary tools stay usable.
            # Direct Skill loading remains available for explicit choices and
            # after a successful load, through the unrestricted branch below.
            ordinary = ([] if selection_stage == 'select' else
                        [t for t in original if self._tool_name(t) not in (*self.TOOL_NAMES, 'skill_tool')])
            ctx.inputs.tools = [*ordinary, self._tool.model_info()]
            schema_chars = sum(
                len(t.model_dump_json()) if callable(getattr(t, 'model_dump_json', None)) else
                len(json.dumps(t if isinstance(t, dict) else vars(t), ensure_ascii=False, default=str))
                for t in ctx.inputs.tools)
            emit(logger, 'model_view', {}, stage=selection_stage,
                 tools_before=len(original), tools_sent=len(ctx.inputs.tools),
                 tool_schema_chars=schema_chars)
            return
        tools = [t for t in (ctx.inputs.tools or []) if self._tool_name(t) not in self.TOOL_NAMES]
        tools.extend(await self._agent.ability_manager.list_tool_info(names=list(self.TOOL_NAMES)))
        ctx.inputs.tools = tools

    async def before_model_call(self, ctx: Any) -> None:
        metrics = self._metrics.attach(ctx)
        metrics.query_sha256 = hashlib.sha256(retrieval_text(self._latest_content(ctx)).encode()).hexdigest()
        if metrics.selection_started is None:
            metrics.selection_started = time.perf_counter()
        builder = getattr(self._agent, "system_prompt_builder", None)
        if builder is None:
            return
        builder.remove_section(self.SECTION)
        try:
            settings = self.refresh()
            metrics.mode = 'tool' if settings.enabled else 'default'
            if not settings.enabled:
                ctx.extra[self.STATE + '.model_stage'] = 'default'
                self._restore(ctx)
                ctx.extra.pop(self.EXPLICIT, None)
                ctx.extra.pop(self.REQUEST, None)
                ctx.extra.pop(self.FALLBACK, None)
                ctx.extra.pop(self.REUSE, None)
                ctx.extra[self.STATE + ".status"] = "disabled"
                # A hot disable may occur after the framework built tools[].
                if ctx.inputs.tools is not None:
                    ctx.inputs.tools = [t for t in ctx.inputs.tools
                                        if self._tool_name(t) not in self.TOOL_NAMES]
                return
            native = self._skill_rail_provider()
            content = self._latest_content(ctx)
            raw_query = retrieval_text(content)
            previous = ctx.extra.get(self.REQUEST)
            if previous is None or previous != raw_query:
                ctx.extra.pop(self.FALLBACK, None)
                ctx.extra[self.REUSE] = RequestState()
            ctx.extra.setdefault(self.REUSE, RequestState())
            # Used for request lifecycle and explicit Skill choices, not query validation.
            ctx.extra[self.REQUEST] = raw_query
            if ctx.extra.get(self.FALLBACK):
                ctx.extra[self.STATE + '.model_stage'] = 'default_fallback'
                self._sync_tool(False)
                if ctx.inputs.tools is not None:
                    ctx.inputs.tools = [t for t in ctx.inputs.tools if self._tool_name(t) not in self.TOOL_NAMES]
                return
            if await self._handle_explicit(ctx, native, settings):
                ctx.extra[self.STATE + '.model_stage'] = 'explicit'
                return
            state = ctx.extra[self.REUSE]
            stage = ('select' if state.tickets else 'route') if not state.loaded else None
            ctx.extra[self.STATE + '.model_stage'] = stage or 'execute'
            await self._inject(ctx, SYSTEM_GUIDANCE, automatic=True)
            await self._expose_tools(ctx, selection_stage=stage)
            self._last_error = None
            ctx.extra[self.STATE + '.status'] = 'awaiting_model'
        except asyncio.CancelledError:
            self._restore(ctx)
            raise
        except Exception as exc:
            self._restore(ctx)
            self._sync_tool(False)
            ctx.extra[self.FALLBACK] = True
            if str(exc) != self._last_error:
                self._last_error = str(exc)
                logger.warning('[SkillSelection] tool setup unavailable; using Flash default', exc_info=True)
        finally:
            metrics.fallback = bool(ctx.extra.get(self.FALLBACK))
            metrics.model_started = time.perf_counter()

    @staticmethod
    def response(status, message, **fields):
        return {'status': status, 'loaded': False, 'selected': None, 'message': message, **fields}

    @staticmethod
    def _allowed(native, pipeline):
        # The native catalog remains the authority; also reject duplicate names
        # because SkillTool loads by name rather than an arbitrary filesystem path.
        from collections import Counter
        counts = Counter(s.name.casefold() for s in native.skills)
        enabled = set(getattr(native, 'enabled_skills', ()) or ())
        disabled = set(getattr(native, 'disabled_skills', ()) or ())
        documents = pipeline.documents if pipeline else {}
        allowed = set()
        for skill in native.skills:
            directory = Path(skill.directory)
            if counts[skill.name.casefold()] != 1 or skill.name in disabled or directory.name in disabled:
                continue
            if enabled and directory.name not in enabled:
                continue
            # Index construction already resolved and checked containment. Avoid
            # opening hundreds of paths twice per search. Only the chosen file
            # needs a live containment/fingerprint check in the load rail.
            identity = os.path.normcase(os.path.abspath(directory))
            document = documents.get(identity)
            if document is not None and document.name == skill.name:
                allowed.add(identity)
        return frozenset(allowed)

    async def handle_action(self, request: SkillSearchInput, ctx):
        # Arguments have already been validated for exactly one action.
        if request.action == 'load':
            return getattr(ctx, 'flash_selection_error', self.response(
                'unavailable', '加载必须经过 Flash 原生工具调用流程。'))
        if request.action == 'fallback':
            if (ctx.extra.get(self.EXPLICIT) or {}).get('error'):
                return self.response('explicit_selection_blocked', '请先说明用户指定技能不可用的原因。')
            self.use_default(ctx, 'candidates_unsuitable')
            return self.response('fallback', '本请求已切换为原生技能流程。'
                                 '下一轮使用原生技能目录和 skill_tool；无需重新检索。')
        return await self._search_from_model(request.query, request.keywords, ctx)

    def use_default(self, ctx, reason):
        ctx.extra[self.FALLBACK] = True
        state = ctx.extra.get(self.REUSE)
        if state is not None:
            state.tickets.clear()
        metrics = self._metrics.attach(ctx)
        metrics.fallback = True
        from .metrics import trace
        emit(logger, 'fallback', trace(ctx, metrics), reason=reason)

    async def _search_from_model(self, query, keywords, ctx):
        state = ctx.extra.setdefault(self.REUSE, RequestState())
        try:
            settings = SelectionSettings.from_config(self._config_provider())
            async with asyncio.timeout(settings.query_timeout_s):
                async with state.lock:
                    for attempt in range(3):
                        try:
                            return await self._search_once(query, keywords, ctx, state)
                        except CatalogChanged:
                            # Never pair old ranking text with newly edited
                            # candidate details. Refresh and recompute permissions.
                            state.results.clear()
                            state.tickets.clear()
                            if attempt == 2:
                                raise
                            self._service.refresh(force=True)
        except (TimeoutError, SelectionTimedOut):
            self.use_default(ctx, 'timeout')
            return self.response('timeout', '检索超时，尚未加载技能；下一轮恢复原生目录与工具。')
        except SelectionBusy:
            self.use_default(ctx, 'busy')
            return self.response('busy', '检索暂忙，尚未加载技能；下一轮恢复原生目录与工具。')
        except Exception:
            self.use_default(ctx, 'failed')
            logger.warning('[SkillSelection] search failed', exc_info=True)
            return self.response('failed', '检索不可用，尚未加载技能；下一轮恢复原生目录与工具。')

    async def _search_once(self, query, keywords, ctx, state):
        started = time.perf_counter()
        settings = self.refresh()
        if not settings.enabled:
            return self.response('disabled', '检索开关已关闭，请使用默认方式。')
        if self.REQUEST not in ctx.extra:
            return self.response('missing_request_context', '缺少当前请求上下文。')
        if (ctx.extra.get(self.EXPLICIT) or {}).get('error'):
            return self.response('explicit_selection_blocked', '用户指定项未能加载，请说明原因，不要换选。')
        service, native = self._service, self._skill_rail_provider()
        # Use a verified snapshot and coalesce background refreshes. Explicit
        # force-refresh and known errors still block/fail; selected files and
        # live native permissions are independently checked before loading.
        check_started = time.perf_counter()
        snapshot = await service.current_snapshot(allow_stale=True)
        generation = snapshot[1]
        async with self._native_refresh_lock:
            native_key = (service, native, generation,
                          tuple(sorted(getattr(native, 'enabled_skills', ()) or ())),
                          tuple(sorted(getattr(native, 'disabled_skills', ()) or ())))
            if self._native_generation is None:
                # The SDK just populated the native catalog before the model
                # hook. Do not repeat a multi-second full scan for query #1.
                self._native_generation = native_key
            elif native_key != self._native_generation:
                # Installers may replace files while preserving their mtime.
                # The framework's metadata cache only compares mtime, so bypass
                # it for this local refresh; never clear other agents' caches.
                # Only a published index/native change needs synchronization;
                # permission-set changes are filtered live without another scan.
                if native_key[:3] != self._native_generation[:3]:
                    cache_enabled = native.enable_cache
                    try:
                        native.enable_cache = False
                        await native.reload_skills()
                    finally:
                        native.enable_cache = cache_enabled
                self._native_generation = native_key
        catalog_check_ms = (time.perf_counter() - check_started) * 1000
        allowed = self._allowed(native, snapshot[0])
        trace = selection_trace(query, ctx, self._agent, settings)
        state.searches += 1
        trace.update(search_number=state.searches, query_source='model_tool_argument', keywords_count=len(keywords))
        permission_key = hashlib.sha256('\0'.join(sorted(allowed)).encode()).hexdigest()

        def cache_key(generation):
            return SearchCacheKey(settings=settings.identity(), service_id=id(service), generation=generation,
                                  permission_key=permission_key, query=query, keywords=tuple(keywords))

        result = state.results.get(cache_key(generation))
        cache_hit = result is not None
        result = await service.search(query, keywords, allowed_ids=allowed, snapshot=snapshot,
                                      cached_result=result)
        RequestState.remember(state.results, cache_key(result.generation), result)
        # A disable or catalog/permission switch during background work invalidates this response.
        current = SelectionSettings.from_config(self._config_provider())
        context_changed = (current.identity() != settings.identity() or service is not self._service
                           or native is not self._skill_rail_provider())
        if context_changed or service.retired or allowed != self._allowed(native, snapshot[0]):
            return self.response('changed', '配置或权限已变化，请重新检索。')
        if not result.candidates:
            emit(logger, 'search', trace, status='no_match', loaded=False,
                 timings_ms={} if cache_hit else result.elapsed_ms,
                 diagnostics={**result.diagnostics, 'catalog_check_ms': catalog_check_ms},
                 cache_hit=cache_hit, total_ms=(time.perf_counter() - started) * 1000)
            self.use_default(ctx, 'no_candidates')
            return self.response('no_match', '没有可用候选，本请求恢复原生技能流程，尚未加载技能。')
        search_id = uuid4().hex
        RequestState.remember(state.tickets, search_id, {
            'result': result, 'settings': settings.identity(), 'service': service,
            'native': native, 'query': query, 'trace': trace,
        })
        candidates = [candidate_view(d) for d, _ in result.candidates]
        emit(logger, 'search', trace, status='candidates', search_id=search_id, loaded=False,
             top=[{'name': d.name, 'score': score} for d, score in result.candidates],
             timings_ms={} if cache_hit else result.elapsed_ms,
             diagnostics={**result.diagnostics, 'catalog_check_ms': catalog_check_ms},
             cache_hit=cache_hit, total_ms=(time.perf_counter() - started) * 1000,
             candidate_chars=len(json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))))
        return self.response('candidates',
            '候选资料未经执行验证。对照原始需求选择后 load；资料不足或候选不合适则 fallback，勿重复检索。',
            search_id=search_id, candidates=candidates)

    async def _handle_explicit(self, ctx, native, settings):
        # Explicit selections bypass ranking; native loads retain the normal
        # skill_tool checks (permissions, credentials, active state).
        content = self._latest_content(ctx)
        query = explicit_task_text(retrieval_text(content))
        request = explicit_request(content, query, (s.name for s in native.skills) if native else ())
        if request is None:
            ctx.extra.pop(self.EXPLICIT, None)
            return False
        selected, error = resolve_names(request, native)
        previous = ctx.extra.get(self.EXPLICIT) or {}
        additional = previous.get('additional_ids', set()) if previous.get('request') == request else set()
        ctx.extra[self.EXPLICIT] = {
            'request': request, 'error': error,
            'ids': frozenset(s.id for s in selected), 'additional_ids': additional,
        }
        names = json.dumps([s.name for s in selected] if not error else request.names, ensure_ascii=False)
        if error:
            prompt = ('用户指定的技能不存在、不可用或名称不唯一，尚未加载。'
                      '请说明原因，不要自动换用其他技能。指定名称：' + names)
        elif any(skill.name.casefold() == 'pptx-craft' for skill in selected):
            prompt = ('用户明确指定以下技能，无需先检索。其中 pptx-craft 按下述 PPT 加速例外选择执行方式，'
                      '符合加速条件时优先调用 skill_acceleration_exec，无需先通过 skill_tool 加载；'
                      '其他指定技能仍通过原生 skill_tool 加载指令后执行对应任务。'
                      '本提示不代表指令已加载。指定名称：' + names + '\n\n' + SYSTEM_GUIDANCE)
        else:
            prompt = ('用户明确指定以下技能，无需先检索。请通过原生 skill_tool 加载指令后执行对应任务；'
                      '本提示不代表指令已加载。指定名称：' + names + '\n\n' + SYSTEM_GUIDANCE)
        await self._inject(ctx, prompt, blocked=bool(error), automatic=not error)
        if not error:
            await self._expose_tools(ctx)
        return True

    async def _inject(self, ctx, content, *, blocked=False, automatic=False):
        builder = self._agent.system_prompt_builder
        hidden = [(name, builder.get_section(name)) for name in self.HIDDEN_SECTIONS]
        ctx.extra[self.STATE + '.hidden'] = (builder, hidden)
        for name, section in hidden:
            if section is not None:
                builder.add_section(without_catalog(section))
        language = getattr(builder, 'language', 'cn') or 'cn'
        builder.add_section(PromptSection(name=self.SECTION, priority=41, content={language: content}))
        tools = getattr(ctx.inputs, 'tools', None)
        if tools:
            ctx.extra[self.STATE + '.tools'] = tools
            hidden_tools = self.HIDDEN_TOOLS | ({'skill_tool'} if blocked else set())
            if not automatic:
                hidden_tools = hidden_tools | set(self.TOOL_NAMES)
            ctx.inputs.tools = [tool for tool in tools if self._tool_name(tool) not in hidden_tools]
        attachments = getattr(self._agent, 'prompt_attachment_manager', None)
        if attachments is not None:
            await attachments.bind_context(ctx).clear_section('skills.runtime_changes')

    async def before_tool_call(self, ctx):
        """Keep named selection in force through subsequent skill_tool calls."""
        name = getattr(ctx.inputs, 'tool_name', '')
        if name in self.TOOL_NAMES:
            if self._tool is not None:
                self._tool.bind(ctx)
            return
        state = ctx.extra.get(self.EXPLICIT)
        if state is None or getattr(ctx.inputs, 'tool_name', '') != 'skill_tool':
            return
        if not SelectionSettings.from_config(self._config_provider()).enabled:
            ctx.extra.pop(self.EXPLICIT, None)
            return
        args = getattr(ctx.inputs, 'tool_args', None)
        if args is None:
            args = getattr(getattr(ctx.inputs, 'tool_call', None), 'arguments', {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        chosen, error = resolve_names(state['request'], self._skill_rail_provider())
        permitted = {s.name for s in chosen if s.id in state['ids']} if not error and not state['error'] else set()
        if not error and not state['error']:
            native = self._skill_rail_provider()
            permitted.update(s.name for s in native.skills
                             if directory_id(s.directory) in state.get('additional_ids', ()))
        loading = getattr(ctx, 'flash_selection_load', {}).get('selected')
        loading = loading.id if loading else None
        if loading and not state['error']:
            native = self._skill_rail_provider()
            permitted.update(s.name for s in native.skills if directory_id(s.directory) == loading)
        if isinstance(args, dict) and args.get('skill_name') in permitted:
            return  # The normal SkillTool still validates nested paths and permissions.
        from openjiuwen.core.foundation.llm.schema.message import ToolMessage
        message = '指定 Skill 校验未通过，或尝试加载未被用户指定的其他 Skill；请确认指定项，不要自动换选。'
        logger.info('[SkillSelection] explicit_tool_blocked requested=%r',
                    args.get('skill_name') if isinstance(args, dict) else None)
        call = getattr(ctx.inputs, 'tool_call', None)
        ctx.extra['_skip_tool'] = True
        ctx.inputs.tool_result = message
        ctx.inputs.tool_msg = ToolMessage(content=message, tool_call_id=getattr(call, 'id', ''))

    async def after_tool_call(self, ctx):
        SkillSearchTool.unbind(ctx)
        await self.load_rail.finish(ctx)
        state = ctx.extra.get(self.REUSE)
        if (state is not None and ctx.inputs.tool_name == 'skill_tool'
                and getattr(getattr(ctx.inputs, 'tool_result', None), 'success', False)):
            state.loaded = True
        from .metrics import record_load
        record_load(ctx)

    async def on_tool_exception(self, ctx):
        SkillSearchTool.unbind(ctx)

    @staticmethod
    def _tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            return str((tool.get("function") or tool).get("name", ""))
        return str(getattr(tool, "name", ""))

    def _restore(self, ctx: Any) -> None:
        if self.STATE + ".tools" in ctx.extra:
            ctx.inputs.tools = ctx.extra.pop(self.STATE + ".tools")
        previous = ctx.extra.pop(self.STATE + ".hidden", None)
        if previous:
            builder, sections = previous
            builder.remove_section(self.SECTION)
            for name, section in sections:
                if section is not None:
                    builder.add_section(section)
                else:
                    builder.remove_section(name)

    async def after_model_call(self, ctx: Any) -> None:
        from .metrics import record_model
        record_model(ctx)
        self._restore(ctx)

    async def on_model_exception(self, ctx: Any) -> None:
        from .metrics import record_model
        record_model(ctx, failed=True)
        self._restore(ctx)

    async def after_task_iteration(self, ctx: Any) -> None:
        SkillSearchTool.clear_request(ctx)
        self._restore(ctx)

    async def before_invoke(self, ctx: Any) -> None:
        self._metrics.begin(ctx)

    async def after_invoke(self, ctx: Any) -> None:
        interrupted = (getattr(ctx, 'exception', None) is not None
                       or (hasattr(ctx.inputs, 'result') and ctx.inputs.result is None))
        contexts = self._metrics.finish(ctx, 'interrupted_or_failed' if interrupted else 'invoke_returned')
        for related in contexts:
            await self._cleanup_request(related)

    async def _cleanup_request(self, ctx):
        SkillSearchTool.clear_request(ctx)
        self._restore(ctx)
        for key in (self.EXPLICIT, self.REQUEST, self.FALLBACK, self.REUSE, self.STATE + '.model_stage'):
            ctx.extra.pop(key, None)

    async def on_invoke_exception(self, ctx: Any) -> None:
        for related in self._metrics.finish(ctx, 'interrupted_or_failed'):
            await self._cleanup_request(related)
