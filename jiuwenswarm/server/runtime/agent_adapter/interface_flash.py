# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash 模式适配器 — 极简单 agent profile。

仿照 :class:`JiuwenSwarmCodeAdapter` 的「独立 mode + 继承 DeepAdapter」结构，flash
是和 agent / code 平级的独立 mode：``create_adapter(mode="flash")`` 工厂分叉，
``AgentManager`` 按 ``mode:sub_mode:project_dir`` 缓存到独立 facade，同一 sidecar
进程同时服务 flash 与 agent 两套 agent，互不串味、不重启。

flash 是一个**固定语义的 mode**——它的定义（行为开关 + rail 白名单 + 工具面）和
它的实现都内聚在本文件；工具权限沿用全局 config，其余 profile 行为不依赖
config.yaml。父类只提供通用工具工厂与生命周期扩展点，不感知具体 flash 工具。
这与 code 模式
一致：CodeAdapter 把 ``_FIXED_RAIL_NAMES`` / ``_is_code_agent`` 等定义硬编码为
类常量，flash 同样把行为开关、keep 白名单与工具面裁剪作为类常量/override。

部署组合提示：``flash.enabled`` 是进程级开关，不区分 channel / 请求来源——
officeclaw 对话与 cron / heartbeat / proactive 等后台流水线同样进入 flash。
flash 白名单不含 ``_llm_retry_rail`` / ``_context_overflow_recovery_rail`` /
``_deepresearch_execution_rail`` 等健壮性 rail，长文档 / 弱网任务的容错弱于
normal；officeclaw 等部署开启 flash 前需评估此影响面，如需按来源豁免应在
mode 解析 guard（``_shared.resolve_agent_request_mode``）增加 channel 过滤。

flash 行为由类常量定义：
- :data:`_FLASH_REACT_OVERRIDE` — react/evolution 覆盖（``enable_task_loop=false``
  等）与渐进式工具的常驻名单（``tool_lazy_load.eager_tools`` 整表替换），深合并
  进 config_base，使 ``_resolve_enable_task_loop`` 读到 task_loop=false。
- :data:`_FLASH_RAIL_KEEP` — rail 白名单（attr_name），``_instantiate_rails`` 实例化
  前据此裁剪 ``_build_agent_rails`` 的全量 rail 表。
- :data:`_PROFILE_PROTECTED_RAILS` — 白名单下也必留的 rail（丢掉会留下半成品
  依赖或破坏安全不变量，如 ``_disabled_tools_rail``）。
- :data:`_FLASH_TOOL_CARD_DROP_NAMES` — ``_get_tool_cards`` 末尾按名剔除的工具卡。

覆盖宿主方法注入 flash 行为，主要分为三组：
- 工具工厂与生命周期 — ``_build_web_tools`` / ``_build_cron_tools`` /
  ``_cron_tool_names`` / ``_build_progressive_tool_rail``，仅为 flash 注册
  ``web_flash`` 与 ``cron_flash``。
- :meth:`create_instance` — 先合并 flash react 覆盖进 config_base，再
  ``await super().create_instance(...)``。
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

工具面 override（rail 构建 / 工具卡注册两个层面，类都来自
``jiuwenswarm.agents.harness.flash`` 包）：
- :meth:`_build_task_planning_rail` → FlashTodoRail（todo 4→1 统一工具）
- :meth:`_build_memory_rail` → FlashMemoryRail（memory 5→1 统一工具 + 群聊只读开关）
- :meth:`_build_filesystem_rail` → SlimSysOperationRail（不注册 powershell /
  list_files；glob / read 注册 flash 增强版）
- :meth:`_resolve_skill_mode` — 钉死 ALL（ListSkillTool 仅 AUTO_LIST 注册，flash
  技能发现走 search_skill 自动安装闭环，技能卡只保留 skill_tool）
- :meth:`_iter_runtime_audio_tools` — 音频全有或全无（无配置不降级 audio_metadata）
- :meth:`_get_tool_cards` — 裁剪 wiki/acp 与 stock 技能三卡，换装 SlimSkillToolkit
  （search_skill 折叠自动安装）
- :meth:`_update_runtime_config` — super() 后修正统一 memory 卡的群聊只读/恢复

reload 一致性：``_get_current_agent_rails`` 的四个复活点（skill_rail /
skill_credential / skill_active_state / disabled_tools）全部在 keep 白名单内，
冷启动表项经 override 构建 flash 变体后，reload 复用/重建走的也是同一批 override，
冷热两态不漂移。

要调 flash 的行为，改本文件的类常量（纳入代码审查/版本管理），而非 config.yaml。
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any

from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.agents.harness.common.memory.config import (
    get_embed_config,
    is_proactive_memory,
)
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
from jiuwenswarm.agents.harness.flash import (
    FlashMemoryRail,
    FlashTodoRail,
    SlimSkillToolkit,
    SlimSysOperationRail,
)
from jiuwenswarm.common.config import (
    get_evolution_review_trigger_enabled,
    get_skill_create_enabled,
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
from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
)

logger = logging.getLogger(__name__)


class JiuwenSwarmFlashAdapter(JiuWenSwarmDeepAdapter):
    """Flash 模式适配器 — 极简单 agent profile（单轮、无 task loop、无自演进）。

    继承 :class:`JiuWenSwarmDeepAdapter`，通过工具工厂、配置合并和 rail 生命周期
    扩展点注入 flash 行为；覆盖五个宿主方法
    （``create_instance`` / ``_apply_reload_config_snapshot`` / ``_instantiate_rails``
    / ``_update_rails_for_mode`` / ``try_start_dreaming``）注入 flash 的行为覆盖与
    rail 裁剪，再覆盖一组构建方法/钩子替换工具面（todo / memory 统一、
    文件系统精简、skill 面只留 skill_tool），不重写 ``_build_agent_rails`` 本体
    ——复用父类按 mode 构建的 rail 表，只在其实例化前用白名单裁剪。
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
    #   tool_lazy_load.eager_tools   — 渐进式工具呈现的常驻名单。深合并对 list 是
    #                                  整表替换，须写全 flash 最终列表（统一 todo
    #                                  替换 todo_create/list/get/modify，去掉
    #                                  list_files；tools_search/invoke_tool 由
    #                                  _normalize_progressive_eager_tools 自动补回）。
    #                                  enabled 不强制——flash 的精简来自 rail 构建层，
    #                                  lazy load 只影响呈现方式，交给部署配置。
    _FLASH_REACT_OVERRIDE: dict[str, Any] = {
        "enable_task_loop": False,
        "evolution": {
            "skill_evolution": False,
            "skill_create": False,
            "review_trigger": False,
        },
        "tool_lazy_load": {
            "eager_tools": [
                "web_search",
                "fetch_webpage",
                "ask_user",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "bash",
                "skill_tool",
                "skill_complete",
                "todo",
            ],
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
        # _filesystem_rail: SlimSysOperationRail（flash override），init() 注册
        #   Read/Write/Edit/Glob/Grep/Bash，不注册 powershell/list_files；
        #   丢掉=无文件/shell 能力。
        "_filesystem_rail",
        # _progressive_tool_rail: ProgressiveToolRail, 系统提示词中的渐进式工具引导。
        "_progressive_tool_rail",
        # 技能执行 + 凭证注入：见上方注释，flash 必须能跑已安装技能。
        # _skill_rail 由 flash 钉 ALL 模式，只注册 skill_tool。
        "_skill_rail",
        "_skill_credential_injection_rail",
        # _skill_active_state_rail / _skill_authorization_rail: 活跃技能状态与
        #   skill_tool 授权门禁——保持与 normal 对等的技能链（上游 flash 裁掉了
        #   这两个，本仓决策保留）。
        "_skill_active_state_rail",
        "_skill_authorization_rail",
        # _memory_rail: FlashMemoryRail（flash override，统一 memory 工具）挂在
        #   动态挂载路径上，进 keep 才不会被 _drop_non_whitelist_dynamic_rails
        #   卸载；闸门是 modes.agent.memory.enabled（flash 记忆档归一 agent）。
        "_memory_rail",
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

    # 工具卡裁剪名单（_get_tool_cards 覆盖里按名剔除）：wiki 三件套与 acp_chat 属
    # normal 会话的能力面，flash 不注册；stock 技能三卡被剔后由 SlimSkillToolkit
    # 的折叠版 search_skill（独立 id）替换。
    _FLASH_TOOL_CARD_DROP_NAMES: frozenset[str] = frozenset({
        "wiki_ingest",
        "wiki_query",
        "wiki_lint",
        "acp_chat",
        "search_skill",
        "install_skill",
        "uninstall_skill",
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

        这是 :meth:`JiuWenSwarmDeepAdapter._get_tool_cards` 的逐字副本，两处差异：
        1. web 工具段由父类的 ``build_jiuwen_harness_named_web_tools``（web_search +
           fetch_webpage 多卡）换成 flash 的 ``self._build_web_tools``（单张 web_flash
           卡）。整方法复制而非 super(). 后处理，是为避免父类把 Deep web 卡注册进
           ability_manager 后再移除的 add+remove 噪声，也避免动 interface_deep.py。
        2. 尾部追加 flash 工具面裁剪：按名剔除（wiki/acp 与 stock 技能三卡）并
           换装 SlimSkillToolkit 的折叠版 search_skill（独立 id，不受共享注册
           表先到先得影响）。被剔除的工具已注册进共享注册表对其他 adapter 无害
           （agent 模式本就注册它们），剔除只影响本会话的可见卡面。
           cron_flash 不经本方法（由 ``_ensure_cron_tools_registered`` 按会话
           生命周期注册）。
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

        # flash 工具面裁剪（见方法 docstring 差异 2）：按名剔除 + slim 换装。
        drop_names = self._FLASH_TOOL_CARD_DROP_NAMES
        tool_cards = [
            card
            for card in tool_cards
            if str(getattr(card, "name", "") or "") not in drop_names
        ]
        try:
            slim_toolkit = SlimSkillToolkit(
                manager=self._skill_manager,
                service_id=self._service_id,
                agent_id=self._agent_id,
                on_installed_skills_changed=self.refresh_enabled_skills_from_db,
            )
            slim_names: list[str] = []
            for tool in slim_toolkit.get_tools():
                registered = self._register_shared_tool(tool)
                tool_cards.append(registered.card)
                slim_names.append(registered.card.name)
            logger.info(
                "[JiuwenSwarmFlashAdapter] SlimSkillToolkit registered: tools=%s",
                slim_names,
            )
        except Exception as exc:
            logger.warning(
                "[JiuwenSwarmFlashAdapter] slim skill tools registration failed: %s",
                exc,
            )

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
        ``_memory_rail`` / ``_external_memory_rail`` 等 rail——这些大多不在 flash 白名单
        内，却会绕过 ``_instantiate_rails`` 的冷启动裁剪被挂上，使白名单形同虚设。

        flash 覆盖此方法：**不调** ``_update_agent_rails()``，只做三件事——
        1. 记 ``_last_mode``（父类首行本就做）；
        2. 记忆 rail：flash 的统一 memory 工具（FlashMemoryRail）走动态挂载路径，
           这里按归一后的 agent 档调 ``_handle_memory_rail_by_config``（闸门
           ``modes.agent.memory.enabled``，未开启则卸载）；``_memory_rail`` 在 keep
           白名单内，不会被后续卸载步骤误删。
        3. 卸载白名单（``_FLASH_RAIL_KEEP`` ∪ ``_PROFILE_PROTECTED_RAILS``）外的、
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
        await self._handle_memory_rail_by_config("agent")
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

    # ── 工具面覆盖（rail 构建 / 工具卡注册） ──────────────────────

    def _build_task_planning_rail(
        self, config: dict[str, Any] | None = None
    ) -> FlashTodoRail | None:
        """flash 用统一 todo rail 替换 TaskPlanningRail（todo 4→1）.

        兼容两处调用：冷启动 rail 表传 react config（统一 todo 无配置项，忽略），
        agent 模式动态挂载不传参。
        """
        try:
            todo_rail = FlashTodoRail()
            logger.info("[JiuwenSwarmFlashAdapter] FlashTodoRail create success")
        except Exception as exc:
            logger.warning("[JiuwenSwarmFlashAdapter] FlashTodoRail create failed: %s", exc)
            todo_rail = None
        return todo_rail

    def _build_memory_rail(self, mode: str) -> FlashMemoryRail | None:
        """flash 用统一 memory rail 替换 MemoryRail（memory 5→1，mode 分发）.

        与父类 ``_build_memory_rail`` 保持同步（差异仅 rail 类与日志标签）。
        flash 记忆档归一到 modes.agent（``is_agent_mode`` 含 "flash"）。
        """
        try:
            config = (
                self._startup_config_base
                if isinstance(self._startup_config_base, dict)
                else get_config()
            )
            embed_config = get_embed_config()
            has_api_key = embed_config.get("api_key") if isinstance(embed_config, dict) else None
            has_base_url = embed_config.get("base_url") if isinstance(embed_config, dict) else None
            has_model = embed_config.get("model") if isinstance(embed_config, dict) else None
            if not all([has_api_key, has_base_url, has_model]):
                logger.warning(
                    "[JiuwenSwarmFlashAdapter] FlashMemoryRail create failed: "
                    "No available embedding config"
                )
            self._is_proactive_memory = is_proactive_memory(mode, config)
            memory_rail = FlashMemoryRail(
                embedding_config=EmbeddingConfig(
                    model_name=embed_config.get("model"),
                    base_url=embed_config.get("base_url"),
                    api_key=embed_config.get("api_key"),
                ),
                is_proactive=self._is_proactive_memory,
            )
            logger.info("[JiuwenSwarmFlashAdapter] FlashMemoryRail create success")
        except Exception as exc:
            logger.warning("[JiuwenSwarmFlashAdapter] FlashMemoryRail create failed: %s", exc)
            memory_rail = None
        return memory_rail

    @staticmethod
    def _build_filesystem_rail() -> SlimSysOperationRail | None:
        """flash 文件系统 rail：不注册 powershell / list_files，
        glob / read 注册 flash 增强版（mtime 排序、多文件并行读）。"""
        try:
            fs_rail = SlimSysOperationRail()
            logger.info("[JiuwenSwarmFlashAdapter] SlimSysOperationRail create success")
        except Exception as exc:
            logger.warning("[JiuwenSwarmFlashAdapter] SlimSysOperationRail create failed: %s", exc)
            fs_rail = None
        return fs_rail

    @staticmethod
    def _resolve_skill_mode(config: dict[str, Any]) -> str:
        """flash 钉死 ALL：ListSkillTool 仅在 AUTO_LIST 注册，flash 的技能发现走
        search_skill 自动安装闭环，技能卡只保留 skill_tool。同时使 reload 的
        skill_mode 变更检测读到稳定值，不会把 rail 拨回 AUTO_LIST。"""
        _ = config
        return SkillUseRail.SKILL_MODE_ALL

    def _iter_runtime_audio_tools(self, agent_id: str | None) -> list[Any]:
        """flash 音频全有或全无：未配置音频模型时不降级注册 audio_metadata.

        flash 会话的音频工具只经本方法进入工具面；无配置返回空列表即完全不
        注册，配置了则与 normal 同等注册全套。
        """
        if self._audio_model_config is None:
            return []
        from openjiuwen.harness.tools import create_audio_tools

        return list(
            create_audio_tools(
                language=self._resolve_runtime_language(),
                audio_model_config=self._audio_model_config,
                agent_id=agent_id,
            )
        )

    async def _update_runtime_config(self, runtime_config: Any) -> None:
        """每回合 super() 后修正统一 memory 卡的可见性。

        super() 的群聊块只认 stock 五件套（对 flash 全是 no-op），且其恢复分支
        会把 stock write_memory/edit_memory 加回 flash 会话（统一卡才是 flash 的
        记忆面）——这里先防御性清扫五件套，再按同一套权限上下文对统一卡做
        三场景修正（全禁→摘卡；群聊分身→只读；其他→恢复+解除只读）。
        """
        await super()._update_runtime_config(runtime_config)
        self._sync_unified_memory_tool_visibility()

    def _sync_unified_memory_tool_visibility(self) -> None:
        """按 TOOL_PERMISSION_CONTEXT 调整统一 memory 工具的可见性/只读。

        与父方法的守卫一致：无权限上下文（普通请求不 set 该 contextvar）时
        不做任何调整。
        """
        perm_ctx = TOOL_PERMISSION_CONTEXT.get()
        if perm_ctx is None:
            return

        instance = self._instance
        if instance is None:
            return

        # 防御性清扫：super() 恢复分支可能把 stock 记忆五件套加回 flash 会话。
        # remove 对不存在的能力是安全 no-op（内部 in 检查 + pop，返回 None），
        # 无需异常包裹。
        for tool_name in (
            "write_memory",
            "edit_memory",
            "read_memory",
            "memory_search",
            "memory_get",
        ):
            instance.ability_manager.remove(tool_name)

        is_group_avatar = perm_ctx.group_digital_avatar and perm_ctx.avatar_mode
        should_disable_memory = (
            not perm_ctx.enable_memory and is_group_avatar
        )
        memory_rail = getattr(self, "_memory_rail", None)

        if should_disable_memory:
            instance.ability_manager.remove("memory")
            if memory_rail is not None:
                memory_rail.set_read_only(True)
        elif is_group_avatar:
            if memory_rail is not None:
                memory_rail.set_read_only(True)
        else:
            if memory_rail is not None:
                memory_rail.restore_memory_tool(instance)
                memory_rail.set_read_only(False)

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
        base_lazy = base_react.get("tool_lazy_load")
        base_eager = (
            base_lazy.get("eager_tools") if isinstance(base_lazy, dict) else None
        )
        for k, v in self._FLASH_REACT_OVERRIDE.items():
            base_react[k] = self._deep_merge_react_value(base_react.get(k), v)
        # eager_tools 是整表替换（设计上 flash 须写全最终名单）：部署配置过的
        # 自定义常驻工具会退到 deferred，这里把差异打出来，避免工具呈现变化
        # 不可观测。
        if isinstance(base_eager, list):
            merged_lazy = out["react"].get("tool_lazy_load")
            merged_eager = (
                merged_lazy.get("eager_tools") if isinstance(merged_lazy, dict) else None
            )
            dropped = [t for t in base_eager if t not in (merged_eager or [])]
            if dropped:
                logger.warning(
                    "[JiuwenSwarmFlashAdapter] flash eager_tools override replaces "
                    "the deployment list; these tools fall back to deferred: %s",
                    dropped,
                )
        self._warn_evolution_env_bypass(out)
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

    def _warn_evolution_env_bypass(self, config_base: dict[str, Any] | None) -> None:
        """env 级开关优先于 react.evolution，可把 flash 的 evolution 覆盖翻回 true，
        进而触发 force-revive 把 enable_task_loop 拉回 true（flash 单轮语义失效）。
        适配器层无法覆盖 env，只能告警暴露。"""
        if get_skill_create_enabled(config_base):
            logger.warning(
                "[JiuwenSwarmFlashAdapter] SKILL_CREATE env overrides flash "
                "evolution.skill_create=false; task_loop force-revive may "
                "re-enable the multi-round loop"
            )
        if get_evolution_review_trigger_enabled(config_base):
            logger.warning(
                "[JiuwenSwarmFlashAdapter] EVOLUTION_REVIEW_TRIGGER env overrides "
                "flash evolution.review_trigger=false; task_loop force-revive may "
                "re-enable the multi-round loop"
            )

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
