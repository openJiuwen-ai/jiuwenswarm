# Copyright (c) Huawei Technologies, Co., Ltd. 2026. All rights reserved.
"""Flash 模式适配器 — 极简单 agent profile。

仿照 :class:`JiuwenSwarmCodeAdapter` 的「独立 mode + 继承 DeepAdapter」结构，flash
是和 agent / code 平级的独立 mode：``create_adapter(mode="flash")`` 工厂分叉，
``AgentManager`` 按 ``mode:sub_mode:project_dir`` 缓存到独立 facade，同一 sidecar
进程同时服务 flash 与 agent 两套 agent，互不串味、不重启。

flash 是一个**固定语义的 mode**——它的定义（行为开关 + rail 白名单）和它的实现
都内聚在本文件；工具权限沿用全局 config，其余 profile 行为不依赖 config.yaml。
父类只提供通用工具工厂与生命周期扩展点，不感知具体 flash 工具。这与 code 模式
一致：CodeAdapter 把 ``_FIXED_RAIL_NAMES`` / ``_is_code_agent`` 等定义硬编码为
类常量，flash 同样把行为开关与 keep 白名单作为类常量。

flash 行为由类常量定义：
- :data:`_FLASH_REACT_OVERRIDE` — react/evolution 覆盖（``enable_task_loop=false`` 等），
  深合并进 config_base，使 ``_resolve_enable_task_loop`` 读到 task_loop=false。
- :data:`_FLASH_RAIL_KEEP` — rail 白名单（attr_name），``_instantiate_rails`` 实例化
  前据此裁剪 ``_build_agent_rails`` 的全量 rail 表。
- :data:`_PROFILE_PROTECTED_RAILS` — 白名单下也必留的 rail（丢掉会留下半成品
  依赖或破坏安全不变量，如 ``_disabled_tools_rail``）。

覆盖宿主方法注入 flash 行为，主要分为三组：
- 工具工厂与生命周期 — ``_build_web_tools`` / ``_build_cron_tools`` /
  ``_cron_tool_names`` / ``_build_progressive_tool_rail``，仅为 flash 注册
  ``web_flash`` 与 ``cron_flash``。
- :meth:`create_instance` — 先合并 flash react 覆盖进 config_base，再
  ``await super().create_instance(...)``。super 内部四步（``_resolve_instance_config_base``
  补缺 / ``coalesce_config_skill_envs`` / ``merge_memory_config_into_config`` /
  ``_merge_enterprise_models_into_config``）都是补缺或只动各自字段，flash 合并进去的
  ``react.evolution`` 一路存活到 ``_resolve_enable_task_loop``。
- :meth:`_apply_reload_config_snapshot` — super 把 ``_config_cache`` 重置成全局
  react，这里再合一次 flash 覆盖并重置两个 cache，防止热重载把 task_loop 翻回 true。
- :meth:`_instantiate_rails` — super 的 ``_build_agent_rails`` 实例化前调用本方法；
  先按白名单裁剪 rail_infos，再 ``super()`` 实例化。
- :meth:`_update_rails_for_mode` — 每请求调用（interface_deep.py:10596）。父类会调
  ``_update_agent_rails()`` 动态注册 context_assemble / memory 等 rail，绕过
  ``_instantiate_rails`` 的白名单裁剪——flash 覆盖此方法不调 ``_update_agent_rails()``，
  只卸载白名单外的动态 rail，闭合白名单。
- :meth:`try_start_dreaming` / :meth:`try_stop_dreaming` — no-op。flash 是「无自演进」
  profile，memory dreaming（被动记忆巩固）属自演进范畴，不挂。

要调 flash 的行为，改本文件的类常量（纳入代码审查/版本管理），而非 config.yaml。
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any

from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import (
    add_collection,
    call_phone,
    convert_timestamp_to_utc8_time,
    create_alarm,
    create_calendar_event,
    create_note,
    delete_alarm,
    delete_collection,
    get_user_location,
    image_reading,
    modify_alarm,
    modify_note,
    query_collection,
    save_file_to_file_manager,
    save_media_to_gallery,
    search_alarms,
    search_calendar_event,
    search_contact,
    search_file,
    search_message,
    search_notes,
    search_photo_gallery,
    send_message,
    upload_file,
    upload_photo,
    view_push_result,
    xiaoyi_gui_agent,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _CRON_TOOL_NAMES,
    _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
    _RailBuildInfo,
    _resolve_instance_config_base,
    acp_chat,
    create_vision_tools,
    generate_image,
    get_config,
    is_skill_retrieval_enabled,
    SkillToolkit,
    SymphonyToolkit,
    video_understanding,
    wiki_ingest,
    wiki_lint,
    wiki_query,
)

logger = logging.getLogger(__name__)


class JiuwenSwarmFlashAdapter(JiuWenSwarmDeepAdapter):
    """Flash 模式适配器 — 极简单 agent profile（单轮、无 task loop、无自演进）。

    继承 :class:`JiuWenSwarmDeepAdapter`，通过工具工厂、配置合并和 rail 生命周期
    扩展点注入 flash 行为，不重写 ``_build_agent_rails`` 本体——复用父类按 mode
    构建的 rail 表，只在其实例化前用白名单裁剪。
    """

    # ── flash 行为定义（类常量，仿 CodeAdapter._FIXED_RAIL_NAMES） ─────────
    # react/evolution 覆盖：深合并进 config_base 的 react 段。这些是 flash「单轮、
    # 无 task loop、无自演进」的定义性属性，非可调参数。
    #   enable_task_loop=false       — flash 不跑多轮 task 协调循环，只单轮 ReAct。
    #   skill_evolution=false        — 门控游离的 SkillEvolutionRail（由
    #                                  get_evolution_enabled 读，不走 keep/drop 表）；
    #                                  keep/drop 碰不到它，必须这里关。
    #   skill_create=false           — 与 review_trigger 同为 false，否则触发 force-revive
    #                                  把 enable_task_loop 强拉回 true。
    #   review_trigger=false         — 同上。
    _FLASH_REACT_OVERRIDE: dict[str, Any] = {
        "enable_task_loop": False,
        "evolution": {
            "skill_evolution": False,
            "skill_create": False,
            "review_trigger": False,
        },
    }

    # rail 白名单（attr_name）：只保留这些 + PROTECTED，其余 drop。这是 flash 能力
    # 的边界——和 code 模式的 _FIXED_RAIL_NAMES 同性质，是 mode 的固定定义。
    #
    # _skill_rail（SkillUseRail）：技能执行能力的唯一来源——SkillTool /
    # ListSkillTool 由其 setup() 注册（全仓库无其它注册点），技能目录/引导 prompt
    # 也由它注入。「技能执行」≠「技能演进」：前者是跑已安装技能，flash 必须保留
    # （否则 search/install 得到却执行不了，后台 skill_turbo PPT 等直接失败）；
    # 后者（SkillEvolutionRail / SkillCreateRail）属自演进，flash 不留。两者分开。
    # _skill_credential_injection_rail：技能 envs（react.skill_envs 凭证）注入，
    # 没有 it 则带凭证的技能跑不起来，与 _skill_rail 同进同出。
    # _ask_user_rail：交互请求由 _set_user_interaction_enabled（在
    # _update_rails_for_mode 之后调用）按 supports_user_interaction 动态挂载/卸载，
    # 非交互请求自动卸载。放进白名单是为了让 _drop_non_whitelist_dynamic_rails
    # 跳过它，避免每请求「卸载→重建」churn；其生命周期交给
    # _set_user_interaction_enabled 管理。
    _FLASH_RAIL_KEEP: frozenset[str] = frozenset({
        "_runtime_prompt_rail",
        "_response_prompt_rail",
        "_stream_event_rail",
        "_task_planning_rail",
        "_security_rail",
        "_heartbeat_rail",
        "_subagent_rail",
        "_permission_rail",
        "_context_processor_rail",
        # _filesystem_rail: SysOperationRail, init() 注册 ReadFile/WriteFile/
        #   EditFile/Glob/ListDir/Grep/Bash；丢掉=无文件/shell 能力。
        "_filesystem_rail",
        # _progressive_tool_rail: ProgressiveToolRail, 系统提示词中的渐进式工具引导。
        "_progressive_tool_rail",
        # 技能执行 + 凭证注入：见上方注释，flash 必须能跑已安装技能。
        "_skill_rail",
        "_skill_credential_injection_rail",
        # ask_user：交互请求动态挂载，留白名单避免 churn（见上方注释）。
        "_ask_user_rail",
    })

    # PROTECTED rails are always built even under a keep-whitelist (dropping
    # them would orphan a dependent that reuses an engine they build, leaving
    # it half-built, or break a security invariant).
    #
    # _disabled_tools_rail：react.disabled_tools 黑名单的唯一执行机制。基线
    # (_build_agent_rails) 将其标注为 security boundary 并无条件 append（不进
    # keep/drop 表），CodeAdapter 也显式保留并注释「跨 mode 统一执行黑名单」。
    # flash 必须同样保留——裁掉会让被禁工具 advertise 且可执行（安全回归）。
    _PROFILE_PROTECTED_RAILS = frozenset({
        "_permission_rail",
        "_disabled_tools_rail",
    })

    def __init__(
        self,
        workspace_dir: str | None = None,
        agent_id: str | None = None,
        service_id: str | None = None,
    ) -> None:
        super().__init__(workspace_dir=workspace_dir, agent_id=agent_id, service_id=service_id)

    def _build_web_tools(self, agent_id: str, cache: Any | None = None) -> list[Any]:
        """Expose the single ``web_flash`` card only for the Flash profile."""
        from jiuwenswarm.agents.harness.flash.tools.web_flash import (
            build_web_flash_tool,
        )

        return [
            build_web_flash_tool(
                agent_id=agent_id,
                language=self._resolve_runtime_language(),
                cache=cache,
            )
        ]

    def _build_cron_tools(self) -> list[Any]:
        """Expose the single ``cron_flash`` card only for the Flash profile."""
        from jiuwenswarm.agents.harness.flash.tools.cron_flash import (
            build_cron_flash_tool,
        )

        service_id = getattr(self, "_env_service_id", None)
        tenant_agent_id = getattr(self, "_env_agent_id", None)
        backend = self._cron_runtime.get_backend(
            service_id=service_id,
            agent_id=tenant_agent_id,
        )
        if backend is None:
            logger.warning(
                "[JiuwenSwarmFlashAdapter] cron backend is not ready, skip cron_flash"
            )
            return []
        self._cron_runtime.ensure_scheduler_started(
            service_id=service_id,
            agent_id=tenant_agent_id,
        )
        return [
            build_cron_flash_tool(
                backend,
                context=self._runtime_cron_tool_context,
                agent_id=self._tool_owner_id(),
                language=self._resolve_runtime_language(),
            )
        ]

    @staticmethod
    def _cron_tool_names() -> frozenset[str]:
        """Use the Flash profile's merged cron card for lifecycle checks."""
        return frozenset({"cron_flash"})

    async def _get_tool_cards(self, agent_id: str):
        """Build flash tool cards.

        这是 :meth:`JiuWenSwarmDeepAdapter._get_tool_cards` 的逐字副本，唯一差异：
        web 工具段由父类的 ``build_jiuwen_harness_named_web_tools``（web_search +
        fetch_webpage 多卡）换成 flash 的 ``self._build_web_tools``（单张 web_flash
        卡）。整方法复制而非 super(). 后处理，是为避免父类把 Deep web 卡注册进
        ability_manager 后再移除的 add+remove 噪声，也避免动 interface_deep.py。
        其余工具段（wiki/vision/audio/video/image_gen/xiaoyi/skill/symphony/acp/
        deepresearch/extra）与父类逐字一致——flash 的能力裁剪在 rail 白名单层做，
        不在 tool_cards 注册层做。
        """
        tool_cards = []

        for wtool in [wiki_ingest, wiki_query, wiki_lint]:
            registered = self._register_shared_tool(wtool)
            tool_cards.append(registered.card)

        from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import (
            get_agent_cache_registry,
        )

        content_cache = await get_agent_cache_registry().get_cache(agent_id)
        for tool_instance in self._build_web_tools(agent_id=agent_id, cache=content_cache):
            registered = self._register_agent_owned_tool(tool_instance, agent_id)
            tool_cards.append(registered.card)

        self._vision_tools = []
        self._vision_tools_registered = False
        if self._vision_model_config is not None:
            try:
                for tool in create_vision_tools(
                    language=self._resolve_runtime_language(),
                    vision_model_config=self._vision_model_config,
                    agent_id=agent_id,
                ):
                    registered = self._register_agent_owned_tool(tool, agent_id)
                    tool_cards.append(registered.card)
                    self._vision_tools.append(registered)
                self._vision_tools_registered = bool(self._vision_tools)
            except Exception as exc:
                self._vision_tools = []
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] vision tools registration failed: %s",
                    exc,
                )

        self._audio_tools = []
        self._audio_tools_registered = False
        try:
            self._audio_tools = []
            for tool in self._iter_runtime_audio_tools(agent_id):
                registered = self._register_agent_owned_tool(tool, agent_id)
                tool_cards.append(registered.card)
                self._audio_tools.append(registered)
            self._audio_tools_registered = bool(self._audio_tools)
        except Exception as exc:
            self._audio_tools = []
            logger.warning(
                "[JiuWenSwarmDeepAdapter] audio tools registration failed: %s",
                exc,
            )

        self._video_tool_registered = False
        if self._video_model_config:
            try:
                registered = self._register_shared_tool(video_understanding)
                tool_cards.append(registered.card)
                self._video_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] video tool registration failed: %s",
                    exc,
                )

        # generate_image tool: use dedicated image_gen model config
        self._image_gen_tool_registered = False
        if self._image_gen_model_config:
            try:
                registered = self._register_shared_tool(generate_image)
                tool_cards.append(registered.card)
                self._image_gen_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] generate_image tool registration failed: %s",
                    exc,
                )

        # 小艺手机端工具：由 channels.xiaoyi.phone_tools_enabled 控制
        config_base = get_config()
        xiaoyi_phone_tools_enabled = (
            config_base.get("channels", {}).get("xiaoyi", {}).get("phone_tools_enabled", False)
        )
        if xiaoyi_phone_tools_enabled and not self._xiaoyi_phone_tools_registered:
            _xiaoyi_tools = [
                get_user_location,
                create_note,
                search_notes,
                modify_note,
                create_calendar_event,
                search_calendar_event,
                search_contact,
                search_photo_gallery,
                upload_photo,
                search_file,
                upload_file,
                call_phone,
                send_message,
                search_message,
                create_alarm,
                search_alarms,
                modify_alarm,
                delete_alarm,
                query_collection,
                add_collection,
                delete_collection,
                save_media_to_gallery,
                save_file_to_file_manager,
                convert_timestamp_to_utc8_time,
                view_push_result,
                image_reading,
                xiaoyi_gui_agent,
            ]
            try:
                for xt in _xiaoyi_tools:
                    registered = self._register_shared_tool(xt)
                    tool_cards.append(registered.card)
                self._xiaoyi_phone_tools_registered = True
                logger.info(
                    "[JiuWenSwarmDeepAdapter] %d xiaoyi phone tools registered", len(_xiaoyi_tools)
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] xiaoyi phone tools registration failed: %s", exc
                )

        try:
            skill_toolkit = SkillToolkit(
                manager=self._skill_manager,
                service_id=self._service_id,
                agent_id=self._agent_id,
                on_installed_skills_changed=self.refresh_enabled_skills_from_db,
            )
            skill_tool_names: list[str] = []
            for tool in skill_toolkit.get_tools():
                registered = self._register_shared_tool(tool)
                tool_cards.append(registered.card)
                skill_tool_names.append(registered.card.name)
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillToolkit registered: tools=%s",
                skill_tool_names,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] skill tools registration failed: %s", exc)

        if is_skill_retrieval_enabled():
            try:
                self._skill_retrieval_tools = self._create_skill_retrieval_tools()
                skill_retrieval_tool_names: list[str] = []
                for tool in self._skill_retrieval_tools:
                    registered = self._register_shared_tool(tool)
                    tool_cards.append(registered.card)
                    skill_retrieval_tool_names.append(registered.card.name)
                self._skill_retrieval_tools_registered = bool(self._skill_retrieval_tools)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit registered: tools=%s",
                    skill_retrieval_tool_names,
                )
            except Exception as exc:
                self._skill_retrieval_tools = []
                self._skill_retrieval_tools_registered = False
                logger.warning("[JiuWenSwarmDeepAdapter] skill retrieval tools registration failed: %s", exc)
        else:
            self._skill_retrieval_tools = []
            self._skill_retrieval_tools_registered = False
            logger.info("[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit skipped: disabled")

        try:
            symphony_toolkit = SymphonyToolkit()
            symphony_tool_names: list[str] = []
            symphony_tools = symphony_toolkit.get_tools(config_base)
            for tool in symphony_tools:
                registered = self._register_shared_tool(tool)
                tool_cards.append(registered.card)
                symphony_tool_names.append(registered.card.name)
            self._symphony_tools = list(symphony_tools)
            self._symphony_tools_registered = bool(symphony_tools)
            logger.info(
                "[JiuWenSwarmDeepAdapter] SymphonyToolkit registered: tools=%s",
                symphony_tool_names,
            )
        except Exception as exc:
            self._symphony_tools = []
            self._symphony_tools_registered = False
            logger.warning(
                "[JiuWenSwarmDeepAdapter] orchestration tools registration failed: %s",
                exc,
            )

        # acp_chat: forward prompts to external stdio ACP agents (see acp_agents in config.yaml)
        try:
            acp_cfg = get_config().get("acp_agents")
            if isinstance(acp_cfg, dict) and acp_cfg:
                registered = self._register_shared_tool(acp_chat)
                tool_cards.append(registered.card)
                logger.info("[JiuWenSwarmDeepAdapter] acp_chat tool registered")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] acp_chat registration failed: %s", exc)

        self._register_deepresearch_tool_cards(tool_cards)

        # 动态加载环境变量配置的非侵入式工具扩展（AGENT_EXTRA_TOOLS，仅企业版）
        self._append_extra_tool_cards(tool_cards)

        return tool_cards

    def _ensure_cron_tools_registered(self, session_id: str | None) -> None:
        """Register this agent's cron tools once, rebuilding only when they change.

        这是 :meth:`JiuWenSwarmDeepAdapter._ensure_cron_tools_registered` 的逐字
        副本，唯一差异：cron 卡名集合由父类的 ``_CRON_TOOL_NAMES`` 换成 flash 的
        ``self._cron_tool_names()``（返回 ``{"cron_flash"}``），与 flash 的
        ``cron_flash`` 单卡生命周期对齐。整方法复制而非 super(). 后处理，是因为
        父类方法用 ``_CRON_TOOL_NAMES`` 操作 Deep 的 cron 卡，flash 必须用
        ``cron_flash`` 名集合才能正确注册/移除/指纹缓存自己的 cron_flash 卡——
        super(). 走的是 Deep 名集合，后处理无法干净纠正。
        """
        if session_id is not None and session_id.startswith(("heartbeat", "cron")):
            return
        if os.getenv("JIUWENCLAW_DISABLE_CRON_TOOLS") == "1":
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "") in self._cron_tool_names():
                    self._instance.ability_manager.remove(existing.name)
            self._cron_tools_registered_language = None
            logger.info(
                "[JiuWenSwarmDeepAdapter] skip cron tool registration: disabled by env"
            )
            return
        language = self._resolve_runtime_language()
        registered_names = {
            getattr(existing, "name", "")
            for existing in (self._instance.ability_manager.list() or [])
        }
        # The language fingerprint alone is not enough: rebuilding the agent (a
        # skill or plugin install re-runs ``create_instance``) hands this adapter
        # a fresh, empty AbilityManager while the fingerprint still reads as
        # registered, which would silently drop the cron tools for good.
        if self._cron_tools_registered_language == language and (registered_names & self._cron_tool_names()):
            return
        try:
            cron_tools = self._build_cron_tools()
            if not cron_tools:
                return
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "") in self._cron_tool_names():
                    self._instance.ability_manager.remove(existing.name)
            for cron_tool in cron_tools:
                self._register_agent_owned_tool(cron_tool, self._tool_owner_id())
                self._instance.ability_manager.add(cron_tool.card)
            self._cron_tools_registered_language = language
            logger.info(
                "[JiuWenSwarmDeepAdapter] %d cron tools registered: language=%s",
                len(cron_tools),
                language,
            )
        except Exception as exc:
            logger.error("[JiuWenSwarmDeepAdapter] 定时工具注册失败: %s", exc)

    def _build_progressive_tool_rail(self, config: dict[str, Any]) -> Any:
        """Keep the two merged Flash tools visible to the model."""
        flash_config = copy.deepcopy(config) if isinstance(config, dict) else {}
        lazy_config = flash_config.setdefault("tool_lazy_load", {})
        if not isinstance(lazy_config, dict):
            lazy_config = {}
            flash_config["tool_lazy_load"] = lazy_config
        configured = lazy_config.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS)
        eager_tools = list(configured) if isinstance(configured, list) else list(
            _DEFAULT_PROGRESSIVE_EAGER_TOOLS
        )
        replaced_tool_names = {"web_search", "fetch_webpage"} | set(
            _CRON_TOOL_NAMES
        )
        retained_tools = []
        for name in eager_tools:
            if name in replaced_tool_names:
                continue
            retained_tools.append(name)
        eager_tools = retained_tools
        for name in ("web_flash", "cron_flash"):
            if name not in eager_tools:
                eager_tools.append(name)
        lazy_config["eager_tools"] = eager_tools
        return super()._build_progressive_tool_rail(flash_config)

    # ── 宿主方法覆盖 ──────────────────────────────────────────────

    async def create_instance(
        self,
        config: dict[str, Any] | None = None,
        *,
        mode: str = "agent",
        sub_mode: str = None,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        """初始化 DeepAgent 实例（flash 模式）.

        先 resolve config_base（合并模板默认），再把 flash 的 react/evolution 覆盖
        合并进去，然后委托父类 ``create_instance`` 完成其余初始化。父类会再 resolve
        一次 config_base（``fill_template_defaults`` 幂等补缺，flash 的显式 react 值
        不被覆盖），且后续 coalesce / merge_memory / merge_enterprise_models 各只动
        自己的字段，故 flash 的 ``enable_task_loop=false`` 等配置一路传到
        ``_resolve_enable_task_loop`` 与 ``_build_agent_rails``。
        """
        resolved = _resolve_instance_config_base(config_base)
        resolved = self._apply_flash_react_override(resolved)
        await super().create_instance(
            config=config, mode=mode, sub_mode=sub_mode, config_base=resolved
        )

    async def _apply_reload_config_snapshot(
        self,
        config_base: dict[str, Any] | None,
        env_overrides: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Reload 后重合 flash react 覆盖，防止热重载把 task_loop 翻回 true.

        父类 ``_apply_reload_config_snapshot`` 把 ``_config_base_cache`` /
        ``_config_cache`` 重置成全局 react；这里在 super 返回后再合一次 flash 覆盖，
        并重置这两个 cache，使后续 model / configure 路径读到 flash 配置。等价于
        旧 per-session 方案里 reload 的 profile 重合，但通过覆盖方法实现，父类无需
        预埋 hook。
        """
        config_base = await super()._apply_reload_config_snapshot(config_base, env_overrides)
        config_base = self._apply_flash_react_override(config_base)
        self._config_base_cache = config_base.copy()
        self._config_cache = config_base.get("react", {}).copy()
        return config_base

    def _instantiate_rails(
        self,
        rail_infos: list["_RailBuildInfo"],
        config_base: dict[str, Any],
    ) -> list[Any]:
        """按 flash 白名单过滤后再实例化 rails.

        父类 ``_build_agent_rails`` 末尾调用本方法；先按 ``_FLASH_RAIL_KEEP`` 白名单
        裁剪 rail_infos，再委托父类 ``_instantiate_rails`` 逐个构建并挂载 standing rails。
        """
        rail_infos = self._filter_rail_infos_by_keep(rail_infos)
        return super()._instantiate_rails(rail_infos, config_base)

    async def _update_rails_for_mode(self, mode: str) -> None:
        """flash 的 rail 生命周期：闭合白名单，不动态注册全量 rail.

        父类 ``_update_rails_for_mode``（interface_deep.py:9990，每请求由
        :10595 调用）调 ``_update_agent_rails()`` 动态注册 ``_context_assemble_rail`` /
        ``_memory_rail`` / ``_external_memory_rail`` 等 rail——这些**不在** flash 白名单
        内，却会绕过 ``_instantiate_rails`` 的冷启动裁剪被挂上，使白名单形同虚设。

        flash 覆盖此方法：**不调** ``_update_agent_rails()``，只做两件事——
        1. 记 ``_last_mode``（父类首行本就做）；
        2. 卸载白名单（``_FLASH_RAIL_KEEP`` ∪ ``_PROFILE_PROTECTED_RAILS``）外的、
           可能被父类/旧 reload 残留注册的动态 rail。

        白名单内的 rail（task_planning / context_processor / skill 等）冷启动已由
        ``_instantiate_rails`` 挂好，这里不重复注册。

        关于 ``_ask_user_rail``：它在白名单内，故本方法的卸载步骤**跳过** it——其
        生命周期交由紧随本方法之后调用的 ``_set_user_interaction_enabled``（interface_deep.py:
        10599）按 ``supports_user_interaction`` 管理：交互请求挂载、非交互请求卸载。
        把它留在白名单正是为了避免每请求「本方法卸载→_set_user_interaction_enabled
        重建」的 churn（参见 PR6431 review #4 修正）。
        """
        self._last_mode = mode
        await self._drop_non_whitelist_dynamic_rails()

    async def try_start_dreaming(self, busy_checker=None) -> None:
        """no-op：flash 不启 memory dreaming.

        flash 是「无自演进」profile（``enable_task_loop=false`` / ``skill_evolution=
        false``），memory dreaming（被动记忆巩固，跑在 idle 时合并会话记忆）属自演进
        范畴，与 flash 语义不符。父类 ``try_start_dreaming`` 对 flash 落到
        ``_dreaming_mode="agent"``（interface_deep.py:9044，``mode.startswith("agent")``
        else ``"agent"``）仍会启 dreaming，故这里显式 no-op 拦截，避免 flash facade
        误启后台记忆巩固。
        """
        return

    async def try_stop_dreaming(self) -> None:
        """no-op：与 ``try_start_dreaming`` 对称，flash 从未启 dreaming，停亦空操作."""
        return

    # ── flash react 覆盖 / rail 裁剪 helpers ──────────────────────

    def _apply_flash_react_override(
        self, config_base: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Deep-merge ``_FLASH_REACT_OVERRIDE`` into config_base's react section.

        Only the react section (incl. nested sub-trees like evolution) is
        overlaid; models, routing, etc. are untouched. Force-revive safe: the
        merge runs before _resolve_enable_task_loop so flash's
        evolution.skill_create=false keeps task_loop=false.

        Recursive: a profile sub-tree (e.g. ``react.evolution``) is deep-merged
        into the base rather than wholesale-replaced, so global deep keys under a
        partially-overridden sub-tree are not silently lost. Non-dict values
        replace outright, matching the intent of an explicit override.
        """
        out = copy.deepcopy(config_base) if isinstance(config_base, dict) else {}
        base_react = out.get("react")
        if not isinstance(base_react, dict):
            base_react = {}
            out["react"] = base_react
        for k, v in self._FLASH_REACT_OVERRIDE.items():
            base_react[k] = self._deep_merge_react_value(base_react.get(k), v)
        return out

    @staticmethod
    def _deep_merge_react_value(base: Any, override: Any) -> Any:
        """Deep-merge ``override`` onto ``base`` for the react overlay.

        Two dicts => recurse so a partially-overridden sub-tree keeps the base's
        sibling keys (e.g. overriding react.evolution.skill_create keeps the
        base's react.evolution.review_trigger). Anything else => the override
        replaces the base (deepcopy so the merged config owns its own copy and
        later mutation never leaks back).
        """
        if isinstance(base, dict) and isinstance(override, dict):
            merged = {k: copy.deepcopy(v) for k, v in base.items()}
            for k, v in override.items():
                merged[k] = JiuwenSwarmFlashAdapter._deep_merge_react_value(
                    merged.get(k), v
                )
            return merged
        return copy.deepcopy(override)

    def _filter_rail_infos_by_keep(
        self,
        rail_infos: list["_RailBuildInfo"],
    ) -> list["_RailBuildInfo"]:
        """Filter rail_infos by ``_FLASH_RAIL_KEEP`` + PROTECTED rails.

        keep: whitelist (attr_names); only these plus PROTECTED are built. All
        others are dropped (PROTECTED always kept — these are rails whose
        absence breaks a dependent or a security invariant, e.g.
        ``_disabled_tools_rail``).
        """
        if not self._FLASH_RAIL_KEEP:
            return rail_infos
        out: list["_RailBuildInfo"] = []
        dropped: list[str] = []
        for info in rail_infos:
            name = info.attr_name
            if name in self._PROFILE_PROTECTED_RAILS or name in self._FLASH_RAIL_KEEP:
                out.append(info)
            else:
                dropped.append(name)
        if dropped:
            logger.info(
                "[JiuwenSwarmFlashAdapter] profile=flash dropped rails: %s",
                dropped,
            )
        return out

    # _update_rails_for_mode 动态注册的 rail attr_name → 人类可读标签。
    # 父类 _update_agent_rails() 会动态注册这一组 rail（task_planning /
    # context_assemble / memory / evolution / skill_create 等）。flash 只保留
    # 白名单内的，故逐个检查：白名单外的若被挂上则卸载——闭合 _instantiate_rails
    # 的冷启动裁剪，防止每请求的动态注册穿透白名单。
    _DYNAMIC_RAIL_ATTRS: tuple[tuple[str, str], ...] = (
        ("_task_planning_rail", "TaskPlanningRail"),
        ("_ask_user_rail", "StructuredAskUserRail"),
        ("_context_assemble_rail", "ContextAssembleRail"),
        ("_context_processor_rail", "ContextProcessorRail"),
        ("_memory_rail", "MemoryRail"),
        ("_external_memory_rail", "ExternalMemoryRail"),
        ("_skill_evolution_rail", "SkillEvolutionRail"),
        ("_evolution_interrupt_rail", "EvolutionInterruptRail"),
        ("_skill_create_rail", "SkillCreateRail"),
    )

    async def _drop_non_whitelist_dynamic_rails(self) -> None:
        """Unregister any dynamically-registered rail outside the flash whitelist.

        Called from :meth:`_update_rails_for_mode` each request. Rails inside
        ``_FLASH_RAIL_KEEP`` ∪ ``_PROFILE_PROTECTED_RAILS`` are left mounted
        (they belong to the flash profile); anything else the parent may have
        registered via ``_update_agent_rails`` is unregistered so the live rail
        set stays within the profile boundary. Idempotent: rails already None
        are skipped.
        """
        keep = self._FLASH_RAIL_KEEP | self._PROFILE_PROTECTED_RAILS
        dropped: list[str] = []
        for attr, label in self._DYNAMIC_RAIL_ATTRS:
            if attr in keep:
                continue
            rail = getattr(self, attr, None)
            if rail is None:
                continue
            try:
                await self._instance.unregister_rail(rail)
            except Exception:
                logger.warning(
                    "[JiuwenSwarmFlashAdapter] failed to unregister %s (%s)",
                    label, attr, exc_info=True,
                )
            setattr(self, attr, None)
            dropped.append(label)
        if dropped:
            logger.info(
                "[JiuwenSwarmFlashAdapter] profile=flash dropped dynamic rails: %s",
                dropped,
            )


__all__ = ["JiuwenSwarmFlashAdapter"]
