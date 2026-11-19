# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Language-neutral discovery metadata for JiuWenSwarm built-in tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.tool import McpToolCard, ToolCard, ToolInfo
from openjiuwen.core.foundation.tool.schema import McpToolInfo

PROGRESSIVE_DISCOVERY_PROPERTY = "progressive_discovery"


@dataclass(frozen=True)
class ToolDiscoveryMetadata:
    """Text used only to display and search for a built-in tool."""

    display_en: str
    aliases_zh: tuple[str, ...]


def _entry(display_en: str, aliases_zh: str) -> ToolDiscoveryMetadata:
    return ToolDiscoveryMetadata(display_en, tuple(aliases_zh.split()))


# The registered card remains authoritative for invocation instructions and
# schema. These summaries and aliases are only a multilingual search projection.
BUILTIN_TOOL_DISCOVERY: dict[str, ToolDiscoveryMetadata] = {
    "coding_memory_edit": _entry("Edit an exact passage in project coding memory.", "编辑 修改 编码记忆 项目记忆"),
    "coding_memory_read": _entry("Read persistent project coding memory.", "读取 查看 编码记忆 项目记忆"),
    "coding_memory_write": _entry("Write facts, decisions, and lessons to project coding memory.", "写入 记录 编码记忆 项目记忆"),
    "read_pdf": _entry("Read PDF text by page or in chunks.", "读取 查看 PDF 文档 分页"),
    "acp_chat": _entry("Exchange messages with an external ACP-compatible coding agent.", "外部智能体 ACP 对话 Codex Gemini"),
    "list_files": _entry("List files and directories in a local path.", "列出 查看 文件 目录 文件夹"),
    "get_current_goal": _entry("Inspect the current persistent goal and its status.", "查看 查询 当前目标 持续目标 状态"),
    "submit_goal_report": _entry("Report progress, completion, or a blocker for the current goal.", "提交 汇报 目标进度 完成 受阻"),
    "image_ocr": _entry("Extract text from an image with OCR.", "图片识字 OCR 提取文字"),
    "visual_question_answering": _entry("Answer questions about an image.", "图片问答 看图 识图 分析图片"),
    "audio_metadata": _entry("Read metadata from an audio file.", "音频元数据 时长 格式"),
    "audio_question_answering": _entry("Answer questions about audio content.", "音频问答 分析音频"),
    "audio_transcription": _entry("Transcribe speech in an audio file to text.", "音频转写 语音转文字 听写"),
    "video_understanding": _entry("Analyze video content and answer questions about it.", "视频理解 分析视频 视频问答"),
    "generate_image": _entry("Generate an image from a text prompt.", "生成图片 文生图 画图"),
    "generate_visual": _entry("Generate a visual artifact from a text prompt.", "生成视觉图 画图 可视化"),
    "generate_video": _entry("Start a video generation task from a text prompt.", "生成视频 文生视频"),
    "check_video_status": _entry("Check a video generation task and download its result.", "查询 视频生成 进度 下载结果"),
    "lsp": _entry("Use a language server for definitions, references, and diagnostics.", "代码定义 查找引用 代码诊断 LSP"),
    "switch_mode": _entry("Switch the current session between normal and plan modes.", "切换模式 计划模式 普通模式"),
    "cron": _entry("Manage standalone scheduled tasks and reminders.", "定时任务 提醒 闹钟 周期通知"),
    "cron_create_job": _entry(
        "Create a standalone scheduled task, one-time reminder, daily notification, or periodic notification.",
        "创建 定时任务 提醒我 一次性提醒 定时提醒 通知我 定时通知 闹钟 周期通知",
    ),
    "cron_delete_job": _entry("Delete a standalone scheduled task or reminder.", "删除 取消 定时任务 提醒 闹钟"),
    "cron_get_job": _entry("Get one standalone scheduled task or reminder.", "查看 查询 单个 定时任务 提醒"),
    "cron_list_jobs": _entry("List standalone scheduled tasks and reminders.", "列出 查看 全部 定时任务 提醒"),
    "cron_preview_job": _entry("Preview upcoming run times for a scheduled task.", "预览 定时任务 下次执行时间"),
    "cron_toggle_job": _entry("Enable or disable a scheduled task.", "启用 禁用 暂停 恢复 定时任务"),
    "cron_update_job": _entry("Update a standalone scheduled task or reminder.", "修改 更新 定时任务 提醒"),
    "heartbeat_cancel_run": _entry("Cancel a running session follow-up.", "取消 正在运行 心跳 跟进任务"),
    "heartbeat_create_job": _entry(
        "Resume this session's existing task later; use cron_create_job for independent schedules.",
        "创建 心跳 跟进任务 稍后继续 检查进展",
    ),
    "heartbeat_delete_job": _entry("Delete a session follow-up schedule.", "删除 取消 心跳 跟进任务"),
    "heartbeat_get_job": _entry("Get one session follow-up schedule.", "查看 查询 单个 心跳 跟进任务"),
    "heartbeat_list_jobs": _entry("List session follow-up schedules.", "列出 查看 心跳 跟进任务"),
    "heartbeat_preview_job": _entry("Preview upcoming times for a session follow-up.", "预览 心跳 跟进任务 下次时间"),
    "heartbeat_run_now": _entry("Run a session follow-up immediately.", "立即运行 马上执行 心跳 跟进任务"),
    "heartbeat_toggle_job": _entry("Enable or disable a session follow-up.", "启用 禁用 暂停 恢复 心跳 跟进任务"),
    "heartbeat_update_job": _entry("Update a session follow-up schedule.", "修改 更新 心跳 跟进任务"),
    "session_continue_queued": _entry("Resume queued work in another persistent session.", "继续 恢复 其他会话 排队任务"),
    "session_list": _entry("List other persistent sessions and their status.", "列出 查看 其他会话 会话状态"),
    "session_message_list": _entry("Inspect cross-session messages and delivery status.", "查看 跨会话消息 投递状态 消息列表"),
    "session_message_resolve": _entry("Resolve an unknown cross-session message state.", "解决 跨会话消息 未知消息状态"),
    "session_read": _entry("Read another session's conversation, progress, and results.", "读取 查看 其他会话 对话 进展 结果"),
    "session_send_message": _entry("Send a message to another persistent session.", "发送消息 通知 其他会话 跨会话"),
    "evolve_review_task": _entry("Create a review task for a local skill evolution proposal.", "创建 技能演进 评审任务"),
    "evolve_skill_experiences": _entry("Run a constrained review of skill experience changes.", "执行 技能经验 演进 评审"),
    "list_skill_experiences": _entry("List recorded skill experiences.", "列出 查看 技能经验"),
    "prepare_skill_evolution": _entry("Prepare a skill evolution proposal for review.", "准备 技能演进 提案"),
    "read_skill_experiences": _entry("Read recorded skill experiences.", "读取 技能经验"),
    "simplify_skill_experiences": _entry("Simplify and consolidate recorded skill experiences.", "整理 精简 合并 技能经验"),
    "install_skill": _entry("Install a skill from the online skill registry.", "安装 技能"),
    "search_skill": _entry("Search the online skill registry.", "搜索 查找 找技能 找一个技能 技能"),
    "uninstall_skill": _entry("Uninstall a local skill.", "卸载 删除 技能"),
    "powershell": _entry("Run terminal commands and scripts with PowerShell.", "终端 命令行 执行命令 PowerShell 脚本"),
    "user_todos": _entry("Manage the user's personal tasks, meetings, reminders, and plans.", "待办 日程 会议 提醒 计划"),
    "enter_worktree": _entry(
        "Create or resume an isolated Git worktree and enter it.",
        "创建 进入 恢复 Git worktree 工作树 隔离分支",
    ),
    "exit_worktree": _entry(
        "Exit the current Git worktree and restore the previous directory.",
        "退出 Git worktree 工作树 恢复目录",
    ),
    "read_text_file": _entry("Read a local file through the IDE ACP connection.", "读取 文件 IDE ACP"),
    "write_text_file": _entry("Write a local file through the IDE ACP connection.", "写入 修改 文件 IDE ACP"),
    "create_terminal": _entry("Create an IDE terminal and execute a command through ACP.", "创建 终端 执行命令 IDE ACP"),
    "read_terminal_output": _entry("Read output from an IDE terminal.", "读取 查看 终端输出 IDE"),
    "wait_for_terminal_exit": _entry("Wait for an IDE terminal command to finish.", "等待 终端命令 完成 退出"),
    "release_terminal": _entry("Release an IDE terminal after its command finishes.", "释放 关闭 终端"),
    "memory_search": _entry("Search long-term memory for prior work and decisions.", "搜索 长期记忆 历史工作 决策 偏好"),
    "session_new": _entry("Start asynchronous subagent sessions for a list of tasks.", "创建 启动 子智能体 子任务 会话"),
    "session_cancel": _entry("Cancel a running subagent session.", "取消 停止 子智能体 子任务 会话"),
    "send_file_to_user": _entry("Deliver a generated file or report to the user.", "发送 交付 文件 报告 给用户"),
    "create_alarm": _entry("Create an alarm on the user's phone.", "创建 手机闹钟 提醒"),
    "search_alarms": _entry("Search alarms on the user's phone.", "搜索 查找 手机闹钟"),
    "modify_alarm": _entry("Modify an alarm on the user's phone.", "修改 手机闹钟"),
    "delete_alarm": _entry("Delete alarms on the user's phone.", "删除 取消 手机闹钟"),
    "create_calendar_event": _entry("Create an event in the user's phone calendar.", "创建 手机日历 日程 会议"),
    "search_calendar_event": _entry("Search events in the user's phone calendar.", "搜索 查找 手机日历 日程 会议"),
    "search_contact": _entry("Search the user's phone contacts.", "搜索 查找 手机联系人 通讯录"),
    "search_file": _entry("Search files on the user's phone.", "搜索 查找 手机文件"),
    "upload_file": _entry("Upload a file from the user's phone.", "上传 手机文件"),
    "image_reading": _entry("Understand an image supplied by the user.", "看图 识图 分析图片"),
    "get_user_location": _entry("Get the user's current geographical location.", "获取 查询 当前位置 定位"),
    "send_message": _entry("Send an SMS message through the user's phone.", "发送 短信 手机消息"),
    "search_message": _entry("Search text messages on the user's phone.", "搜索 查找 手机短信 消息"),
    "create_note": _entry("Create a note on the user's phone.", "创建 手机笔记 备忘录"),
    "search_notes": _entry("Search notes on the user's phone.", "搜索 查找 手机笔记 备忘录"),
    "modify_note": _entry("Append content to a note on the user's phone.", "修改 追加 手机笔记 备忘录"),
    "call_phone": _entry("Make a phone call using the user's phone.", "拨打 电话 手机通话"),
    "search_photo_gallery": _entry("Search the user's phone photo gallery.", "搜索 查找 手机相册 照片"),
    "upload_photo": _entry("Upload a photo from the user's phone gallery.", "上传 手机相册 照片"),
    "view_push_result": _entry("Read scheduled-task results or pushed notifications.", "查看 推送通知 定时任务结果"),
    "save_media_to_gallery": _entry("Save an image or video to the user's phone gallery.", "保存 图片 视频 到手机相册"),
    "save_file_to_file_manager": _entry("Save a file to the user's phone file manager.", "保存 文件 到手机 文件管理"),
    "convert_timestamp_to_utc8_time": _entry("Convert a timestamp to UTC+8 time.", "转换 时间戳 北京时间 UTC8"),
    "query_collection": _entry("Search the user's saved knowledge collections.", "搜索 查询 收藏 知识库"),
    "delete_collection": _entry("Delete items from the user's saved knowledge collections.", "删除 收藏 知识"),
    "add_collection": _entry("Save public knowledge to the user's personal collection.", "添加 保存 收藏 知识"),
    "xiaoyi_gui_agent": _entry("Operate phone apps through screen interaction.", "操作 手机应用 点击 屏幕 GUI"),
}


def discovery_metadata(name: str) -> ToolDiscoveryMetadata | None:
    """Return maintained discovery metadata for one built-in name."""
    return BUILTIN_TOOL_DISCOVERY.get(str(name or ""))


def discovery_description(tool: ToolInfo, language: str) -> str:
    """Return a localized catalogue summary without changing ``tool``."""
    raw = str(getattr(tool, "description", "") or "")
    if isinstance(tool, McpToolInfo) or language != "en":
        return raw
    metadata = discovery_metadata(str(getattr(tool, "name", "") or ""))
    return metadata.display_en if metadata is not None else raw


def discovery_search_text(tool: ToolInfo) -> str:
    """Return hidden bilingual search terms for a known built-in tool."""
    if isinstance(tool, McpToolInfo):
        return ""
    metadata = discovery_metadata(str(getattr(tool, "name", "") or ""))
    if metadata is None:
        return ""
    return " ".join((metadata.display_en, *metadata.aliases_zh))


def apply_discovery_metadata(card: Any) -> Any:
    """Attach generic progressive-discovery metadata to a built-in ToolCard."""
    if not isinstance(card, ToolCard) or isinstance(card, McpToolCard):
        return card
    metadata = discovery_metadata(card.name)
    if metadata is None:
        return card
    properties = dict(card.properties or {})
    properties[PROGRESSIVE_DISCOVERY_PROPERTY] = {
        "display": {"en": metadata.display_en},
        "aliases": list(metadata.aliases_zh),
    }
    card.properties = properties
    return card


__all__ = [
    "BUILTIN_TOOL_DISCOVERY",
    "PROGRESSIVE_DISCOVERY_PROPERTY",
    "ToolDiscoveryMetadata",
    "apply_discovery_metadata",
    "discovery_description",
    "discovery_metadata",
    "discovery_search_text",
]
