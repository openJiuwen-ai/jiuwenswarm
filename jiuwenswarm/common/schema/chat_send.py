# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""chat.send 参数契约。

本文件定义 chat.send 的参数结构。
"""

from typing import TypedDict, NotRequired


class ChatSendParams(TypedDict, total=False):
    """chat.send 参数契约（TypedDict，供类型标注与文档）。

    说明：
    - content: 用户消息正文（主字段，保留 /skill 等标记原样）
    - query: DEPRECATED. 历史双发字段，将逐步迁移到 content。
            参见 PLAN_chat_send_params_standardization.md §3.3-2。
    - skills: 用户选中的 skill 名列表（当前为 prompt 提示语义，非强制生效）。
    - mode: 运行模式（agent.plan / agent.fast / code.normal / team 等）
    - attachments: 附件列表（@file 等）
    - files: 文件更新字典（传统字段，逐步迁出到 attachments）
    - agent_template_name: 当前会话期望专家名；缺失保持，非空 load/切换，"" 卸载当前专家。
    - plugin_names: 当前会话期望插件名全集；缺失保持，list[str] 差量同步，[] 卸载全部插件。
    """

    content: str
    """用户消息正文（主字段）。保留 /skill 等标记原样，不剥离。"""

    query: str
    """DEPRECATED: 历史双发字段，当前与 content 同值。

    将逐步废弃，未来统一只用 content。新代码应优先读 content；query 保留为兼容过渡。
    """

    skills: NotRequired[list[str]]
    """用户选中的 skill 名列表（可选）。

    - 来源：TUI/web 前端从 content 提取（如 /doc /review）或 UI 选择器。
    - 语义：当前为 prompt 提示（塞入 user_message_context["skills_to_use"]），
            模型可见但非强制。真正强制生效需 A3 阶段（SkillSelectionRail）。
    - 空列表或缺失：不指定 skill，agent 自主判断。
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

    plan_entry_source: NotRequired[str]
    """plan 模式入口来源（internal use）。"""

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

    plugin_names: NotRequired[list[str]]
    """Desired plugin package names for this turn ([] clears all)."""
