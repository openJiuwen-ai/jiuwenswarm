# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""专家能力 Mixin：会话级专家（AgentTemplate 包）的装载/切换/重放/提示。

宿主（JiuWenSwarmDeepAdapter）需提供的成员见类内「宿主成员契约」类型声明块

状态字段（``_expert_load_record`` / ``_current_expert_id``）以**类级默认值**提供；
``_expert_apply_lock`` 为惰性 property（每实例一把，首次访问创建）。
前两者与 ``_instance`` 同生命周期——实例重建时必须置 None
（宿主 ``create_instance`` 负责，旧 LoadRecord 在新实例账本是未知 id）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentKind,
)

logger = logging.getLogger(__name__)


class ExpertApplyBusyError(RuntimeError):
    """expert 卸装持锁复验时发现会话正在执行回合（闭合守卫与应用之间的 TOCTOU）。

    服务层（ExpertService）捕获后映射为 BUSY 错误码，与守卫拒绝的响应一致。
    """


class ExpertCapabilityMixin:
    """专家能力：装载/切换/重放/notice"""

    # 宿主成员契约
    _instance: Any                              # DeepAgent 实例（None = 未装配）
    _parent_session_id: str | None              # session 级子适配器的属主 session
    _is_session_scoped_adapter: bool
    _session_adapters: dict[str, Any]           # root 的 session 子适配器表
    if TYPE_CHECKING:
        def _is_session_live(self, session_id: str) -> bool: ...
        def _is_session_active(self, session_id: str) -> bool: ...
        def _session_adapter_key(self, session_id: str | None) -> str: ...

    _EXPERT_SWITCH_NOTICE_SECTION = "expert.switch_notice"
    _EXPERT_IDENTITY_ATTACHMENT_SECTION = "expert.current_identity"

    # ── 专家状态（类级默认值 与 _instance 同生命周期，
    #    create_instance 重建时由宿主置 None）──
    _expert_load_record: Any = None
    _current_expert_id: str | None = None
    # 复用对账一次性旗标：既有适配器复用分支的补重放只试一次
    # （防包损坏时逐轮重试刷日志；恢复阀门是适配器驱逐重建）。
    # 实例重建时由装配扩展（BEFORE_INSTANCE_READY）随专家态一并复位。
    _expert_reuse_reconciled: bool = False

    @property
    def _expert_apply_lock(self) -> asyncio.Lock:
        """每实例一把，首次访问惰性创建（只读 property，不可赋值）。

        串行化 expert 卸装：「先卸后装」跨多个 await 点，无锁时并发
        expert.load 会泄漏装载（旧 record 不再被跟踪）并污染 identity 快照还原链。
        """
        try:
            return self.__expert_apply_lock_value
        except AttributeError:
            lock = self.__expert_apply_lock_value = asyncio.Lock()
            return lock

    async def _apply_expert(
            self,
            expert_id: str | None,
            *,
            package_dir: Any = None,
            notify: bool = True,
            reject_if_live: bool = False,
    ) -> list[str]:
        """把本 session 的专家切换到 expert_id（None/"" = 回到无专家）。返回 warnings。

        调用方职责：BUSY 守卫（expert_switch_blocked）与 metadata 写入（服务层在成功后写）。
        本方法职责：先卸后装；fetch/装载失败时实例状态回滚为「无专家」并抛错。
        先卸后装是硬约束：rail 按类名去重（重复绑定 raise），prompt section 的
        previous_snapshot 还原链也要求快照不被另一位专家污染。

        全程持 _expert_apply_lock：卸装跨多个 await 点，无锁时并发
        expert.load 会泄漏装载并污染快照还原链。
        package_dir：服务层已 fetch + 校验过的包目录（避免二次下载）；为 None 时
        本方法自行 fetch + 校验（入口 _replay_expert_from_metadata 重放路径）。
        notify：切换成功后在系统提示词写一条身份变更说明；重放路径传 False
        （重建不等于用户切换，不该冒出"此前回复由默认助手给出"的错误提示）。
        无论 notify 取值，成功后都会把当前身份同步到每轮尾部注入的
        prompt attachment（见 _sync_expert_identity_attachment）。
        reject_if_live：持锁后复验会话活性（闭合服务层守卫与 apply 之间的 TOCTOU），
        会话正在执行回合则抛 ExpertApplyBusyError；仅服务层入口（apply_expert）传 True，
        重放/重挂路径本身就在会话装配期，必须传 False。
        """
        from jiuwenswarm.server.runtime.expert import expert_store as _expert_store

        async with self._expert_apply_lock:
            if (
                reject_if_live
                and self._parent_session_id
                and self._is_session_live(self._parent_session_id)
            ):
                raise ExpertApplyBusyError("当前回合执行中，请等回合结束")
            expert_id = expert_id or None
            if expert_id == self._current_expert_id:
                return []
            if self._instance is None:
                raise RuntimeError("expert apply requires a live DeepAgent instance")
            previous_expert_id = self._current_expert_id
            if self._expert_load_record is not None:  # 1) 必须先卸
                # unload_extension 内部 _unbind 的叶子分支只从 enabled_skills discard 技能名、
                # 不删 mount_root，且空化后 enabled_skills 为空 set（≠ None）——下一轮
                # _prepare_skills 重扫会把已卸载专家的技能概述捞回 SKILLS section。
                # 故先从 record 抓 SKILL ref 的 directory（unload 后 record 置 None），
                # 卸完再补做 agent-core 漏掉的那步：删 mount_root + 清 enabled + 重扫。
                expert_skill_dirs = self._collect_expert_skill_dirs(self._expert_load_record)
                await self._instance.unload_extension(self._expert_load_record)
                await self._purge_expert_skill_mounts(expert_skill_dirs)
                self._expert_load_record = None
                self._current_expert_id = None
            warnings: list[str] = []
            if expert_id is not None:  # 2) 再装
                try:
                    if package_dir is None:
                        package_dir = await _expert_store.get_expert_source().fetch(expert_id)
                    warnings = _expert_store.validate_expert_package(package_dir)
                    record = await self._instance.load_agent_template(str(package_dir))
                    self._expert_load_record = record
                    self._current_expert_id = expert_id
                except Exception as exc:
                    # 装载失败 = 回滚为无专家；提示同步更正，避免残留「当前专家：旧专家」
                    logger.warning(
                        "[session_id=%s] [JiuWenSwarmDeepAdapter] expert apply failed: "
                        "expert=%s previous=%s, rollback to no-expert: %s",
                        self._parent_session_id, expert_id, previous_expert_id, exc,
                    )
                    if notify:
                        self._refresh_expert_switch_notice(previous_expert_id, None)
                    await self._sync_expert_identity_attachment()
                    raise
            if notify:
                self._refresh_expert_switch_notice(previous_expert_id, self._current_expert_id)
            await self._sync_expert_identity_attachment()
            return warnings

    @staticmethod
    def _collect_expert_skill_dirs(load_record: Any) -> list[Path]:
        """从 LoadRecord.refs 抽出本专家绑定过的 SKILL ref 的 directory（叶子目录）。

        agent-core 的 ``_bind_skill``（extension_binder.py:270-273）把
        ``ResourceRef.extra["directory"]`` 写成专家包内技能的叶子目录；卸载前
        抓取它们，供 ``_purge_expert_skill_mounts`` 补做 agent-core 漏掉的清理。
        """
        from openjiuwen.harness.resources import ResourceKind

        refs = getattr(getattr(load_record, "refs", None), "__iter__", None)
        if refs is None:
            return []
        dirs: list[Path] = []
        for ref in load_record.refs:
            if getattr(ref, "kind", None) != ResourceKind.SKILL:
                continue
            directory = (getattr(ref, "extra", None) or {}).get("directory")
            if directory:
                dirs.append(Path(str(directory)).expanduser().resolve())
        return dirs

    async def _purge_expert_skill_mounts(self, skill_dirs: list[Path]) -> None:
        """卸载专家后补做 agent-core 漏掉的那步。

        agent-core 的 ``_unbind`` SKILL 叶子分支（extension_binder.py:403-410）只
        ``enabled_skills.discard(name)``、不删 ``skills_dir`` 里的 mount_root；当
        该技能是 enabled 集合最后一个时 discard 后变空 set，``_filter_skills``
        （skill_use_rail.py:257）又把空 set 当「不过滤」——下一轮 ``_prepare_skills``
        重扫父目录把专家技能概述捞回 SKILLS section。本方法在 jiuwenswarm 侧补齐：
        删 mount_root + 清 enabled（空则置 None，区别于 agent-core 留空 set）+ 重扫。

        幂等：删一个不在的 root、discard 一个不在的 name 均不报错；若上游 openjiuwen
        将来修复了 ``_unbind``/``_filter_skills``，本方法与之共存不冲突。
        """
        if not skill_dirs:
            return
        from openjiuwen.harness.rails import SkillUseRail

        instance = self._instance
        rails = []
        skill_rail = getattr(self, "_skill_rail", None)
        if skill_rail is not None:
            rails.append(skill_rail)
        finder = getattr(instance, "find_rails_by_type", None)
        if callable(finder):
            for rail in finder((SkillUseRail,)):
                if rail not in rails:
                    rails.append(rail)

        for leaf in skill_dirs:
            if not leaf.exists():
                continue
            is_leaf = (leaf / "SKILL.md").is_file()
            mount_root = str(leaf.parent) if is_leaf else str(leaf)
            skill_name = leaf.name if is_leaf else ""

            for rail in rails:
                current = list(rail.skills_dir or [])
                mounted = {str(Path(item).expanduser().resolve()) for item in current}
                if mount_root not in mounted:
                    continue
                rail.skills_dir = [
                    item
                    for item in current
                    if str(Path(item).expanduser().resolve()) != mount_root
                ]
                if skill_name and getattr(rail, "enabled_skills", None) is not None:
                    rail.enabled_skills.discard(skill_name)
                    if not rail.enabled_skills:
                        # 关键：空 set（agent-core 留的）在 _filter_skills 里 = 不过滤，
                        # 必须置 None 才表示「无 enabled 限制」，否则残留技能通行。
                        rail.enabled_skills = None
                rail.enable_cache = False
                rail.clear_skills()
                if rail.skills_dir:
                    try:
                        await rail.reload_skills()
                    except Exception as exc:
                        logger.warning(
                            "[session_id=%s] [JiuWenSwarmDeepAdapter] expert skill "
                            "mount purge reload failed: %s",
                            self._parent_session_id, exc,
                        )
                break

    def _refresh_expert_switch_notice(
        self, previous: str | None, current: str | None
    ) -> None:
        """专家切换/退出后在系统提示词写一条身份变更说明。

        让模型确定性地知道「此前的回复是另一种身份给出的」，而不是靠从对话历史
        自行推断。section 随 _instance 生命周期：实例重建即消失；重放路径不调本方法。
        """
        if previous is None and current is None:
            return
        instance = self._instance
        builder = getattr(instance, "system_prompt_builder", None)
        if builder is None:
            logger.debug(
                "[session_id=%s] [JiuWenSwarmDeepAdapter] expert switch notice skipped: "
                "no prompt builder, previous=%s current=%s",
                self._parent_session_id, previous, current,
            )
            return
        builder.remove_section(self._EXPERT_SWITCH_NOTICE_SECTION)  # 幂等
        if current:
            # 陈述 + 祈使双段：历史里残留旧身份产出的回复，仅靠陈述句弱模型
            # 容易延续旧口吻——显式行为指令（参考 WorkBuddy cancelled 语义）
            prev_desc_cn = f"专家「{previous}」" if previous else "默认助手（无专家）"
            cn = (
                f"# 当前专家：{current}\n"
                f"从现在起以专家「{current}」的角色与工作流作答。\n"
                f"本对话在此之前的回复由{prev_desc_cn}给出；"
                "不要延续此前身份的行为与口吻。"
            )
            prev_desc_en = (
                f'expert "{previous}"' if previous else "the default assistant (no expert)"
            )
            en = (
                f"# Current expert: {current}\n"
                f"From now on, respond in the role and workflow of expert "
                f"\"{current}\".\n"
                f"Earlier replies in this conversation were given by {prev_desc_en}; "
                "do not carry over that identity's behavior or tone."
            )
        else:
            cn = (
                "# 当前无专家\n"
                f"本对话已切回默认助手身份：立即停止使用专家「{previous}」的角色与"
                "工作流，以默认助手作答。\n"
                f"在此之前的回复由专家「{previous}」给出；不要延续该专家的行为与口吻。"
            )
            en = (
                "# No expert active\n"
                "This conversation has returned to the default assistant: immediately "
                f"stop using the role and workflow of expert \"{previous}\", and "
                "respond as the default assistant.\n"
                f"Earlier replies were given by expert \"{previous}\"; do not carry "
                "over that expert's behavior or tone."
            )
        builder.add_section(
            PromptSection(
                name=self._EXPERT_SWITCH_NOTICE_SECTION,
                content={"cn": cn, "en": en},
                priority=11,  # 紧跟 identity(10)，在 conventions(15) 之前
            )
        )
        instance.apply_prompt_builder_to_react_agent()

    async def _sync_expert_identity_attachment(self) -> None:
        """把当前专家身份同步为每轮尾部注入的 prompt attachment（与 notice 互补）。

        system 侧 notice 在位置 0，长对话有注意力稀释、且「在此之前」无序列锚点；
        附件通道（runtime.setting / git_status 同款）在每次模型调用（含 ReAct
        中间轮）把本附件作为尾部 user 消息注入最终 window——压缩后注入、不进
        历史、每轮重申。附件管理器随 DeepAgent 实例生死，重放/重挂路径经
        _apply_expert 自然重建；实例内热重载不影响附件。
        失败不抛穿：附件缺失只损失每轮重申，system 侧 notice 仍在。

        语言：跟随 system_prompt_builder.language（cn/en，每轮对话请求由宿主
        _update_prompt_for_mode 同步）写单语。注意 notice 是 cn/en 双份由
        builder 按语言渲染、随语言切换即时生效；附件只在 apply/replay 时同步，
        会话中途切语言且未切专家时附件语言滞后到下次 apply（可接受的边际窗口）。
        """
        instance = self._instance
        manager = getattr(instance, "prompt_attachment_manager", None)
        session_id = self._parent_session_id
        if manager is None or not session_id:
            return
        expert_id = self._current_expert_id
        try:
            if expert_id:
                builder = getattr(instance, "system_prompt_builder", None)
                language = str(getattr(builder, "language", "cn") or "cn")
                if language.lower().startswith("en"):
                    content = (
                        f"Current expert: {expert_id} — respond in this expert's "
                        "role and workflow."
                    )
                else:
                    content = f"当前专家：{expert_id}——以该专家的角色与工作流作答。"
                await manager.add_section(
                    session_id=session_id,
                    section=self._EXPERT_IDENTITY_ATTACHMENT_SECTION,
                    content=content,
                    kind=PromptAttachmentKind.GENERIC,
                    source="jiuwenswarm.expert_capability",
                    priority=10,  # 附件块内按 priority 升序，身份锚排最前
                    content_kind="text/markdown",
                )
            else:
                await manager.clear_section(
                    session_id=session_id,
                    section=self._EXPERT_IDENTITY_ATTACHMENT_SECTION,
                )
        except Exception as exc:
            logger.warning(
                "[session_id=%s] [JiuWenSwarmDeepAdapter] sync expert identity "
                "attachment failed: expert=%s: %s",
                session_id, expert_id, exc,
            )

    def _write_expert_load_failure_notice(self, expert_id: str) -> None:
        """重放/重挂失败降级后写一条失败说明。

        重建 ≠ 用户切换，所以正常重放不写 notice；但装载失败导致的身份回退是
        用户与模型都该知道的真实变化——否则 metadata 绑定专家、实际默认助手，
        双方无从察觉。
        """
        instance = self._instance
        builder = getattr(instance, "system_prompt_builder", None)
        if builder is None:
            return
        builder.remove_section(self._EXPERT_SWITCH_NOTICE_SECTION)  # 幂等
        cn = (
            f"# 专家「{expert_id}」装载失败\n"
            "已回退为默认助手身份：以默认助手作答，"
            f"不要沿用专家「{expert_id}」的角色与工作流。\n"
            f"在此之前的回复可能由专家「{expert_id}」给出。"
        )
        en = (
            f"# Expert \"{expert_id}\" failed to load\n"
            "Falling back to the default assistant: respond as the default assistant "
            f"and do not use the role or workflow of expert \"{expert_id}\".\n"
            "Earlier replies in this conversation may have been given by "
            f"expert \"{expert_id}\"."
        )
        builder.add_section(
            PromptSection(
                name=self._EXPERT_SWITCH_NOTICE_SECTION,
                content={"cn": cn, "en": en},
                priority=11,
            )
        )
        instance.apply_prompt_builder_to_react_agent()

    async def _reapply_expert_after_prompt_rebuild(self) -> None:
        """prompt_builder 重建后按当前专家重挂（旧 LoadRecord 的 refs 已随旧 builder 失效）。

        失败降级为无专家、不抛穿（reload 路径不能因仓库暂不可达而崩）。
        缓存优先：有本地缓存包就直接重挂，不重新 fetch——避免高频
        reload / 懒 reload 在 chat 装配点被网络下载阻塞；包更新走用户主动的
        expert.load（fetch 会刷新缓存）。
        """
        from jiuwenswarm.server.runtime.expert import expert_store as _expert_store

        expert_id = self._current_expert_id
        if not expert_id:
            return
        self._expert_load_record = None
        self._current_expert_id = None
        try:
            await self._apply_expert(
                expert_id,
                package_dir=_expert_store.get_cached_expert_package_dir(expert_id),
                notify=False,
            )
        except Exception as exc:
            logger.exception(
                "[session_id=%s] [JiuWenSwarmDeepAdapter] expert re-apply after "
                "prompt rebuild failed: %s; degrade to no-expert: %s",
                self._parent_session_id,
                expert_id,
                exc,
            )
            self._write_expert_load_failure_notice(expert_id)

    async def _replay_expert_from_metadata(self) -> None:
        """驱逐重建/首次装配后，从 session metadata 重放专家。

        失败降级为无专家、不中断会话；不清 metadata（瞬时故障下次重建可自愈，
        持续性损坏由 experts.list 的 available=false 呈现）。缓存优先：
        有本地缓存包就直接重挂， rebuild 不被网络下载阻塞。
        """
        from jiuwenswarm.server.runtime.expert import expert_store as _expert_store
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        session_id = self._parent_session_id
        if not session_id:
            return
        try:
            metadata = get_session_metadata(session_id, cache_bust=True)
        except Exception as exc:
            logger.exception(
                "[session_id=%s] [JiuWenSwarmDeepAdapter] expert replay: "
                "read metadata failed: %s",
                session_id, exc,
            )
            return
        expert_id = (metadata or {}).get("expert_id") or None
        if not expert_id:
            return
        # 专家团（expert_type="team"）走 team 线冷构造（assembly._apply_agent_group），
        # 不是会话 DeepAgent 热加载——团包 manifest 不是 agent_template，误走
        # load_agent_template 必失败降级并写错误 notice，必须跳过（防污染关键点）。
        if str((metadata or {}).get("expert_type") or "agent") == "team":
            logger.info(
                "[session_id=%s] [JiuWenSwarmDeepAdapter] expert replay skipped: "
                "expert=%s 是专家团（team 线装配）",
                session_id,
                expert_id,
            )
            return
        try:
            await self._apply_expert(
                expert_id,
                package_dir=_expert_store.get_cached_expert_package_dir(expert_id),
                notify=False,
            )
            logger.info(
                "[session_id=%s] [JiuWenSwarmDeepAdapter] expert replayed: expert=%s",
                session_id,
                expert_id,
            )
        except Exception as exc:
            logger.exception(
                "[session_id=%s] [JiuWenSwarmDeepAdapter] expert replay: load %s failed; "
                "degrade to no-expert: %s",
                session_id,
                expert_id,
                exc,
            )
            self._write_expert_load_failure_notice(expert_id)

    async def _reconcile_expert_binding_on_reuse(self) -> None:
        """既有适配器复用时的专家绑定对账（预热洞修复）。

        场景：session 级实例由后台预热提前建完（warm pool 的 prepare_session），
        create_instance 尾部的专家重放跑在 session.create 写 metadata 之前而空放；
        首问起 _get_or_create_session_adapter 走既有分支直接复用，重放永不补跑，
        人设整段会话缺席。本方法由复用分支每轮调用：当前未挂专家但 metadata
        有绑定时补一次重放。

        一次性语义：成功则 _current_expert_id 置位、后续轮次属性检查即早退；
        进入重放前即置 _expert_reuse_reconciled，失败（包损坏等）不逐轮重试——
        与 create 期重放的一次性语义对齐。metadata.expert_id 能写入必先经过
        fetch+校验（load_expert / session.create 判型），本地必有缓存包，
        重放不触网。
        """
        if self._current_expert_id or self._expert_reuse_reconciled:
            return
        session_id = self._parent_session_id
        if not session_id:
            return
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        try:
            metadata = get_session_metadata(session_id)  # 缓存读，逐轮成本可忽略
        except Exception:
            return
        expert_id = str((metadata or {}).get("expert_id") or "").strip()
        if not expert_id:
            return
        # team 线冷构造不经此挂载（replay 内部对 expert_type=team 自带跳过），
        # 这里提前拦掉省一次 cache_bust 读盘
        if str((metadata or {}).get("expert_type") or "agent") == "team":
            return
        self._expert_reuse_reconciled = True
        logger.info(
            "[session_id=%s] [JiuWenSwarmDeepAdapter] 复用既有实例但专家未挂载，"
            "补做重放: expert=%s（实例或为预热产物，create 期重放早于 metadata 写入）",
            session_id, expert_id,
        )
        await self._replay_expert_from_metadata()

    def expert_switch_blocked(self, session_id: str | None) -> bool:
        """在 root 适配器上调用：该 session 正处于回合执行中则 True。

        双侧信号：root 自身计数（部分路径在 root 标记）+ 子适配器的
        _is_session_live——chat 的 _mark_session_active 发生在委托后的
        子适配器上（process_message_impl 委托后标记），
        因此必须问子适配器，而不是只看 root。
        """
        sid = self._session_adapter_key(session_id)
        if self._is_session_active(sid):
            return True
        child = self._session_adapters.get(sid)
        return bool(child and child._is_session_live(sid))

    async def apply_expert(
            self, expert_id: str | None, *, package_dir: Any = None
    ) -> list[str]:
        """公开入口：切换本 session 的专家（语义见 _apply_expert）。

        服务层路径专用：持锁后复验会话活性，回合执行中抛 ExpertApplyBusyError
        （ExpertService 映射为 BUSY 响应）。
        """
        return await self._apply_expert(
            expert_id, package_dir=package_dir, reject_if_live=True
        )
