"""Task-scoped, isolated skill access for dynamically selected experts."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.single_agent.skills.skill_manager import Skill
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperation, SysOperationCard
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails._multimodal import should_enable_read_image_multimodal
from openjiuwen.harness.resources.extension_resolver import LoadRecord, ResolvedSkill
from openjiuwen.harness.tools import BashTool, ReadFileTool


logger = logging.getLogger(__name__)
_SKILL_TOOLS = ("skill_tool", "list_skill")


def snapshot_public_skills(agent: Any) -> list[Skill]:
    """Copy the parent's currently visible skills without discovering new paths."""
    find_rails = getattr(agent, "find_rails_by_type", None)
    if not callable(find_rails):
        return []
    visible: dict[str, Skill] = {}
    for rail in find_rails((SkillUseRail,)):
        enabled = rail.enabled_skills
        disabled = rail.disabled_skills
        for skill in rail.skills_meta:
            if skill.name in disabled or (enabled and skill.name not in enabled):
                continue
            if skill.name not in visible:
                visible[skill.name] = skill.model_copy(deep=True)
    return list(visible.values())


async def _settle(task: asyncio.Task) -> Any:
    """Finish an owned operation even when its caller is cancelled repeatedly."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class _SkillReadOperation(SysOperation):
    """Unregistered local channel used only by skill content/metadata readers."""

    def __init__(self) -> None:
        self._skill_work_config = LocalWorkConfig(sandbox_root=[], restrict_to_sandbox=True)
        super().__init__(SysOperationCard(
            id=f"dynamic_expert_skills_{uuid4().hex}",
            mode=OperationMode.LOCAL,
            work_config=self._skill_work_config,
        ))

    def set_roots(self, directories: list[str]) -> None:
        self._skill_work_config.sandbox_root = list(directories)

    def fs(self):
        # Core treats an empty sandbox_root as workspace/cwd defaults. An
        # unmounted skill reader has no access, including to those defaults.
        if not self._skill_work_config.sandbox_root:
            raise PermissionError("No dynamic expert skill directories are mounted")
        return super().fs()


class DynamicExpertSkillRail(SkillUseRail):
    """Expose an expert's own skills followed by a frozen public skill view.

    Core records and unloads the actual bindings. Its binder normally converts
    a leaf into its parent mount, so the refresh methods here deliberately use
    only explicitly admitted leaves; a shared parent directory is never a
    discovery boundary. Each instance belongs to one child agent.
    """

    def __init__(
        self,
        *,
        public_skills: list[Skill],
        expert_skills: list[ResolvedSkill],
        runtime_owner_id: str | None = None,
        runtime_sysop_id: str | None = None,
        runtime_mcp_ids: list[str] | None = None,
    ) -> None:
        super().__init__(skills_dir=[], skill_mode=self.SKILL_MODE_AUTO_LIST, include_tools=False)
        self._skill_read_operation = _SkillReadOperation()
        self.sys_operation = self._skill_read_operation
        self._public_skills = [skill.model_copy(deep=True) for skill in public_skills]
        self._expert_skills = list(expert_skills)
        self._agent: Any = None
        self._runtime_owner_id = runtime_owner_id
        self._runtime_sysop_id = runtime_sysop_id
        self._runtime_mcp_ids = set(runtime_mcp_ids or [])
        self._load_record: LoadRecord | None = None
        self._catalogue: dict[str, Skill] = {}
        self._mounted = False
        self._closed = False
        self._config_skills_before_mount: list[str] | str | None = None
        self._has_config_snapshot = False
        self._lifecycle_lock = asyncio.Lock()
        self._sessions: dict[int, Any] = {}

    @property
    def load_record(self) -> LoadRecord | None:
        """The successful Core load still awaiting cleanup, if any."""
        return self._load_record

    def set_sys_operation(self, sys_operation: SysOperation) -> None:
        # Core injects the child's normal sandbox here. Skill packages can be
        # outside that workspace, so only this rail's read-only tool surface
        # uses a separate, narrowly rooted operation. The child config and its
        # ordinary file/shell tools retain their original operation and policy.
        self.sys_operation = self._skill_read_operation

    def init(self, agent: Any) -> None:
        if self._closed:
            raise RuntimeError("Dynamic expert skill round has ended")
        if self._agent is not None and self._agent is not agent:
            raise RuntimeError("DynamicExpertSkillRail cannot be shared between agents")
        self._agent = agent
        # Register before any operation that can fail. TaskTool's finally also
        # runs when the first invocation fails during initialization.
        agent.register_task_resource_cleanup(self.cleanup, prepare=self.prepare)
        self._init_tools()

    def _init_tools(self) -> None:
        super().init(self._agent)
        agent = self._agent
        ability_manager = agent.ability_manager
        for name in _SKILL_TOOLS:
            card = self._owned_tool_cards.get(name)
            if card is None or ability_manager.get(name) is not card:
                raise RuntimeError(f"Dynamic expert must own its {name} tool")
        # Keep the normal SkillUseRail filesystem fallback for simple expert
        # packages, using the child's original sandbox rather than the skill
        # reader. Existing template tools and parent references retain ownership.
        operation = agent.deep_config.sys_operation
        if operation is None:
            return
        language = agent.system_prompt_builder.language
        tools = []
        if ability_manager.get("read_file") is None:
            tools.append(ReadFileTool(
                operation,
                language=language,
                agent_id=agent.card.id,
                enable_image_multimodal=should_enable_read_image_multimodal(agent),
            ))
        if ability_manager.get("bash") is None:
            tools.append(BashTool(operation, language=language, agent_id=agent.card.id))
        for tool in tools:
            result = ability_manager.add_ability(tool.card, tool)
            if result.added:
                self._owned_tool_cards[tool.card.name] = tool.card

    async def prepare(self) -> None:
        """Mount once before model execution, retaining the mount on interrupt."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Dynamic expert skill round has ended")
            if self._mounted:
                return
            if self._agent is None:
                raise RuntimeError("DynamicExpertSkillRail has not been initialized")
            if self._load_record is not None:
                await self._cleanup_atomic()
            self._init_tools()
            task = asyncio.create_task(self._mount())
            try:
                await asyncio.shield(task)
                if self._closed:
                    raise RuntimeError("Dynamic expert skill round has ended")
            except asyncio.CancelledError:
                # Core's batch rollback catches Exception, not CancelledError.
                # Keep cancellation out of the batch, obtain its LoadRecord,
                # and unload it before propagating the original cancellation.
                try:
                    await _settle(task)
                except Exception:
                    logger.exception("[symphony.experts] cancelled skill mount failed")
                try:
                    await _settle(asyncio.create_task(self._cleanup()))
                except Exception:
                    logger.exception("[symphony.experts] cancelled skill cleanup failed")
                raise
            except Exception:
                await self._cleanup_atomic()
                raise

    async def _mount(self) -> None:
        admitted: dict[str, Skill | None] = {}
        names: set[str] = set()
        for resolved in self._expert_skills:
            for directory in self._expert_leaf_dirs(resolved):
                if directory.name in names:
                    continue
                admitted[str(directory)] = None
                names.add(directory.name)
        for skill in self._public_skills:
            if skill.name in names:
                continue
            directory = skill.directory.expanduser().resolve()
            if not (directory / "SKILL.md").is_file():
                raise FileNotFoundError(f"Public skill is no longer available: {directory}")
            if str(directory) not in admitted:
                admitted[str(directory)] = skill.model_copy(update={"directory": directory}, deep=True)
                names.add(skill.name)

        # The same operation instance is already held by skill_tool. Updating
        # its LocalWorkConfig roots also updates its cached filesystem reader;
        # no global SysOperation or derived file/shell tools are registered.
        self._skill_read_operation.set_roots(list(admitted))
        catalogue: dict[str, Skill] = {}
        for raw_directory, public_skill in admitted.items():
            directory = Path(raw_directory)
            if public_skill is not None:
                await self._read_skill_text(directory / "SKILL.md", head=1)
                catalogue[raw_directory] = public_skill
                continue
            # Core's _load_skill swallows filesystem/parse errors and
            # fabricates fallback metadata. Read strictly so a broken
            # package cannot run as if its required skills were mounted.
            metadata = await self._load_front_matter(directory / "SKILL.md")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError(f"Invalid expert skill metadata: {directory}")
            description = (metadata or {}).get("description")
            catalogue[raw_directory] = Skill(
                name=directory.name,
                description=str(description or f"Skill located in {directory}"),
                directory=directory,
            )
        self._catalogue = catalogue
        previous_skills = self._agent.deep_config.skills
        self._config_skills_before_mount = (
            list(previous_skills) if isinstance(previous_skills, list) else previous_skills
        )
        self._has_config_snapshot = True
        if catalogue:
            self._load_record = await self._agent.load_plugin_ability(skills=[
                ResolvedSkill(directory=directory, mode=self.SKILL_MODE_AUTO_LIST)
                for directory in catalogue
            ])
        self._mounted = True

    @staticmethod
    def _expert_leaf_dirs(resolved: ResolvedSkill) -> list[Path]:
        if not resolved.directory or not resolved.directory.strip():
            raise ValueError("Expert skill directory cannot be empty")
        directory = Path(resolved.directory).expanduser().resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f"Expert skill directory does not exist: {directory}")
        if (directory / "SKILL.md").is_file():
            candidates = [directory]
        else:
            candidates = [
                path.resolve() for path in sorted(directory.iterdir(), key=lambda path: path.name)
                if path.is_dir() and (path / "SKILL.md").is_file()
            ]
        enabled = set(resolved.enabled_skills or [])
        admitted = [path for path in candidates if not enabled or path.name in enabled]
        if not admitted:
            raise ValueError(f"Declared expert skill contains no usable SKILL.md: {directory}")
        return admitted

    async def _prepare_skills(self) -> None:
        # Core mutates bound_leaf_dirs before reload_skills. Do not use its
        # skills_dir roots: those roots can contain disabled public siblings.
        mounted = self.bound_leaf_dirs
        self.skills = [skill for directory, skill in self._catalogue.items() if directory in mounted]

    def _build_skills_snapshot_signature(self) -> tuple[tuple[str, float], ...]:
        return tuple((str(skill.directory), 0.0) for skill in self.skills)

    def get_skills_for_session(self, session: Any = None) -> list[Skill]:
        # Persisted SkillUseRail baselines may outlive their mounts. Never let
        # the tool merge an old baseline back into this task's allowed view.
        return list(self.skills)

    def _ensure_session_baseline(self, ctx: AgentCallbackContext) -> None:
        session = getattr(ctx, "session", None)
        if session is not None:
            self._sessions[id(session)] = session
            self._save_session_baseline(session, self.skills)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            await self.prepare()
            await super().before_invoke(ctx)
        except Exception as exc:
            # Ordinary rail exceptions are logged and swallowed by Core.
            # AbortError is its supported way to stop before the model runs.
            raise AbortError("动态专家技能挂载失败", cause=exc) from exc

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        try:
            await self.prepare()
            await super().before_model_call(ctx)
        except Exception as exc:
            raise AbortError("动态专家技能挂载失败", cause=exc) from exc

    async def cleanup(self) -> None:
        """Release bindings and tools on success, failure, cancel, or round close."""
        async with self._lifecycle_lock:
            await self._cleanup_atomic()

    async def close(self) -> None:
        """End the round permanently and drain any in-progress mount."""
        self._closed = True
        task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle(task)
            raise

    async def _close(self) -> None:
        async with self._lifecycle_lock:
            try:
                await self._cleanup()
            finally:
                try:
                    self._teardown_child_runtime()
                finally:
                    await self._cleanup_mcp_references()

    async def _cleanup_mcp_references(self) -> None:
        """Drop this child's MCP references and disconnect unshared servers."""
        agent = self._agent
        owner_id = self._runtime_owner_id or (agent.card.id if agent is not None else None)
        if not owner_id:
            return
        server_ids = set(self._runtime_mcp_ids)
        if agent is not None:
            server_ids.update(config.server_id for config in agent.deep_config.mcps or [])
        manager = Runner.resource_mgr
        for server_id in sorted(server_ids):
            if manager.get_mcp_server_config(server_id) is None:
                continue
            for resource_id in [server_id, *manager.get_mcp_tool_ids(server_id)]:
                if owner_id not in (manager.get_resource_tag(resource_id) or []):
                    continue
                result = manager.remove_resource_tag(resource_id, owner_id, skip_if_tag_not_exists=True)
                if result.is_err():
                    raise result.msg()
            if manager.get_resource_tag(server_id):
                continue
            # The MCP-specific API disconnects the client. Generic resource/tag
            # removal would leave the connection alive after deleting its tags.
            results = await manager.remove_mcp_server(
                server_id, skip_if_tag_not_exists=True, ignore_exception=True,
            )
            for result in results if isinstance(results, list) else [results]:
                if result.is_err():
                    raise result.msg()

    def _teardown_child_runtime(self) -> None:
        agent = self._agent
        if agent is None:
            self._teardown_uninitialized_runtime()
            return
        try:
            # Core removes only stateful tools qualified by this child's owner;
            # inherited parent references and stateless shared tools survive.
            agent.ability_manager.teardown_tools()
        finally:
            config = agent.deep_config
            operation = config.sys_operation
            owner_id = config.tool_owner_id or agent.card.id
            expected_id = f"{agent.card.name}_{agent.card.id}"
            if (
                owner_id == agent.card.id
                and operation is not None
                and operation.id == expected_id
                and Runner.resource_mgr.get_sys_operation(expected_id) is operation
            ):
                result = Runner.resource_mgr.remove_sys_operation(expected_id)
                if result.is_err():
                    raise result.msg()

    def _teardown_uninitialized_runtime(self) -> None:
        """Reclaim factory resources when another rail failed before our init."""
        owner_id = self._runtime_owner_id
        if not owner_id:
            return
        try:
            # get_tool() returns already-registered concrete tools. With no
            # session it neither constructs providers nor installs trace proxies.
            for tool in Runner.resource_mgr.get_tool():
                card = getattr(tool, "card", None)
                if card is None or card.stateless or card.id != f"{card.name}_{owner_id}":
                    continue
                result = Runner.resource_mgr.remove_tool(card.id)
                if result.is_err():
                    raise result.msg()
        finally:
            sysop_id = self._runtime_sysop_id
            if (
                sysop_id and sysop_id.endswith(f"_{owner_id}")
                and Runner.resource_mgr.get_sys_operation(sysop_id) is not None
            ):
                result = Runner.resource_mgr.remove_sys_operation(sysop_id)
                if result.is_err():
                    raise result.msg()

    async def _cleanup_atomic(self) -> None:
        task = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle(task)
            raise

    async def _cleanup(self) -> None:
        try:
            if self._load_record is not None:
                await self._agent.unload_extension(self._load_record)
                self._load_record = None
        finally:
            self._mounted = False
            self._skill_read_operation.set_roots([])
            self._catalogue.clear()
            self.clear_skills()
            # Keep failed unload bookkeeping intact so round.close can retry.
            if self._load_record is None:
                self.skills_dir = []
                self.bound_leaf_dirs = set()
                self.enabled_skills = set()
                if self._has_config_snapshot:
                    # Core determines leaf-vs-root again during unload. If a
                    # SKILL.md disappeared, its original parent can remain in
                    # config.skills despite a successful unload. This child
                    # has one skill owner; restore the pre-mount configuration.
                    self._agent.deep_config.skills = self._config_skills_before_mount
                    self._config_skills_before_mount = None
                    self._has_config_snapshot = False
            for session in self._sessions.values():
                try:
                    self._save_session_baseline(session, [])
                except Exception:
                    logger.exception("[symphony.experts] failed to clear skill session baseline")
            self._sessions.clear()
            if self.system_prompt_builder is not None:
                self.system_prompt_builder.remove_section(SectionName.SKILLS)
            if self._agent is not None:
                super().uninit(self._agent)


__all__ = ["DynamicExpertSkillRail", "snapshot_public_skills"]
