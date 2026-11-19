"""Visible catalog and root-skill appendix on actual SDK model carriers."""

from __future__ import annotations

import asyncio
import copy
import logging

from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, ToolCallInputs
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.skills.skill_lifecycle_events import extract_skill_lifecycle_event, is_root_skill_load

from jiuwenswarm.server.runtime.skill_turbo.accelerator_renderer import START, render_appendix, render_catalog, render_guide
from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import SkillTurboPromptRail, _build_skill_turbo_guide_text

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

logger = logging.getLogger(__name__)


class SkillAcceleratorRail(SkillTurboPromptRail):
    priority = 93

    def __init__(self, *, acceleration_disabled=False, config=None):
        super().__init__(acceleration_disabled=acceleration_disabled)
        self.config = copy.deepcopy(config if config is not None else {'enabled': True})

    def callback_priority(self, event):
        if event == AgentCallbackEvent.BEFORE_MODEL_CALL:
            return 93
        if event == AgentCallbackEvent.AFTER_TOOL_CALL:
            return 85
        return self.priority

    def _visible_names(self, session):
        host = self._agent
        if host is None:
            return set()
        rails = host.find_rails_by_type((SkillUseRail,))
        return {skill.name for rail in rails for skill in rail.get_skills_for_session(session)}

    async def _catalog(self, session):
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import collect_accelerator_catalog
        snapshot = await asyncio.to_thread(collect_accelerator_catalog)
        visible = self._visible_names(session)
        entries = {k: v for k, v in snapshot.entries.items() if k in visible and snapshot.manifest_valid.get(k, True)}
        accelerators = {k: tuple(s for s in v if s.enabled) for k, v in snapshot.accelerators.items()
                                 if k in visible and snapshot.manifest_valid.get(k, False) and v}
        from dataclasses import replace
        return replace(snapshot, entries=entries, accelerators=accelerators)

    def _enabled(self):
        return self.config.get('enabled', False) is True and not self._acceleration_disabled

    def _legacy_guide(self, language, names):
        return _build_skill_turbo_guide_text(language, names)

    async def before_model_call(self, ctx):
        try:
            await self._before_model_call(ctx)
        except AbortError:
            raise
        except Exception:
            if self.system_prompt_builder is not None:
                self.system_prompt_builder.remove_section('skill_accelerators')
                self.system_prompt_builder.remove_section('skill_turbo_guide')
            logger.warning('accelerator disclosure failed', exc_info=True)

    async def _before_model_call(self, ctx):
        self.system_prompt_builder = getattr(ctx.agent, 'system_prompt_builder', None) or self.system_prompt_builder
        builder = self.system_prompt_builder
        if builder is None:
            return
        guide_priority = self._resolve_priority('skill_turbo_guide', 40)
        for name in ('skill_accelerators', 'skill_turbo_guide'):
            builder.remove_section(name)
        tools = ctx.inputs.tools or []
        if not self._enabled() or not any(card.name == 'skill_acceleration_exec' for card in tools):
            for i, card in enumerate(tools):
                if card.name == 'skill_acceleration_exec':
                    local = card.model_copy(deep=True)
                    local.description = 'Skill acceleration is disabled for this request.'
                    tools[i] = local
            return
        snapshot = await self._catalog(getattr(ctx, 'session', None))
        language = self._resolve_language()
        segmented = snapshot.accelerators
        legacy = sorted(set(snapshot.entries) - set(segmented))
        index = render_catalog(segmented, language=language,
                               line_budget=self.config.get('catalog_line_budget', 96),
                               max_per_skill=self.config.get('catalog_max_per_skill', 8))
        guide = render_guide(language, batch_enabled=self.config.get('batch_enabled', True),
                             max_batch_steps=self.config.get('max_batch_steps', 8)) if segmented else ''
        if legacy:
            guide += '\nLegacy rules ONLY for ' + ', '.join(legacy) + '; never apply to declared segment skills:\n' + self._legacy_guide(language, legacy)
        # Each model input receives its own card; ability-manager/navigation templates stay generic.
        for i, card in enumerate(tools):
            if card.name == 'skill_acceleration_exec':
                local = card.model_copy(deep=True)
                schema = getattr(local, 'parameters', None)
                if not isinstance(schema, dict):
                    schema = getattr(local, 'input_params', None)
                if isinstance(schema, dict) and not self.config.get('batch_enabled', True):
                    schema.get('properties', {}).pop('accelerators', None)
                local.description = (('当前内部支持：' if language == 'cn' else 'Currently supported: ') + '、'.join(sorted(snapshot.entries)) + '\n' + guide + '\n' + index) if guide else 'No visible accelerators.'
                tools[i] = local
        if guide:
            builder.add_section(PromptSection(name='skill_turbo_guide', content={language: guide}, priority=guide_priority))
        if index and self.config.get('catalog_enabled', True) and builder.has_section(SectionName.SKILLS):
            builder.add_section(PromptSection(name='skill_accelerators', content={language: index}, priority=41))

    async def after_tool_call(self, ctx):
        if not self._enabled() or not self.config.get('load_appendix_enabled', True):
            return
        if not isinstance(ctx.inputs, ToolCallInputs) or ctx.exception is not None:
            return
        event = extract_skill_lifecycle_event(ctx)
        if not is_root_skill_load(event):
            return
        result = ctx.inputs.tool_result
        if getattr(result, 'success', None) is not True:
            return
        data = getattr(result, 'data', None)
        if not isinstance(data, dict):
            return
        content = data.get('content') or getattr(ctx.inputs.tool_msg, 'content', None)
        if not isinstance(content, str) or START in content:
            return
        snapshot = await self._catalog(getattr(ctx, 'session', None))
        specs = snapshot.accelerators.get(event.skill_name)
        if not specs:
            return
        progress = await self._progress(ctx, event.skill_name)
        text = content + '\n\n' + render_appendix(event.skill_name, specs, progress, language=self._resolve_language(),
            batch_enabled=self.config.get('batch_enabled', True), max_batch_steps=self.config.get('max_batch_steps', 8))
        result.data = {**data, 'content': text}
        if ctx.inputs.tool_msg is not None:
            ctx.inputs.tool_msg = ctx.inputs.tool_msg.model_copy(update={'content': text})

    async def _progress(self, ctx, skill):
        from openjiuwen.core.session.agent import create_agent_session
        from jiuwenswarm.server.runtime.skill_turbo.resume_context import set_skill_turbo_id
        from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import get_current_request_metadata
        from jiuwenswarm.agents.harness.common.rails.task_execution_rail import get_current_task_id
        from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
        metadata = get_current_request_metadata() or {}
        workspace = metadata.get('output_dir')
        if not workspace or ctx.session is None:
            return {'progress': 'unavailable', 'reason': 'explicit workspace/session missing'}
        host = self._agent
        session = create_agent_session(session_id=ctx.session.get_session_id(), card=host.card)
        set_skill_turbo_id(session, host.card)
        try:
            ledger = await SkillRunLedger.current(session, skill=skill, workspace_base=workspace,
                                                 task_id=str(get_current_task_id() or '').removeprefix('todo:'))
            if ledger is None:
                return {'progress': 'no_progress'}
            return {'progress': {**ledger.digest(), 'segments': {
                name: run['status'] for name, run in ledger.data['latest_runs'].items()}}}
        except AbortError:
            raise
        except Exception as exc:
            return {'progress': 'unavailable', 'reason': str(exc)}
        finally:
            await session.close_stream()

    def uninit(self, agent):
        builder = getattr(agent, 'system_prompt_builder', None) or self.system_prompt_builder
        if builder is not None:
            builder.remove_section('skill_accelerators')
        super().uninit(agent)
