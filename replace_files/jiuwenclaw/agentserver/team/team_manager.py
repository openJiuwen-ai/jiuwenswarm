# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Team lifecycle manager."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from openjiuwen.agent_teams.agent.team_agent import TeamAgent
from openjiuwen.agent_teams.paths import team_home
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.spawn.context import reset_session_id, set_session_id
from openjiuwen.harness import DeepAgent

from jiuwenclaw.agentserver.team.bootstrap import configure_agent_teams_home

configure_agent_teams_home()

from jiuwenclaw.agentserver.team.config_loader import (
    load_team_spec_dict,
)
from jiuwenclaw.agentserver.team.monitor_handler import TeamMonitorHandler
from jiuwenclaw.agentserver.team.team_runtime_inheritance import (
    RAIL_WHITELIST,
    MemberRailConfig,
    build_member_rails,
    filter_inheritable_ability_cards,
    get_default_model_name,
)
from jiuwenclaw.agentserver.tools.deepresearch_tools import (
    push_deepresearch_route,
    reset_deepresearch_route,
)
from jiuwenclaw.utils import format_session_log

logger = logging.getLogger(__name__)


class TeamManager:
    """Manage team instances across sessions."""

    def __init__(self):
        self._team_agents: dict[str, TeamAgent] = {}
        self._team_monitors: dict[str, TeamMonitorHandler] = {}
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def has_stream_task(self, session_id: str) -> bool:
        return session_id in self._stream_tasks

    def pop_stream_task(self, session_id: str) -> asyncio.Task | None:
        return self._stream_tasks.pop(session_id, None)

    @staticmethod
    def _load_team_spec(session_id: str) -> TeamAgentSpec:
        return TeamAgentSpec.model_validate(load_team_spec_dict(session_id))

    @staticmethod
    async def _cleanup_team_runtime_state(
        spec: TeamAgentSpec,
    ) -> tuple[list[str], list[str]]:
        from openjiuwen.agent_teams.paths import get_agent_teams_home
        from openjiuwen.agent_teams.spawn.shared_resources import get_shared_db
        from openjiuwen.agent_teams.tools.database import DatabaseConfig

        db_config = spec.storage.build() if spec.storage else DatabaseConfig()
        if db_config.db_type == "sqlite" and not db_config.connection_string:
            db_config.connection_string = str(get_agent_teams_home() / "team.db")
        try:
            shared_db = get_shared_db(db_config)
            return await shared_db.cleanup_all_runtime_state()
        except Exception as exc:
            logger.warning("[TeamManager] runtime cleanup failed for team=%s: %s", spec.team_name, exc)
            return [], []

    @staticmethod
    def register_member_runtime_tools(
        agent: DeepAgent,
        *,
        session_id: str,
        request_id: str | None,
        channel_id: str | None,
        request_metadata: dict[str, Any] | None,
    ) -> None:
        from jiuwenclaw.config import get_config
        from jiuwenclaw.agentserver.cron_config import should_register_cron_tools
        from jiuwenclaw.agentserver.deep_agent.cron_runtime import CronRuntimeBridge
        from jiuwenclaw.agentserver.tools.send_file_to_user import SendFileToolkit
        from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
        from openjiuwen.core.runner import Runner

        agent_id = getattr(getattr(agent, "card", None), "id", None)
        if should_register_cron_tools():
            cron_runtime = CronRuntimeBridge()
            cron_context = SimpleNamespace(
                tool_scope=f"team_member_{agent_id or 'unknown'}",
                channel_id=channel_id or "web",
                session_id=session_id,
                metadata=request_metadata,
                mode="team",
            )

            try:
                cron_tools = cron_runtime.build_tools(context=cron_context, agent_id=agent_id)
                for cron_tool in cron_tools:
                    if not Runner.resource_mgr.get_tool(cron_tool.card.id):
                        Runner.resource_mgr.add_tool(cron_tool)
                    agent.ability_manager.add(cron_tool.card)
                logger.info("[TeamManager] Registered %d cron tools for member agent=%s", len(cron_tools), agent_id)
            except Exception as exc:
                logger.warning("[TeamManager] cron tool registration failed for member agent=%s: %s", agent_id, exc)
        else:
            logger.info("[TeamManager] skip cron tool registration for member agent=%s: disabled by env", agent_id)
        # 设置 DeepResearch 路由上下文（Team 模式）
        dr_token = push_deepresearch_route(
            request_id=request_id or "",
            channel_id=channel_id or "web",
            session_id=session_id,
        )

        # 整个方法体放在 try-finally 中，确保路由上下文在退出时重置
        try:
            # Team 成员的 skill 步骤追踪（skill_step facade）：写入 skill_step.md，
            # 与 SkillComplianceRail 共用该文件路径。
            # 这里再调一次只是保险兜底：主 adapter 未初始化的场景（如单元测试直接构造 team）也能可用。
            # 与 SkillComplianceRail 一致使用显式 team session，避免依赖未绑定的 plan 上下文。
            try:
                skill_step_toolkit = SkillStepToolkit(session_id=session_id)
                skill_step_tool_names: list[str] = []
                for step_tool in skill_step_toolkit.get_tools():
                    if not Runner.resource_mgr.get_tool(step_tool.card.id):
                        Runner.resource_mgr.add_tool(step_tool)
                    agent.ability_manager.add(step_tool.card)
                    skill_step_tool_names.append(step_tool.card.name)
                logger.info(
                    "[TeamManager] skill_step facade registered for member agent=%s session=%s tools=%s",
                    agent_id, session_id, skill_step_tool_names,
                )
            except Exception as exc:
                logger.warning(
                    "[TeamManager] skill_step facade registration failed for member agent=%s: %s",
                    agent_id, exc,
                )

            # SendFileToolkit 注册（仅在 request_id 和 channel_id 有效时）
            if request_id and channel_id:
                try:
                    config = get_config()
                    send_file_enabled = (
                        config.get("channels", {})
                        .get(str(channel_id), {})
                        .get("send_file_allowed", False)
                    )
                    if send_file_enabled:
                        for existing in list(agent.ability_manager.list() or []):
                            if getattr(existing, "name", "").startswith("send_file_to_user"):
                                agent.ability_manager.remove(existing.name)

                        send_file_toolkit = SendFileToolkit(
                            request_id=request_id,
                            session_id=session_id,
                            channel_id=channel_id,
                            metadata=request_metadata,
                        )
                        for sf_tool in send_file_toolkit.get_tools():
                            Runner.resource_mgr.add_tool(sf_tool)
                            agent.ability_manager.add(sf_tool.card)
                        logger.info("[TeamManager] SendFileToolkit registered for channel=%s", channel_id)
                    else:
                        logger.info(
                            "[TeamManager] SendFileToolkit skipped: send_file_allowed=False for channel=%s",
                            channel_id,
                        )
                except Exception as exc:
                    logger.warning("[TeamManager] SendFileToolkit registration failed: %s", exc)
            else:
                logger.info("[TeamManager] SendFileToolkit skipped: missing request_id or channel_id")

        finally:
            # 重置 DeepResearch 路由上下文
            reset_deepresearch_route(dr_token)

    @staticmethod
    def build_agent_customizer(
        spec: TeamAgentSpec,
        deep_agent: DeepAgent,
        session_id: str,
        *,
        request_id: str | None = None,
        channel_id: str | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> Callable[..., None]:
        from jiuwenclaw.agentserver.deep_agent.rails.team_member_skill_toolkit_rail import (
            MemberSkillToolkitRail,
        )
        from jiuwenclaw.agentserver.skill_manager import SkillManager
        from jiuwenclaw.agentserver.extensions.rail_manager import get_rail_manager
        from jiuwenclaw.utils import get_agent_skills_dir

        global_skills_dir = get_agent_skills_dir()
        global_skills_state_path = global_skills_dir / "skills_state.json"
        resolved_channel = channel_id or "default"
        resolved_model_name = get_default_model_name()

        def resolve_member_spec(
            member_name: str | None,
            role: str | None,
        ) -> Any:
            if member_name and member_name in spec.agents:
                return spec.agents[member_name]
            if role and role in spec.agents:
                return spec.agents[role]
            return spec.agents.get("leader")

        def resolve_member_skills(
            member_name: str | None,
            role: str | None,
        ) -> tuple[bool, list[str]]:
            member_spec = resolve_member_spec(member_name, role)
            if member_spec is None or not hasattr(member_spec, "skills"):
                return False, []

            skills = getattr(member_spec, "skills", None)
            if skills is None:
                return False, []

            return True, [str(skill).strip() for skill in skills if str(skill).strip()]

        def copy_member_configured_skills(
            member_skills_dir: Path,
            selected_skills: list[str],
        ) -> None:
            """Copy member-configured skills to member's own skills directory."""
            if not global_skills_dir.exists():
                logger.warning("[TeamManager] global_skills_dir does not exist: %s", global_skills_dir)
                return

            selected_skill_set = set(selected_skills)
            member_skills_dir.mkdir(parents=True, exist_ok=True)
            copied_count = 0
            for skill_dir in global_skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                if not (skill_dir / "SKILL.md").is_file():
                    continue
                if skill_dir.name not in selected_skill_set:
                    continue
                dest = member_skills_dir / skill_dir.name
                if dest.exists():
                    continue
                shutil.copytree(skill_dir, dest)
                copied_count += 1
                logger.info("[TeamManager] Copied skill '%s' to member workspace", skill_dir.name)

            existing_skill_names = {
                path.name for path in member_skills_dir.iterdir() if path.is_dir()
            }
            missing = sorted(selected_skill_set - existing_skill_names)
            if missing:
                logger.warning("[TeamManager] configured skills not found in global dir: %s", missing)

            logger.info("[TeamManager] Total configured skills copied to member: %d", copied_count)

        def build_member_skill_state(member_skills_dir: Path) -> dict[str, Any]:
            state: dict[str, Any] = {
                "marketplaces": [],
                "installed_plugins": [],
                "local_skills": [],
            }
            if global_skills_state_path.is_file():
                try:
                    loaded_state = json.loads(global_skills_state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_state, dict):
                        state.update(loaded_state)
                except Exception as exc:
                    logger.warning("[TeamManager] failed to load global skills_state.json: %s", exc)

            state["marketplaces"] = SkillManager.normalize_marketplaces(
                state.get("marketplaces")
            )

            actual_skill_names = sorted(
                path.name
                for path in member_skills_dir.iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            )
            actual_skill_set = set(actual_skill_names)

            installed_plugins = []
            for plugin in state.get("installed_plugins", []):
                if not isinstance(plugin, dict):
                    continue
                plugin_name = str(plugin.get("name", "")).strip()
                if not plugin_name or plugin_name not in actual_skill_set:
                    continue
                installed_plugins.append(plugin)

            local_skills = []
            for local_skill in state.get("local_skills", []):
                if not isinstance(local_skill, dict):
                    continue
                skill_name = str(local_skill.get("name", "")).strip()
                if not skill_name or skill_name not in actual_skill_set:
                    continue
                local_skills.append(local_skill)

            existing_plugin_names = {
                str(plugin.get("name", "")).strip()
                for plugin in installed_plugins
                if isinstance(plugin, dict)
            }
            existing_local_names = {
                str(local_skill.get("name", "")).strip()
                for local_skill in local_skills
                if isinstance(local_skill, dict)
            }
            for skill_name in actual_skill_names:
                if skill_name not in existing_plugin_names:
                    installed_plugins.append(
                        {
                            "name": skill_name,
                            "marketplace": "",
                            "version": "",
                            "commit": "",
                            "source": "project",
                            "installed_at": "",
                        }
                    )
                if skill_name not in existing_local_names:
                    local_skills.append(
                        {
                            "name": skill_name,
                            "origin": str(member_skills_dir / skill_name),
                            "source": "project",
                        }
                    )

            state["installed_plugins"] = installed_plugins
            state["local_skills"] = local_skills
            return state

        def write_member_skill_state(member_skills_dir: Path) -> None:
            state_file = member_skills_dir / "skills_state.json"
            state = build_member_skill_state(member_skills_dir)
            state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("[TeamManager] Wrote member skills_state.json: %s", state_file)

        def customizer(
            agent: DeepAgent,
            member_name: str | None = None,
            role: str | None = None,
        ) -> None:
            logger.info(
                "[TeamManager] customizer called: channel=%s member_name=%s role=%s",
                resolved_channel,
                member_name,
                role,
            )
            agent_ws = agent.deep_config.workspace if agent.deep_config else None
            if agent_ws:
                logger.debug("[TeamManager] member workspace.root_path=%s", agent_ws.root_path)
            else:
                logger.warning("[TeamManager] agent deep_config.workspace is None")

            try:
                from jiuwenclaw.agentserver.team.member_subagents import assign_team_member_subagents

                assign_team_member_subagents(deep_agent, agent)
            except Exception as exc:
                logger.warning("[TeamManager] assign_team_member_subagents failed: %s", exc)

            inheritable_cards = filter_inheritable_ability_cards(
                deep_agent,
                exclude_tool_names=frozenset({"task_tool"}),
            )
            existing_ability_ids = {card.id for card in agent.ability_manager.list() or []}
            added_count = 0
            for card in inheritable_cards:
                if card.id not in existing_ability_ids:
                    agent.ability_manager.add(card)
                    existing_ability_ids.add(card.id)
                    added_count += 1
                else:
                    logger.debug("[TeamManager] Ability '%s' already exists, skipped", card.name)
            logger.info(
                "[TeamManager] Added %d inheritable abilities (total: %d)",
                added_count,
                len(existing_ability_ids),
            )

            member_workspace = agent.deep_config.workspace if agent.deep_config else None
            if member_workspace and member_workspace.root_path:
                member_skills_dir = Path(member_workspace.root_path) / "skills"
                skills_configured, selected_skills = resolve_member_skills(member_name, role)

                # Copy member-configured skills to member's own skills directory
                # Note: global skills are already copied to team shared directory in create_team
                try:
                    # Ensure member skills directory exists
                    member_skills_dir.mkdir(parents=True, exist_ok=True)
                    if skills_configured and selected_skills:
                        copy_member_configured_skills(member_skills_dir, selected_skills)
                    # Member directory always needs skills_state.json
                    write_member_skill_state(member_skills_dir)
                except Exception as exc:
                    logger.warning("[TeamManager] skill copy failed: %s", exc)

                # Create independent SkillManager and SkillToolkit for member
                try:
                    agent.add_rail(
                        MemberSkillToolkitRail(
                            workspace_dir=str(member_workspace.root_path),
                        )
                    )
                    logger.info(
                        "[TeamManager] MemberSkillToolkitRail queued for member workspace: %s",
                        member_workspace.root_path,
                    )
                except Exception as exc:
                    logger.warning("[TeamManager] MemberSkillToolkitRail setup failed: %s", exc)

                try:
                    from jiuwenclaw.config import get_config as _get_cfg
                    _react = ((_get_cfg() or {}).get("react") or {})
                    _ce_enabled = bool(
                        (_react.get("context_engine_config") or {}).get("enabled", True)
                    )
                except Exception:
                    _react = {}
                    _ce_enabled = True

                try:
                    member_rails = build_member_rails(
                        MemberRailConfig(
                            skills_dir=str(member_skills_dir),
                            language="cn",
                            channel=resolved_channel,
                            agent_name=getattr(agent.card, "name", "team_member"),
                            model_name=resolved_model_name,
                            session_id=session_id,
                            member_id=getattr(agent.card, "id", None),
                            context_engine_enabled=_ce_enabled,
                            react_config=_react,
                        )
                    )
                    logger.info(
                        format_session_log(
                            session_id,
                            "[TeamManager] member context_engine_config.enabled=%s (member_id=%s)",
                        ),
                        _ce_enabled,
                        getattr(agent.card, "id", None),
                    )
                    for rail in member_rails:
                        if type(rail).__name__ in RAIL_WHITELIST:
                            agent.add_rail(rail)
                        else:
                            logger.debug("[TeamManager] Skipping non-whitelisted rail: %s", type(rail).__name__)
                    logger.info("[TeamManager] Added %d rails for team member", len(member_rails))
                except Exception as exc:
                    logger.warning("[TeamManager] build_member_rails failed: %s", exc)

            rail_manager = get_rail_manager()
            for rail_name in rail_manager.get_registered_rail_names():
                try:
                    rail_instance = rail_manager.load_rail_instance_without_enabled_check(rail_name)
                    if rail_instance is not None:
                        agent.add_rail(rail_instance)
                        logger.info("[TeamManager] Added extension rail: %s", rail_name)
                except Exception as exc:
                    logger.warning("[TeamManager] add rail %s failed: %s", rail_name, exc)

            TeamManager.register_member_runtime_tools(
                agent,
                session_id=session_id,
                request_id=request_id,
                channel_id=channel_id,
                request_metadata=request_metadata,
            )

        return customizer


    @staticmethod
    def _copy_global_skills_to_team_shared_dir(spec: TeamAgentSpec) -> None:
        """Copy global skills to team shared directory (executed once after team build)."""
        from jiuwenclaw.utils import get_agent_skills_dir

        global_skills_dir = get_agent_skills_dir()
        if not global_skills_dir.exists():
            logger.warning("[TeamManager] global_skills_dir does not exist: %s", global_skills_dir)
            return

        # Resolve team workspace path
        ws_config = spec.workspace
        ws_path = ws_config.root_path if ws_config and ws_config.root_path else None
        if not ws_path:
            ws_path = str(team_home(spec.team_name) / "team-workspace")

        team_shared_skills_dir = Path(ws_path) / "skills"

        # Check if already copied (via marker file)
        copied_marker = team_shared_skills_dir / ".team_skills_copied"
        if copied_marker.exists():
            logger.info("[TeamManager] Team shared skills already copied, skipping")
            return

        # Copy entire skills directory (including skills_state.json)
        team_shared_skills_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(global_skills_dir, team_shared_skills_dir, dirs_exist_ok=True)

        # Write marker file to indicate copy completed
        copied_marker.write_text("", encoding="utf-8")
        logger.info("[TeamManager] Copied global skills dir to team shared: %s", team_shared_skills_dir)

    async def create_team(
        self,
        session_id: str,
        deep_agent: DeepAgent,
        request_id: str | None = None,
        channel_id: str | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> TeamAgent:
        logger.info(format_session_log(session_id, "[TeamManager] building TeamAgentSpec: session_id=%s"), session_id)
        spec = self._load_team_spec(session_id)
        deleted_tables, cleared_tables = await self._cleanup_team_runtime_state(spec)
        if deleted_tables or cleared_tables:
            logger.info(
                "[TeamManager] pre-create cleanup deleted dynamic tables=%s cleared static tables=%s",
                deleted_tables,
                cleared_tables,
            )

        spec.agent_customizer = self.build_agent_customizer(
            spec,
            deep_agent,
            session_id,
            request_id=request_id,
            channel_id=channel_id,
            request_metadata=request_metadata,
        )

        logger.info("[TeamManager] TeamAgentSpec ready: team_name=%s", spec.team_name)

        token = set_session_id(session_id)
        try:
            logger.info("[TeamManager] creating TeamAgent from spec")
            team_agent = spec.build()
            self._team_agents[session_id] = team_agent

            # After build, copy global skills to team shared directory (only once)
            self._copy_global_skills_to_team_shared_dir(spec)

            logger.info(
                format_session_log(
                    session_id,
                    f"[TeamManager] Team created: session_id={session_id} team_name={spec.team_name}"
                )
            )
            return team_agent
        finally:
            reset_session_id(token)

    async def get_or_create_team(
        self,
        session_id: str,
        deep_agent: DeepAgent,
        request_id: str | None = None,
        channel_id: str | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> TeamAgent:
        async with self._lock:
            team_agent = self._team_agents.get(session_id)
            if team_agent is not None:
                return team_agent

            await self._destroy_other_sessions(session_id)
            return await self.create_team(
                session_id,
                deep_agent,
                request_id,
                channel_id,
                request_metadata,
            )

    async def interact(self, session_id: str, user_input: str) -> bool:
        team_agent = self._team_agents.get(session_id)
        if team_agent is None:
            logger.warning(format_session_log(session_id, "[TeamManager] interact failed, missing team"))
            return False

        try:
            await team_agent.interact(user_input)
            logger.debug(format_session_log(session_id, "[TeamManager] interact sent"))
            return True
        except Exception as exc:
            logger.error(format_session_log(session_id, f"[TeamManager] interact failed: error={exc}"))
            return False

    async def destroy_team(self, session_id: str) -> bool:
        async with self._lock:
            return await self._destroy_team(session_id)

    async def _destroy_other_sessions(self, current_session_id: str) -> None:
        stale_session_ids = [sid for sid in list(self._team_agents.keys()) if sid != current_session_id]
        for stale_session_id in stale_session_ids:
            await self._destroy_team(stale_session_id)

    async def _destroy_team(self, session_id: str) -> bool:
        stream_task = self._stream_tasks.pop(session_id, None)
        if stream_task and not stream_task.done():
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    format_session_log(session_id, f"[TeamManager] stream stop failed: error={exc}")
                )

        monitor_handler = self._team_monitors.pop(session_id, None)
        if monitor_handler is not None:
            try:
                await monitor_handler.stop()
            except Exception as exc:
                logger.warning(
                    format_session_log(session_id, f"[TeamManager] monitor stop failed: error={exc}")
                )

        team_agent = self._team_agents.pop(session_id, None)
        cleaned = False
        cleanup_spec: TeamAgentSpec | None = None
        try:
            cleanup_spec = self._load_team_spec(session_id)
            if team_agent is None:
                logger.info(
                    format_session_log(session_id, "[TeamManager] no in-memory team, run runtime cleanup fallback only")
                )
                return False

            token = set_session_id(session_id)
            try:
                cleaned = await team_agent.destroy_team(force=True)
            finally:
                reset_session_id(token)

            logger.info(
                format_session_log(session_id, f"[TeamManager] Team cleaned via core API: cleaned={cleaned}")
            )
        except Exception as exc:
            logger.error(
                format_session_log(session_id, f"[TeamManager] destroy team failed: error={exc}")
            )
        finally:
            if cleanup_spec is None:
                try:
                    cleanup_spec = self._load_team_spec(session_id)
                except Exception as exc:
                    logger.warning(
                        format_session_log(session_id,
                                           f"[TeamManager] failed to rebuild team spec for cleanup: error={exc}")
                    )
                    cleanup_spec = None
            deleted_tables: list[str] = []
            cleared_tables: list[str] = []
            if cleanup_spec is not None:
                deleted_tables, cleared_tables = await self._cleanup_team_runtime_state(cleanup_spec)
            if deleted_tables or cleared_tables:
                logger.info(
                    "[TeamManager] fallback cleanup after destroy deleted dynamic tables=%s "
                    "cleared static tables=%s",
                    deleted_tables,
                    cleared_tables,
                )

        return cleaned

    async def cleanup_all(self) -> None:
        async with self._lock:
            session_ids = list(self._team_agents.keys())
            for session_id in session_ids:
                await self._destroy_team(session_id)
            logger.info("[TeamManager] all teams cleaned")

    def get_team_agent(self, session_id: str) -> TeamAgent | None:
        return self._team_agents.get(session_id)

    def register_monitor(self, session_id: str, handler: TeamMonitorHandler) -> None:
        self._team_monitors[session_id] = handler

    def register_stream_task(self, session_id: str, task: asyncio.Task) -> None:
        self._stream_tasks[session_id] = task

    async def cancel_all_stream_tasks(self, reason: str = "") -> None:
        """Gateway 与 AgentServer 断开时取消 Team 后台 stream 协程（含 create_task 绕开 SessionManager 的任务）。"""
        async with self._lock:
            pending = list(self._stream_tasks.items())
        for session_id, task in pending:
            if task.done():
                continue
            logger.info(
                format_session_log(session_id, f"[TeamManager] {reason}cancel stream task")
            )
            task.cancel()
        for session_id, task in pending:
            if task.done():
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    format_session_log(session_id, f"[TeamManager] stream task await after cancel failed: {exc}")
                )
        async with self._lock:
            self._stream_tasks.clear()


_team_manager: TeamManager | None = None


def get_team_manager() -> TeamManager:
    global _team_manager
    if _team_manager is None:
        _team_manager = TeamManager()
    return _team_manager


def reset_team_manager() -> None:
    global _team_manager
    _team_manager = None
