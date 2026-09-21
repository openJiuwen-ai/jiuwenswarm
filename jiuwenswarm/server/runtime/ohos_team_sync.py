"""OHOS relay 专家团链路（新增模块，仅鸿蒙端生效）。

relay（office-claw 通道）把预置/用户团队拓扑通过 ``sync_agents_configs``
的 ``params.teams`` 推给 sidecar，并经 ``chat.send`` 的 ``params.team_name``
显式引用团队。团队基线代码不消费这些字段；本模块负责翻译、注入、
绑定与模型鉴权水合，仅供基线文件里的 ``is_ohos_runtime()`` 门控块调用。
Windows 端不触发任何调用路径，运行行为与团队基线完全一致。

本模块的实现复制自 7ff8dc88d（09-16 OA.05000090 事故的无门控修复，
09-18 评审时随跨平台改动一并还原），按评审约束收束为鸿蒙门控版；
基线文件的现有代码行零改动，仅在固定位置插入门控调用块。

基线函数 ``_build_modes_team_mapping`` 只认 ``agent_key``，而 relay 载荷
用 ``agent_id`` 引用 agents 模板库——通过调用前的载荷数据归一解决，
不改基线翻译函数。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from jiuwenswarm.common.platform import is_ohos_runtime

logger = logging.getLogger(__name__)


def validate_teams_payload(teams: Any) -> dict[str, Any]:
    """校验 relay ``teams`` 载荷结构（结构检查，语义校验留给翻译层）。

    载荷形态（relay-claw ``RelayClawTeamsPayload``）：
    ``{agents: {template_key: template}, team: [team_spec, ...]}``。
    畸形载荷不得阻断 agent env 同步——由调用方记日志后丢弃。
    ``team`` 缺省/为空是有意义的信号（relay 语义：空数组 → 清空
    modes.team），因此保留为 ``[]``。
    """
    if not isinstance(teams, dict):
        raise ValueError("teams must be an object when provided")
    agents = teams.get("agents")
    if agents is not None and not isinstance(agents, dict):
        raise ValueError("teams.agents must be an object")
    team = teams.get("team")
    if team is None:
        team = []
    if not isinstance(team, list):
        raise ValueError("teams.team must be an array")
    return {"agents": agents if agents is not None else {}, "team": team}


def _normalize_member_agent_ref(member_raw: dict[str, Any]) -> str:
    """归一成员的 agents 模板引用键（数据预处理，不改基线翻译函数）。

    两种协议引用 ``agents`` 模板库条目：
    - 旧版（web 团队编辑器）：``agent_key``；
    - relay sync 载荷（relay-claw 91e1fa6c0, 2026-08-07）：``agent_id``
      ——即 catalog agent id，与 sync ``agents[].agent_id`` 同名。

    ``agent_key`` 同时存在时优先；输出恒归一为 ``agent_key``，下游
    基线 ``_build_modes_team_mapping`` 只见单一字段名。
    """
    raw = member_raw.get("agent_key")
    if raw is None or not str(raw).strip():
        raw = member_raw.get("agent_id")
    return str(raw or "").strip()


def _normalize_teams_agent_refs(teams: dict[str, Any]) -> dict[str, Any]:
    """遍历载荷，把 leader/teammate/predefined_members 引用归一成 agent_key。"""
    normalized = copy.deepcopy(teams)
    for team_raw in normalized.get("team") or []:
        if not isinstance(team_raw, dict):
            continue
        leader_raw = team_raw.get("leader")
        if isinstance(leader_raw, dict):
            leader_raw["agent_key"] = _normalize_member_agent_ref(leader_raw)
        teammate_raw = team_raw.get("teammate")
        if isinstance(teammate_raw, dict):
            teammate_raw["agent_key"] = _normalize_member_agent_ref(teammate_raw)
        members_raw = team_raw.get("predefined_members")
        if isinstance(members_raw, list):
            for member_raw in members_raw:
                if isinstance(member_raw, dict):
                    member_raw["agent_key"] = _normalize_member_agent_ref(member_raw)
    return normalized


def build_sync_modes_team(
    teams: dict[str, Any] | None,
    service_id: str,
) -> dict[str, Any] | None:
    """把 relay ``teams`` 载荷翻译成 ``modes.team`` 映射。

    载荷缺失/不可用时返回 ``None``（调用方语义：配置保持原样——
    relay 无 ``teams`` 字段 → sidecar 手写的 modes.team 存活）；
    ``team[]`` 为空返回 ``{}``（relay 语义：清空）。
    """
    if teams is None:
        return None
    from jiuwenswarm.common.config import _build_modes_team_mapping

    try:
        mapping = _build_modes_team_mapping(_normalize_teams_agent_refs(teams))
    except Exception as exc:  # noqa: BLE001 — 团队拓扑不得阻断同步
        logger.warning(
            "[OHOS-TeamSync] sync teams payload rejected (agent sync "
            "continues without modes.team injection): service_id=%s error=%s",
            service_id,
            exc,
        )
        return None
    logger.info(
        "[OHOS-TeamSync] sync teams payload consumed: service_id=%s templates=%d",
        service_id,
        len(mapping),
    )
    return mapping


def inject_teams_into_sync_entries(
    raw_params: Any,
    agents_payload: list[dict[str, Any]],
    service_id: str,
) -> None:
    """sync 流程注入入口：提取载荷 → 翻译 → 原地注入各 entry["config"]。

    仅 OHOS 生效；``modes_team=None``（载荷缺失/畸形）时全部跳过。
    非空 → 整表替换 ``modes.team``，空表 → 移除，其余 ``modes`` 键保留。
    """
    if not is_ohos_runtime():
        return
    teams_raw = raw_params.get("teams") if isinstance(raw_params, dict) else None
    teams: dict[str, Any] | None = None
    if teams_raw is not None:
        try:
            teams = validate_teams_payload(teams_raw)
        except ValueError as exc:
            logger.warning(
                "[OHOS-TeamSync] invalid teams payload dropped: %s", exc
            )
    modes_team = build_sync_modes_team(teams, service_id)
    if modes_team is None:
        return
    for entry in agents_payload:
        config = entry.get("config") if isinstance(entry, dict) else None
        if not isinstance(config, dict):
            continue
        modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
        next_modes = {**modes}
        if modes_team:
            next_modes["team"] = copy.deepcopy(modes_team)
        else:
            next_modes.pop("team", None)
        entry["config"] = {**config, "modes": next_modes}


def _write_team_identity(
    *,
    session_id: str,
    request: Any,
    canonical_mode: str,
    user_content: Any,
    binding: Any,
    sessions_root: Any,
) -> None:
    """把绑定的团队写入 session metadata（正常路径与 CONFLICT 竞态共用）。

    team stream 优先从 session metadata 解析团队身份；调用方已绑定
    tenant env ns，本写入与 team stream 读取落在同一 root
    （agent_<agent_id>）。
    """
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from jiuwenswarm.server.runtime.session.session_metadata import (
        update_session_metadata,
    )

    update_session_metadata(
        session_id=session_id,
        channel_id=request.channel_id or None,
        user_content=user_content,
        mode=canonical_mode,
        team_name=binding.team_name,
        runtime_team_name=TeamManager.build_session_scoped_team_name(
            binding.team_name,
            session_id,
        ),
        team_template_id=binding.template_id,
        touch_last_message_at=False,
        sync_write=True,
        sessions_root=sessions_root,
    )


def bind_requested_team(request: Any, context: dict[str, Any]) -> Any | None:
    """把 session 显式绑定到请求的 ``team_name``（relay 预置/用户团队）。

    解析顺序（先中先用）：
    1. binding store 既有绑定——复用（多 session 团队）；
    2. 同名 ``modes.team`` 模板（relay 预置/用户团队经 sync teams 载荷
       推送，``team_name = oc_team_<id>``）——从模板创建绑定；
    3. 未命中 → ``None`` → 调用方回落按描述生成（旧版自由团队会话与
       老版 relay）。

    失败被抑制：任何 store/template 异常返回 ``None``，让生成兜底仍
    有机会，而不是让 chat 失败。
    """
    from jiuwenswarm.agents.harness.team import list_team_template_summaries
    from jiuwenswarm.server.handlers._shared import _effective_config_for_request
    from jiuwenswarm.server.handlers.team import _create_team_binding_from_template
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStoreError,
        get_team_binding_store,
    )

    session_id = str(context.get("session_id") or "")
    team_name = str(context.get("team_name") or "")
    canonical_mode = str(context.get("canonical_mode") or "")
    user_content = context.get("user_content")
    sessions_root = context.get("sessions_root")

    def _bind_and_record(binding: Any) -> Any | None:
        binding_store = get_team_binding_store()
        bound = binding_store.bind_session(
            team_name=binding.team_name,
            session_id=session_id,
        )
        _write_team_identity(
            session_id=session_id,
            request=request,
            canonical_mode=canonical_mode,
            user_content=user_content,
            binding=bound,
            sessions_root=sessions_root,
        )
        return bound

    try:
        binding_store = get_team_binding_store()
        binding = binding_store.get(team_name)
        if binding is None:
            config_base = _effective_config_for_request(request)
            template_ids = {
                str(item.get("template_id") or "")
                for item in list_team_template_summaries(config_base)
            }
            if team_name not in template_ids:
                return None
            binding = _create_team_binding_from_template(
                team_name=team_name,
                template_id=team_name,
                config_base=config_base,
            )
        return _bind_and_record(binding)
    except TeamBindingStoreError as exc:
        if str(exc.code) == "CONFLICT":
            # 与另一 session 竞态创建同名团队——重读、绑定、记录，与正常
            # 路径完全一致。
            try:
                binding_store = get_team_binding_store()
                binding = binding_store.get(team_name)
                if binding is None:
                    return None
                return _bind_and_record(binding)
            except Exception:  # noqa: BLE001 — 回落到生成
                return None
        logger.warning(
            "[OHOS-TeamSync] requested team bind failed, falling back to "
            "generated binding: session_id=%s team_name=%s error=%s",
            session_id,
            team_name,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — 不得让 chat 失败
        logger.warning(
            "[OHOS-TeamSync] requested team bind failed, falling back to "
            "generated binding: session_id=%s team_name=%s error=%s",
            session_id,
            team_name,
            exc,
        )
        return None


async def ensure_auto_team_binding_ns_bound(ctx: Any, request: Any) -> Any | None:
    """OHOS 版 ``chat.send`` 自动团队绑定入口（含 tenant env ns 绑定窗口）。

    结构复制自 7ff8cd88d chat.py：先绑定 tenant env ns——session
    metadata 读写必须落在 team stream 读取的同一 root
    （agent_<agent_id>），否则 team stream 回落 ``templates[0]``
    （2026-09-16 exam-prep 事故：请求跑错团队）——再按
    「显式绑定（relay ``params.team_name``）→ 按描述生成兜底」处理。
    """
    from jiuwenswarm.common.local_env_config import (
        bind_agent_env_ns,
        reset_agent_env_ns,
    )
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

    if request.req_method != ReqMethod.CHAT_SEND:
        return None

    params = request.params if isinstance(request.params, dict) else {}
    if not isinstance(request.params, dict):
        request.params = params
    session_id = str(request.session_id or params.get("session_id") or "").strip()
    if not session_id:
        return None

    tenant_agent_id, tenant_service_id, _ = TenantAgentPool.extract_ids(request)
    ns_token = bind_agent_env_ns(tenant_service_id, tenant_agent_id)
    try:
        return await _ensure_binding_ns_bound_impl(
            request,
            params=params,
            session_id=session_id,
        )
    finally:
        reset_agent_env_ns(ns_token)


async def _ensure_binding_ns_bound_impl(
    request: Any,
    *,
    params: dict[str, Any],
    session_id: str,
) -> Any | None:
    """tenant env ns 已绑定的自动建队实现（显式绑定优先，生成兜底）。"""
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from jiuwenswarm.server.handlers._shared import (
        _effective_config_for_request,
        _is_team_metadata_mode,
        _request_query_text,
        _session_team_binding_lock,
        _sessions_dir_for_request,
        resolve_agent_request_mode,
    )
    from jiuwenswarm.server.handlers.team import _create_generated_team_binding
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        update_session_metadata,
    )
    from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
    from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store

    sessions_root = _sessions_dir_for_request(request)
    metadata = get_session_metadata(
        session_id,
        cache_bust=True,
        sessions_root=sessions_root,
    )
    raw_mode = params.get("mode")
    effective_mode = (
        raw_mode
        if isinstance(raw_mode, str) and raw_mode.strip()
        else metadata.get("mode")
    )
    _, _, canonical_mode = resolve_agent_request_mode(effective_mode)
    if not _is_team_metadata_mode({"mode": canonical_mode}):
        return None

    existing_team_name = str(metadata.get("team_name") or "").strip()
    if existing_team_name:
        params.setdefault("team_name", existing_team_name)
        template_id = str(metadata.get("team_template_id") or "").strip()
        if template_id:
            params.setdefault("team_template_id", template_id)
        return existing_team_name

    query = _request_query_text(request)
    if not query:
        return None

    async with _session_team_binding_lock(session_id):
        metadata = get_session_metadata(
            session_id,
            cache_bust=True,
            sessions_root=sessions_root,
        )
        existing_team_name = str(metadata.get("team_name") or "").strip()
        if existing_team_name:
            params.setdefault("team_name", existing_team_name)
            template_id = str(metadata.get("team_template_id") or "").strip()
            if template_id:
                params.setdefault("team_template_id", template_id)
            return existing_team_name

        # relay 预置/用户团队以显式 params.team_name 引用 modes.team 模板
        # （oc_team_<teamId>，sync teams 载荷推送）。优先精确绑定请求的
        # 团队；未命中回落按描述生成（自由团队会话与不带 team_name 的
        # 老版 relay）。模板缺失（relay 无 teams 载荷）→ 两级查找都未
        # 命中 → 生成路径，即基线行为完整保留。
        requested_team_name = str(params.get("team_name") or "").strip()
        binding = None
        if requested_team_name:
            binding = bind_requested_team(
                request,
                {
                    "session_id": session_id,
                    "team_name": requested_team_name,
                    "canonical_mode": canonical_mode,
                    "user_content": query,
                    "sessions_root": sessions_root,
                },
            )
        if binding is not None:
            logger.info(
                "[OHOS-TeamSync] bound requested team before chat: "
                "session_id=%s team_name=%s template_id=%s",
                session_id,
                binding.team_name,
                binding.template_id,
            )
            params["team_name"] = binding.team_name
            params["team_template_id"] = binding.template_id
            request.metadata = dict(request.metadata or {})
            request.metadata["team_name"] = binding.team_name
            request.metadata["team_template_id"] = binding.template_id
            return binding.team_name

        binding, _template = await _create_generated_team_binding(
            description=query,
            config_base=_effective_config_for_request(request),
        )
        binding_store = get_team_binding_store()
        entity_store = get_team_entity_store()
        try:
            binding = binding_store.bind_session(
                team_name=binding.team_name,
                session_id=session_id,
            )
            update_session_metadata(
                session_id=session_id,
                channel_id=request.channel_id or None,
                user_content=query,
                mode=canonical_mode,
                team_name=binding.team_name,
                runtime_team_name=TeamManager.build_session_scoped_team_name(
                    binding.team_name,
                    session_id,
                ),
                team_template_id=binding.template_id,
                touch_last_message_at=False,
                sync_write=True,
                sessions_root=sessions_root,
            )
        except Exception:
            cleanup_errors: list[str] = []
            cleanup_steps = (
                lambda: binding_store.unbind_session(
                    team_name=binding.team_name,
                    session_id=session_id,
                ),
                lambda: binding_store.delete(binding.team_name),
                lambda: entity_store.delete_team_directory(binding.team_name),
            )
            for cleanup_step in cleanup_steps:
                try:
                    cleanup_step()
                except Exception as cleanup_exc:  # noqa: BLE001
                    cleanup_errors.append(str(cleanup_exc))
            if cleanup_errors:
                logger.warning(
                    "[OHOS-TeamSync] auto team binding rollback incomplete: "
                    "session_id=%s team_name=%s errors=%s",
                    session_id,
                    binding.team_name,
                    cleanup_errors,
                )
            raise

        params["team_name"] = binding.team_name
        params["team_template_id"] = binding.template_id
        request.metadata = dict(request.metadata or {})
        request.metadata["team_name"] = binding.team_name
        request.metadata["team_template_id"] = binding.template_id
        logger.info(
            "[OHOS-TeamSync] auto-created and bound team before chat: "
            "session_id=%s team_name=%s template_id=%s",
            session_id,
            binding.team_name,
            binding.template_id,
        )
        return binding


def hydrate_team_model_auth(
    request: Any,
    config_base: dict[str, Any],
) -> dict[str, Any]:
    """把 tip ``default_headers`` 鉴权合并进团队成员的 ``models.defaults``。

    OfficeClaw / Huawei MaaS 经 tip ``default_headers`` 环境同步真实
    LLM 鉴权（``Authorization: Basic ...``）；``models.defaults`` 条目
    只带占位 ``huawei-maas-session`` api_key 或干脆没有。deep adapter
    在 agent 创建时（``_build_model_from_entry``）会把这些头合并进
    自己的 model client，但团队成员在 chat stream 里从
    ``config_base.models.defaults`` 构建模型 client——不在任何
    overlay 绑定窗口内——不水合则调用缺 ``Authorization`` 被 MaaS
    拒绝（APIG.0303）。

    仅重绑 tenant env overlay 读一次头（与
    ``_effective_config_for_request`` 同窗口模式），把 Authorization
    合并进每个 ``models.defaults[*].model_client_config.custom_headers``
    （Authorization tip 优先，其余 setdefault——对齐
    ``_build_model_from_entry``），返回浅深拷贝。原 config dict 不做
    原地变更；团队模板快照不持久化 ``models`` 段，凭据只留在内存。

    非 tip 环境（有效 env 无 ``default_headers``）原样返回——包括
    纯磁盘配置流。仅 OHOS 生效。
    """
    if not is_ohos_runtime():
        return config_base
    try:
        from jiuwenswarm.common.local_env_config import (
            bind_agent_env_ns,
            bind_task_env_overlay,
            build_effective_env_overlay,
            read_default_headers,
            reset_agent_env_ns,
            reset_task_env_overlay,
        )
        from jiuwenswarm.server.runtime.sync_agents_configs import (
            materialize_sync_env,
        )
        from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
        from jiuwenswarm.server.runtime.tenant_catalog_registry import (
            TenantCatalogRegistry,
        )
        from jiuwenswarm.server.runtime.tenant_context import (
            bind_workspace_key,
            reset_workspace_key,
        )

        agent_id, service_id, workspace_key = TenantAgentPool.extract_ids(request)
        env: dict[str, Any] = {}
        spec = TenantCatalogRegistry.get_instance().get(service_id, agent_id)
        if spec is not None and isinstance(spec.env, dict):
            env = materialize_sync_env(spec.env) or {}

        ns_token = bind_agent_env_ns(service_id, agent_id)
        wk_token = bind_workspace_key(workspace_key)
        try:
            overlay_token = bind_task_env_overlay(
                build_effective_env_overlay(
                    env, service_id=service_id, agent_id=agent_id
                )
            )
            try:
                tip_headers = read_default_headers()
            finally:
                reset_task_env_overlay(overlay_token)
        finally:
            reset_agent_env_ns(ns_token)
            reset_workspace_key(wk_token)

        if not tip_headers:
            return config_base

        models = config_base.get("models")
        if not isinstance(models, dict):
            return config_base
        defaults = models.get("defaults")
        if not isinstance(defaults, list) or not defaults:
            return config_base

        hydrated = dict(config_base)
        models_copy = dict(models)
        defaults_copy = []
        merged_entries = 0
        for entry in defaults:
            if not isinstance(entry, dict):
                defaults_copy.append(entry)
                continue
            model_client_config = entry.get("model_client_config")
            if not isinstance(model_client_config, dict):
                defaults_copy.append(entry)
                continue
            client_config_copy = dict(model_client_config)
            existing = client_config_copy.get("custom_headers")
            merged = dict(existing) if isinstance(existing, dict) else {}
            for header_name, header_value in tip_headers.items():
                key = str(header_name)
                # tip 凭据必须赢得 Authorization（MaaS Basic auth）。
                if key.lower() == "authorization":
                    merged[key] = str(header_value)
                else:
                    merged.setdefault(key, str(header_value))
            client_config_copy["custom_headers"] = merged
            defaults_copy.append(
                {**entry, "model_client_config": client_config_copy}
            )
            merged_entries += 1
        if not merged_entries:
            return config_base
        models_copy["defaults"] = defaults_copy
        hydrated["models"] = models_copy
        logger.info(
            "[OHOS-TeamSync] hydrated tip default_headers into team model "
            "config: service_id=%s agent_id=%s entries=%d header_keys=%s",
            service_id,
            agent_id,
            merged_entries,
            sorted(str(name) for name in tip_headers),
        )
        return hydrated
    except Exception:
        logger.warning(
            "[OHOS-TeamSync] team model auth hydration failed; falling back "
            "to unhydrated config_base",
            exc_info=True,
        )
        return config_base
