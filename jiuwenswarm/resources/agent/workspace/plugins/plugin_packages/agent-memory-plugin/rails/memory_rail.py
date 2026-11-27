"""Compose the host's existing memory Rails for this plugin mount."""

from importlib import import_module
from logging import getLogger
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import (
    build_jiuwen_memory_rail,
    resolve_coding_memory_project_name,
)
from jiuwenswarm.agents.harness.common.memory.plugin import mutable_config, resolve_memory_plugin_options

logger = getLogger(__name__)


def _work_lifecycle(base: type) -> type:
    from openjiuwen.harness.rails.memory.jiuwen_memory_rail import EXTERNAL_MEMORY_PREFETCH_SECTION, SectionName

    class WorkPluginMemoryRail(base):
        async def aclose(self, *, timeout: float = 10.0, grace_timeout: float = 5.0) -> None:
            task = self._sync_task
            if task is not None:
                await task
            if self.attachment_manager is not None:
                await self.attachment_manager.clear_source(source="agent_core.jiuwen_memory_rail")
            if self._session_end_extract and self._initialized:
                try:
                    await self._provider.on_session_end([])
                except Exception:
                    logger.exception("Agent Memory session extraction failed")
            await self._provider.shutdown()
            self._initialized = False

        def uninit(self, agent: Any) -> None:
            self._turn_buffer.clear()
            for name, card in self._owned_tool_cards.items():
                if agent.ability_manager.get(name) is card:
                    agent.ability_manager.remove_ability(name)
            self._owned_tool_cards.clear()
            if self.system_prompt_builder is not None:
                self.system_prompt_builder.remove_section(SectionName.EXTERNAL_MEMORY)
                self.system_prompt_builder.remove_section(EXTERNAL_MEMORY_PREFETCH_SECTION)
                self.system_prompt_builder = None
            self.attachment_manager = None

    return WorkPluginMemoryRail


def _code_lifecycle(base: type) -> type:
    class CodePluginMemoryRail(base):
        async def aclose(self, *, timeout: float = 10.0, grace_timeout: float = 5.0) -> None:
            await super().aclose()
            if not self._closed:
                # Construction can fail before the Rail attaches an owner.
                await self._provider.shutdown()
                self._closed = True

    return CodePluginMemoryRail


def _team_lifecycle(base: type, enabled: Any) -> type:
    class TeamPluginMemoryRail(base):
        @property
        def closed(self) -> bool:
            """Whether this Rail has completed shutdown."""
            return self._closed

        def _active(self) -> bool:
            return not self.closed and enabled()

        def get_callbacks(self) -> dict:
            # The Team mount forwards before_invoke on the same turn it attaches us.
            return {event: callback for event, callback in super().get_callbacks().items()
                    if callback != self.before_invoke}

        async def _initialize(self) -> bool:
            return self._active() and await super()._initialize()

        async def before_model_call(self, ctx: Any) -> None:
            if self._active():
                await super().before_model_call(ctx)

        async def after_tool_call(self, ctx: Any) -> None:
            if self._active():
                await super().after_tool_call(ctx)

        def _submit_sources(self, sources: list) -> bool:
            return self._active() and super()._submit_sources(sources)

        async def on_task_accepted(self, event: Any) -> None:
            if self._active():
                await super().on_task_accepted(event)

        async def aclose(self, *, timeout: float = 10.0, grace_timeout: float = 5.0) -> None:
            await super().aclose()
            if not self._closed:
                await self._provider.shutdown()
                self._closed = True

    return TeamPluginMemoryRail


class MemoryRailFactory(DeepAgentRail):
    @classmethod
    def prepare_plugin_context(cls, runtime: Any) -> dict[str, Any]:
        runtime.require("memory_context")
        if set(runtime.options) - {"mode", "base_url", "api_key", "identity_org", "identity_user", "sdk"}:
            raise ValueError("Unknown Agent Memory plugin options")
        options = resolve_memory_plugin_options(runtime.host_config, runtime.package_config, runtime.options)
        import_module("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
        if runtime.profile == "code":
            from openjiuwen.harness.rails import JiuwenMemoryCodingRail

            if not callable(getattr(JiuwenMemoryCodingRail, "aclose", None)):
                raise ValueError("Installed agent-core does not support closing the Code memory Rail")
        if runtime.profile == "team":
            from openjiuwen.harness.rails import JiuwenTeamMemoryRail

            host = runtime.require("memory_context")
            if not host.team_binding or not callable(host.team_enabled):
                raise ValueError("Team memory requires host-bound coordinates and session selection")
            if not callable(getattr(JiuwenTeamMemoryRail, "aclose", None)):
                raise ValueError("Installed agent-core does not support closing the Team memory Rail")
        return options

    @classmethod
    def from_plugin_context(cls, *, params: dict[str, Any], context: Any) -> Any:
        runtime = context.extras["plugin_runtime"]
        host = runtime.require("memory_context")
        config = mutable_config(runtime.host_config)
        memory_config = config.setdefault("memory", {})
        memory_config.setdefault("builtin", {})["jiuwen"] = mutable_config(runtime.options)
        is_code = runtime.profile == "code"
        is_team = runtime.profile == "team"
        lifecycle = _code_lifecycle if is_code else _work_lifecycle
        if is_team:
            def lifecycle(base: type) -> type:
                return _team_lifecycle(base, host.team_enabled)
        rail = build_jiuwen_memory_rail(
            config,
            agent_name=host.agent_name,
            scene="team" if is_team else "code" if is_code else "agent",
            project=host.team_project if is_team else resolve_coding_memory_project_name(host.project_dir),
            shared_document_root=True,
            team_binding=host.team_binding if is_team else None,
            collective_agent=host.team_agent if is_team else "",
            wrap_rail_class=lifecycle,
        )
        if rail is None:
            raise RuntimeError("Agent Memory Rail could not be constructed")
        return runtime.register_resource(rail)
