# -*- coding: utf-8 -*-
"""
Flash 段位注册表 —— 段号即 priority，渲染顺序 = 编号顺序。

v1 · 对齐 01-段序表 v1 + 05-目录结构总览-新（2026-11-11）

职责：
1. 段位常量表：全部 flash 段的唯一编号来源。新 rail 注入段前从本表领号。
2. 迁移期对账：每个目标段登记「现状来源」（current），迁移 PR 完成后断言
   ``code_priority == order``（代码里的 priority 收敛到段号）。
3. 审计对账：FlashPromptAuditRail 每轮校验装配结果 ∈ 本表 + 段号 ∈ 所属带。

两根轴（勿混）：
- rail 执行序 = rail 类自身的 priority 属性（大者先，控制钩子运行顺序）
- 段位 = 本表的段号（= PromptSection.priority，控制最终提示词里的排序）

段号即 priority：段号（0/10/20...1080），直接作为 PromptSection 的
priority 使用，SystemPromptBuilder 按 priority 升序渲染，故渲染顺序 = 编号顺序。
静态区 0xx–7xx + 999（relay 尾部包 / request_system_prompt）；动态区 1000+。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────
# ⚠ RELAY 仓涉点清单（改动主要在 jiuwenswarm-1102 仓，尽量不动 relay）
#   凡本节列出的内容都源自/涉及 relay 仓，一律在 flash 侧处理，不编辑 relay 源码。
# ─────────────────────────────────────────────────────────────
# A. relay TS 层注入的提示词（RelayClawAgentService.ts → request_system_prompt 段 110）：
#   1. request_system_prompt 产品内容包 —— 会话级稳定，点 7 _STRIP 不能剥（见 PENDING_ADJUDICATION）
#   2. relay [记忆纠正规则] 六条 —— 记忆域已回退 deep 原样（撤销 640 归并），relay 原块保留
#   3. relay [文件输出约束] —— 220 删重复表述（✅ 已落地：FlashRelayMergeRail 剥原块）
#   4. relay 记忆纠正「删除=回收站即合规」—— 120 并入（✅ 已落地：FlashSafetyRail 段尾追加）
#   5. 归 relay 的 4 段（原 700/710/720/730：文件生成/再改改/多产物收尾/PDF）—— 本期删除，不改 relay
# B. relay 技能仓（relay_claw\office-claw-skills\，部署侧 /opt/officeace/office-claw-skills/）：
#   6. manifest.yaml（400 条，default_enabled 26 条）—— 730 技能清单数据源（只读）
# C. relay workspace 记忆文件（IDENTITY.md / USER.md / MEMORY.md / daily_memory）：
#   7. 记忆域操作对象（IDENTITY/USER/MEMORY/daily）+ 1070 用户身份 —— flash 侧经 stock 五件套 memory 工具操作，不改 relay 源码
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# 域 → 段位区间（三位数，百位=域，域内 10 步进）
# ─────────────────────────────────────────────────────────────
FLASH_SECTION_BANDS: dict[str, range] = {
    "identity": range(0, 40),  # 0xx 身份与总则（020/030 存疑）
    "security": range(100, 130),  # 1xx 安全与边界
    "workspace": range(200, 230),  # 2xx 工作空间（230/240 已迁 4xx，留空号）
    "tools": range(300, 360),  # 3xx 工具（320 存疑；350 渐进式工具导航，tool_nav 大块静态驻留）
    "execution": range(400, 470),  # 4xx 任务执行（400–460 连续段）
    "interaction": range(500, 550),  # 5xx 交互与输出（530 空号，已迁 410）
    "memory": range(600, 660),  # 6xx 记忆（内容回退 deep 原样巨石段，仅占 600 一号）
    "skills": range(700, 760),  # 7xx 技能（总则/加载/选择/执行规范/清单/加速，700–750）
    "facts": range(1000, 1090),  # 10xx 运行事实 + 动态规则（原 8xx 整体上移 1000 基础；8xx 留空号不复用）
}


# ─────────────────────────────────────────────────────────────
# 段状态词汇表
#   active        已有独立代码段，迁号即可
#   merged        现与其它段合并在巨石/大段里，拆分后独立
#   planned       01 新段，待建
#   adjudicate    存疑（保号待裁决，v1 新增；裁决方向以 05「七·段目标对照」为准，
#                 待工具真名/外部门禁/字段级约束等外部事实定型后按该方向定稿）
#   dynamic       动态段（② 动态 / ③ 运行时，不写进静态系统提示词）
# ─────────────────────────────────────────────────────────────


@dataclass
class SectionSpec:
    """一个段的登记元信息。"""
    name: str                              # 段名（01 表）
    carrier: str                           # system / durable / attachment
    status: str                            # 见词汇表
    source: str = ""                       # 现状来源（迁移期，迁移完成后可清空）
    current: list[tuple[str, int, str]] = field(default_factory=list)
    # current = [(现 section 名, 现 priority, 注入方/位置)]，迁移完成后清空
    plan: str = ""                         # 迁移动作说明
    relay: str = ""                        # 涉及 relay 仓的内容（非空 = 本期在 flash 侧处理 relay 注入内容，不改 relay 源码）


# ─────────────────────────────────────────────────────────────
# 全段登记（44 段：静态 34 + 10xx 10）
# 顺序 = 编号顺序 = 渲染顺序
#
# carrier 语义（v1 三段分区）：
#   system     静态区（0xx–7xx + 999 尾部包）—— 写进系统提示词，会话内 hash 不变
#   durable    ② 动态区（1000/1010/1040/1050/1060/1070）—— 会话级快照，启动拼一次 / 变化才追加
#              ⚠ 本期延期：durable 快照基建不构建，此 6 段本期保持现状（仍由 RuntimePromptRail
#                等现有注入承载，不剥离），后续补建基建后再迁
#   attachment ③ 运行时区（1020/1030/1080）—— 每轮/会话注入，PromptAttachment 承载（本期做）
# ─────────────────────────────────────────────────────────────
FLASH_SECTION_REGISTRY: dict[int, SectionSpec] = {
    # ═══ 0xx 身份与总则 ═══
    0: SectionSpec("身份", "system", "merged",
                     source="prompt_builder.py:571（:280 人设句）",
                     current=[("identity", 10, "构造期 build_agent_identity_prompt")],
                     plan="FlashPersonaRail 拆出；固定写死不加占位"),
    10: SectionSpec("人格（性格值 → 1030；受众 → 1070）", "system", "merged",
                     source=":851-852 Personality Override",
                     current=[("identity", 10, "构造期")],
                     plan="FlashPersonaRail；只留读取方式与优先级，性格值占位由 1030 动态填充"),
    20: SectionSpec("执行优先级（唯一裁决）", "system", "active",
                     current=[("execution_priority", 20, "FlashPersonaRail")],
                     plan="已落地：六级裁决表（外部门禁清单由 1080 注入，此处仅引用）；§七 方向定稿"),
    30: SectionSpec("不确定与失败", "system", "active",
                     source=":167-168",
                     current=[("uncertainty_failure", 30, "FlashSafetyRail")],
                     plan="已从安全段拆出独立小段（末两条：任务失败 / 不确定）"),

    # ═══ 1xx 安全与边界 ═══
    100: SectionSpec("安全总纲", "system", "active",
                     source=":160-168/:196-207",
                     current=[("safety_general", 100, "FlashSafetyRail")],
                     plan="已落地：030 两条拆走 + 删「正确的响应」三行"),
    110: SectionSpec("拒绝行为", "system", "active",
                     source=":188-194",
                     current=[("refusal_behavior", 110, "FlashSafetyRail")],
                     plan="已落地：原文保留拆分（§七 同上拼接）"),
    120: SectionSpec("删除规范", "system", "active",
                     source=":170-186",
                     current=[("deletion_protocol", 120, "FlashSafetyRail")],
                     plan=("已落地：拆分 + relay 记忆纠正「删除=回收站即合规」并入"
                           "（§七 120 归并，FlashSafetyRail 段尾追加）"),
                     relay="relay 记忆纠正「删除=回收站即合规」并入（flash 侧，不改 relay TS）"),

    # ═══ 2xx 工作空间与文件（230/240 空号 → 迁 460/420）═══
    200: SectionSpec("目录模型（四术语表）", "system", "active",
                     source=":284-306；L17-39/L438-451/L801-816/L827-848",
                     current=[("workspace_model", 200, "FlashPersonaRail")],
                     plan="已落地：四处重复合一为四术语表（绝对路径引 1010 不硬编码）"),
    210: SectionSpec("路径与权限", "system", "merged",
                     source=":319-342 沙箱",
                     current=[("runtime_environment", 210, "FlashPersonaRail"),
                              ("trusted_dirs_policy", 90, "RuntimePromptRail")],
                     plan="只留规则（workdir/cd 不跨调用/工作区外确认/权限拒停重试）；路径值剥到 1010；本期延期（依赖 durable 1000/1010，运行环境暂留 210）"),
    220: SectionSpec("写入位置", "system", "active",
                     source=":364-372",
                     current=[("output_placement", 220, "FlashPersonaRail")],
                     plan=("已落地：五级优先级链（用户指定>项目>cwd>内部>系统禁止）；"
                           "relay [文件输出约束] 重复表述已由 FlashRelayMergeRail 剥除"),
                     relay="relay [文件输出约束] 重复表述删（FlashRelayMergeRail 剥 request_system_prompt 末尾块，不改 relay TS）"),

    # ═══ 3xx 工具（320 存疑；350 渐进式工具导航）═══
    300: SectionSpec("工具选择总则", "system", "merged",
                     current=[("bash使用原则段", 0, "ContextAssembleRail（可用工具/去重）")],
                     plan="FlashToolGuideRail 拆出；删「一个问题只用一个工具」绝对禁"),
    310: SectionSpec("可用工具", "system", "active",
                     current=[("tools", 30, "ContextAssembleRail build_tools_section")],
                     plan=("SDK tools 段（# 可用工具 目录 + ## task_tool 使用原则）迁号 310；"
                           "300/330 已拆出，工具发现规则（tools_search/invoke_tool）归 350 渐进式工具导航")),
    320: SectionSpec("call_goal 约定", "system", "adjudicate",
                     source="stream_event_rail.py:1195",
                     current=[("tool_call_goal", 40, "StreamEventRail")],
                     plan="迁号 320；若为字段级约束随工具面承载则删（01 §6.3）"),
    330: SectionSpec("bash 纪律", "system", "merged",
                     current=[("bash使用原则段", 0, "ContextAssembleRail（bash 部分）")],
                     plan="FlashToolGuideRail 拆出；新增 Windows 多行引号脚本文件陷阱"),
    340: SectionSpec("工具结果与失败", "system", "merged",
                     source=":362",
                     current=[("identity", 10, "构造期"), ("BrowserToolPolicy", 0, "SDK 待核验")],
                     plan="从 400 第 8 条拆出 + Browser Policy 合并"),
    350: SectionSpec("渐进式工具导航", "system", "merged",
                     current=[("tool_nav", 70, "ProgressiveToolRail 清单全量")],
                     plan="工具导航大块静态驻留工具域（拍板：大块不沉动态 1000+）；flash 侧 re-add 迁号 350，ProgressiveToolRail 注入不动（deep 共用）"),

    # ═══ 4xx 任务执行（400–460 连续段）═══
    400: SectionSpec("执行质量准则", "system", "merged",
                     source=":344-362 八条",
                     current=[("identity", 10, "构造期")],
                     plan="已落地：改写为通用准则+场景化准则（修复.md）；「工具结果优先」改名「拒绝盲信写入」（340 按名引用，不再从 400 剥离）"),
    410: SectionSpec("思考纪律 🔒（自 530 迁入）", "system", "active",
                     source="prompt_builder.py:78-80（实验措辞红线）",
                     current=[("thinking_discipline", 410, "FlashResponseRail")],
                     plan="已落地：迁号 410 + 三矛盾修正（修复.md ①-⑤：条件触发句/终稿落盘轮限定/编号病句/错别字/保留重复）；①-③ 位于写文件轮小节红线区、改动须重放复验"),
    420: SectionSpec("产物格式偏好（自 240 迁入）", "system", "merged",
                     source=":374-380",
                     current=[("identity", 10, "构造期")],
                     plan="迁号 420"),
    430: SectionSpec("todo 纪律（三段合一）", "system", "merged",
                     current=[("todo", 85, "FlashTodoRail")],
                     plan="合并一段：建→拆→改状态"),
    440: SectionSpec("子代理委派【内容重写】", "system", "active",
                     source="flash_subagent_rail.py:405",
                     current=[("task_tool", 85, "FlashSubagentRail"), ("model_select", 86, "SDK")],
                     plan="迁号 440；按 v1 §3.4 重写内容；委派由模型按上下文压力自主判断"),
    450: SectionSpec("上下文压缩与召回", "system", "merged",
                     current=[("offload", 90, "SDK OFFLOAD"), ("compression_recall", 90, "SDK RECALL")],
                     plan="迁号 450（offload + compression_recall 同占 450，audit 报 duplicate advisory）；两段合一待 SDK 可改后做"),
    460: SectionSpec("文件发送（自 230 迁入）", "system", "merged",
                     source=":382-403",
                     current=[("identity", 10, "构造期")],
                     plan="迁号 460"),

    # ═══ 5xx 交互与输出（530 空号）═══
    500: SectionSpec("语言", "system", "active",
                     current=[("language_output", 500, "FlashResponseRail")],
                     plan="已落地：迁号 500 + 删「永远/Always」绝对语言声明、按运行时当前语言回复（值迁 1030）"),
    510: SectionSpec("输入消息约定", "system", "active",
                     source="response_prompt_rail.py:132",
                     current=[("response", 510, "FlashResponseRail")],
                     plan="已落地：迁号 510 + 删与 500 重复的 preferred_response_language 语言说明"),
    520: SectionSpec("最终可见回复", "system", "active",
                     source="response_prompt_rail.py:134",
                     current=[("final_visible_reply", 520, "FlashResponseRail")],
                     plan="已落地：迁号 520 + 删 50-70 行死上限，改「按交付物复杂度与受众需求完整呈现」"),
    540: SectionSpec("模型与能力问答", "system", "active",
                     source="runtime_prompt_rail.py:477",
                     current=[("runtime.model_answer_policy", 540, "FlashResponseRail")],
                     plan="已落地：迁号 540 + 运行时缺失说「当前环境未提供该信息」、不断言「未配置」（值迁 1030）"),

    # ═══ 6xx 记忆（内容回退 deep 原样，仅迁号）═══
    600: SectionSpec("记忆（deep 原样巨石段）", "system", "active",
                     source="MemoryRail build_memory_section（deep 原样）",
                     current=[("memory", 50, "MemoryRail build_memory_section")],
                     plan="内容回退 deep 原样、仅迁号 600；610-650 六段拆分撤销、留空号（memory@50 → 600）"),

    # ═══ 7xx 技能（常驻，v1 新增域）═══
    700: SectionSpec("技能总则", "system", "merged",
                     current=[("skills", 40, "SkillUseRail 开头总则")],
                     plan="独立段 700"),
    710: SectionSpec("技能选择与互斥", "system", "merged",
                     current=[("skills", 40, ":244-246/:289-290")],
                     plan="互斥组裁决；v1.2 起前置于加载（先选后加载）"),
    720: SectionSpec("技能加载规则", "system", "merged",
                     current=[("skills", 40, ":241/:307-311/:254/:279")],
                     plan="加载入口统一 skill_tool；协议段「经验查询前置 + 加载 SKILL.md」两块并入（flash 720）"),
    730: SectionSpec("技能执行规范", "system", "merged",
                     current=[("skill_protocol", 45, "SkillProtocolPromptRail 执行规范 9 条")],
                     plan=("执行纪律 9 条（声明步骤/必须建 todo/严格顺序/闸门/忠实/错误/降级/打断；"
                           "⚠️消息语义并入第 9 条）；用户拍板放 730，原 730 清单/740 加速顺延"
                           "——FlashSkillRail 注入 730，flash 侧接管")),
    740: SectionSpec("技能清单（原 730 顺延）", "system", "merged",
                     current=[("skills", 40, ":244-311 技能段")],
                     plan=("常驻清单 = 唯一技能索引；数据源 manifest.yaml"
                           "（启用集 ~5k，非旧全量 10,772）"),
                     relay=("relay 技能仓 relay_claw/office-claw-skills/manifest.yaml"
                            "（部署侧 /opt/officeace/office-claw-skills/）——只读数据源，不改 relay")),
    750: SectionSpec("技能加速通道（原 740 顺延）", "system", "merged",
                     current=[("skill_turbo_guide", 45, "FlashTurboPromptRail / skill_acceleration_exec")],
                     plan="触发/排除/降级/暂停定型；终态归 750；shared skill_protocol 段在被 730 接管后剥除"),

    # ═══ 10xx 运行事实 + 动态规则（原 8xx 上移 1000 基础；② 动态 6 段 + ③ 运行时 3 段）═══
    1000: SectionSpec("环境与 Shell", "durable", "dynamic",
                      source="RuntimePromptRail:639",
                      current=[("env", 89, "RuntimePromptRail")],
                      plan="平台/Shell/OS + shell 规则 + 4 行命令差异表 + mkdir；每轮重建，独立实体"),
    1001: SectionSpec("Python 库与权限", "durable", "dynamic",
                      current=[("runtime_environment", 210, "FlashPersonaRail identity 拆分（一次性裁剪）")],
                      plan=("从 identity 残留「运行环境」块一次性裁剪出独立实体："
                            "Python 库清单 + 命令执行权限（沙箱）；丢弃与 env 重复的"
                            "平台/Shell/6 行命令表。两实体各自生命周期，无跨轮合并")),
    1010: SectionSpec("路径事实", "durable", "dynamic",
                      current=[("tui_project", 99, "RuntimePromptRail"), ("trusted_dirs", 90, "事实半")],
                      plan="绝对路径收拢；本期保持现状（durable 延期），后续迁 durable 快照"),
    1020: SectionSpec("时间事实", "attachment", "dynamic",
                      current=[("time", 130, "RuntimePromptRail")],
                      plan="迁 attachment；每轮 upsert 刷新"),
    1030: SectionSpec("会话事实（模型/语言/渠道/人格）", "attachment", "dynamic",
                      current=[("runtime.setting", 95, "attachment 通道（已走）")],
                      plan="flash_state_rail 统一；受众剥离→1070（本期延期，受众暂留 1030）"),
    1040: SectionSpec("记忆加载状态", "durable", "dynamic",
                      current=[("memory_policy", 52, ":347/:374 状态条")],
                      plan="剥出两条状态；本期保持现状（durable 延期），后续迁 durable 快照"),
    1050: SectionSpec("工具索引", "durable", "dynamic",
                      current=[],
                      plan="压为「名称+一行用途」；长描述回工具面；导航大块已静态驻留 350（渐进式工具导航），1050 只留运行时「名称+一行」索引；本期保持现状"),
    1060: SectionSpec("技能变化（delta）", "durable", "dynamic",
                      plan="会话内 skills 增删/替换/启停 diff；变化才追加 supersede（「以最新为准」）；本期延期（durable 基建不构建），生成源后续协同补齐"),
    1070: SectionSpec("用户身份", "durable", "dynamic",
                      current=[("USER.md" + "/Personality", 0, "记忆文件")],
                      plan="快照生成源与刷新时机待定；本期延期（durable 基建不构建）；受众画像静态半迁入同步推迟",
                      relay="relay workspace USER.md + Personality 记忆文件"),
    1080: SectionSpec("动态规则", "attachment", "dynamic",
                      current=[("安全段" + "rail 注入", 0, "散落")],
                      plan="统一到动态规则附件生成源（对标 DSH CONTEXT 沙箱/审批策略段）；020 只引用、正文不入静态提示词"),
}


# ─────────────────────────────────────────────────────────────
# 现状同号组（迁移完成前有效；撞号裁决靠 audit 打点）
# 迁移完成后应全部消除——每段独占一个段号
# ─────────────────────────────────────────────────────────────
_SAME_ORDER_GROUPS: list[dict[str, Any]] = [
    {"code_priority": 40, "members": {"skills", "tool_call_goal", "skill_selection"}},
    {"code_priority": 45, "members": {"skill_turbo_guide", "skill_protocol"}},
    {"code_priority": 70, "members": {"workspace", "tool_nav"}},
]


# ─────────────────────────────────────────────────────────────
# 01 表未收录、归属待裁决的现存段（audit 打点补齐后归位）
# ─────────────────────────────────────────────────────────────
PENDING_ADJUDICATION: dict[str, dict[str, Any]] = {
    "a2ui": dict(priority=61, note="01 表无对应——建议 5xx 交互域（工具面化）"),
    "a2ui_boost": dict(priority=117, note="01 表无对应——建议 5xx 交互域（工具面化）"),
    "dr_brief_refresh": dict(priority=121, note="deepresearch 附属——待裁决"),
    "request_system_prompt": dict(priority=999, note=(
        "relay 产品内容包（TS 层）——静态区末尾 999（拍板：撞号修复挪出 110，不沉动态 1000+，"
        "对位 1080 动态规则语义）；FlashRelayMergeRail 每轮收口；会话级稳定，不能剥"
    )),
    "turn2_scene_pack": dict(priority="sticky", note="③ 运行时区；sticky 永不清，不占号"),
}


# ─────────────────────────────────────────────────────────────
# 领号 API（dsh getSectionOrder 的 flash 版）
# 新 rail 注入段前调用本函数领号，杜绝野号
# ─────────────────────────────────────────────────────────────
def get_section_order(order: int) -> int:
    """返回段号（= priority）。段号必须已在 REGISTRY 登记。"""
    if order not in FLASH_SECTION_REGISTRY:
        raise KeyError(
            f"未登记的段号 {order}——先在 FLASH_SECTION_REGISTRY 领号，"
            f"再注入段。可用段号：{sorted(FLASH_SECTION_REGISTRY)}"
        )
    return order


def register_section(order: int, name: str, carrier: str, status: str = "planned") -> None:
    """新增段登记（开发期用）。已在册的同号段 → raise。"""
    if order in FLASH_SECTION_REGISTRY:
        existing = FLASH_SECTION_REGISTRY[order]
        raise ValueError(
            f"段号 {order} 已被 {existing.name!r} 占用——段号不复用，"
            f"请用同域空号或申请新百位"
        )
    FLASH_SECTION_REGISTRY[order] = SectionSpec(name, carrier, status)


def section_in_band(order: int, band: str) -> bool:
    """校验段号是否落在所属域的段位区间内。"""
    r = FLASH_SECTION_BANDS.get(band)
    return r is not None and order in r


_CARRIERS = frozenset({"system", "durable", "attachment"})


def resolve_carrier(priority: int, name: str | None = None) -> str:
    """段号（+ 段名）→ carrier 的唯一来源（供 snapshot / audit 判定静态区）。

    已登记的段号取 ``SectionSpec.carrier``；未登记的段号（Deep 动态段 / 运行时注入，
    如 daily_memory_context@52、runtime env、request_system_prompt 等）一律归
    ``attachment``——它们不属于 flash 静态区，不应计入静态 hash / 前缀度量。

    段名落 :data:`PENDING_ADJUDICATION` 的现存段（如 relay ``request_system_prompt``，
    钉在 999 静态区末尾、避开与 110 拒绝行为撞号），即便其段号空间与 flash 静态带相邻，
    也归 ``attachment``——归属未决（对位 1080 动态规则语义）、非 flash 静态区。
    """
    if name is not None and name in PENDING_ADJUDICATION:
        return "attachment"
    spec = FLASH_SECTION_REGISTRY.get(priority)
    if spec is not None and spec.carrier in _CARRIERS:
        return spec.carrier
    return "attachment"


__all__ = [
    "FLASH_SECTION_BANDS",
    "FLASH_SECTION_REGISTRY",
    "SectionSpec",
    "PENDING_ADJUDICATION",
    "get_section_order",
    "register_section",
    "resolve_carrier",
    "section_in_band",
]
