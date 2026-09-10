# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TaskExecutionRail — Emit task.start/task.complete/task.update lifecycle events.

Tracks todo status transitions (pending->in_progress->completed) and emits
lifecycle events to the frontend. Binds the current task_id via ContextVar
so downstream tool/artifact events can be attributed to the active task.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenswarm.common.utils import logger
from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    is_skip_invoke_task_update_sync,
    clear_skip_invoke_task_update_sync,
    set_stale_todo_ids,
    clear_stale_todo_ids,
    set_pre_invoke_todo_ids,
    set_current_invoke_todo_ids,
    get_pre_invoke_todo_ids,
)

_ACTIVE_TASK_ID: ContextVar[str | None] = ContextVar(
    "active_task_id", default=None
)
SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY = (
    "_jiuwenswarm_skill_turbo_outer_todo_active"
)


def get_current_task_id() -> str | None:
    """Return current task id for stream payload correlation."""
    return _ACTIVE_TASK_ID.get()


# 图像产物扩展名白名单
_IMAGE_ARTIFACT_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
})

# 非产物路径黑名单
# 供 TaskExecutionRail 与 SkillTurboArtifactRail 共用
_ALWAYS_EXCLUDED_PATH_PATTERNS = [
    re.compile(r'SKILL\.md', re.IGNORECASE),
    re.compile(r'AGENT\.md', re.IGNORECASE),
    re.compile(r'[/\\]node_modules[/\\]', re.IGNORECASE),
    re.compile(r'[/\\]path[/\\]to[/\\]', re.IGNORECASE),  # /path/to/output 示例
    re.compile(r'/user/specified', re.IGNORECASE),  # 示例路径
    re.compile(r'\{skill_root\}', re.IGNORECASE),  # skill 变量引用
    re.compile(r'[/\\]\.jiuwenclaw[/\\]', re.IGNORECASE),
    re.compile(r'^\./[^/\\]+[/\\]SKILL\.md$', re.IGNORECASE),  # skill 子目录引用
    re.compile(r'^\./[^/\\]+[/\\]AGENT\.md$', re.IGNORECASE),  # skill 子目录引用
    # bash/powershell 大输出落盘目录（供模型查阅，非用户产物）
    re.compile(r'[/\\][^/\\]*bash_outputs[/\\]', re.IGNORECASE),
    re.compile(r'[/\\][^/\\]*powershell_outputs[/\\]', re.IGNORECASE),
    # 临时/缓存文件（基线快照与正文提取共用排除）
    re.compile(r'\.tmp$', re.IGNORECASE),
    re.compile(r'~\$\w', re.IGNORECASE),  # Office 临时锁文件 ~$xxx.pptx
    re.compile(r'\.swp$', re.IGNORECASE),
    re.compile(r'\.DS_Store$', re.IGNORECASE),
]


def _is_excluded_path(path_str: str) -> bool:
    """检查路径是否应排除（非产物）。

    与 enterprise_dev 对齐：用黑名单排除已知非产物路径模式，而非按扩展名
    白名单收紧——真实交付物扩展名众多（csv/html/md 等），白名单会漏报。
    正文回退扫描另由 _ARTIFACT_PATH_PATTERNS 限定路径形态。
    """
    for pattern in _ALWAYS_EXCLUDED_PATH_PATTERNS:
        if pattern.search(path_str):
            return True
    return False

# ---------------------------------------------------------------------------
# 产物检测工具白名单（供 TaskExecutionRail 与 SkillTurboArtifactRail 共用）
# ---------------------------------------------------------------------------
# 图像生成工具（generate_image 为 jiuwenswarm 工具名）
IMAGE_TOOL_NAMES = frozenset({"generate_image"})
# write/edit 类工具：产物路径从 tool_args 或结果中提取
WRITE_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "write", "write_text_file",
})
# 代码执行类工具：产物路径从 stdout/stderr 正则提取
CODE_EXEC_TOOL_NAMES = frozenset({
    "bash", "exec_command", "mcp_exec_command",
})
# invoke_tool：按需工具的间接调用入口，需解包内部工具名和结果
INVOKE_TOOL_NAMES = frozenset({"invoke_tool"})
# send_file_to_user：产物路径从 tool_args 显式提取
SEND_FILE_TOOL_NAMES = frozenset({"send_file_to_user"})

# invoke_tool 解包后的只读查询类内部工具（不产文件）跳过产物检测：
# 其大文本结果（如 evaluate_script ~800K HTML）会触发 _ARTIFACT_PATH_PATTERNS
# findall 爆炸 + 逐条 stat()，阻塞事件循环数百秒（实测 633s → WS 1006）。
READONLY_INNER_TOOLS = frozenset({
    # chrome-devtools / 浏览器自动化只读查询
    "evaluate_script", "list_pages", "get_page_content", "get_page_text",
    "snapshot", "take_snapshot", "get_console_logs", "get_cookies",
    "get_network_log", "screenshot",  # 截图产物由调用方另行处理
    # 通用只读探查
    "search_skill", "tools_search",
})
# 触发产物检测的全部工具（对齐 clowder-ai artifact_emitter 白名单思路）
ARTIFACT_DETECTION_TOOL_NAMES = frozenset(
    IMAGE_TOOL_NAMES | WRITE_TOOL_NAMES | CODE_EXEC_TOOL_NAMES
    | INVOKE_TOOL_NAMES | SEND_FILE_TOOL_NAMES
)

# mtime 校验容差（秒）：覆盖 FAT32 2 秒时间戳粒度等文件系统精度问题
_MTIME_TOLERANCE_S = 2.0

# 产物路径检测超时（秒）：防止 stat() 对不可达网络路径同步阻塞 event loop
# （公开常量：供 SkillTurboArtifactRail 跨模块复用）
ARTIFACT_DETECT_TIMEOUT_S = 2.0

# 正文回退扫描的单行最大长度
_BODY_SCAN_MAX_LINE_LEN = 8192

# 结构化提取时识别为路径字段的键名关键词
_STRUCTURED_PATH_FIELD_KEYWORDS = frozenset({
    "path", "file", "files", "output", "outputs",
    "artifact", "artifacts", "generated", "result",
})


def _is_unc_path(path_str: str) -> bool:
    """检查是否为 UNC 网络路径（\\\\host\\share），避免 stat() 同步阻塞。"""
    return path_str.startswith("\\\\") or path_str.startswith("//")

# 文件路径检测的正则表达式模式（仿 PR#1440；调用方按黑名单排除过滤）
_FILE_PATH_PATTERNS = [
    # Windows绝对路径 (D:\path, D:/path)
    re.compile(r'[A-Za-z]:[/\\][^\s\]\}\)\,\'\"`<>，。；、：]+'),
    # Unix绝对路径 (/path/to/file)
    re.compile(r'/[^\s\]\}\)\,\'\"`<>，。；、：]+'),
    # 相对路径带有扩展名 (./path, path/file.ext)
    re.compile(
        r'(?<![/\\])(?:\.{1,2}[/\\])?(?:[^\s\]\}\)\,\'\"`<>，。；、：]+[/\\])+'
        r'[^\s\]\}\)\,\'\"`<>，。；、：]+\.[a-zA-Z0-9]{1,10}'
    ),
]

_PATH_TRAILING_CHARS = "'\"`\\]\\}\\),.;:，。；、："
_PYTHON_SCRIPT_EXTENSIONS = frozenset({".py", ".pyw"})

# 产物路径正文回退扫描正则（宽松策略，允许空格，匹配任意绝对路径）
# 与 _FILE_PATH_PATTERNS 的区别：\s → \r\n（允许空格，仅换行截断）
# 且要求路径以扩展名结尾，避免匹配到无扩展名的目录名
_ARTIFACT_PATH_PATTERNS = [
    # Windows绝对路径，允许空格（停在换行/引号/括号等边界）
    # (?<![A-Za-z]) 排除 URL 协议：https:// 中的 s: 会被误认为盘符
    re.compile(
        r'(?<![A-Za-z])[A-Za-z]:[/\\][^\r\n\]\}\)\'\"`<>，。；、：]+'
        r'\.[a-zA-Z0-9]{1,10}'
    ),
    # Unix绝对路径，允许空格
    # (?<![:/]) 排除 URL：避免 //host 被 normpath 转为 UNC 网络路径后
    # stat() 同步阻塞 event loop（对齐问题201的22秒阻塞根因）
    re.compile(
        r'(?<![:/])/[^\r\n\]\}\)\'\"`<>，。；、：]+'
        r'\.[a-zA-Z0-9]{1,10}'
    ),
]

# 产物路径正则扫描的文本长度上限：超过直接跳过 findall，避免超大正文
# （如 evaluate_script ~800K HTML）爆炸匹配 + 逐条 stat() 阻塞事件循环。
# 64K 覆盖正常 bash stdout 的产物路径声明。
_ARTIFACT_SCAN_MAX_TEXT_BYTES = 64 * 1024

# ---------------------------------------------------------------------------
# 基线 diff 产物检测参数（bash 类工具：工具执行前建工作区快照，
# 执行后增量 diff 出新增/变更文件作为候选产物，不依赖工具输出文本）
# ---------------------------------------------------------------------------
# 快照文件数上限：超限放弃基线 diff，降级文本提取
MAX_SCAN_FILES = 2000
# 单文件 sha256 上限：超限跳过哈希，信任 mtime/size 判定
MAX_HASH_BYTES = 100 * 1024 * 1024
# 静默期 finalize：等待文件大小稳定（防后台进程 / tmp->rename 竞态）
STABLE_INTERVAL_S = 0.5
# finalize 静默期总窗口（秒）：所有候选共享一个窗口批量轮询，
# 不随候选数线性放大（逐文件串行等待会超出外层 ARTIFACT_DETECT_TIMEOUT_S，
# 导致整体超时后基线不更新、变化累积、下一轮继续超时的循环）
FINALIZE_TIMEOUT_S = 1.0
# diff 阶段 sha256 累计预算（秒）：耗尽后放弃 hash 保守判变更，
# 无死循环（该文件下次 diff mtime/size 未变即跳过）
_HASH_BUDGET_S = 0.3
# before_tool_call 懒建基线快照超时（秒）：超时后本会话禁用基线路径
# （公开常量：供 SkillTurboArtifactRail 跨模块复用）
BASELINE_SNAPSHOT_TIMEOUT_S = 2.0

# 工作区快照：normcase 相对路径 -> (绝对路径, mtime_ns, size, sha256|None)
# sha256 懒计算，仅 diff 发现 mtime/size 变化时才读取文件计算
WorkspaceSnapshot = dict[str, tuple[str, int, int, str | None]]


def _clean_path_candidate(path_str: str) -> str:
    """清理正则提取到的路径候选首尾非法字符。"""
    return path_str.strip().strip(_PATH_TRAILING_CHARS).strip()


def _iter_structured_path_values(value: Any, parent_key: str = "") -> list[str]:
    """从结构化工具结果中递归提取路径字段值。

    遍历 dict/list/tuple/set，当键名含 path/file/output/artifact 等关键词时
    取其字符串值作为路径候选。structured 提取命中后可跳过正文正则扫描。
    """
    paths: list[str] = []
    key_lower = parent_key.lower()
    key_is_path_like = any(
        keyword in key_lower for keyword in _STRUCTURED_PATH_FIELD_KEYWORDS
    )

    if isinstance(value, dict):
        for child_key, child_value in value.items():
            paths.extend(
                _iter_structured_path_values(child_value, str(child_key))
            )
        return paths

    if isinstance(value, (list, tuple, set)):
        for item in value:
            paths.extend(_iter_structured_path_values(item, parent_key))
        return paths

    if key_is_path_like and value is not None:
        candidate = _clean_path_candidate(str(value))
        if candidate:
            paths.append(candidate)

    return paths


def _scan_body_text_for_paths(
    result_text: str,
    *,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """逐行扫描正文提取路径候选。

    逐行处理避免超长单行正则灾难；单行超 _BODY_SCAN_MAX_LINE_LEN 跳过。
    """
    # 纵深防御：主防线是 detect_artifact_paths 的 READONLY_INNER_TOOLS 短路，
    # 这里兜底非 invoke_tool 通道直接走正文扫描的超大输出（633s stat 风暴）。
    if len(result_text) > _ARTIFACT_SCAN_MAX_TEXT_BYTES:
        logger.warning(
            "[TaskExecutionRail] artifact scan skipped: result text too "
            "large len=%d max=%d (super-large tool output would block "
            "event loop on stat() storm)",
            len(result_text), _ARTIFACT_SCAN_MAX_TEXT_BYTES,
        )
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for line in result_text.splitlines():
        if cancel_event is not None and cancel_event.is_set():
            break
        if len(line) > _BODY_SCAN_MAX_LINE_LEN:
            continue
        for pattern in _ARTIFACT_PATH_PATTERNS:
            for match in pattern.findall(line):
                cleaned = _clean_path_candidate(match)
                if not cleaned:
                    continue
                identity = cleaned.replace("\\", "/").lower()
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(cleaned)
    return candidates


def _parse_tool_args_payload(tool_args: Any) -> dict[str, Any]:
    if tool_args is None:
        return {}
    payload: Any = tool_args
    if isinstance(tool_args, str):
        try:
            payload = json.loads(tool_args)
        except (TypeError, ValueError):
            return {}
    if isinstance(payload, dict):
        return payload
    return {}


# 工具结果中承载文本输出的字段名（stdout/stderr 等，取原始文本避免 JSON 转义）
_RESULT_TEXT_KEYS = ("stdout", "stderr", "output", "result")


def _collect_result_text_fields(payload: dict[str, Any]) -> list[str]:
    """收集 dict 中 stdout/stderr/output/result 字段的字符串值。"""
    parts: list[str] = []
    for key in _RESULT_TEXT_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    return parts


def _tool_result_to_text(tool_result: Any) -> str:
    if tool_result is None:
        return ""
    if isinstance(tool_result, str):
        return tool_result
    if isinstance(tool_result, dict):
        # 优先取 stdout/stderr 等字段的原始文本；json.dumps 会把反斜杠
        # 转义成 \\、换行转义成 \n，导致路径提取错乱（跨行粘连成无效路径）
        parts = _collect_result_text_fields(tool_result)
        if parts:
            return "\n".join(parts)
        return json.dumps(tool_result, ensure_ascii=False)
    # ToolOutput (pydantic): 直接取 data 中的 stdout/stderr/output/result，
    # 避免 str() 序列化导致反斜杠转义和路径含空格被正则截断
    data = getattr(tool_result, "data", None)
    if isinstance(data, dict):
        parts = _collect_result_text_fields(data)
        if parts:
            return "\n".join(parts)
    if hasattr(tool_result, "__dict__"):
        return str(tool_result)
    return str(tool_result)


def _unwrap_invoke_tool(
    tool_args: Any, tool_result: Any
) -> tuple[str | None, Any]:
    """解包 invoke_tool 的内部工具名和结果。

    invoke_tool 的 tool_args 形如 {"tool_name": "generate_image", ...}，
    tool_result 形如 {"success": True, "tool_name": "generate_image",
    "result": "..."} 或字符串。

    返回 (inner_tool_name, inner_result)。无法解包时返回 (None, None)。
    """
    # 优先从 tool_args 获取内部工具名
    args = _parse_tool_args_payload(tool_args)
    inner_name = args.get("tool_name")
    if not isinstance(inner_name, str):
        inner_name = None

    # 从 tool_result 中提取内部结果
    if isinstance(tool_result, dict):
        # dict 结果：取 "result" 字段作为内部结果
        inner_result = tool_result.get("result")
        # 同时尝试从 result dict 补充内部工具名
        if inner_name is None:
            rn = tool_result.get("tool_name")
            if isinstance(rn, str):
                inner_name = rn
    elif isinstance(tool_result, str):
        # 字符串结果：直接作为内部结果
        inner_result = tool_result
    else:
        # ToolOutput 等对象：尝试取 data 中的 result 字段
        data = getattr(tool_result, "data", None)
        if isinstance(data, dict):
            inner_result = data.get("result")
            if inner_name is None:
                rn = data.get("tool_name")
                if isinstance(rn, str):
                    inner_name = rn
        else:
            inner_result = tool_result

    if inner_name is None:
        return None, None
    return inner_name, inner_result


def _extract_raw_paths_from_result_text(
    tool_result: Any,
) -> list[str]:
    """从工具输出结果中正则提取路径候选（不按扩展名过滤）。"""
    result_text = _tool_result_to_text(tool_result)
    if not result_text:
        return []

    seen: set[str] = set()
    paths: list[str] = []
    for pattern in _FILE_PATH_PATTERNS:
        for match in pattern.findall(result_text):
            cleaned = _clean_path_candidate(match)
            if not cleaned:
                continue
            identity = cleaned.replace("\\", "/").lower()
            if identity in seen:
                continue
            seen.add(identity)
            paths.append(cleaned)
    return paths


def _extract_file_paths_from_write_tool(
    tool_name: str,
    tool_args: Any,
    tool_result: Any,
) -> list[str]:
    """从 write/edit 类工具参数或结果中提取产物路径。"""
    paths: list[str] = []
    payload = _parse_tool_args_payload(tool_args)
    for key in ("path", "file_path", "target_file", "abs_file_path"):
        value = str(payload.get(key) or "").strip()
        if value:
            paths.append(value)

    if paths:
        return list(dict.fromkeys(paths))

    if tool_name in {"write_file", "edit_file", "write", "write_text_file"}:
        for candidate in _extract_raw_paths_from_result_text(tool_result):
            if Path(candidate).suffix.lower() in _PYTHON_SCRIPT_EXTENSIONS:
                paths.append(candidate)
    return list(dict.fromkeys(paths))


def _extract_image_paths_from_tool_result(tool_result: Any) -> list[str]:
    """从工具输出结果中提取图像产物路径（结构化优先）。

    先从结果 dict 的 path/output/result 等键提取，命中即返回；
    未命中则回退到正文逐行宽松正则扫描，再按图像扩展名白名单过滤。
    """
    image_paths: list[str] = []
    seen: set[str] = set()

    # 1. 结构化提取优先
    result_dict: dict[str, Any] | None = None
    if isinstance(tool_result, dict):
        result_dict = tool_result
    elif hasattr(tool_result, "__dict__"):
        result_dict = tool_result.__dict__
    if result_dict is not None:
        for p in _iter_structured_path_values(result_dict):
            if Path(p).suffix.lower() in _IMAGE_ARTIFACT_EXTENSIONS:
                identity = p.replace("\\", "/").lower()
                if identity not in seen:
                    seen.add(identity)
                    image_paths.append(p)
        if image_paths:
            return image_paths

    # 2. 回退：保守正则逐行扫描
    for path in _scan_body_text_for_paths(_tool_result_to_text(tool_result)):
        if Path(path).suffix.lower() in _IMAGE_ARTIFACT_EXTENSIONS:
            identity = path.replace("\\", "/").lower()
            if identity not in seen:
                seen.add(identity)
                image_paths.append(path)
    return image_paths


def _artifact_identity(path: str) -> tuple[str, int, int] | None:
    """返回文件的 (规范化绝对路径, mtime_ns, size) 身份标识。

    stat 失败（文件不存在等）返回 None。
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (
        os.path.normcase(os.path.abspath(path)),
        st.st_mtime_ns,
        st.st_size,
    )


def _is_path_within(path: Path, base: Path) -> bool:
    """判断 path 是否位于 base 目录内（大小写不敏感，兼容 Windows）。"""
    try:
        resolved = str(path.resolve())
    except OSError:
        return False
    norm_resolved = os.path.normcase(resolved)
    norm_base = os.path.normcase(str(base))
    return norm_resolved == norm_base or norm_resolved.startswith(
        norm_base + os.sep
    )


def _validate_artifact_candidates(
    raw_paths: list[str],
    tool_start_time: float | None,
    workspace_base: Path | None,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """校验路径候选，返回通过全部校验的产物路径。

    校验项：UNC 过滤 + 黑名单排除 + 存在性/mtime/工作区校验 + 去重。
    """
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        if cancel_event is not None and cancel_event.is_set():
            break
        path = os.path.normpath(raw_path)
        if _is_unc_path(path):
            logger.debug("[artifact-detect] skip UNC path: %s", path)
            continue
        file_path = Path(path)
        if _is_excluded_path(path):
            continue
        try:
            st = file_path.stat()
        except OSError:
            continue
        if (
            workspace_base is not None
            and not _is_path_within(file_path, workspace_base)
        ):
            continue
        if (
            tool_start_time is not None
            and st.st_mtime < tool_start_time - _MTIME_TOLERANCE_S
        ):
            continue
        identity = os.path.normcase(os.path.abspath(path))
        if identity in seen:
            continue
        seen.add(identity)
        paths.append(path)
    return paths


def _file_sha256(path: str) -> str | None:
    """计算文件 sha256（超 MAX_HASH_BYTES 或读取失败返回 None）。"""
    try:
        if os.path.getsize(path) > MAX_HASH_BYTES:
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _snapshot_workspace(
    workspace_base: Path,
    cancel_event: threading.Event | None = None,
) -> WorkspaceSnapshot | None:
    """递归快照工作区文件：rel_key -> (abs, mtime_ns, size, sha256)。

    sha256 懒计算（快照阶段为 None）；黑名单目录剪枝不进入；
    文件数超 MAX_SCAN_FILES 或扫描异常返回 None（降级文本提取）。
    """
    snapshot: WorkspaceSnapshot = {}
    try:
        for root, dirs, files in os.walk(workspace_base, followlinks=False):
            if cancel_event is not None and cancel_event.is_set():
                return None
            # 目录剪枝：黑名单目录（node_modules 等）不进入（补尾部斜杠以匹配模式）
            dirs[:] = [
                d for d in dirs
                if not _is_excluded_path(os.path.join(root, d) + os.sep)
            ]
            for name in files:
                abs_path = os.path.join(root, name)
                if _is_excluded_path(abs_path):
                    continue
                try:
                    st = os.stat(abs_path)
                except OSError:
                    continue
                # os.walk 的 files 已排除目录；stat 成功后复用 st_mode
                # 判常规文件，避免 isfile 造成第二次 stat 系统调用
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = os.path.normcase(os.path.relpath(abs_path, workspace_base))
                snapshot[rel.replace("\\", "/")] = (
                    abs_path, st.st_mtime_ns, st.st_size, None,
                )
                if len(snapshot) > MAX_SCAN_FILES:
                    logger.warning(
                        "[artifact-detect] workspace scan exceeds "
                        "MAX_SCAN_FILES=%d, fallback to text extraction",
                        MAX_SCAN_FILES,
                    )
                    return None
    except OSError as exc:
        logger.warning(
            "[artifact-detect] workspace scan failed: %s", exc,
        )
        return None
    return snapshot


def _diff_snapshot(
    old: WorkspaceSnapshot,
    new: WorkspaceSnapshot,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """对比新旧快照，返回新增/变更文件的绝对路径列表。

    仅检测增量（新增/变更），不检测删除：遍历 new.keys()，基线独有
    （已删除）的文件不会出现在候选中。下游 hook（水印/署名等）只
    消费新生成的产物，删除检测无消费方；残留的旧 entry 无害——
    文件被重建时仍会走 mtime/size/hash 对比正确检出。

    变更判定：mtime/size 变化后再算 sha256 对比。基线 hash 为懒计算
    （首次变化时基线 hash 未知，保守判为变更），本次算出的 hash 写回
    new 快照，作为下次基线的参照，此后 touch（内容未变）可精确排除。
    hash 计算受 _HASH_BUDGET_S 累计预算约束：预算耗尽后放弃 hash
    保守判变更，避免大量变更文件把 sha256 读盘耗时放大到外层超时之外。
    """
    candidates: list[str] = []
    hash_deadline = time.monotonic() + _HASH_BUDGET_S
    for key, (abs_path, mtime_ns, size, new_hash) in new.items():
        if cancel_event is not None and cancel_event.is_set():
            break
        old_entry = old.get(key)
        if old_entry is None:
            logger.debug("[artifact-detect] baseline new: %s", abs_path)
            candidates.append(abs_path)
            continue
        old_mtime, old_size, old_hash = old_entry[1], old_entry[2], old_entry[3]
        if old_mtime == mtime_ns and old_size == size:
            continue
        # mtime/size 变化：算 hash 并写回本次快照（下次基线的参照）
        if new_hash is not None:
            cur_hash = new_hash
        elif time.monotonic() < hash_deadline:
            cur_hash = _file_sha256(abs_path)
        else:
            cur_hash = None  # 预算耗尽：保守判变更
        new[key] = (abs_path, mtime_ns, size, cur_hash)
        if (
            cur_hash is not None
            and old_hash is not None
            and cur_hash == old_hash
        ):
            logger.debug("[artifact-detect] baseline touch skip: %s", abs_path)
            continue
        logger.debug("[artifact-detect] baseline changed: %s", abs_path)
        candidates.append(abs_path)
    return candidates


def _finalize_candidates(
    paths: list[str],
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """静默期 finalize（批量轮询）：所有候选共享窗口等待大小稳定。

    防后台进程 / tmp->rename 竞态。所有候选并行轮询（总耗时与候选数
    解耦，上限 FINALIZE_TIMEOUT_S），超时仍未稳定的文件也保留
    （对齐原"超时取最后一次"的宽松语义，宁多勿漏）。
    """
    pending: dict[str, int] = {}
    for path in paths:
        try:
            pending[path] = os.path.getsize(path)
        except OSError:
            continue  # 已消失，剔除
    stable: set[str] = set()
    deadline = time.monotonic() + FINALIZE_TIMEOUT_S
    while pending and time.monotonic() < deadline:
        time.sleep(STABLE_INTERVAL_S)
        if cancel_event is not None and cancel_event.is_set():
            break
        still: dict[str, int] = {}
        for path, size0 in pending.items():
            try:
                current = os.path.getsize(path)
            except OSError:
                continue  # 等待期间消失，剔除
            if current == size0:
                stable.add(path)
            else:
                still[path] = current  # 仍在写，下一轮再看
        pending = still
    return [p for p in paths if p in stable or p in pending]


def _refresh_baseline_entries(
    snapshot: WorkspaceSnapshot,
    workspace_base: Path,
    paths: list[str],
) -> None:
    """局部刷新基线：hook 可能原地改写文件（水印），重新 stat + hash。"""
    for path in paths:
        try:
            rel = os.path.normcase(os.path.relpath(path, workspace_base))
        except ValueError:
            continue
        key = rel.replace("\\", "/")
        try:
            st = os.stat(path)
            new_hash = _file_sha256(path)
        except OSError:
            snapshot.pop(key, None)
            continue
        snapshot[key] = (
            os.path.abspath(path), st.st_mtime_ns, st.st_size, new_hash,
        )


def update_baseline_after_hook(
    snapshot: WorkspaceSnapshot | None,
    fired: bool,
    paths: list[str],
    workspace_base: Path | None = None,
) -> WorkspaceSnapshot | None:
    """返回 fire 后应存为实例基线的新快照（hook 可能原地改写文件，局部刷新）。

    snapshot 为 None（非基线路径/降级）时返回 None，调用方保持原基线不变。
    """
    if snapshot is None:
        return None
    if fired and paths:
        if workspace_base is not None:
            _refresh_baseline_entries(snapshot, workspace_base, paths)
        else:
            logger.debug(
                "[artifact-detect] baseline refresh skipped: no workspace_base"
            )
    return snapshot


def _detect_via_baseline(
    baseline: WorkspaceSnapshot,
    workspace_base: Path,
    cancel_event: threading.Event | None = None,
) -> tuple[list[str], WorkspaceSnapshot | None]:
    """基线 diff 检测：返回 (候选路径, 本次扫描快照)。

    快照失败/超限返回 ([], None)，调用方降级文本提取。
    """
    snapshot = _snapshot_workspace(workspace_base, cancel_event)
    if snapshot is None:
        return [], None
    candidates = _diff_snapshot(baseline, snapshot, cancel_event)
    candidates = _finalize_candidates(candidates, cancel_event)
    candidates = _validate_artifact_candidates(
        candidates,
        tool_start_time=None,  # 基线 diff 已保证变化，无需 mtime 校验
        workspace_base=workspace_base,
        cancel_event=cancel_event,
    )
    return candidates, snapshot


def _extract_artifact_paths_from_result(
    tool_result: Any,
    tool_start_time: float | None = None,
    workspace_base: Path | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """从工具输出中提取产物路径（结构化优先 + 正文宽松正则回退）。

    处理流程：
    1. 结构化提取：从结果 dict 的 path/file/output/result 等键提取候选，
       候选校验通过即返回；弱键垃圾候选（如 {"result": "ok"}）校验失败后
       继续走正文回退，不屏蔽真实路径
    2. 正文回退扫描：逐行宽松正则提取（单行超 _BODY_SCAN_MAX_LINE_LEN
       跳过，正则见 _ARTIFACT_PATH_PATTERNS）
    3. 统一校验：见 _validate_artifact_candidates
    """
    # 1. 结构化提取优先
    if isinstance(tool_result, dict):
        result_dict: dict[str, Any] | None = tool_result
    elif hasattr(tool_result, "__dict__"):
        result_dict = tool_result.__dict__
    else:
        result_dict = None
    if result_dict is not None:
        paths = _validate_artifact_candidates(
            _iter_structured_path_values(result_dict),
            tool_start_time,
            workspace_base,
            cancel_event,
        )
        if paths:
            return paths

    # 2. 结构化未命中则回退到正文逐行扫描
    result_text = _tool_result_to_text(tool_result)
    if not result_text:
        return []
    return _validate_artifact_candidates(
        _scan_body_text_for_paths(result_text, cancel_event=cancel_event),
        tool_start_time,
        workspace_base,
        cancel_event,
    )


class ArtifactDetection(NamedTuple):
    """统一产物检测结果。

    tool_name 为解包后的有效工具名（invoke_tool 间接调用时为内部工具名，
    如 generate_image），供 hook 上下文与日志归因使用。
    """

    tool_name: str
    paths: list[str]
    # 本次检测的当前工作区快照（仅基线 diff 路径返回，供调用方更新基线）
    baseline_snapshot: WorkspaceSnapshot | None = None
    # 基线 diff 快照失败/超限：调用方据此禁用本会话基线路径（降级文本提取）
    baseline_scan_failed: bool = False


def detect_artifact_paths(
    tool_name: str,
    tool_args: Any,
    tool_result: Any,
    *,
    tool_start_time: float | None = None,
    workspace_base: Path | None = None,
    cancel_event: threading.Event | None = None,
    baseline: WorkspaceSnapshot | None = None,
) -> ArtifactDetection:
    """统一产物检测入口，供 TaskExecutionRail / SkillTurboArtifactRail 共用。

    处理流程：
    1. invoke_tool 解包（内部工具名 + 内部结果替换外层值）
    2. 按工具类型分提取策略：
       - 图像工具：专用提取（返回权威路径，不做 mtime/工作区校验）
       - write 类工具：从 tool_args / 结果中提取
       - send_file_to_user：从 tool_args 显式提取
       - 代码执行类：正则提取 + 黑名单/mtime/工作区过滤
    3. 统一出口：仅保留实际存在的文件（write/send_file 提取的路径
       可能并不存在，需过滤后再触发 hook）
    """
    # invoke_tool：解包内部工具名和结果，按内部工具类型分提取策略
    if tool_name in INVOKE_TOOL_NAMES:
        inner_name, inner_result = _unwrap_invoke_tool(tool_args, tool_result)
        if inner_name is None:
            return ArtifactDetection(tool_name, [])
        # 只读内部工具不产文件，短路避免 633s stat 风暴（见 READONLY_INNER_TOOLS）。
        if inner_name in READONLY_INNER_TOOLS:
            return ArtifactDetection(inner_name, [])
        tool_name = inner_name
        tool_result = inner_result
        # 内部工具参数位于 invoke_tool 的 arguments 字段中
        payload = _parse_tool_args_payload(tool_args)
        inner_args = payload.get("arguments")
        tool_args = inner_args if inner_args is not None else {}

    paths: list[str] = []
    baseline_snapshot: WorkspaceSnapshot | None = None
    baseline_scan_failed = False

    if tool_name in IMAGE_TOOL_NAMES:
        # 图像工具返回权威路径，不做 mtime/工作区校验
        paths = _extract_image_paths_from_tool_result(tool_result)
    elif tool_name in WRITE_TOOL_NAMES:
        # write 类工具：维持原有提取逻辑
        paths = _extract_file_paths_from_write_tool(
            tool_name, tool_args, tool_result
        )
    elif tool_name in SEND_FILE_TOOL_NAMES:
        # send_file_to_user：从 tool_args 提取显式路径
        payload = _parse_tool_args_payload(tool_args)
        for key in ("path", "file_path", "file", "abs_file_path"):
            value = str(payload.get(key) or "").strip()
            if value:
                paths.append(value)
        raw_list = payload.get("abs_file_path_list")
        if isinstance(raw_list, list):
            paths.extend(str(p).strip() for p in raw_list if str(p).strip())
        paths = list(dict.fromkeys(paths))
    else:
        # bash / mcp_exec_command：优先基线 diff（不依赖工具输出文本），
        # 基线缺失/快照失败时降级文本提取
        if baseline is not None and workspace_base is not None:
            paths, baseline_snapshot = _detect_via_baseline(
                baseline, workspace_base, cancel_event,
            )
            # 快照失败/超限：调用方据此禁用本会话基线路径，
            # 避免后续工具反复无效扫描（超限工作区）
            baseline_scan_failed = baseline_snapshot is None
        if not paths:
            paths = _extract_artifact_paths_from_result(
                tool_result,
                tool_start_time=tool_start_time,
                workspace_base=workspace_base,
                cancel_event=cancel_event,
            )

    # 统一出口：仅保留实际存在的文件（跳过 UNC 网络路径避免同步阻塞）
    paths = [p for p in paths if not _is_unc_path(p) and Path(p).exists()]
    return ArtifactDetection(
        tool_name, paths, baseline_snapshot, baseline_scan_failed,
    )


def resolve_workspace_base() -> Path | None:
    """解析请求级工作区根目录，用于产物路径范围校验。"""
    try:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            get_effective_request_workspace_dir,
        )

        epd = get_effective_request_workspace_dir()
        if epd:
            return Path(epd).resolve()
    except ImportError:
        logger.debug(
            "[artifact-detect] context_vars unavailable, "
            "falling back to default workspace resolution",
            exc_info=True,
        )
    except Exception as exc:
        logger.debug(
            "[artifact-detect] Failed to get effective_request_workspace_dir: %s",
            exc,
        )
    return None


def pop_tool_start_time(
    start_times: dict[str, float], ctx: AgentCallbackContext
) -> float | None:
    """取出本次工具调用的开始时间（before_tool_call 时按 tool_call_id 记录）。"""
    tc = getattr(ctx.inputs, "tool_call", None)
    tool_call_id = str(getattr(tc, "id", "") or "")
    if not tool_call_id:
        return None
    return start_times.pop(tool_call_id, None)


async def detect_artifact_paths_safe(
    ctx: AgentCallbackContext,
    session_id: str,
    tool_start_time: float | None,
    *,
    log_prefix: str,
    baseline: WorkspaceSnapshot | None = None,
) -> ArtifactDetection | None:
    """线程中执行产物检测并加超时保护，超时/异常时返回 None（跳过检测）。

    将同步的 detect_artifact_paths 移到线程中执行，避免 stat() 对不可达
    网络路径同步阻塞 event loop。
    供 TaskExecutionRail 与 SkillTurboArtifactRail 共用；调用方需保证
    ctx.inputs 为 ToolCallInputs。
    """
    cancel_event = threading.Event()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                detect_artifact_paths,
                ctx.inputs.tool_name,
                getattr(ctx.inputs, "tool_args", None),
                getattr(ctx.inputs, "tool_result", None),
                tool_start_time=tool_start_time,
                workspace_base=resolve_workspace_base(),
                cancel_event=cancel_event,
                baseline=baseline,
            ),
            timeout=ARTIFACT_DETECT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # 超时后 set 通知后台线程在下一个检查点退出（stat 卡住时线程
        # 自行结束，不影响 event loop）
        cancel_event.set()
        logger.warning(
            "%s artifact detection timed out (%.1fs), skipping "
            "session_id=%s tool=%s",
            log_prefix,
            ARTIFACT_DETECT_TIMEOUT_S,
            session_id,
            ctx.inputs.tool_name,
        )
        return None
    except Exception as exc:
        # 检测失败不应打断 rail 回调链，记录后跳过本工具的产物检测
        logger.warning(
            "%s artifact detection failed, skipping session_id=%s "
            "tool=%s error=%s",
            log_prefix,
            session_id,
            ctx.inputs.tool_name,
            exc,
            exc_info=True,
        )
        return None


def extract_effective_project_dir(metadata: Any) -> str | None:
    """从请求 metadata 提取 effective_project_dir（strip 后非空才返回）。

    供 StreamEventRail 的 ContextVar 重绑块与 TaskExecutionRail 的
    workspace 副本直读共用，避免提取逻辑双份维护。
    """
    if not isinstance(metadata, dict):
        return None
    epd = metadata.get("effective_project_dir")
    if isinstance(epd, str) and epd.strip():
        return epd.strip()
    return None


class WorkspaceBaselineState:
    """工作区基线懒建状态，供 TaskExecutionRail 与 SkillTurboArtifactRail 组合复用。

    双检锁懒建基线：并行 tool_call 时首个 bash 建一次快照，其余等待复用。
    快路径不碰锁（会话首个 bash 之后的常态调用零开销）；拿锁后双检，等待
    期间其他协程可能已建好/已禁用。基线必须先于本轮任何执行类工具建立，
    等待者串行化到快照完成（毫秒级）后才放行工具执行；超时场景等待者拿锁
    后双检发现已禁用，直接走降级，不再重复烧一次完整超时扫描。

    workspace_base：调用方直读的请求级工作区（如 TaskExecutionRail 从
    metadata 副本解析）；为 None 时回退 resolve_workspace_base() 读
    ContextVar，供无副本的调用方（SkillTurboArtifactRail）使用。

    基线状态（snapshot/disabled）本质上是 per-workspace 的，而本实例
    随 rail 跨请求复用（per-session）：记录状态所属的 snapshot_base，
    ensure() 发现工作区切换（ACP 请求级 workspace_dir 可变）时重置
    状态重建基线，避免用 W1 的基线 diff W2 的状态导致全量误报；
    disabled 结论同样只对所属工作区有效，切换时重置重新尝试。
    """

    def __init__(self) -> None:
        # 工作区基线快照：bash 类工具执行前的文件状态，供增量 diff 检测产物
        self.snapshot: WorkspaceSnapshot | None = None
        # 基线状态所属的工作区：成功/禁用都记录，供切换检测与重置判定
        # （snapshot_base=None 表示尚未建过，_same_base 恒 False）
        self.snapshot_base: Path | None = None
        # 基线 diff 会话级禁用标志：快照超时/失败一次即禁用本会话基线路径
        # （降级文本提取，与基线引入前行为一致），避免反复无效扫描/超时
        self.disabled = False
        # 基线懒建双检锁：并行 tool_call 时首个 bash 建一次快照，等待者复用
        self.init_lock = asyncio.Lock()

    def _same_base(self, base: Path) -> bool:
        """工作区一致性判定：normcase 归一后字符串比较。

        两个 base 来源（metadata 副本 / ContextVar）均已 .resolve()，
        normcase 覆盖 Windows 大小写与斜杠方向差异即可。
        """
        return (
            self.snapshot_base is not None
            and os.path.normcase(str(self.snapshot_base))
            == os.path.normcase(str(base))
        )

    @property
    def effective(self) -> WorkspaceSnapshot | None:
        """当前生效的基线：禁用后返回 None（跳过基线 diff，走文本提取）。"""
        return None if self.disabled else self.snapshot

    async def ensure(
        self,
        tool_name: str,
        tool_args: Any,
        *,
        log_prefix: str,
        workspace_base: Path | None = None,
    ) -> None:
        """懒建基线：首个 bash 类工具执行前建工作区快照，等待者复用。

        invoke_tool 间接调用时按解包出的内部工具名判定（与 after 路径
        detect_artifact_paths 的解包对齐），使 invoke_tool -> bash 场景
        也能在执行前懒建基线，而非降级文本提取。无需建立时保持现状。
        工作区切换时重置 snapshot/disabled 重建基线（见类 docstring）。
        """
        effective_name = tool_name
        if tool_name in INVOKE_TOOL_NAMES:
            inner_name, _ = _unwrap_invoke_tool(tool_args, None)
            if inner_name is not None:
                effective_name = inner_name
        if effective_name not in CODE_EXEC_TOOL_NAMES:
            return
        base = workspace_base if workspace_base is not None else resolve_workspace_base()
        if base is None:
            return
        # 快路径：已建/已禁用且工作区未变才直接返回——不比较 base 会把
        # 旧工作区的基线/禁用结论套到新工作区（跨请求切换时全量误报）
        if (self.snapshot is not None or self.disabled) and self._same_base(base):
            return
        async with self.init_lock:
            # 双检同样带 base 比较：等待者拿锁期间若他人建的是其他工作区
            # 的基线，不可复用（否则回到陈旧基线误报问题）
            if (self.snapshot is not None or self.disabled) and self._same_base(base):
                return
            # 工作区切换：旧 snapshot/disabled 结论只对旧工作区有效，
            # 重置后重建（disabled 随切换重置：旧工作区超限/超时不代表
            # 新工作区也超限，切换是一次性的，无重试风暴）
            if not self._same_base(base):
                logger.info(
                    "%s workspace changed %s -> %s, rebuild baseline",
                    log_prefix, self.snapshot_base, base,
                )
                self.snapshot = None
                self.disabled = False
            # 线程 + 超时 + cancel_event 三重防护，防 stat 阻塞 event loop；
            # 超时一次即禁用本会话基线路径，避免反复重试超时
            cancel_event = threading.Event()
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.to_thread(_snapshot_workspace, base, cancel_event),
                    timeout=BASELINE_SNAPSHOT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # 通知后台线程在下一个检查点退出；本会话禁用基线路径，
                # 避免后续工具反复重试超时
                cancel_event.set()
                logger.warning(
                    "%s baseline snapshot timed out (%.1fs), disable baseline "
                    "diff for this session",
                    log_prefix,
                    BASELINE_SNAPSHOT_TIMEOUT_S,
                )
                self.disabled = True
                self.snapshot_base = base
                return
            if snapshot is not None:
                logger.info(
                    "%s baseline ready files=%d", log_prefix, len(snapshot)
                )
                self.snapshot = snapshot
                self.snapshot_base = base
            else:
                # 快照失败/超限返回 None（具体原因已由 _snapshot_workspace
                # 记录）：本会话禁用基线路径，与超时路径保持一致，避免后续
                # bash 工具反复重扫（超限工作区每次都要扫满 MAX_SCAN_FILES
                # 才放弃）；after 路径 baseline_scan_failed 因 baseline 为
                # None 无法兜底，必须在此禁用
                self.disabled = True
                self.snapshot_base = base
                logger.warning(
                    "%s baseline snapshot unavailable, disable baseline "
                    "diff for this session",
                    log_prefix,
                )


def filter_unhooked(
    paths: list[str], hooked: set[tuple[str, int, int]]
) -> list[str]:
    """过滤已触发过 hook 且内容未变化的文件（防重复后处理，如水印叠盖）。"""
    result: list[str] = []
    for path in paths:
        identity = _artifact_identity(path)
        if identity is not None and identity in hooked:
            continue
        result.append(path)
    return result


def mark_hooked(
    paths: list[str], hooked: set[tuple[str, int, int]]
) -> None:
    """记录已触发 hook 的文件身份（hook 可能原地改写文件，故事后重新 stat）。"""
    for path in paths:
        identity = _artifact_identity(path)
        if identity is not None:
            hooked.add(identity)


async def fire_artifact_hook(
    session_id: str,
    tool_name: str,
    task_id: str | None,
    artifact_paths: list[str],
    *,
    log_prefix: str,
) -> bool:
    """触发产物后处理扩展 hook，返回是否成功触发。

    对同一批产物同时触发 IMAGE_ARTIFACT_POST_PROCESS 和
    ARTIFACT_POST_PROCESS，扩展在 handler 内部按扩展名自行过滤
    （如加水印、.py Unicode 归一化）。跳过（Registry 未初始化 / import
    失败）或扩展抛错返回 False，调用方据此决定是否记录去重身份，避免
    失败后永久跳过该文件。
    """
    try:
        from jiuwenswarm.extensions.registry import ExtensionRegistry
        from jiuwenswarm.extensions.hook_event import (
            AgentServerHookEvents,
        )
        from jiuwenswarm.extensions.hooks_context import (
            ArtifactPostProcessHookContext,
            ImageArtifactHookContext,
        )
    except ImportError as exc:
        logger.warning(
            "%s skip artifact post-process hook, import failed: %s",
            log_prefix, exc,
        )
        return False

    registry = ExtensionRegistry.get_instance()

    # 同时触发两个事件，扩展各自按扩展名过滤
    for event_name, ctx_cls in (
        (AgentServerHookEvents.IMAGE_ARTIFACT_POST_PROCESS, ImageArtifactHookContext),
        (AgentServerHookEvents.ARTIFACT_POST_PROCESS, ArtifactPostProcessHookContext),
    ):
        hook_ctx = ctx_cls(
            session_id=session_id,
            tool_name=tool_name,
            task_id=task_id,
            artifact_paths=artifact_paths,
        )
        try:
            await registry.trigger(event_name, hook_ctx)
        except RuntimeError:
            logger.warning(
                "%s skip %s: ExtensionRegistry not initialized",
                log_prefix, event_name,
            )
            return False
        except Exception as exc:
            logger.warning(
                "%s %s failed session_id=%s tool=%s error=%s",
                log_prefix, event_name, session_id, tool_name, exc,
            )
            return False

    logger.info(
        "%s artifact hooks done session_id=%s tool=%s count=%d",
        log_prefix,
        session_id,
        tool_name,
        len(artifact_paths),
    )
    return True


@dataclass
class TaskExecutionContext:
    task_id: str
    task_content: str
    task_index: int
    total_tasks: int
    parent_request_id: str
    start_time: float
    source: Literal["todo"]
    status: Literal["running", "succeeded", "failed", "skipped"] = "running"


class TaskExecutionRail(DeepAgentRail):
    """Emit task.start/task.complete/task.update around todo execution transitions.

    TODO_TOOLS (todo_create, todo_modify, todo_list, todo_get) trigger todo
    state change detection via _sync_todo_and_emit_transitions. Non-todo tools
    bind the current in-progress todo task via the _ACTIVE_TASK_ID ContextVar.
    """

    _BINDING_IN_PROGRESS = frozenset({"in_progress"})
    _BINDING_PENDING = frozenset({"pending", "waiting"})
    _TODO_DONE_STATUSES = frozenset({"completed", "cancelled"})

    priority = 85
    # Do NOT inherit this rail into a general-purpose subagent. init() binds
    # ``self._deep_agent`` to the agent handed in, and the subagent's
    # _ensure_initialized re-runs init_rail with the *child* — rebinding the
    # shared instance to the subagent forever (the parent never re-inits).
    # After that _get_todo_workspace_path resolves todo.json under the child's
    # empty sub_agents workspace, _load_todo_from_json returns [], the todo map
    # stays empty, and no pending->in_progress transition is ever detected again
    # — so every later parent stage's task.start stops firing (e.g. PPT stage4+
    # missing from history.json after a general-purpose subagent ran in stage3).
    inherit_to_subagents = False

    TODO_TOOLS = frozenset({
        "todo_create", "todo_get", "todo_list", "todo_modify",
    })
    SKILL_COMPLETE_TOOLS = frozenset({"skill_complete"})
    # 触发产物后处理 hook 的工具（共享常量，见模块级定义）
    ARTIFACT_DETECTION_TOOLS = ARTIFACT_DETECTION_TOOL_NAMES

    def __init__(self) -> None:
        super().__init__()
        self._todo_map: dict[str, dict[str, Any]] = {}
        self._todo_map_before_tool: dict[str, dict[str, Any]] = {}
        self._active_tasks: dict[str, TaskExecutionContext] = {}
        self._todo_started: set[str] = set()
        self._deep_agent: Any | None = None
        # 中断恢复 skip 机制：before_invoke 检测到 skip 标志时（本请求由
        # prepare_* 注入了恢复提示），记录旧 todo id，_emit_task_update_event
        # 据此过滤已清理的旧项，避免向前端回灌跨请求残留 todo。
        self._stale_todo_ids: set[str] = set()
        # 产物检测：工具调用开始时间（按 tool_call_id 记录，用于 mtime 校验）
        self._tool_start_times: dict[str, float] = {}
        # 已触发过产物 hook 的文件身份（路径+mtime_ns+size），防止重复后处理
        self._hooked_artifacts: set[tuple[str, int, int]] = set()
        # 工作区基线懒建状态：bash 类工具执行前的文件快照，供增量 diff 检测产物
        self._baseline = WorkspaceBaselineState()
        # 请求 metadata 副本（adapter 每轮 bind_request 注入）：工具运行在
        # supervisor round task 不继承请求任务的 ContextVar，且本 rail 先于
        # StreamEventRail 执行（priority 大者先），workspace 须从副本直读
        self._skill_turbo_request_metadata: dict | None = None

    def set_skill_turbo_request_metadata(self, metadata: dict | None) -> None:
        """注入当前请求 metadata 副本，供 before_tool_call 解析请求级工作区。"""
        self._skill_turbo_request_metadata = (
            dict(metadata) if isinstance(metadata, dict) else None
        )

    def _resolve_metadata_workspace_base(self) -> Path | None:
        """从请求 metadata 副本直读请求级工作区。

        resolve_workspace_base() 读 ``_effective_request_workspace_dir``
        ContextVar，该绑定在请求任务设置、supervisor round task 不继承，
        且 StreamEventRail 的重绑发生在本 rail 之后（priority 数值大者
        先执行），before 阶段读不到。这里直接从 adapter 注入的 metadata
        副本提取 effective_project_dir；不 set/reset ContextVar，避免
        与 StreamEventRail after 阶段的 token reset 乱序冲突。
        """
        epd = extract_effective_project_dir(
            self._skill_turbo_request_metadata
        )
        if epd is not None:
            try:
                return Path(epd).resolve()
            except OSError:
                logger.warning(
                    "[TaskExecutionRail] invalid effective_project_dir in "
                    "request metadata: %r",
                    epd,
                )
        return None

    def get_current_task_id(self) -> str | None:
        return _ACTIVE_TASK_ID.get()

    def init(self, agent: Any) -> None:
        self._deep_agent = agent

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        session_id = ""
        if ctx.session is not None:
            try:
                session_id = str(ctx.session.get_session_id() or "")
            except Exception:
                logger.debug(
                    "[TaskExecutionRail] before_invoke: "
                    "failed to get session_id",
                    exc_info=True,
                )
        # skip 标志置位时捕获旧 todo id 供 _emit_task_update_event 过滤。
        skip_invoke = (
            ctx.session is not None
            and is_skip_invoke_task_update_sync(ctx.session)
        )
        if skip_invoke:
            self._stale_todo_ids = set(self._todo_map.keys())
            # 同步写入 session，供 StreamEventRail._emit_todo_updated（todo.updated
            # 旁路）过滤同一批跨请求残留——那条旁路读不到本 rail 的实例字段。
            if ctx.session is not None:
                try:
                    set_stale_todo_ids(ctx.session, self._stale_todo_ids)
                except Exception:
                    logger.debug(
                        "[TaskExecutionRail] before_invoke: set_stale_todo_ids failed",
                        exc_info=True,
                    )
            logger.info(
                "[TaskExecutionRail] before_invoke: skip_invoke_task_update "
                "flag set, captured %d stale todo ids=%s",
                len(self._stale_todo_ids),
                list(self._stale_todo_ids),
            )
        else:
            self._stale_todo_ids = set()
            # 与实例字段同步清掉 session 上的残留（上轮崩溃兜底）。
            if ctx.session is not None:
                try:
                    clear_stale_todo_ids(ctx.session)
                except Exception:
                    logger.debug(
                        "[TaskExecutionRail] before_invoke: clear_stale_todo_ids failed",
                        exc_info=True,
                    )
        logger.info(
            "[TaskExecutionRail] before_invoke reset tracking: "
            "session_id=%s prev_todo_map_size=%d prev_active_tasks=%s "
            "skip_invoke=%s",
            session_id,
            len(self._todo_map),
            list(self._active_tasks.keys()),
            skip_invoke,
        )
        self._todo_map = {}
        self._todo_map_before_tool = {}
        self._active_tasks = {}
        self._todo_started = set()
        self._tool_start_times = {}
        _ACTIVE_TASK_ID.set(None)
        if isinstance(ctx.inputs, InvokeInputs):
            await self._init_task_tracking(ctx.session)
            has_active_tasks = any(
                t.get("status") in ("pending", "in_progress")
                for t in self._todo_map.values()
            )
            if has_active_tasks and not skip_invoke:
                parent_request_id = self._extract_request_id(ctx)
                await self._emit_task_update_event(
                    ctx.session, parent_request_id
                )
        self._bind_context_to_in_progress_task()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Bind task_id before LLM calls."""
        self._bind_context_to_in_progress_task()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_name = ctx.inputs.tool_name

        if tool_name in self.TODO_TOOLS:
            session_id = ""
            if ctx.session is not None:
                try:
                    session_id = str(
                        ctx.session.get_session_id() or ""
                    )
                except Exception:
                    logger.debug(
                        "[TaskExecutionRail] before_tool_call: "
                        "failed to get session_id",
                        exc_info=True,
                    )
            logger.info(
                "[TaskExecutionRail] todo snapshot before_tool: "
                "session=%s todo_map_size=%d active_tasks=%s",
                session_id,
                len(self._todo_map),
                list(self._active_tasks.keys()),
            )
            self._todo_map_before_tool = dict(self._todo_map)
            return

        if tool_name in self.SKILL_COMPLETE_TOOLS:
            if self._has_incomplete_todos(self._todo_map):
                tc = ctx.inputs.tool_call
                tool_call_id = str(getattr(tc, "id", "") or "")
                msg = (
                    "[SKILL_COMPLETE_BLOCKED] todo.json 中仍有未完成任务，"
                    "请先用 todo_modify 将全部已完成项标为 completed。"
                )
                ctx.extra["_skip_tool"] = True
                ctx.inputs.tool_result = msg
                ctx.inputs.tool_msg = ToolMessage(
                    content=msg, tool_call_id=tool_call_id,
                )
            return

        if tool_name in self.ARTIFACT_DETECTION_TOOLS:
            # 记录工具开始时间，供 after_tool_call 做 mtime 校验
            tc = ctx.inputs.tool_call
            tool_call_id = str(getattr(tc, "id", "") or "")
            if tool_call_id:
                self._tool_start_times[tool_call_id] = time.time()
            # bash 类工具懒建基线（含 invoke_tool 间接调用的内部工具名
            # 判定、超时禁用与并行去重，详见 WorkspaceBaselineState.ensure）。workspace_base 从 metadata
            # 副本直读：ContextVar 在本 rail 执行时尚未由 StreamEventRail
            # 重绑（priority 大者先执行）
            await self._baseline.ensure(
                tool_name,
                getattr(ctx.inputs, "tool_args", None),
                log_prefix="[TaskExecutionRail]",
                workspace_base=self._resolve_metadata_workspace_base(),
            )

        self._bind_context_to_in_progress_task()
        if tool_name == "skill_acceleration_exec":
            # Rail callbacks and the tool body may run in copied Contexts.
            # Export only display ownership; this does not affect tool
            # registration, selection, or execution.
            ctx.extra[SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY] = (
                _ACTIVE_TASK_ID.get() is not None
            )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_name = ctx.inputs.tool_name

        if tool_name in self.TODO_TOOLS:
            await self._sync_todo_and_emit_transitions(ctx)
            return

        await self._auto_advance_pending_to_in_progress(ctx)
        # todo_create 可能已把首项写成 in_progress，但故意未发 task.start；
        # 首个工作工具再懒启动，避免「只建待办 + 回复用户」时前端任务栈吞掉正文。
        await self._lazy_start_in_progress_todo_on_work_tool(ctx)

        if tool_name in self.ARTIFACT_DETECTION_TOOLS:
            await self._trigger_artifact_hooks(ctx)
            return

    async def _auto_advance_pending_to_in_progress(
        self, ctx: AgentCallbackContext
    ) -> None:
        """Auto-advance the current pending task to in_progress when a work tool is called.

        When the LLM calls a non-todo work tool and the bound active task is still
        ``pending``, we automatically transition it to ``in_progress`` — no
        ``todo_modify`` call needed.  This eliminates todo-only LLM rounds that
        would otherwise be spent just flipping status from pending to in_progress.
        """
        active_id = _ACTIVE_TASK_ID.get()
        if not active_id:
            return
        raw_id = active_id.removeprefix("todo:")
        task = self._todo_map.get(raw_id)
        if not task or task.get("status") not in self._BINDING_PENDING:
            return

        session = ctx.session
        if session is None:
            return

        session_id = session.get_session_id()
        parent_request_id = self._extract_request_id(ctx)

        todo_path = self._get_todo_workspace_path(session_id)
        if todo_path is None or not todo_path.exists():
            return

        try:
            with open(todo_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list):
                return
        except (OSError, ValueError):
            return

        changed = False
        for item in items:
            if item.get("id") == raw_id and str(
                item.get("status", "pending")
            ).lower() in self._BINDING_PENDING:
                item["status"] = "in_progress"
                changed = True
                break

        if not changed:
            return

        try:
            with open(todo_path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
        except OSError:
            logger.warning(
                "[TaskExecutionRail] auto_advance: failed to write "
                "todo.json session_id=%s task_id=%s",
                session_id,
                raw_id,
            )
            return

        if raw_id not in self._todo_started:
            task["status"] = "in_progress"
            await self._emit_task_start_event(
                session, raw_id, task, parent_request_id, source="todo",
            )
            self._todo_started.add(raw_id)

        self._todo_map[raw_id]["status"] = "in_progress"
        _ACTIVE_TASK_ID.set(f"todo:{raw_id}")

        await self._emit_task_update_event(session, parent_request_id)

        logger.info(
            "[TaskExecutionRail] auto_advance: pending→in_progress "
            "session_id=%s task_id=%s",
            session_id,
            raw_id,
        )

    async def _lazy_start_in_progress_todo_on_work_tool(
        self, ctx: AgentCallbackContext
    ) -> None:
        """Emit deferred ``task.start`` when work begins on an in_progress todo.

        ``todo_create`` may mark the first item ``in_progress`` without emitting
        ``task.start`` (see ``_sync_todo_and_emit_transitions``). The first
        non-todo work tool must open the UI task segment so subsequent tool /
        progress events still attach correctly.
        """
        session = ctx.session
        if session is None:
            return
        self._bind_context_to_in_progress_task()
        active_id = _ACTIVE_TASK_ID.get()
        if not active_id or not active_id.startswith("todo:"):
            return
        raw_id = active_id.removeprefix("todo:")
        task = self._todo_map.get(raw_id)
        if not task or str(task.get("status", "")).lower() != "in_progress":
            return
        if raw_id in self._todo_started:
            return
        parent_request_id = self._extract_request_id(ctx)
        await self._emit_task_start_event(
            session, raw_id, task, parent_request_id, source="todo",
        )
        self._todo_started.add(raw_id)
        await self._emit_task_update_event(session, parent_request_id)
        logger.info(
            "[TaskExecutionRail] lazy_start: in_progress todo opened on work tool "
            "task_id=%s",
            raw_id,
        )

    async def _trigger_artifact_hooks(
        self, ctx: AgentCallbackContext
    ) -> None:
        """产物落盘后触发扩展 hook。

        同时触发 IMAGE_ARTIFACT_POST_PROCESS 和 ARTIFACT_POST_PROCESS，
        扩展在 handler 内按扩展名自行过滤（如加水印、.py Unicode 归一化）。
        已触发过 hook 且内容未变化的文件会被跳过，防止重复后处理
        （如水印叠盖）。ExtensionRegistry 未初始化或扩展抛错时仅记
        warning，不阻断主流程。
        """
        session = ctx.session
        if session is None:
            return
        try:
            session_id = session.get_session_id()
        except Exception:
            logger.debug(
                "[TaskExecutionRail] artifact hooks: "
                "failed to get session_id",
                exc_info=True,
            )
            return

        # 线程 + 超时 + 异常兜底执行产物检测，避免阻塞 event loop
        # （基线禁用后传 None：跳过基线 diff，走文本提取）
        detection = await detect_artifact_paths_safe(
            ctx,
            session_id,
            pop_tool_start_time(self._tool_start_times, ctx),
            log_prefix="[TaskExecutionRail]",
            baseline=self._baseline.effective,
        )
        if detection is None:
            return
        # 基线 diff 快照失败/超限：禁用本会话基线路径，避免反复无效扫描
        if detection.baseline_scan_failed:
            self._baseline.disabled = True
            logger.warning(
                "[TaskExecutionRail] baseline scan failed, disable baseline "
                "diff for this session"
            )
        task_id = _ACTIVE_TASK_ID.get()
        snapshot = detection.baseline_snapshot

        # 去重：跳过已 hook 过且内容未变化的文件
        paths = filter_unhooked(detection.paths, self._hooked_artifacts)

        fired = False
        if paths:
            fired = await fire_artifact_hook(
                session_id=session_id,
                tool_name=detection.tool_name,
                task_id=task_id,
                artifact_paths=paths,
                log_prefix="[TaskExecutionRail]",
            )
            if fired:
                mark_hooked(paths, self._hooked_artifacts)

        # 更新基线：仅拿到本次快照时更新（snapshot 为 None 表示非基线路径/
        # 降级，保持原基线不变）；hook 可能原地改写文件（水印），局部刷新
        # 候选条目（含 sha256 读文件，放线程防阻塞 event loop）
        if snapshot is not None:
            self._baseline.snapshot = await asyncio.to_thread(
                update_baseline_after_hook,
                snapshot, fired, paths, resolve_workspace_base(),
            )

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        self._todo_map_before_tool = {}
        # 本轮 invoke 结束后清掉 skip 标志与 stale 缓存：skip 只在
        # prepare→before_invoke 这一段窗口生效，之后模型若实际推进
        # todo（todo_modify 写盘 + 下一次 emit 应正常广播新快照）。
        if ctx.session is not None:
            try:
                clear_skip_invoke_task_update_sync(ctx.session)
                clear_stale_todo_ids(ctx.session)
            except Exception:
                logger.debug(
                    "[TaskExecutionRail] after_invoke: "
                    "clear_skip_invoke_task_update_sync failed",
                    exc_info=True,
                )
        self._stale_todo_ids = set()
        self._bind_context_to_in_progress_task()

    # ------------------------------------------------------------------
    # Task list state loading
    # ------------------------------------------------------------------

    async def _init_task_tracking(
        self, session: Session | None
    ) -> None:
        if session is None:
            return
        session_id = session.get_session_id()
        try:
            todo_items = self._load_todo_from_json(session_id)
            if todo_items:
                self._todo_map = self._build_map_from_todo_items(
                    todo_items
                )
                # 记录磁盘加载的 todo ids 快照，用于区分「磁盘旧残留」与「本轮 LLM 新建」
                try:
                    set_pre_invoke_todo_ids(session, set(self._todo_map.keys()))
                except Exception:
                    logger.debug(
                        "[TaskExecutionRail] set_pre_invoke_todo_ids failed",
                        exc_info=True,
                    )
                logger.info(
                    "[TaskExecutionRail] Loaded todo.json "
                    "session_id=%s tasks=%d",
                    session_id,
                    len(todo_items),
                )
        except Exception as exc:
            logger.debug(
                "[TaskExecutionRail] Failed to load todo.json: %s",
                exc,
            )

    def _load_todo_from_json(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        todo_path = self._get_todo_workspace_path(session_id)
        if todo_path is None or not todo_path.exists():
            return []
        with open(todo_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []

    def _get_todo_workspace_path(
        self, session_id: str
    ) -> Path | None:
        """Resolve todo.json path from the deep agent's workspace config."""
        da = self._deep_agent
        if da is None:
            return None
        try:
            deep_config = da.deep_config
            workspace_path = Path(
                deep_config.workspace.get_node_path(WorkspaceNode.TODO)
            )
            return workspace_path / session_id / "todo.json"
        except Exception as exc:
            logger.debug(
                "[TaskExecutionRail] Failed to resolve todo "
                "workspace path: %s",
                exc,
            )
            return None

    def _build_map_from_todo_items(
        self, items: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        mapped: dict[str, dict[str, Any]] = {}
        total = len(items)
        for index, item in enumerate(items):
            task_id = item.get("id", str(index))
            status = item.get("status", "pending")
            if isinstance(status, str):
                normalized_status = status.lower()
            else:
                normalized_status = str(status).lower()
            mapped[task_id] = {
                "content": item.get(
                    "content", item.get("activeForm", "")
                ),
                "status": normalized_status,
                "index": index,
                "total": total,
            }
        return mapped

    @staticmethod
    def _has_incomplete_todos(
        todo_map: dict[str, dict[str, Any]]
    ) -> bool:
        if not todo_map:
            return False
        return any(
            str(task.get("status", "pending")).lower()
            not in TaskExecutionRail._TODO_DONE_STATUSES
            for task in todo_map.values()
        )

    # ------------------------------------------------------------------
    # State transition detection + event emission
    # ------------------------------------------------------------------

    async def _sync_todo_and_emit_transitions(
        self, ctx: AgentCallbackContext
    ) -> None:
        """Diff todo state before vs after a todo tool call and emit events.

        - pending -> in_progress  => task.start
          (except ``todo_create``: defer start until a work tool — see below)
        - in_progress -> completed => task.complete
        - pending -> completed (skipped in_progress) => task.start then
          task.complete. Frontend hidePending hides pending rows, and the
          left task list falls back to task.start segments while streaming;
          without start/complete the stage is invisible until the frozen
          completed snapshot appears after the run finishes.
        Always emits task.update (full snapshot) at the end.

        ``todo_create`` always marks the first item ``in_progress`` and tells
        the model to execute immediately. Emitting ``task.start`` at create
        time forces RelayClaw/frontends to treat the following user-facing
        reply as task-scoped text (and dual-write thinking), so create-only
        reminder flows show empty main bubbles. Defer ``task.start`` for
        create; ``todo_modify`` / work-tool lazy start still open the segment.
        """
        if ctx.session is None:
            return
        session_id = ctx.session.get_session_id()
        parent_request_id = self._extract_request_id(ctx)
        # Duck-type tool_name: production uses ToolCallInputs; tests may pass
        # SimpleNamespace. Missing attr → treat as non-create (emit start).
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        defer_start_on_create = tool_name == "todo_create"

        try:
            todo_items = self._load_todo_from_json(session_id)
        except Exception as exc:
            logger.warning(
                "[TaskExecutionRail] Failed to load todo.json: %s",
                exc,
            )
            return

        current_map = self._build_map_from_todo_items(todo_items)
        previous_map = self._todo_map_before_tool or self._todo_map

        completed_in_batch: list[str] = []
        for task_id, current in current_map.items():
            prev = previous_map.get(task_id)
            prev_status = prev.get("status", "") if prev else ""
            curr_status = current.get("status", "")

            if (
                curr_status == "in_progress"
                and prev_status not in ("in_progress", "completed")
            ):
                if defer_start_on_create:
                    logger.info(
                        "[TaskExecutionRail] todo_create: defer task.start "
                        "for in_progress task_id=%s session_id=%s",
                        task_id,
                        session_id,
                    )
                elif task_id not in self._todo_started:
                    await self._emit_task_start_event(
                        ctx.session,
                        task_id,
                        current,
                        parent_request_id,
                        source="todo",
                    )
                    self._todo_started.add(task_id)
            elif (
                curr_status == "completed"
                and prev_status != "completed"
            ):
                completed_in_batch.append(task_id)
                # Include deferred todo_create in_progress (never emitted start):
                # frontend still needs start+complete for a visible completed row.
                if task_id not in self._todo_started:
                    logger.info(
                        "[TaskExecutionRail] completed without prior start: "
                        "emit task.start+task.complete: %s "
                        "prev_status=%r session_id=%s",
                        task_id,
                        prev_status,
                        session_id,
                    )
                    await self._emit_task_start_event(
                        ctx.session,
                        task_id,
                        current,
                        parent_request_id,
                        source="todo",
                    )
                    self._todo_started.add(task_id)
                await self._emit_task_complete_event(
                    ctx.session,
                    task_id,
                    current,
                    status="succeeded",
                    parent_request_id=parent_request_id,
                )

        self._todo_map = current_map
        self._todo_map_before_tool = {}
        # 记录本轮 invoke 实际存在的 todo ids，供过滤 stale 时排除本轮新建的同 ID 项
        try:
            set_current_invoke_todo_ids(ctx.session, set(current_map.keys()))
        except Exception:
            logger.debug(
                "[TaskExecutionRail] set_current_invoke_todo_ids failed",
                exc_info=True,
            )
        self._bind_context_after_todo_sync(
            completed_in_batch, current_map
        )
        await self._emit_task_update_event(
            ctx.session, parent_request_id
        )

    async def _emit_task_start_event(
        self,
        session: Session,
        task_id: str,
        task: dict[str, Any],
        parent_request_id: str,
        source: Literal["todo"],
    ) -> None:
        full_task_id = f"{source}:{task_id}"

        if full_task_id in self._active_tasks:
            _ACTIVE_TASK_ID.set(full_task_id)
            return

        context = TaskExecutionContext(
            task_id=full_task_id,
            task_content=str(task.get("content", "")),
            task_index=int(task.get("index", 0)),
            total_tasks=int(task.get("total", 0)),
            parent_request_id=parent_request_id,
            start_time=time.time(),
            source=source,
        )
        self._active_tasks[full_task_id] = context
        _ACTIVE_TASK_ID.set(full_task_id)

        logger.info(
            "[TaskExecutionRail] task.start: %s - %s",
            full_task_id,
            context.task_content,
        )

        try:
            await session.write_stream(
                OutputSchema(
                    type="task.start",
                    index=0,
                    payload={
                        "task_id": context.task_id,
                        "task_content": context.task_content,
                        "task_index": context.task_index,
                        "total_tasks": context.total_tasks,
                        "parent_request_id": context.parent_request_id,
                        "timestamp": context.start_time,
                        "source": source,
                    },
                )
            )
        except Exception:
            logger.debug(
                "[TaskExecutionRail] task.start emit failed",
                exc_info=True,
            )

    async def _emit_task_complete_event(
        self,
        session: Session,
        task_id: str,
        task: dict[str, Any],
        *,
        status: Literal["succeeded", "failed", "skipped"],
        error: str | None = None,
        parent_request_id: str = "",
    ) -> None:
        full_task_id = f"todo:{task_id}"
        context = self._active_tasks.pop(full_task_id, None)
        timestamp = time.time()

        if context:
            duration_ms = int(
                (timestamp - context.start_time) * 1000
            )
            payload_task_id = context.task_id
            task_content = context.task_content
            source = context.source
        else:
            duration_ms = 0
            payload_task_id = full_task_id
            task_content = str(task.get("content", ""))
            source = "todo"

        if get_current_task_id() == full_task_id:
            _ACTIVE_TASK_ID.set(None)

        logger.info(
            "[TaskExecutionRail] task.complete: %s - %s (%dms)",
            full_task_id,
            status,
            duration_ms,
        )

        try:
            await session.write_stream(
                OutputSchema(
                    type="task.complete",
                    index=0,
                    payload={
                        "task_id": payload_task_id,
                        "task_content": task_content,
                        "status": status,
                        "duration_ms": duration_ms,
                        "error": error,
                        "timestamp": timestamp,
                        "source": source,
                        "parent_request_id": parent_request_id,
                    },
                )
            )
        except Exception:
            logger.debug(
                "[TaskExecutionRail] task.complete emit failed",
                exc_info=True,
            )

    async def _emit_task_update_event(
        self,
        session: Session,
        parent_request_id: str | None = None,
    ) -> None:
        """Send full task list snapshot (all todos) to the frontend."""
        session_id = session.get_session_id()
        todo_items = self._load_todo_from_json(session_id)
        # 兜底过滤：即便 skip 窗口未拦下，也剔除已被 prepare_* 清理的
        # 旧 todo id，防止跨请求残留项回灌前端快照。键推导须与
        # _build_map_from_todo_items 一致：有 id 用 id，无 id 用 str(index)。
        if self._stale_todo_ids:
            # 仅过滤 stale 集中仍处于终态（cancelled/completed）的旧残留项。
            # 本轮 LLM 通过 todo_create/todo_modify 重建的同 ID 项状态为
            # pending/in_progress，不会被过滤。
            # 额外排除本轮新建的同 ID 项：若 id 不在磁盘快照（pre_invoke_todo_ids）
            # 中，说明是本轮 LLM 新建的，即使 id 与 stale 集合重合也不应过滤。
            pre_invoke_ids = get_pre_invoke_todo_ids(session)
            filtered: list[dict[str, Any]] = []
            _DONE_STATUSES = frozenset({"cancelled", "completed"})  # pylint: disable=huawei-invalid-name
            for idx, item in enumerate(todo_items):
                key = str(item.get("id", str(idx)))
                status = str(item.get("status", "")).lower()
                if key in self._stale_todo_ids and status in _DONE_STATUSES:
                    # 仅当 id 存在于磁盘快照时才认定为真旧残留
                    if pre_invoke_ids and key not in pre_invoke_ids:
                        filtered.append(item)
                        continue
                    continue
                filtered.append(item)
            todo_items = filtered
        todo_tasks = self._format_tasks_for_update(
            todo_items, source="todo"
        )

        all_tasks = todo_tasks
        total = len(all_tasks)
        completed = sum(
            1 for t in all_tasks
            if t.get("status") == "completed"
        )
        in_progress = sum(
            1 for t in all_tasks
            if t.get("status") == "in_progress"
        )
        pending = sum(
            1 for t in all_tasks
            if t.get("status") == "pending"
        )

        payload: dict[str, Any] = {
            "tasks": all_tasks,
            "total_tasks": total,
            "completed_tasks": completed,
            "in_progress_tasks": in_progress,
            "pending_tasks": pending,
            "timestamp": time.time(),
        }

        if parent_request_id:
            payload["parent_request_id"] = parent_request_id

        try:
            await session.write_stream(
                OutputSchema(
                    type="task.update",
                    index=0,
                    payload=payload,
                )
            )
        except Exception:
            logger.debug(
                "[TaskExecutionRail] task.update emit failed",
                exc_info=True,
            )

        logger.info(
            "[TaskExecutionRail] task.update: %d tasks - "
            "%d completed, %d in_progress, %d pending",
            total,
            completed,
            in_progress,
            pending,
        )

    def _format_tasks_for_update(
        self,
        items: list[dict[str, Any]],
        source: Literal["todo"],
    ) -> list[dict[str, Any]]:
        """Format todo items into task dicts for task.update payload."""
        formatted: list[dict[str, Any]] = []
        for item in items:
            task_id = str(
                item.get("id", item.get("idx", ""))
            )
            task: dict[str, Any] = {
                "task_id": task_id,
                "task_content": item.get(
                    "content", item.get("activeForm", "")
                ),
                "task_index": item.get(
                    "index", item.get("idx", 0)
                ),
                "source": source,
                "status": item.get("status", "pending"),
            }
            full_task_id = f"{source}:{task_id}"
            context = self._active_tasks.get(full_task_id)
            if context:
                task["start_time"] = context.start_time
            formatted.append(task)
        return formatted

    # ------------------------------------------------------------------
    # Task binding (ContextVar management)
    # ------------------------------------------------------------------

    def _task_candidates_by_status(
        self,
        allowed: frozenset[str],
    ) -> list[tuple[int, str]]:
        candidates: list[tuple[int, str]] = []
        for task_id, task in self._todo_map.items():
            if str(task.get("status", "")).lower() in allowed:
                candidates.append(
                    (int(task.get("index", 0)), task_id)
                )
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates

    def _pick_task_id_for_binding(self) -> str | None:
        """Pick the first in_progress task, else first pending."""
        active = self._task_candidates_by_status(
            self._BINDING_IN_PROGRESS
        )
        if active:
            return active[0][1]
        pending = self._task_candidates_by_status(
            self._BINDING_PENDING
        )
        if pending:
            return pending[0][1]
        return None

    def _pick_next_pending_after(
        self, completed_task_id: str
    ) -> str | None:
        completed = self._todo_map.get(completed_task_id)
        if not completed:
            return self._pick_task_id_for_binding()
        completed_index = int(completed.get("index", 0))
        pending: list[tuple[int, str]] = []
        for task_id, task in self._todo_map.items():
            if (
                str(task.get("status", "")).lower()
                in self._BINDING_PENDING
            ):
                task_index = int(task.get("index", 0))
                if task_index > completed_index:
                    pending.append((task_index, task_id))
        if not pending:
            return None
        pending.sort(key=lambda item: (item[0], item[1]))
        return pending[0][1]

    def _set_active_task_binding(
        self, raw_task_id: str | None
    ) -> None:
        if raw_task_id:
            full_task_id = f"todo:{raw_task_id}"
            _ACTIVE_TASK_ID.set(full_task_id)
            logger.debug(
                "[TaskExecutionRail] task_id binding: %s",
                full_task_id,
            )
            return
        _ACTIVE_TASK_ID.set(None)

    def _bind_context_after_todo_sync(
        self,
        completed_in_batch: list[str],
        current_map: dict[str, dict[str, Any]],
    ) -> None:
        """Re-bind task_id after todo.json changed.

        in_progress wins over 'next pending after completed' so S3
        in_progress + S4 pending does not bind to S4 when S1/S2 complete
        in the same batch.
        """
        in_progress = self._task_candidates_by_status(
            self._BINDING_IN_PROGRESS
        )
        if in_progress:
            self._set_active_task_binding(in_progress[0][1])
            return
        if completed_in_batch:
            anchor_id = max(
                completed_in_batch,
                key=lambda tid: int(
                    current_map.get(tid, {}).get("index", 0)
                ),
            )
            next_id = self._pick_next_pending_after(anchor_id)
            if next_id:
                self._set_active_task_binding(next_id)
                return
        self._bind_context_to_in_progress_task()

    def _bind_context_to_in_progress_task(self) -> None:
        """Bind stream/artifact task_id to in_progress, else first pending."""
        self._set_active_task_binding(
            self._pick_task_id_for_binding()
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_request_id(ctx: AgentCallbackContext) -> str:
        value = getattr(ctx.inputs, "request_id", None)
        if value:
            return str(value).strip()
        if isinstance(ctx.inputs, dict):
            raw = ctx.inputs.get("request_id")
            if raw:
                return str(raw).strip()
        # ToolCallInputs usually has no request_id; fall back to the active
        # perf request context so task.* UI payloads still carry
        # parent_request_id when the rail is enabled.
        try:
            from jiuwenswarm.perf.context import (
                extract_session_id_from_callback,
                get_request_context,
            )

            session_id = None
            if ctx.session is not None:
                try:
                    session_id = str(ctx.session.get_session_id() or "").strip() or None
                except Exception:
                    session_id = extract_session_id_from_callback(ctx)
            else:
                session_id = extract_session_id_from_callback(ctx)
            req_ctx = get_request_context(session_id=session_id)
            if req_ctx:
                return str(req_ctx.get("request_id") or "").strip()
        except Exception:
            logger.debug(
                "[TaskExecutionRail] request_id fallback failed",
                exc_info=True,
            )
        return ""
