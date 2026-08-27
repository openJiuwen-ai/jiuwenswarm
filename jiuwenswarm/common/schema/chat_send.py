# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""chat.send 参数契约。

本文件定义 chat.send 的参数结构。
"""

from typing import TypedDict, NotRequired

# ── ``plan_entry_source`` 字面量契约常量 ──
# 这些字面量是前后端共享的硬契约：
# - 后端 ``AgentWebSocketServer._is_explicit_plan_entry_request`` 只认这些字面量
#   （通过 ``_PLAN_ENTRY_SOURCES`` 间接引用本常量）；
# - TUI ``app-state.ts`` ``pendingPlanEntrySource`` 与 Web 前端
#   ``useWebSocket.ts`` ``resolvePlanEntryPayload`` 序列化成 ``plan_entry_source``
#   字段，必须使用同名字面量。
# 改动这些取值前先跑 ``tests/unit_tests/test_plan_entry_source_contract.py``。
PLAN_ENTRY_SOURCE_SLASH_COMMAND = "slash_command"
"""TUI 的 ``/plan`` 命令产出的 entry source。"""

PLAN_ENTRY_SOURCE_PLAN_TOGGLE = "plan_toggle"
"""Web 用户手动打开 Plan 开关的那一条消息产出的 entry source。"""

PLAN_ENTRY_SOURCES: frozenset[str] = frozenset(
    {
        PLAN_ENTRY_SOURCE_SLASH_COMMAND,
        PLAN_ENTRY_SOURCE_PLAN_TOGGLE,
    }
)
"""``plan_entry_source`` 字段的合法取值集合（防重入闸门只认这几种）。"""


class ChatSendParams(TypedDict, total=False):
    """chat.send 参数契约（TypedDict，供类型标注与文档）。

    说明：
    - content: 用户消息正文（主字段，保留 /skill 等标记原样）
    - query: DEPRECATED. 历史双发字段，将逐步迁移到 content。
            参见 PLAN_chat_send_params_standardization.md §3.3-2。
    - skills: 用户选中的 skill 名列表；单 Agent 作为 prompt 提示，
              普通 Team 同时更新 Team 级 Skill 可见性，专家团不允许额外选择。
    - mode: 运行模式（agent.plan / agent.fast / code.normal / team 等）
    - attachments: 附件列表（@file 等）
    - files: 文件更新字典（传统字段，逐步迁出到 attachments）
    - agent_template_name: 当前会话期望专家名；缺失保持，非空 load/切换，"" 卸载当前专家。
    - agent_group_name: 首次 Team 构建时选择的专家团包；会话建立后不可切换。
    - plugin_names: 当前会话期望插件名全集；缺失保持，list[str] 差量同步，[] 卸载全部插件。
    """

    content: str
    """用户消息正文（主字段）。保留 /skill 等标记原样，不剥离。"""

    message: NotRequired[str]
    """创建/修改 Skill 等场景的约定字段，与 content 等价。"""

    query: str
    """DEPRECATED: 历史双发字段，当前与 content 同值。

    将逐步废弃，未来统一只用 content。新代码应优先读 content；query 保留为兼容过渡。
    """

    skills: NotRequired[list[str]]
    """用户选中的 skill 名列表（可选）。

    - 来源：TUI/web 前端从 content 提取（如 /doc /review）或 UI 选择器。
    - 单 Agent：作为 prompt 提示（塞入 user_message_context["skills_to_use"]）。
    - 普通 Team（未绑定专家团）：同时作为 Team 级 Skill 选择，写入团队可见性配置，
            Leader 和成员的 Skill rail 都会合成该选择。
    - 普通 Team 中缺失字段：保持已有 Team Skill 选择；显式 ``[]``：
            清除 Team 级显式选择并恢复默认继承全库。
    - 普通 Team 中非空列表：每项必须是已安装、未禁用的 Skill 目录名；
            否则在 Team 运行前返回 ``chat.error``。
    - 本次选择或会话已绑定专家团：非空列表返回 ``chat.error``；
            缺失或 ``[]`` 不修改专家团 Skill 配置，包内声明的 Skill 正常保留。
    - 创建/修改 Skill：固定传 ``["skill-creator"]``（所有 Skill Creator 的统一入口）。
    """

    metadata: NotRequired[dict]
    """请求级元数据。创建/修改 Skill 时含 ``scene``（create_skill|edit_skill）、
    可选 ``target_skill``，以及编辑场景可选 ``target_skill_type``
    （``skill`` / ``swarm_skill`` / ``multimodal_skill``）。
    """

    mode: NotRequired[str]
    """运行模式。如 agent.plan / agent.fast / code.normal / code.team / team。"""

    attachments: NotRequired[list[dict]]
    """附件列表（@file 等）。结构待统一定义。"""

    files: NotRequired[dict]
    """文件更新字典（传统字段）。逐步迁出到 attachments，当前兼容保留。"""

    trusted_dirs: NotRequired[list[str]]
    """可信目录列表（权限白名单）。"""

    project_dir: NotRequired[str]
    """项目根目录（稳定身份）。"""

    cwd: NotRequired[str]
    """当前工作目录。"""

    workspace_dir: NotRequired[str]
    """工作空间目录。"""

    supports_user_interaction: NotRequired[bool]
    """客户端是否能处理 ask_user 等用户交互。缺省为 True，兼容现有客户端。"""

    eternal_conversation_enabled: NotRequired[bool]
    """DEPRECATED：旧 Session 的一次性迁移字段。

    新客户端必须在 ``session.create`` 发送 ``persist_session``；本字段不能覆盖
    已初始化 Session 的权威值。
    """

    plan_entry_source: NotRequired[str]
    """plan 模式入口来源（internal use）。

    合法取值见模块级常量 :data:`PLAN_ENTRY_SOURCES`：
    ``PLAN_ENTRY_SOURCE_SLASH_COMMAND``（TUI ``/plan``）或
    ``PLAN_ENTRY_SOURCE_PLAN_TOGGLE``（Web 手动打开开关）。
    前端（TUI ``app-state.ts`` / Web ``useWebSocket.ts``）与后端
    ``AgentWebSocketServer._is_explicit_plan_entry_request`` 必须引用同名字面量，
    否则防重入闸门失效。详见
    ``tests/unit_tests/test_plan_entry_source_contract.py``。
    """

    answers: NotRequired[list]
    """用户交互问答（interrupt resume 场景）。"""

    original_request: NotRequired[str]
    """原始请求（supplement 场景保留）。"""

    session_id: NotRequired[str]
    """会话 ID（Web 前端通过 params 传递，通常由 Message 框架层提取到 request.session_id）。"""

    model_name: NotRequired[str]
    """模型名称（Web 前端可选传递）。"""

    request_id: NotRequired[str]
    """请求 ID（interrupt resume / 问答回复场景关联）。"""

    source: NotRequired[str]
    """来源标识（如 permission_interrupt / confirm_interrupt / ask_user_interrupt / evolution_interrupt）。"""

    is_supplement: NotRequired[bool]
    """是否为补充请求（Gateway 用于判断 supplement 流程）。"""

    supplement_input: NotRequired[str]
    """补充请求的原始输入。"""

    plan_approval_kind: NotRequired[str]
    """team.plan 审批类型（如 plan_approval）。"""

    plan_content: NotRequired[str]
    """team.plan 审批内容。"""

    plan_language: NotRequired[str]
    """team.plan 审批语言（cn / en）。"""

    approval_schema: NotRequired[str]
    """审批 schema（evolution interrupt 场景）。"""

    evolution_meta: NotRequired[dict]
    """进化元数据（evolution interrupt 场景）。"""

    activate_response: NotRequired[dict]
    """auto_harness 激活响应（{interaction_id, action, feedback}）。"""

    team: NotRequired[bool]
    """团队模式布尔标志。"""

    run: NotRequired[dict]
    """Run 上下文结构（cron / 定时任务场景由 Gateway 注入）。"""

    cron: NotRequired[dict]
    """定时任务信息（由 Gateway cron scheduler 注入）。"""

    agent_template_name: NotRequired[str]
    """Desired expert package name for this turn ("" clears)."""

    agent_group_name: NotRequired[str]
    """AgentGroup selected for the Team session (non-empty, immutable).

    Mutually exclusive with a non-empty ``skills`` selection, including when
    the AgentGroup is inherited from session metadata on a later request.
    """

    plugin_names: NotRequired[list[str]]
    """Desired plugin package names for this turn ([] clears all)."""
