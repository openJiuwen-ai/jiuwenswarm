"""Translate a validated load action before Flash's existing tool lifecycle.

No nested tool execution: native permission interruptions must reach the agent
loop directly instead of being caught as errors inside another tool's invoke.
"""
import json
import logging
import time

from pydantic import ValidationError
from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.tool_display import extract_call_goal

from .catalog import directory_id
from .config import SelectionSettings
from .diagnostics import emit
from .explicit import ExplicitRequest, resolve_names
from .sources import read_sources, source_fingerprint
from .tool import SearchOutput, SkillSearchInput, SkillSearchTool

logger = logging.getLogger(__name__)


class SkillSelectionLoadRail(DeepAgentRail):
    # Normalize aliases before native authorization (95), credentials, active
    # state, and tool-security callbacks inspect the tool name and arguments.
    priority = 10000

    def __init__(self, selection):
        super().__init__()
        self.selection = selection

    async def before_tool_call(self, ctx):
        if getattr(ctx.inputs, 'tool_name', '') != SkillSearchTool.TOOL_NAME:
            return
        try:
            raw = ctx.inputs.tool_call.arguments
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, RecursionError):
            # Tool dispatch owns malformed arguments; they are not load failures.
            return
        if not isinstance(payload, dict) or payload.get('action') != 'load':
            return
        try:
            args = SkillSearchInput.model_validate(payload)
        except ValidationError:
            # Leave the call untouched so SkillSearchTool can report a retryable
            # input error without discarding candidates or restoring the catalog.
            return
        try:
            state = ctx.extra.get(self.selection.REUSE)
            ticket = state.tickets.get(args.search_id) if state is not None else None
            if ticket is None:
                self.reject(ctx, 'invalid_search', '检索编号不属于当前请求或已过期，请重新检索。')
                return
            candidates = [d for d, _ in ticket['result'].candidates if d.name == args.skill_name]
            if len(candidates) != 1:
                self.reject(ctx, 'invalid_choice', '只能加载本次候选中的准确名称。')
                return
            selected = candidates[0]
            if not self.valid(ticket, selected, ctx.session):
                ticket['service'].refresh(force=True)
                self.reject(ctx, 'changed', '配置、权限或候选文件已变化，请重新检索。')
                return
            if (ctx.extra.get(self.selection.EXPLICIT) or {}).get('error'):
                self.reject(ctx, 'explicit_selection_blocked', '用户指定项不可用，请先说明原因。')
                return
            # Keep call-specific state on the callback context. ctx.extra may
            # be shared by parallel tool calls in the same request.
            ctx.flash_selection_load = dict(ticket=ticket, selected=selected,
                                            search_id=args.search_id, started=time.perf_counter())
            native_input = {'skill_name': selected.name, 'relative_file_path': 'SKILL.md'}
            display, _ = extract_call_goal(payload)
            if display:
                native_input['call_goal'] = display
            native_args = json.dumps(native_input)
            ctx.inputs.tool_call.name = 'skill_tool'
            ctx.inputs.tool_call.arguments = native_args
            ctx.inputs.tool_name = 'skill_tool'
            ctx.inputs.tool_args = native_args
        except Exception:
            logger.warning('[FlashSkillSelection] candidate validation failed', exc_info=True)
            self.reject(ctx, 'load_failed', '候选校验失败，尚未加载技能。')

    def reject(self, ctx, status, message):
        # Leave the original tool call in place. Its ordinary callback returns
        # this error; no shared _skip_tool flag can affect a parallel tool call.
        ctx.flash_selection_error = self.selection.response(status, message)
        if status in {'changed', 'load_failed', 'invalid_search'}:
            self.selection.use_default(ctx, status)

    def valid(self, ticket, selected, session):
        rail = self.selection
        config = rail.config
        settings = SelectionSettings.from_config(config)
        disabled = (config.get('react') or {}).get('disabled_tools', [])
        if isinstance(disabled, str):
            disabled = [s.strip() for s in disabled.split(',')]
        native = rail.native
        permitted, error = resolve_names(ExplicitRequest('candidate', (selected.name,)), native)
        if not (settings.enabled and settings.identity() == ticket['settings']
                and SkillSearchTool.TOOL_NAME not in (disabled or [])
                and native is ticket['native'] and rail.service is ticket['service']
                and not ticket['service'].retired and not error and len(permitted) == 1
                and permitted[0].id == selected.id
                and source_fingerprint('', read_sources(selected.directory)) == selected.fingerprint):
            return False
        # SkillTool resolves names against this session view, which can retain
        # a baseline directory even after the live catalog moves the same name.
        skills = native.get_skills_for_session(session)
        matches = [skill for skill in skills if skill.name == selected.name]
        return len(matches) == 1 and directory_id(matches[0].directory) == selected.id

    @staticmethod
    def result_matches(native_output, selected):
        data = getattr(native_output, 'data', None)
        if not isinstance(data, dict):
            return False
        directory, body = data.get('skill_directory'), data.get('skill_content')
        if not isinstance(directory, str) or not isinstance(body, str):
            return False
        if directory_id(directory) != selected.id:
            return False
        # Compare the actual raw document, not the media/layout-enriched
        # `content` field. Match the index's utf-8-sig/universal-newline reads.
        body = body.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
        return source_fingerprint('', read_sources(selected.directory, raw=body)) == selected.fingerprint

    async def finish(self, ctx):
        load = getattr(ctx, 'flash_selection_load', None)
        if load is None:
            return
        native_output = getattr(ctx.inputs, 'tool_result', None)
        selected, ticket = load['selected'], load['ticket']
        try:
            valid = (self.valid(ticket, selected, ctx.session)
                     and self.result_matches(native_output, selected))
        except Exception:
            valid = False
        if not getattr(native_output, 'success', False):
            # Preserve denial details and native tool result; do not claim load.
            return
        if not valid:
            data = self.selection.response('changed', '配置、权限或文件已变化，或实际加载内容与候选不一致，请重新检索。')
        else:
            state = ctx.extra.get(self.selection.REUSE)
            if state is not None:
                state.loaded = True
            explicit = ctx.extra.get(self.selection.EXPLICIT)
            if explicit is not None:
                explicit.setdefault('additional_ids', set()).add(selected.id)
            data = {'status': 'selected', 'loaded': True, 'selected': selected.name,
                    'instructions': native_output.data}
            emit(logger, 'choice', {**ticket['trace'], 'search_id': load['search_id']},
                 status='selected', selected=selected.name, loaded=True, decision_by='main_llm',
                 load_ms=(time.perf_counter() - load['started']) * 1000)
        output = SearchOutput(success=valid, data=data)
        ctx.inputs.tool_result = output
        ctx.inputs.tool_msg = ToolMessage(content=str(output), tool_call_id=ctx.inputs.tool_call.id)
