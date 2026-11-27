# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuWenClaw Facade - 统一入口与 SDK 适配层.

此模块提供：
- 统一的 JiuWenClaw 公开 API
- SDK 工厂路由（通过环境变量选择）
- 公共编排逻辑（session 队列、Skills 路由、heartbeat、流式包装）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, AsyncIterator, Tuple

from dotenv import load_dotenv

from jiuwenclaw.agentserver.agent_adapters import (
    AgentAdapter,
    create_adapter,
    resolve_sdk_choice,
)
from jiuwenclaw.agentserver.memory.config import get_memory_mode
from jiuwenclaw.agentserver.session_history import append_history_record
from jiuwenclaw.agentserver.session_manager import SessionManager
from jiuwenclaw.agentserver.session_metadata import (
    _safe_session_subdir,
    get_resolved_project_dir,
    update_session_metadata,
    validate_project_dir,
)
from jiuwenclaw.agentserver.skill_manager import SkillManager
from jiuwenclaw.config import get_config
from jiuwenclaw.utils import format_session_log
from jiuwenclaw.extensions.registry import ExtensionRegistry
from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.hook_event import AgentServerHookEvents
from jiuwenclaw.schema.hooks_context import MemoryHookContext
from jiuwenclaw.schema.message import EventType, ReqMethod
from jiuwenclaw.utils import (
    get_agent_home_dir,
    get_agent_sessions_relative_dir,
    get_agent_workspace_dir,
    get_agent_workspace_relative_dir,
    get_env_file,
    get_user_workspace_dir,
)

load_dotenv(dotenv_path=get_env_file())

logger = logging.getLogger(__name__)

# _session_project_dir 进程内缓存上限（超出则淘汰最久未访问；磁盘 metadata 仍为权威来源）
_SESSION_PROJECT_DIR_CACHE_MAX = 4096

_SKILL_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.SKILLS_LIST: "handle_skills_list",
    ReqMethod.SKILLS_INSTALLED: "handle_skills_installed",
    ReqMethod.SKILLS_GET: "handle_skills_get",
    ReqMethod.SKILLS_MARKETPLACE_LIST: "handle_skills_marketplace_list",
    ReqMethod.SKILLS_INSTALL: "handle_skills_install",
    ReqMethod.SKILLS_UNINSTALL: "handle_skills_uninstall",
    ReqMethod.SKILLS_IMPORT_LOCAL: "handle_skills_import_local",
    ReqMethod.SKILLS_MARKETPLACE_ADD: "handle_skills_marketplace_add",
    ReqMethod.SKILLS_MARKETPLACE_REMOVE: "handle_skills_marketplace_remove",
    ReqMethod.SKILLS_MARKETPLACE_TOGGLE: "handle_skills_marketplace_toggle",
    ReqMethod.SKILLS_SKILLNET_SEARCH: "handle_skills_skillnet_search",
    ReqMethod.SKILLS_SKILLNET_INSTALL: "handle_skills_skillnet_install",
    ReqMethod.SKILLS_SKILLNET_INSTALL_STATUS: "handle_skills_skillnet_install_status",
    ReqMethod.SKILLS_SKILLNET_EVALUATE: "handle_skills_skillnet_evaluate",
    ReqMethod.SKILLS_CLAWHUB_GET_TOKEN: "handle_skills_clawhub_get_token",
    ReqMethod.SKILLS_CLAWHUB_SET_TOKEN: "handle_skills_clawhub_set_token",
    ReqMethod.SKILLS_CLAWHUB_SEARCH: "handle_skills_clawhub_search",
    ReqMethod.SKILLS_CLAWHUB_DOWNLOAD: "handle_skills_clawhub_download",
    ReqMethod.SKILLS_EVOLUTION_STATUS: "handle_skills_evolution_status",
    ReqMethod.SKILLS_EVOLUTION_GET: "handle_skills_evolution_get",
    ReqMethod.SKILLS_EVOLUTION_SAVE: "handle_skills_evolution_save",
}

# Tools 管理请求路由表
_TOOL_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.TOOLS_ADD: "handle_tools_add",
}

# SkillDev 请求方法集合（统一委托给 SkillDevService）
_SKILLDEV_METHODS: frozenset[ReqMethod] = frozenset(
    m for m in ReqMethod
    if m.value.startswith("skilldev.") and m != ReqMethod.SKILLDEV_CANCEL
)

# Preserve cross-service context-size hints if they are present in request.params.
_CONTEXT_SIZE_HINT_KEYS: tuple[str, ...] = (
    "context_size",
    "context_window_size",
    "context_window",
    "max_context",
    "max_context_size",
    "max_context_message_num",
    "max_input_tokens",
    "max_prompt_tokens",
    "n_ctx",
    "ctx_len",
)


def build_user_prompt(content: str, files: dict | list, channel: str, language: str) -> str:
    """Build user prompt for the agent."""
    if language == "zh":
        prompt = "你收到一条消息：\n"
    else:
        prompt = "You receive a new message:\n"
    if channel in ["cron", "heartbeat"]:
        return prompt + json.dumps(
            {
                "source": "system",
                "preferred_response_language": language,
                "content": content,
                "type": channel,
            },
            ensure_ascii=False,
        )
    # 兼容 files 为 dict 或 list 格式
    # 空容器统一输出 "{}"（对 Agent 来说空 dict 和空 list 都表示无文件）
    files_json = json.dumps(files, ensure_ascii=False) if files else "{}"
    return prompt + json.dumps(
        {
            "source": channel,
            "preferred_response_language": language,
            "content": content,
            "files_updated_by_user": files_json,
            "type": "user input",
        },
        ensure_ascii=False,
    )


class JiuWenClaw:
    """JiuWenClaw 统一门面.

    提供：
    - SDK 工厂路由
    - 统一对外 API（create_instance, reload_agent_config, process_message, process_message_stream）
    - 公共编排（session 队列、Skills / Tools 路由、heartbeat、流式包装）
    """

    def __init__(
            self,
            user_workspace_dir: str | None = None,
            agent_id: str | None = None,
            service_id: str | None = None,
    ) -> None:
        self._adapter: AgentAdapter | None = None
        self._skilldev_adapter: AgentAdapter | None = None
        self._sdk_name: str | None = None
        self._agent_id = agent_id
        self._service_id = service_id
        user_workspace_path = Path(user_workspace_dir) if user_workspace_dir else get_user_workspace_dir()
        self._workspace_dir = str(user_workspace_path / get_agent_workspace_relative_dir())
        self._sessions_dir = user_workspace_path / get_agent_sessions_relative_dir()
        self._skill_manager = SkillManager(workspace_dir=self._workspace_dir)
        self._session_manager = SessionManager()
        # session_id -> 已解析的 project_dir（与 metadata 绑定，见 _effective_project_dir_for_session）
        self._session_project_dir: OrderedDict[str, str] = OrderedDict()
        # SkillDev 模式：懒初始化，首次 skilldev.* 请求时构造
        self._skilldev_service = None
        self._tool_manager = None
        # 延迟初始化配置：create_instance 保存，_ensure_adapter 首次触发时消费
        self._pending_config: dict[str, Any] | None = None
        self._pending_mode: str = ""

    def _get_skilldev_service(self):
        """懒初始化并返回 SkillDevService 实例.

        SkillDevService 是无状态的，单实例即可服务所有请求。
        首次调用时从当前 JiuWenClaw 配置中提取最小依赖并构造。
        """
        if self._skilldev_service is not None:
            return self._skilldev_service

        from jiuwenclaw.agentserver.skilldev import SkillDevDeps, SkillDevService, StateStore, WorkspaceProvider
        from jiuwenclaw.agentserver.skilldev.session_history import SkillDevSessionHistoryService
        from jiuwenclaw.utils import get_workspace_dir

        # skilldev_base = get_workspace_dir() / "skilldev"
        skilldev_base = Path(self._workspace_dir) / "skilldev"
        state_store = StateStore(skilldev_base)
        workspace_provider = WorkspaceProvider(skilldev_base)

        config = get_config()
        model_configs = config.get("models", {})
        default_model = model_configs.get("default", {})

        deps = SkillDevDeps(
            model_name=default_model.get("model_client_config", {}).get("model_name", ""),
            model_client_config=default_model.get("model_client_config", {}),
            model_config_obj=default_model.get("model_config_obj", {}),
            sysop_config=None,
            state_store=state_store,
            workspace_provider=workspace_provider,
            session_history=SkillDevSessionHistoryService(base_dir=skilldev_base, state_store=state_store),
        )
        self._skilldev_service = SkillDevService(deps)
        logger.info("[JiuWenClaw] SkillDevService 初始化完成")
        return self._skilldev_service

    async def _ensure_adapter(self, create_instance=True) -> AgentAdapter:
        """确保 adapter 已初始化，如果未初始化则根据环境变量创建."""
        if self._adapter is None:
            self._sdk_name = resolve_sdk_choice()
            self._adapter = await create_adapter(
                self._sdk_name,
                workspace_dir=self._workspace_dir,
                agent_id=self._agent_id,
                service_id=self._service_id,
            )
            if hasattr(self._adapter, "set_skill_manager"):
                self._adapter.set_skill_manager(self._skill_manager)
            self._skill_manager.set_skillnet_install_complete_hook(
                self.create_instance
            )
            # 若 create_instance 已保存 pending 配置，首次初始化时消费
            if (self._pending_config is not None or self._pending_mode) and create_instance:
                await self._adapter.create_instance(self._pending_config, mode=self._pending_mode)
            logger.info(
                "[session=%s] [JiuWenClaw] Initialized adapter: sdk=%s, workspace_dir=%s",
                self._service_id,
                self._sdk_name,
                self._workspace_dir
            )
        return self._adapter

    async def _ensure_skilldev_adapter(self) -> AgentAdapter:
        """确保 skilldev adapter 已初始化，懒加载创建."""
        if self._skilldev_adapter is None:
            from pathlib import Path

            from jiuwenclaw.agentserver.skilldev import StateStore
            from jiuwenclaw.agentserver.skilldev.session_history import SkillDevSessionHistoryService

            self._skilldev_adapter = await create_adapter(
                "skilldev",
                workspace_dir=self._workspace_dir,
                agent_id=self._agent_id,
                service_id=self._service_id,
            )
            if hasattr(self._skilldev_adapter, "set_skill_manager"):
                self._skilldev_adapter.set_skill_manager(self._skill_manager)
            skilldev_base = Path(self._workspace_dir) / "skilldev"
            state_store = StateStore(skilldev_base)
            session_history = SkillDevSessionHistoryService(
                base_dir=skilldev_base,
                state_store=state_store,
            )
            if hasattr(self._skilldev_adapter, "set_session_history"):
                self._skilldev_adapter.set_session_history(session_history)
            logger.info(
                "[session=%s] [JiuWenClaw] Initialized skilldev adapter: workspace_dir=%s",
                self._service_id,
                self._workspace_dir
            )
        return self._skilldev_adapter

    def _get_tool_manager(self):
        """懒初始化 ``ToolManager``（依赖 ``get_instance`` 上的底层 Agent）。"""
        from jiuwenclaw.agentserver.tool_manager import ToolManager

        if self._tool_manager is None:
            self._tool_manager = ToolManager(
                get_agent=lambda: self.get_instance(),
                get_tools_dir=lambda: Path(self._workspace_dir) / "tools",
            )
        return self._tool_manager

    def _session_project_dir_get(self, session_id: str) -> str | None:
        d = self._session_project_dir
        if session_id not in d:
            return None
        d.move_to_end(session_id)
        return d[session_id]

    def _session_project_dir_set(self, session_id: str, value: str) -> None:
        d = self._session_project_dir
        d.pop(session_id, None)
        d[session_id] = value
        while len(d) > _SESSION_PROJECT_DIR_CACHE_MAX:
            d.popitem(last=False)

    def _effective_project_dir_for_session(
            self,
            session_id: str,
            param_project_dir: str | None,
    ) -> str:
        """解析本会话工程目录：首次非空 params.project_dir 绑定并落 metadata；冲突保留首次。"""
        raw = (param_project_dir or "").strip()
        sessions_root = str(self._sessions_dir)
        default = str(get_agent_workspace_dir().resolve())
        can_persist = _safe_session_subdir(session_id, sessions_root) is not None
        if not raw:
            existing = self._session_project_dir_get(session_id)
            if existing is not None:
                return existing
            if not can_persist:
                logger.warning(
                    format_session_log(
                        session_id,
                        "[JiuWenClaw] 跳过 metadata 读取 project_dir：非法 session_id",
                    )
                )
                return default
            loaded = get_resolved_project_dir(session_id, sessions_root)
            self._session_project_dir_set(session_id, loaded)
            return loaded

        resolved = str(validate_project_dir(raw, default=Path(default)))
        existing = self._session_project_dir_get(session_id)
        if existing is None:
            self._session_project_dir_set(session_id, resolved)
            if can_persist:
                update_session_metadata(
                    session_id=session_id,
                    project_dir=resolved,
                    sessions_root=self._sessions_dir,
                )
            else:
                logger.warning(
                    format_session_log(
                        session_id,
                        "[JiuWenClaw] 跳过 metadata 写入 project_dir：非法 session_id",
                    )
                )
            return resolved
        if existing == resolved:
            if can_persist:
                update_session_metadata(
                    session_id=session_id,
                    project_dir=existing,
                    sessions_root=self._sessions_dir,
                )
            return existing
        logger.warning(
            format_session_log(
                session_id,
                f"[JiuWenClaw] 忽略冲突的 project_dir：保留 {existing!r}，收到 {resolved!r}",
            )
        )
        return existing

    def _apply_effective_project_dir_to_request(
            self,
            request: AgentRequest,
            session_id: str,
            inputs: dict[str, Any],
    ) -> None:
        """写入 effective_project_dir 到 request.metadata 与 inputs。"""
        param = None
        if isinstance(request.params, dict):
            pd = request.params.get("project_dir")
            if isinstance(pd, str):
                param = pd
        effective = self._effective_project_dir_for_session(session_id, param)
        md = dict(request.metadata) if isinstance(request.metadata, dict) else {}
        md["effective_project_dir"] = effective
        request.metadata = md
        inputs["effective_project_dir"] = effective

    async def create_instance(self, config: dict[str, Any] | None = None, *, mode: str = "agent") -> None:
        """初始化 Agent 实例.

        Args:
            config: 可选配置，透传给底层 adapter.
            mode: 实例化模式，"claw"（默认）或 "code"，透传给底层 adapter.
        """
        # 保存配置供 _ensure_adapter 首次触发时消费，避免 SKILLDEV-only 场景下多余初始化 _adapter
        self._pending_config = config
        self._pending_mode = mode
        logger.info("[JiuWenClaw] Agent instance base created, adapter deferred")

        project_mcp_names: set[str] = set()
        host_project_mcp_path = self._get_tool_manager().find_host_project_mcp_json()
        try:
            if host_project_mcp_path is None:
                logger.info(
                    "[JiuWenClaw] 未找到宿主项目 .mcp.json，跳过 MCP 工具导入: CAT_CAFE_MCP_CWD=%s",
                    os.getenv("CAT_CAFE_MCP_CWD", ""),
                )
            else:
                project_mcp_payload = await self._get_tool_manager().load_project_mcp_json(host_project_mcp_path)
                project_mcp_names = {
                    item["name"]
                    for item in project_mcp_payload.get("saved", [])
                    if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
                }
                if not project_mcp_payload.get("skipped"):
                    logger.info(
                        "[JiuWenClaw] 已从宿主项目 .mcp.json 导入 MCP 工具: count=%s source=%s",
                        len(project_mcp_names),
                        project_mcp_payload.get("source", str(host_project_mcp_path)),
                    )
        except Exception as exc:
            logger.warning("[JiuWenClaw] 从宿主项目 .mcp.json 导入 MCP 工具失败: %s", exc)

        try:
            await self._get_tool_manager().load_tools_from_disk(skip_server_names=project_mcp_names)
        except Exception as exc:
            logger.warning("[JiuWenClaw] 从 agent/tools 加载落盘 MCP 工具失败: %s", exc)
        self._register_extension_tools()

    def _register_extension_tools(self) -> None:
        """将 ExtensionRegistry 登记的扩展本地工具挂到 Runner 与 ability_manager。"""
        instance = self.get_instance()
        if instance is None:
            return
        try:
            from openjiuwen.core.foundation.tool import ToolCard
            from openjiuwen.core.runner import Runner

            from jiuwenclaw.extensions.sdk.local_tool_builder import make_extension_tools

            ext_reg = ExtensionRegistry.get_instance()
            existing_ext_tool_names: set[str] = {
                ab.name
                for ab in instance.ability_manager.list()
                if isinstance(ab, ToolCard)
            }
            for ext_tool in make_extension_tools(ext_reg.extension_local_tool_entries):
                tname = ext_tool.card.name
                tid = ext_tool.card.id
                if tname in existing_ext_tool_names:
                    logger.warning(
                        "[JiuWenClaw] extension tool name conflicts with existing tool, skip: %s",
                        tname,
                    )
                    continue
                try:
                    if Runner.resource_mgr.get_tool(tid):
                        logger.warning(
                            "[JiuWenClaw] extension tool id already in resource_mgr, skip: %s",
                            tid,
                        )
                        continue
                except Exception as exc:
                    logger.debug(
                        "[JiuWenClaw] extension tool get_tool probe failed for %s, will try add: %s",
                        tid,
                        exc,
                    )
                try:
                    Runner.resource_mgr.add_tool(ext_tool)
                    instance.ability_manager.add(ext_tool.card)
                    existing_ext_tool_names.add(tname)
                    logger.info(
                        "[JiuWenClaw] extension tool add success, name : %s", tname,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClaw] extension tool register failed (%s): %s",
                        tname,
                        exc,
                    )
        except RuntimeError:
            logger.warning("[JiuWenClaw] ExtensionRegistry unavailable, skip extension tools")

    async def reload_agent_config(
            self,
            config_base: dict[str, Any] | None = None,
            env_overrides: dict[str, Any] | None = None,
    ) -> None:
        """从配置重新加载.

        Args:
            config_base: 可选的完整配置快照；传入时优先使用它而不是读取本地 config.yaml。
            env_overrides: 可选的环境变量增量；仅覆盖请求中出现的 key。
        """
        adapter = await self._ensure_adapter()
        await adapter.reload_agent_config(config_base, env_overrides)
        logger.info("[JiuWenClaw] Agent config reloaded: sdk=%s", self._sdk_name)

    def _build_inputs(self, request: AgentRequest) -> Tuple[dict[str, Any], str]:
        """构建 adapter 所需的 inputs 字典."""
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

        config_base = get_config()
        memory_mode = get_memory_mode(config_base)
        query = request.params.get("query", "")
        channel = request.session_id.split('_')[0] if request.session_id else "web"
        language = config_base.get("preferred_language", "zh")

        if isinstance(query, InteractiveInput):
            final_query = query
        else:
            answers = request.params.get("answers", [])
            if answers:
                request_id = request.params.get("request_id", "")
                interactive_input = self._build_interactive_input_from_answers(request_id, answers)
                final_query = interactive_input if interactive_input is not None else build_user_prompt(
                    query,
                    files=request.params.get("files", {}),
                    channel=channel,
                    language=language
                )
            else:
                final_query = build_user_prompt(
                    query,
                    files=request.params.get("files", {}),
                    channel=channel,
                    language=language
                )

        inputs: dict[str, Any] = {
            "conversation_id": request.session_id,
            "query": final_query,
            "channel": channel,
            "language": language,
        }

        # 传递 enable_memory 参数
        enable_memory = request.metadata.get("enable_memory", True) if request.metadata else True
        inputs["enable_memory"] = enable_memory

        run = request.params.get("run")
        if run:
            inputs["run"] = run

        for key in _CONTEXT_SIZE_HINT_KEYS:
            value = request.params.get(key)
            if value is not None:
                inputs[key] = value

        # 返回原始 query（未经 build_user_prompt 包装）
        # Team 模式需要使用原始 query，而不是 JSON 包装后的 prompt
        return inputs, memory_mode, query

    def _build_interactive_input_from_answers(
            self, request_id: str, answers: list[dict]
    ) -> Any:
        """从用户答案构建 InteractiveInput.

        Args:
            request_id: 工具调用 ID
            answers: 用户答案列表，每个答案对应一个问题

        Returns:
            InteractiveInput 实例
        """
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

        interactive_input = InteractiveInput()

        answer = answers[0] if answers else {}
        selected_options = answer.get("selected_options", []) if isinstance(answer, dict) else []
        custom_input = answer.get("custom_input", "") if isinstance(answer, dict) else ""

        if "本次允许" in selected_options:
            confirm_payload = {"approved": True, "auto_confirm": False, "feedback": ""}
        elif "总是允许" in selected_options:
            confirm_payload = {
                "approved": True,
                "auto_confirm": True,
                "persist_allow": True,
                "feedback": "",
            }
        elif "拒绝" in selected_options:
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": custom_input or "用户拒绝"}
        else:
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": "未知选项"}

        interactive_input.update(request_id, confirm_payload)
        logger.info(
            "[JiuWenClaw] InteractiveInput.update: request_id=%s approved=%s auto_confirm=%s",
            request_id,
            confirm_payload.get("approved"),
            confirm_payload.get("auto_confirm"),
        )

        return interactive_input

    async def _handle_skilldev_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 SkillDev 相关请求，返回 None 表示不是 SkillDev 请求."""
        if request.req_method not in _SKILLDEV_METHODS:
            return None

        service = self._get_skilldev_service()
        try:
            chunks = []
            async for chunk in service.handle(request):
                chunks.append(chunk)
            final = chunks[-1] if chunks else None
            payload = final.payload if final else {}
        except Exception as exc:
            logger.error(format_session_log(request.session_id, f"[JiuWenClaw] skilldev 请求处理失败: {exc}"))
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_skills_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 Skills 相关请求，返回 None 表示不是 Skills 请求."""
        if request.req_method not in _SKILL_ROUTES:
            return None

        handler_name = _SKILL_ROUTES[request.req_method]
        handler = getattr(self._skill_manager, handler_name)
        try:
            payload = await handler(request.params)
            _reload_after_skills = handler_name in [
                "handle_skills_install",
                "handle_skills_uninstall",
                "handle_skills_import_local",
                "handle_skills_skillnet_install",
                "handle_skills_clawhub_download",
            ]
            if handler_name == "handle_skills_skillnet_install" and payload.get("pending"):
                _reload_after_skills = False
            if _reload_after_skills:
                await self.create_instance()
        except Exception as exc:
            logger.error(format_session_log(request.session_id, f"[JiuWenClaw] skills 请求处理失败: {exc}"))
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_tools_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 tools.* 相关请求，返回 None 表示不是 Tools 管理请求."""
        if request.req_method not in _TOOL_ROUTES:
            return None

        handler_name = _TOOL_ROUTES[request.req_method]
        tm = self._get_tool_manager()
        handler = getattr(tm, handler_name)
        try:
            payload = await handler(request.params)
        except Exception as exc:
            logger.error(format_session_log(request.session_id, f"[JiuWenClaw] tools 请求处理失败: {exc}"))
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _process_interrupt(self, request: AgentRequest) -> AgentResponse:
        """处理 interrupt 请求.

        根据 intent 分流：
        - pause: 暂停 ReAct 循环（不取消任务）
        - resume: 恢复已暂停的 ReAct 循环
        - cancel: 取消所有运行中的任务
        - supplement: 取消当前任务但保留 todo

        Args:
            request: AgentRequest，params 中可包含：
                - intent: 中断意图 ('pause' | 'cancel' | 'resume' | 'supplement')
                - new_input: 新的用户输入（用于切换任务）

        Returns:
            AgentResponse 包含 interrupt_result 事件数据
        """
        intent = request.params.get("intent", "cancel")
        is_skilldev = request.req_method == ReqMethod.SKILLDEV_CANCEL
        logger.info(
            "[session=%s] [interrupt] received: intent=%s is_skilldev=%s",
            request.session_id,
            intent,
            is_skilldev,
        )
        if is_skilldev:
            adapter = await self._ensure_skilldev_adapter()
        else:
            adapter = await self._ensure_adapter()
        response = await adapter.process_interrupt(request)

        if intent == "pause":
            # 暂停：不取消任务，只暂停 ReAct 循环
            logger.info("[session=%s] [interrupt] paused ReAct loop", request.session_id)
            return response

        if intent == "resume":
            # 恢复：恢复 ReAct 循环
            logger.info("[session=%s] [interrupt] resumed ReAct loop", request.session_id)
            return response

        if intent == "supplement":
            # 取消当前 session 的任务
            session_id = self._session_manager.get_session_id(request.session_id)
            logger.info(
                "[session=%s] [interrupt] supplement: cancelling current session task",
                request.session_id,
            )
            await self._session_manager.cancel_session_task(session_id, "interrupt(supplement): ")
            # SkillDev 场景下额外清理 adapter 资源
            if is_skilldev and adapter is not None:
                try:
                    if hasattr(adapter, "cleanup_task_resources"):
                        adapter.cleanup_task_resources()
                except Exception as exc:
                    logger.warning(
                        "[session=%s] [interrupt] adapter cleanup_task_resources failed: %s",
                        request.session_id,
                        exc,
                    )
            return response

        # cancel: 取消所有 session 的任务
        logger.info(
            "[session=%s] [interrupt] cancel: cancelling all session tasks (intent=%s)",
            request.session_id,
            intent,
        )
        await self._session_manager.cancel_all_session_tasks(f"interrupt(intent={intent}): ")
        # SkillDev 场景下额外清理 adapter 资源
        if is_skilldev and adapter is not None:
            try:
                if hasattr(adapter, "cleanup_task_resources"):
                    adapter.cleanup_task_resources()
            except Exception as exc:
                logger.warning(
                    "[session=%s] [interrupt] adapter cleanup_task_resources failed: %s",
                    request.session_id,
                    exc,
                )
        return response

    async def process_message(self, request: AgentRequest) -> AgentResponse:
        """处理非流式请求.

        支持多 session 并发执行，同 session 内任务按先进后出顺序执行.
        """
        if request.req_method in (ReqMethod.CHAT_CANCEL, ReqMethod.SKILLDEV_CANCEL):
            return await self._process_interrupt(request)

        if request.req_method == ReqMethod.SKILLDEV_ANSWER:
            skilldev_adapter = await self._ensure_skilldev_adapter()
            return await skilldev_adapter.handle_user_answer(request)

        if request.req_method == ReqMethod.CHAT_ANSWER:
            adapter = await self._ensure_adapter()
            return await adapter.handle_user_answer(request)

        if str(request.session_id or "").startswith("heartbeat"):
            adapter = await self._ensure_adapter(create_instance=False)
            heartbeat_response = await adapter.handle_heartbeat(request)
            if heartbeat_response is not None:
                return heartbeat_response

        skilldev_response = await self._handle_skilldev_request(request)
        if skilldev_response is not None:
            return skilldev_response

        skills_response = await self._handle_skills_request(request)
        if skills_response is not None:
            return skills_response

        tools_response = await self._handle_tools_request(request)
        if tools_response is not None:
            return tools_response
        adapter = await self._ensure_adapter()
        session_id = self._session_manager.get_session_id(request.session_id)
        query = request.params.get("query", "")
        append_history_record(
            session_id=session_id,
            request_id=request.request_id,
            channel_id=request.channel_id,
            role="user",
            content=query,
            timestamp=time.time(),
            channel_metadata=request.metadata,
            mode=request.params.get("mode", "unknown"),
            sessions_root=self._sessions_dir,
        )

        logger.info(
            format_session_log(
                session_id,
                f"[JiuWenClaw] 处理请求: request_id={request.request_id} "
                f"channel_id={request.channel_id} sdk={self._sdk_name}",
            )
        )

        inputs, memory_mode, raw_query = self._build_inputs(request)
        self._apply_effective_project_dir_to_request(request, session_id, inputs)

        # cloud memory: before chat hook
        if memory_mode == "cloud":
            mem_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                extra=request.params,
            )
            await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_BEFORE_CHAT, mem_ctx)
            memory_block = "\n\n".join(b for b in mem_ctx.memory_blocks if b)
            inputs["memory_block"] = memory_block

        async def run_agent_task():
            # 注册 Cat Cafe MCP（请求级环境变量）
            cat_cafe_mcp = request.params.get("cat_cafe_mcp")
            if isinstance(cat_cafe_mcp, dict):
                try:
                    await self._get_tool_manager().register_request_scoped_cat_cafe_mcp(cat_cafe_mcp)
                except Exception as exc:
                    logger.warning(format_session_log(session_id, f"[JiuWenClaw] cat_cafe_mcp 注册失败: {exc}"))
            return await adapter.process_message_impl(request, inputs)

        result = await self._session_manager.submit_and_wait(session_id, run_agent_task)

        if result.ok and result.payload.get("content"):
            content = result.payload["content"]
            content_str = content if isinstance(content, str) else str(content)
            append_history_record(
                session_id=session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                role="assistant",
                event_type="chat.final",
                content=content_str,
                timestamp=time.time(),
                mode=request.params.get("mode", "unknown"),
                sessions_root=self._sessions_dir,
            )

            # cloud memory: after chat hook
            if memory_mode == "cloud":
                after_ctx = MemoryHookContext(
                    session_id=request.session_id or "default",
                    request_id=request.request_id or "",
                    channel_id=request.channel_id,
                    agent_name="main_agent",
                    workspace_dir=str(get_agent_home_dir()),
                    assistant_message=content_str,
                    extra=request.params,
                )
                await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_AFTER_CHAT, after_ctx)

        return result

    async def process_message_stream(
            self, request: AgentRequest
    ) -> AsyncIterator[AgentResponseChunk]:
        """处理流式请求.

        支持多 session 并发执行，同 session 内任务按先进后出顺序执行.
        """
        if request.req_method == ReqMethod.SKILLDEV_CHAT:
            adapter = await self._ensure_skilldev_adapter()
            try:
                async for chunk in adapter.handle_skilldev_chat_stream(request):
                    yield chunk
            except Exception as exc:
                logger.error("[JiuWenClaw] skilldev.chat 流式请求处理失败: %s", exc)
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "skilldev.error", "error": str(exc)},
                    is_complete=True,
                )
            return

        # SkillDev 流式请求：直接委托给 SkillDevService，绕过 ReActAgent
        if request.req_method in _SKILLDEV_METHODS:
            service = self._get_skilldev_service()
            try:
                async for chunk in service.handle(request):
                    yield chunk
            except Exception as exc:
                logger.error(format_session_log(request.session_id, f"[JiuWenClaw] skilldev 流式请求处理失败: {exc}"))
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "skilldev.error", "error": str(exc)},
                    is_complete=True,
                )
            return

        adapter = await self._ensure_adapter()

        session_id = self._session_manager.get_session_id(request.session_id)
        query = request.params.get("query", "")

        mode = request.params.get("mode", "") if isinstance(request.params, dict) else ""
        team_flag = request.params.get("team", False) if isinstance(request.params, dict) else False
        is_team_mode = team_flag or (isinstance(mode, str) and mode.strip().lower() == "team")

        append_history_record(
            session_id=session_id,
            request_id=request.request_id,
            channel_id=request.channel_id,
            role="user",
            content=query,
            timestamp=time.time(),
            channel_metadata=request.metadata,
            mode=request.params.get("mode", "unknown"),
            sessions_root=self._sessions_dir,
        )

        logger.info(
            format_session_log(
                session_id,
                f"[JiuWenClaw] 处理流式请求: request_id={request.request_id} "
                f"channel_id={request.channel_id} sdk={self._sdk_name}",
            )
        )

        inputs, memory_mode, raw_query = self._build_inputs(request)
        self._apply_effective_project_dir_to_request(request, session_id, inputs)
        rid = request.request_id
        cid = request.channel_id

        # Team 模式：使用原始 query，而不是 build_user_prompt 包装后的内容
        if is_team_mode:
            inputs["query"] = raw_query
            logger.info(
                format_session_log(
                    session_id,
                    f"[JiuWenClaw] Team模式使用原始query: {raw_query[:100] if raw_query else ''}",
                )
            )

        # cloud memory: before chat hook
        if memory_mode == "cloud":
            mem_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                extra=request.params,
            )
            await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_BEFORE_CHAT, mem_ctx)
            memory_block = "\n\n".join(b for b in mem_ctx.memory_blocks if b)
            inputs["memory_block"] = memory_block

        # Team 模式: 检查是否是后续请求（需要绕过 Session Manager）
        is_team_first_request = True
        if is_team_mode:
            from jiuwenclaw.agentserver.team import get_team_manager
            team_manager = get_team_manager()
            is_team_first_request = not team_manager.has_stream_task(session_id)
            logger.info(
                format_session_log(
                    session_id,
                    f"[JiuWenClaw] Team模式: is_first={is_team_first_request}",
                )
            )

        stream_queue = asyncio.Queue()
        stream_done = asyncio.Event()
        final_answer_content = ""
        final_answer_chunks: list[str] = []
        adapter_emitted_terminal_chunk = False

        async def run_stream_task():
            try:
                # 注册 Cat Cafe MCP（请求级环境变量）
                cat_cafe_mcp = request.params.get("cat_cafe_mcp")
                if isinstance(cat_cafe_mcp, dict):
                    try:
                        await self._get_tool_manager().register_request_scoped_cat_cafe_mcp(cat_cafe_mcp)
                    except Exception as exc:
                        logger.warning(format_session_log(session_id, f"[JiuWenClaw] cat_cafe_mcp 注册失败: {exc}"))
                async for chunk in adapter.process_message_stream_impl(request, inputs):
                    await stream_queue.put(("chunk", chunk))
            except asyncio.CancelledError:
                logger.info(format_session_log(session_id, f"[JiuWenClaw] 流式任务被取消: request_id={rid}"))
                await stream_queue.put(("error", asyncio.CancelledError()))
            except Exception as exc:
                logger.exception(format_session_log(session_id, f"[JiuWenClaw] 流式任务异常: {exc}"))
                await stream_queue.put(("error", exc))
            finally:
                stream_done.set()

        # Team 模式: 后续请求直接执行，绕过 Session Manager 队列
        # 因为 Team 是长期运行的(persistent)，interact 调用不需要等待前一个任务完成
        # 且 team_helpers 内部已有请求锁保证同一 session 的请求串行执行
        if is_team_mode and not is_team_first_request:
            logger.info(
                format_session_log(
                    session_id,
                    f"[JiuWenClaw] Team模式后续请求，直接执行: request_id={rid}",
                )
            )
            asyncio.create_task(run_stream_task())
        else:
            await self._session_manager.submit_task(session_id, run_stream_task)

        try:
            while not stream_done.is_set() or not stream_queue.empty():
                try:
                    item = await asyncio.wait_for(stream_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                event_type, data = item

                if event_type == "error":
                    if isinstance(data, asyncio.CancelledError):
                        logger.info(format_session_log(session_id, f"[JiuWenClaw] 流式处理被中断: request_id={rid}"))
                        raise data
                    append_history_record(
                        session_id=session_id,
                        request_id=rid,
                        channel_id=cid,
                        role="assistant",
                        event_type="chat.error",
                        content=str(data),
                        timestamp=time.time(),
                        mode=request.params.get("mode", "unknown"),
                        sessions_root=self._sessions_dir,
                    )
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload={"event_type": "chat.error", "error": str(data)},
                        is_complete=False,
                    )
                else:
                    if isinstance(data, AgentResponseChunk):
                        if data.is_complete:
                            adapter_emitted_terminal_chunk = True
                        if isinstance(data.payload, dict) and isinstance(data.payload.get("event_type"), str):
                            et = str(data.payload.get("event_type"))
                            should_record = et.startswith("chat.") or et.startswith("task.")
                            if not should_record and et == EventType.TEAM_MESSAGE.value:
                                should_record = True

                            if should_record:
                                payload_dict = dict(data.payload)
                                extra_fields = {k: v for k, v in payload_dict.items() if
                                                k not in ("event_type", "content", "task_id")}
                                if et == EventType.TEAM_MESSAGE.value and "event" in payload_dict:
                                    event_data = payload_dict.get("event", {})
                                    if isinstance(event_data, dict):
                                        for k, v in event_data.items():
                                            if k not in ("type", "timestamp", "content"):
                                                extra_fields[k] = v
                                append_history_record(
                                    session_id=session_id,
                                    request_id=rid,
                                    channel_id=cid,
                                    role="assistant",
                                    event_type=et,
                                    content=data.payload.get("content") or data.payload.get("error") or "",
                                    timestamp=time.time(),
                                    extra=extra_fields if extra_fields else None,
                                    mode=request.params.get("mode", "unknown"),
                                    sessions_root=self._sessions_dir,
                                    task_id=payload_dict.get("task_id"),
                                )
                            if et == "chat.final":
                                final_answer_content = str(data.payload.get("content", ""))
                            elif et == "chat.delta":
                                final_answer_chunks.append(str(data.payload.get("content", "")))
                        yield data
                    elif isinstance(data, dict) and isinstance(data.get("event_type"), str):
                        et = str(data.get("event_type"))
                        should_record = et.startswith("chat.") or et.startswith("task.")
                        if not should_record and et == EventType.TEAM_MESSAGE.value:
                            should_record = True

                        if should_record:
                            extra_fields = {
                                k: v
                                for k, v in data.items()
                                if k not in ("event_type", "content", "task_id")
                            }
                            if et == EventType.TEAM_MESSAGE.value and "event" in data:
                                event_data = data.get("event", {})
                                if isinstance(event_data, dict):
                                    for k, v in event_data.items():
                                        if k not in ("type", "timestamp", "content"):
                                            extra_fields[k] = v
                            append_history_record(
                                session_id=session_id,
                                request_id=rid,
                                channel_id=cid,
                                role="assistant",
                                event_type=et,
                                content=data.get("content") or data.get("error") or "",
                                timestamp=time.time(),
                                extra=extra_fields if extra_fields else None,
                                mode=request.params.get("mode", "unknown"),
                                sessions_root=self._sessions_dir,
                                task_id=data.get("task_id"),
                            )
                        if et == "chat.final":
                            final_answer_content = str(data.get("content", ""))
                        elif et == "chat.delta":
                            final_answer_chunks.append(str(data.get("content", "")))
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=data,
                            is_complete=False,
                        )
        except asyncio.CancelledError:
            logger.info(format_session_log(session_id, f"[JiuWenClaw] 流式处理被中断: request_id={rid}"))
            raise

        # cloud memory: after chat hook
        if memory_mode == "cloud":
            assistant_message = final_answer_content or "".join(final_answer_chunks)
            after_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                assistant_message=assistant_message,
                extra=request.params,
            )
            await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_AFTER_CHAT, after_ctx)

        if not adapter_emitted_terminal_chunk:
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={"is_complete": True},
                is_complete=True,
            )

    # ---------- 实例获取 ----------

    def get_instance(self):
        if self._adapter is None:
            return None
        return self._adapter._instance

    # ---------- 资源清理 ----------

    async def cancel_inflight_work(self, log_prefix: str = "[gateway disconnect] ") -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时调用：取消 session 流式任务并中止 adapter 内层循环。"""
        await self._session_manager.cancel_all_session_tasks(log_prefix)
        for adp in (self._adapter, self._skilldev_adapter):
            if adp is None:
                continue
            abort_fn = getattr(adp, "abort_on_gateway_disconnect", None)
            if not callable(abort_fn):
                continue
            try:
                await abort_fn()
            except Exception:
                logger.exception("[JiuWenClaw] adapter.abort_on_gateway_disconnect failed")

    async def cleanup(self) -> None:
        """清理资源，准备销毁实例.

        每次 initialize 重建 agent 时调用。
        不清理记忆数据（记忆数据保留在文件系统中）。
        """
        logger.info("[JiuWenClaw] cleanup: 清理资源")

        if self._adapter is not None:
            try:
                if hasattr(self._adapter, "cleanup"):
                    await self._adapter.cleanup()
            except Exception as e:
                logger.warning("[JiuWenClaw] Adapter cleanup failed: %s", e)
            self._adapter = None

        if self._skilldev_adapter is not None:
            try:
                if hasattr(self._skilldev_adapter, "cleanup"):
                    await self._skilldev_adapter.cleanup()
            except Exception as e:
                logger.warning("[JiuWenClaw] SkillDev adapter cleanup failed: %s", e)
            self._skilldev_adapter = None

        self._tool_manager = None

        logger.info("[JiuWenClaw] cleanup: 完成")

    def is_working(self) -> bool:
        """返回 Agent 是否正在工作."""
        task = self._session_manager.get_session_tasks()
        queue = self._session_manager.get_session_queues()
        if self._adapter is None:
            return False
        return self._adapter.is_working(task, queue)
