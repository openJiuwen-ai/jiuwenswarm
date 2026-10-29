# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Management RPC provider, created only after a matching request.

The facade retains the execution/session owner and its compatibility methods.
This provider owns optional SkillDev and task-asset services; neither is created
by ordinary chat or by a lookup for an unrelated method.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import SKILLS_REBUILD_SILENT
from jiuwenswarm.server.runtime.skill.skill_manager import SkillRpcError
from jiuwenswarm.server.runtime.skill.archive_store import ARCHIVE_DIRNAME
from jiuwenswarm.server.runtime.agent_adapter.management_routes import (
    _SKILLDEV_METHODS,
    _SKILL_ROUTES,
    _SESSION_ASSET_ROUTES,
    _SKILL_VISIBILITY_WRITE_HANDLERS,
    _SKILL_SYNC_HANDLERS,
    _SKILL_SYNC_WS_REJECTED,
    _PLUGIN_ROUTES,
    _PACKAGE_ROUTES,
    _AGENT_GROUP_PACKAGE_METHODS,
)

logger = logging.getLogger(__name__)


class ManagementRequests:
    """Per-facade optional services with the facade as execution owner."""

    def __init__(self, host: Any) -> None:
        self._host = host
        self._skilldev_service = None
        self._session_asset_manager = None

    @property
    def session_asset_manager(self):
        if self._session_asset_manager is None:
            from jiuwenswarm.server.runtime.session_assets import get_session_asset_manager

            self._session_asset_manager = get_session_asset_manager()
        return self._session_asset_manager

    def _get_skilldev_service(self):
        """懒初始化并返回 SkillDevService 实例.

        SkillDevService 是无状态的，单实例即可服务所有请求。
        首次调用时从当前 JiuWenSwarm 配置中提取最小依赖并构造。
        """
        if self._host._skilldev_service is not None:
            return self._host._skilldev_service

        from jiuwenswarm.agents.harness.common.tools.mcp_toolkits import get_mcp_tools
        from jiuwenswarm.common.utils import get_workspace_dir
        from jiuwenswarm.server.runtime.skill.skilldev import (
            SkillDevDeps,
            SkillDevService,
            StateStore,
            WorkspaceProvider,
        )

        skilldev_base = get_workspace_dir() / "skilldev"
        state_store = StateStore(skilldev_base)
        workspace_provider = WorkspaceProvider(skilldev_base)

        config = get_config()
        model_configs = config.get("models", {})
        default_model = model_configs.get("default", {})

        deps = SkillDevDeps(
            model_name=default_model.get("model_name", ""),
            model_client_config=default_model.get("model_client_config", {}),
            mcp_tools_factory=get_mcp_tools,  # 直接复用已加载的 MCP 工具工厂
            sysop_config=None,
            state_store=state_store,
            workspace_provider=workspace_provider,
        )
        self._host._skilldev_service = SkillDevService(deps)
        logger.info("[JiuWenSwarm] SkillDevService 初始化完成")
        return self._host._skilldev_service

    async def _handle_skilldev_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 SkillDev 相关请求，返回 None 表示不是 SkillDev 请求."""
        if request.req_method not in _SKILLDEV_METHODS:
            return None

        service = self._host._get_skilldev_service()
        try:
            chunks = []
            async for chunk in service.handle(request):
                chunks.append(chunk)
            final = chunks[-1] if chunks else None
            payload = final.payload if final else {}
        except Exception as exc:
            logger.error("[JiuWenSwarm] skilldev 请求处理失败: %s", exc)
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
        # skills.sync.diff/package/apply 已收缩出 WS 路由（经 WS 调用会绕过
        # HTTP 层 token 鉴权），但它们仍是合法 ReqMethod——若不在此显式
        # 拒绝，请求会沿 facade 链路落入聊天处理路径，空 query 触发一次
        # 无意义的 LLM 调用（收缩前是干净的 handler 报错）。
        if request.req_method in _SKILL_SYNC_WS_REJECTED:
            message = (
                "skills.sync.diff/package/apply 未开放 WS 调用，"
                "请使用带鉴权的 HTTP /skill-sync/* 接口"
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "code": "SKILL_SYNC_WS_UNSUPPORTED",
                    "error": message,
                    "message": message,
                },
                metadata=request.metadata,
            )
        if request.req_method not in _SKILL_ROUTES:
            return None

        handler_name = _SKILL_ROUTES[request.req_method]
        handler = getattr(self._host._skill_manager, handler_name)
        try:
            params = dict(request.params) if isinstance(request.params, dict) else {}
            if handler_name in _SKILL_SYNC_HANDLERS:
                # 长生命周期实例先重载磁盘 state（外部流程可能已写
                # local_skills / skill_configs），同步结果才与磁盘一致
                try:
                    self._host._skill_manager.reload_state()
                except Exception:  # noqa: BLE001
                    logger.warning("[skills] reload state before sync failed")
            if handler_name in (
                "handle_skills_import_local",
                "handle_skills_get",
                "handle_skills_files_get",
                "handle_skills_experience_request",
            ):
                # download_token / 正文图片 token 校验需要绑定当前会话 sid
                params["_session_id"] = str(request.session_id or "").strip()
                if handler_name == "handle_skills_experience_request":
                    params["_channel_id"] = str(request.channel_id or "").strip()
                if handler_name == "handle_skills_files_get" and not params.get("session_id"):
                    params["session_id"] = params["_session_id"]
            if handler_name in {
                "handle_skills_retrieval_status",
                "handle_skills_retrieval_index_build",
            }:
                params.pop("_session_profile", None)
                profile_getter = getattr(
                    self._host._adapter,
                    "get_skill_retrieval_status_profile",
                    None,
                )
                if callable(profile_getter):
                    profile = profile_getter(request.session_id)
                    if isinstance(profile, dict):
                        params["_session_profile"] = profile
            payload = await handler(params)
            _reload_after_skills = handler_name in [
                "handle_skills_install",
                "handle_skills_import_local",
                "handle_skills_import_upload",
                "handle_skills_toggle",
                "handle_skills_skillnet_install",
                "handle_skills_online_search_install",
                "handle_skills_clawhub_download",
                "handle_skills_team_skills_hub_install",
                "handle_skill_sync_reload",
            ]
            if handler_name == "handle_skill_sync_reload" and not (
                payload.get("applied") or []
            ):
                # 仅当无任何技能实际落盘（web 进程 apply 全部 skipped /
                # 全部安装失败）时才不重建；通知方只在 applied 非空时
                # 发送，此处为防御性兜底。
                _reload_after_skills = False
            if (
                handler_name
                in {
                    "handle_skills_skillnet_install",
                    "handle_skills_online_search_install",
                }
                and payload.get("pending")
            ):
                _reload_after_skills = False
            if _reload_after_skills:
                await self._host.create_instance()
                await self._host._reload_team_skill_rails(request.session_id)
            elif handler_name == "handle_skills_uninstall" and payload.get("success"):
                # 卸载只需轻量刷新 skill rail，不需要全量重建 agent 实例。
                # SkillUseRail 会通过文件系统签名检测到目录删除并自动刷新，
                # 这里主动调用 reload_skills() 确保立即生效，避免延迟到下一次模型调用。
                await self._host._refresh_skill_rails_after_change()
                await self._host._reload_team_skill_rails(request.session_id)
            elif handler_name in _SKILL_VISIBILITY_WRITE_HANDLERS and payload.get("success"):
                # 可见性只改 metadata，库内容没变：agent 无需重建，只要让各
                # skill rail 重新读一次 metadata，授权/撤权即刻生效。
                await self._host._refresh_skill_rails_after_change()
                await self._host._reload_team_skill_rails(request.session_id)
            elif handler_name == "handle_skills_evolution_save" and payload.get("success"):
                await self._host._refresh_skill_rails_after_change()
            elif handler_name == "handle_skills_rebuild" and self._host._is_skills_rebuild_followup(
                payload
            ):
                # 与 /evolve_rebuild 同 prompt+Agent，但 RPC 内同步静默跑完，不经 Gateway chat.send
                try:
                    await self._host._run_skills_rebuild_followup(request, payload)
                except SkillRpcError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "[JiuWenSwarm] skills.rebuild 静默 Agent 失败: request_id=%s skill=%s",
                        request.request_id,
                        payload.get("skill_name"),
                    )
                    raise SkillRpcError(
                        "SKILL_REBUILD_FAILED",
                        f"rebuild Agent 失败: {exc}",
                    ) from exc
                payload = {"success": True}
            elif handler_name == "handle_skills_create_from_knowledge":
                if self._host._is_skills_create_from_knowledge_followup(payload):
                    payload = await self._host._run_skills_create_from_knowledge_silent(
                        request, payload
                    )
                    if payload.get("success"):
                        await self._host.create_instance()
                        await self._host._reload_team_skill_rails(request.session_id)
        except Exception as exc:
            logger.error("[JiuWenSwarm] skills 请求处理失败: %s", exc)
            err_payload: dict = {"error": str(exc), "message": str(exc)}
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code.strip():
                err_payload["code"] = code.strip()
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=err_payload,
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_session_asset_request(self, request: AgentRequest) -> AgentResponse | None:
        """Handle task-asset requests (session.assets.*); None means not one of them."""
        if request.req_method not in _SESSION_ASSET_ROUTES:
            return None
        handler = getattr(self._host._session_asset_manager, _SESSION_ASSET_ROUTES[request.req_method])
        params = dict(request.params) if isinstance(request.params, dict) else {}
        params.setdefault("session_id", request.session_id or "")
        try:
            payload = await handler(params)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str):
                logger.error("[JiuWenSwarm] session.assets request failed: method=%s error=%s", request.req_method, exc)
            err_payload: dict = {"error": str(exc), "message": str(exc)}
            if isinstance(code, str) and code.strip():
                err_payload["code"] = code.strip()
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=err_payload,
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    @staticmethod
    def _is_skills_rebuild_followup(payload: Any) -> bool:
        """判断 skills.rebuild 响应是否需要静默 follow-up."""
        if not isinstance(payload, dict):
            return False
        return payload.get("result_type") == "followup" and bool(payload.get("success"))

    @staticmethod
    def _is_skills_create_from_knowledge_followup(payload: Any) -> bool:
        """判断 skills.create_from_knowledge 响应是否需要静默 follow-up."""
        if not isinstance(payload, dict):
            return False
        return payload.get("result_type") == "followup" and bool(payload.get("success"))

    async def _run_skills_rebuild_followup(
        self,
        request: AgentRequest,
        payload: dict[str, Any],
    ) -> None:
        """同步静默执行 rebuild Agent follow-up，并按版本目标同步 workspace / 版本副本.

        不经外层 process_message_stream（避免写用户会话 history / 推 UI），
        直接走 adapter.process_message_stream_impl，并使用临时 session 隔离 checkpointer。
        """
        followup = str(payload.get("followup_prompt") or "").strip()
        if not followup:
            raise SkillRpcError("SKILL_REBUILD_FAILED", "evolve_rebuild follow-up prompt 为空")

        target = payload.get("rebuild_target") if isinstance(payload.get("rebuild_target"), dict) else {}
        skill_dir_raw = str(target.get("skill_dir") or "").strip()
        content_root_raw = str(target.get("content_root") or "").strip()
        swap_workspace = bool(target.get("swap_workspace"))
        is_default = bool(target.get("is_default"))
        skill_dir = Path(skill_dir_raw) if skill_dir_raw else None
        content_root = Path(content_root_raw) if content_root_raw else None
        should_write_back = is_default or swap_workspace

        workspace_backup: Path | None = None
        try:
            if swap_workspace and skill_dir is not None and content_root is not None:
                # 非默认版本：临时把版本 mid-state 放到 workspace，供 Agent 按 skill 名改写
                workspace_backup = Path(tempfile.mkdtemp(prefix="skill-rebuild-ws-bak-"))
                for child in list(skill_dir.iterdir()):
                    if child.name == ARCHIVE_DIRNAME:
                        continue
                    dest = workspace_backup / child.name
                    if child.is_dir() and not child.is_symlink():
                        shutil.copytree(child, dest)
                    elif child.is_file():
                        shutil.copy2(child, dest)
                self._host._skill_manager.sync_workspace_from_version_content(skill_dir, content_root)

            params = dict(request.params) if isinstance(request.params, dict) else {}
            params["query"] = followup
            params["log_as_user"] = False
            params.setdefault("mode", params.get("mode") or "agent")

            metadata = dict(request.metadata) if isinstance(request.metadata, dict) else {}
            metadata["skills_rebuild_silent"] = True

            # Windows 禁止路径分量含 ':'；用 '-' 隔离，避免 sessions 目录 mkdir 失败。
            raw_rid = str(request.request_id or "").strip() or "anon"
            safe_rid = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_rid).strip("._-") or "anon"
            rebuild_session_id = f"skills-rebuild-{safe_rid}"
            chat_request = AgentRequest(
                request_id=f"{request.request_id}-rebuild-followup",
                channel_id=request.channel_id,
                session_id=rebuild_session_id,
                chat_id=request.chat_id,
                req_method=ReqMethod.CHAT_SEND,
                params=params,
                is_stream=True,
                timestamp=request.timestamp,
                metadata=metadata,
            )
            adapter = self._host._ensure_adapter(mode=self._host._adapter_mode_for_request(chat_request))
            inputs, _, _ = self._host._build_inputs(chat_request)
            silent_token = SKILLS_REBUILD_SILENT.set(True)
            try:
                async for _chunk in adapter.process_message_stream_impl(chat_request, inputs):
                    pass
            finally:
                SKILLS_REBUILD_SILENT.reset(silent_token)

            if skill_dir is not None and content_root is not None and should_write_back:
                # Agent 改写 workspace 后回写目标版本副本
                self._host._skill_manager.copy_workspace_business_to_version(skill_dir, content_root)
                version = target.get("version")
                if isinstance(version, str) and version.strip():
                    from jiuwenswarm.server.runtime.skill.archive_store import touch_version_metadata

                    touch_version_metadata(skill_dir, version.strip())
            await self._host._refresh_skill_rails_after_change()
        finally:
            if workspace_backup is not None and skill_dir is not None:
                try:
                    for child in list(skill_dir.iterdir()):
                        if child.name == ARCHIVE_DIRNAME:
                            continue
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                    for child in workspace_backup.iterdir():
                        dest = skill_dir / child.name
                        if child.is_dir() and not child.is_symlink():
                            shutil.copytree(child, dest)
                        elif child.is_file():
                            shutil.copy2(child, dest)
                finally:
                    shutil.rmtree(workspace_backup, ignore_errors=True)

    @staticmethod
    def _coerce_optional_str_list(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    @staticmethod
    def _build_skills_knowledge_followup_request(
        request: AgentRequest,
        *,
        followup: str,
        skills: list[str],
        trusted_dirs: list[str],
        input_file: str,
    ) -> AgentRequest:
        params = dict(request.params) if isinstance(request.params, dict) else {}
        params["query"] = followup
        params["log_as_user"] = False
        params.setdefault("mode", params.get("mode") or "agent")
        params["skills"] = skills
        if trusted_dirs:
            params["trusted_dirs"] = trusted_dirs
        if input_file:
            params["files"] = {
                "uploaded_documents": [
                    {"path": input_file, "filename": Path(input_file).name}
                ]
            }
        param_metadata = (
            params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        )
        params["metadata"] = {
            **param_metadata,
            "scene": "create_skill",
        }

        metadata = dict(request.metadata) if isinstance(request.metadata, dict) else {}
        metadata["skills_create_from_knowledge_silent"] = True
        metadata["scene"] = "create_skill"

        # Windows 禁止路径分量含 ':'；用 '-' 隔离，避免 sessions 目录 mkdir 失败。
        raw_rid = str(request.request_id or "").strip() or "anon"
        safe_rid = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_rid).strip("._-") or "anon"
        return AgentRequest(
            request_id=f"{request.request_id}-knowledge-followup",
            channel_id=request.channel_id,
            session_id=f"skills-knowledge-{safe_rid}",
            chat_id=request.chat_id,
            req_method=ReqMethod.CHAT_SEND,
            params=params,
            is_stream=True,
            timestamp=request.timestamp,
            metadata=metadata,
        )

    @staticmethod
    def _cleanup_knowledge_upload_file(input_file: str) -> None:
        if not input_file:
            return
        try:
            path = Path(input_file)
            if path.is_file() and "jiuwenswarm_knowledge_upload_" in str(path.parent):
                shutil.rmtree(path.parent, ignore_errors=True)
        except OSError:
            pass

    async def _run_skills_create_from_knowledge_silent(
        self,
        request: AgentRequest,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """静默执行知识转 Skill：隔离临时目录生成 → 校验 → 安装到 workspace."""
        followup = str(payload.get("followup_prompt") or "").strip()
        output_dir = str(payload.get("output_dir") or "").strip()
        if not followup or not output_dir:
            raise SkillRpcError(
                "SKILL_INVALID_PACKAGE",
                "create-from-knowledge follow-up 参数不完整",
            )

        skills = self._host._coerce_optional_str_list(payload.get("skills"))
        trusted_dirs = self._host._coerce_optional_str_list(payload.get("trusted_dirs"))
        input_file = str(payload.get("input_file") or "").strip()
        skills_root = Path(self._host._skill_manager.skills_dir)
        before_names = set(self._host._skill_manager.list_installed_skill_dir_names())

        try:
            chat_request = self._host._build_skills_knowledge_followup_request(
                request,
                followup=followup,
                skills=skills,
                trusted_dirs=trusted_dirs,
                input_file=input_file,
            )
            adapter = self._host._ensure_adapter(mode=self._host._adapter_mode_for_request(chat_request))
            inputs, _, _ = self._host._build_inputs(chat_request)
            async for _chunk in adapter.process_message_stream_impl(chat_request, inputs):
                pass

            after_names = set(self._host._skill_manager.list_installed_skill_dir_names())
            skip_names = {
                "_marketplace",
                "_pending_knowledge",
                "skill-omni-creation",
                "skill-creator",
                "skill-creator-normal",
                "skill-creator-router",
                "swarmskill-creator",
                "agent-creator",
                "agent-group-creator",
                "plugin-creator",
            }
            workspace_candidates = [
                skills_root / name
                for name in sorted(after_names - before_names)
                if name not in skip_names
            ]
            result = self._host._skill_manager.finalize_create_from_knowledge(
                output_dir,
                workspace_candidates=workspace_candidates,
                existing_skill_names=before_names,
            )
            if result.get("success"):
                await self._host._refresh_skill_rails_after_change()
            return result
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)
            self._host._cleanup_knowledge_upload_file(input_file)

    async def _handle_plugins_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 Plugin 相关请求，返回 None 表示不是 Plugin 请求."""
        if request.req_method not in _PLUGIN_ROUTES:
            return None

        handler_name = _PLUGIN_ROUTES[request.req_method]
        handler = getattr(self._host._skill_manager, handler_name)
        try:
            payload = await handler(request.params)
            # install / uninstall / reload 之后重建 Agent 实例
            _reload_after = handler_name in [
                "handle_plugins_install",
                "handle_plugins_uninstall",
                "handle_plugins_reload",
            ]
            if _reload_after:
                await self._host.create_instance()
        except Exception as exc:
            logger.error("[JiuWenSwarm] plugins 请求处理失败: %s", exc)
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

    async def _handle_package_catalog_request(self, request: AgentRequest) -> AgentResponse | None:
        """Route catalog / lifecycle ReqMethods to package_manager and wrap AgentResponse."""
        from jiuwenswarm.server.runtime import extension_package_manager as package_manager

        method = request.req_method
        if method not in _PACKAGE_ROUTES:
            return None
        params = request.params if isinstance(request.params, dict) else {}
        # Frontend contract uses `id`; accept legacy `name` as alias.
        name = params.get("id") if params.get("id") not in (None, "") else params.get("name")
        try:
            if method == ReqMethod.AGENT_GROUPS_LIST:
                cards = await package_manager.list_agent_groups_with_hub(params)
                payload: dict[str, Any] = {"agentGroups": cards}
                if hasattr(cards, "cache"):
                    payload["cache"] = cards.cache
            elif method == ReqMethod.AGENT_GROUPS_SHOW:
                group = await package_manager.show_agent_group_with_hub(str(name or ""))
                if group is None:
                    raise ValueError(f"agent_group not found: {name!r}")
                payload = {"group": group}
            elif method == ReqMethod.AGENT_GROUPS_FILE_LIST:
                payload = {
                    "tree": await package_manager.list_agent_group_files_with_hub(str(name or ""))
                }
            elif method == ReqMethod.AGENT_GROUPS_FILE_READ:
                payload = await package_manager.read_agent_group_file_with_hub(
                    str(name or ""), str(params.get("path", ""))
                )
            elif method == ReqMethod.AGENT_GROUPS_CREATE:
                payload = package_manager.create_agent_group(params)
            elif method == ReqMethod.AGENT_GROUPS_IMPORT_LOCAL:
                payload = package_manager.import_agent_group(params)
            elif method == ReqMethod.AGENT_GROUPS_INSTALL:
                await package_manager.install_agent_group_with_hub(params)
                payload = {}
            elif method == ReqMethod.AGENT_GROUPS_UNINSTALL:
                from jiuwenswarm.server.runtime.organization_packages import (
                    uninstall_organization_agent_group,
                )

                await uninstall_organization_agent_group(params)
                payload = {}
            elif method == ReqMethod.AGENT_TEMPLATES_LIST:
                cards = await package_manager.list_agent_templates_with_hub(params)
                payload = {"templates": cards}
                if hasattr(cards, "cache"):
                    payload["cache"] = cards.cache
            elif method == ReqMethod.AGENT_TEMPLATES_SHOW:
                card = await package_manager.show_agent_template_with_hub(
                    str(name or "")
                )
                if card is None:
                    raise ValueError(f"agent_template not found: {name!r}")
                payload = {"template": card}
            elif method == ReqMethod.AGENT_TEMPLATES_FILE_LIST:
                payload = {
                    "tree": await package_manager.list_agent_template_files_with_hub(
                        str(name or "")
                    )
                }
            elif method == ReqMethod.AGENT_TEMPLATES_FILE_READ:
                payload = await package_manager.read_agent_template_file_with_hub(
                    str(name or ""), str(params.get("path", ""))
                )
            elif method == ReqMethod.PLUGIN_PACKAGES_LIST:
                cards = await package_manager.list_plugin_packages_with_hub(params)
                payload = {"packages": cards}
                if hasattr(cards, "cache"):
                    payload["cache"] = cards.cache
            elif method == ReqMethod.PLUGIN_PACKAGES_SHOW:
                card = await package_manager.show_plugin_package_with_hub(
                    str(name or "")
                )
                if card is None:
                    raise ValueError(f"plugin not found: {name!r}")
                payload = {"package": card}
            elif method == ReqMethod.AGENT_TEMPLATES_INSTALL:
                ok, payload = await asyncio.wait_for(
                    package_manager.install_equipment_from_hub_gated("agent_templates", params),
                    timeout=120,
                )
                if not ok:
                    return AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload=payload,
                        metadata=request.metadata,
                    )
            elif method == ReqMethod.PLUGIN_PACKAGES_INSTALL:
                ok, payload = await asyncio.wait_for(
                    package_manager.install_equipment_from_hub_gated("plugin_packages", params),
                    timeout=120,
                )
                if not ok:
                    return AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload=payload,
                        metadata=request.metadata,
                    )
            elif method == ReqMethod.AGENT_TEMPLATES_UNINSTALL:
                unload_live = getattr(self._host, "_unload_live_equipment", None)
                if unload_live is not None:
                    runtime_name = package_manager.resolve_equipment_runtime_id(
                        "agent_templates", name
                    )
                    await unload_live("agent_templates", runtime_name)
                payload = package_manager.uninstall_equipment_with_notice(
                    "agent_templates", params
                )
            elif method == ReqMethod.PLUGIN_PACKAGES_UNINSTALL:
                unload_live = getattr(self._host, "_unload_live_equipment", None)
                if unload_live is not None:
                    runtime_name = package_manager.resolve_equipment_runtime_id(
                        "plugin_packages", name
                    )
                    await unload_live("plugin_packages", runtime_name)
                payload = package_manager.uninstall_equipment_with_notice(
                    "plugin_packages", params
                )
            elif method == ReqMethod.AGENT_TEMPLATES_IMPORT_LOCAL:
                payload = package_manager.import_agent_template(params)
            elif method == ReqMethod.AGENT_TEMPLATES_UPDATE:
                # 先卸载已加载到运行会话的模板，使本次更新对当前会话立即生效；
                # 否则 _load_agent_template_for_request 的版本比对会命中旧 record
                # （本地包 manifest 不写 version 键，恒 ""==""→True）而不重载。
                unload_live = getattr(self._host, "_unload_live_equipment", None)
                if unload_live is not None:
                    runtime_name = package_manager.resolve_equipment_runtime_id(
                        "agent_templates", name
                    )
                    await unload_live("agent_templates", runtime_name)
                getattr(package_manager, _PACKAGE_ROUTES[method])(params)
                payload = {}
            elif method == ReqMethod.AGENT_TEMPLATES_DELETE:
                unload_live = getattr(self._host, "_unload_live_equipment", None)
                if unload_live is not None:
                    runtime_name = package_manager.resolve_equipment_runtime_id(
                        "agent_templates", name
                    )
                    await unload_live("agent_templates", runtime_name)
                package_manager.delete_agent_template(params)
                payload = {}
            elif method == ReqMethod.PLUGIN_PACKAGES_IMPORT_LOCAL:
                payload = package_manager.import_plugin_package(params)
            else:
                # lifecycle: create → ok + {}
                getattr(package_manager, _PACKAGE_ROUTES[method])(params)
                payload = {}
        except Exception as exc:
            from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import (
                invalidate_hub_catalog,
            )
            from jiuwenswarm.server.runtime.marketplace.hub_client import (
                HubNotFoundError,
            )
            error_payload = {"error": "安装超时，请稍后重试" if isinstance(exc, asyncio.TimeoutError) else str(exc)}
            if isinstance(exc, HubNotFoundError):
                kind = "agent_template" if method.value.startswith("agent_templates.") else "plugin"
                invalidate_hub_catalog(kind)
                error_payload["code"] = "HUB_ASSET_NOT_FOUND"
            logger.warning("[extension_package_manager] request %s failed: %s", method, exc)
            error_code = getattr(exc, "code", None)
            if method in _AGENT_GROUP_PACKAGE_METHODS and not isinstance(error_code, str):
                error_code = "AGENT_GROUP_REQUEST_FAILED"
            if isinstance(error_code, str) and error_code:
                error_payload["code"] = error_code
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=error_payload,
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )
