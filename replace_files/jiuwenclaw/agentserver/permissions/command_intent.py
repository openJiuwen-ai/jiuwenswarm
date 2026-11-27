# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase-1 command intent extraction (L1 shlex + L3-Cmd LLM).

本模块只负责"把命令串拆成原子的 ``CommandIntent`` 列表"，**不做**任何权限判定，
也**不读取**任何外部脚本文件。判定与持久化由 ``file_guard`` / ``permission_rail`` 负责。

L1（shlex）：
  - 从 path-aware 命令（``cp / mv / rm / cat`` 等）抽 read/write 路径
  - 从 ``> / >> / <`` 等重定向抽 read/write 路径
  - 仅当形如 ``python x.py`` / ``bash x.sh`` 时，将 **脚本文件** 标记为 ``action=exec``
    （**不会**为 ``cp / mv / rm`` 等系统命令本身生成 ``action=exec``，那属于子线 A）

L3-Cmd（LLM）：
  - 触发条件：``shell_ast.has_risky_structure()`` 命中（长管道、``find ... -exec``、
    ``bash -c '...'`` 等），或调用方是代码类工具（``run_python`` 等代码字段命令）
  - 输入：**仅命令串本身**（不读任何外部文件，不发任何外部请求）
  - 输出：固定 JSON Schema 的 ``CommandIntent[]``；schema 校验失败 → 退回到 L1 + ASK

`CommandIntent.action` 始终是单值（``read|write|exec``），复合命令通过多条 ``CommandIntent`` 表达。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from jiuwenclaw.agentserver.permissions.files.extract import (
    extract_shell_path_accesses,
)
from jiuwenclaw.agentserver.permissions.shell_ast import (
    parse_shell_for_permission,
)

logger = logging.getLogger(__name__)


CommandIntentAction = Literal["read", "write", "exec"]
CommandIntentSource = Literal["shlex", "llm", "script_scan"]

# 触发 L3-Cmd 的代码类工具（参数是"代码"而非系统命令）
_CODE_TOOLS: frozenset[str] = frozenset({"run_python", "python_eval", "execute_code"})

# Shell 类工具
_SHELL_TOOLS: frozenset[str] = frozenset({"bash", "mcp_exec_command", "create_terminal"})

# L3-Cmd 默认超时（秒）
# 实测真实 LLM RT 通常 5–10s，3s 几乎必超时；这里给到 15s 作为安全默认值，
# 仍小于 ConfirmInterruptRail 用户审批本身的等待，不会拖慢主流程。
# 个别慢模型 / 高负载环境可通过 ``permissions.command_intent.timeout_seconds`` 覆盖。
_DEFAULT_LLM_TIMEOUT_SECONDS: float = 15.0

# 命令字符串送入 LLM 前的最大长度，避免长 prompt
_MAX_LLM_INPUT_CHARS: int = 4000


@dataclass(frozen=True)
class CommandIntent:
    """单条原子操作意图。

    - ``summary``: 给用户的人话摘要（用于审批卡）
    - ``action``: 单值（``read`` / ``write`` / ``exec``）；复合命令拆成多条
    - ``paths``:  规范化绝对路径列表；都属于同一 ``action`` 这一种动作
    - ``executable``: 触发该动作的可执行文件名（``python`` / ``bash`` / ``find`` …）
    - ``source``: 抽取来源（L1=``shlex``；L3-Cmd=``llm``；阶段二脚本扫描=``script_scan``）
    """
    summary: str
    action: CommandIntentAction
    paths: tuple[str, ...]
    executable: str | None = None
    source: CommandIntentSource = "shlex"


def is_command_intent_enabled(permission_config: dict[str, Any] | None) -> bool:
    """读取 ``permissions.command_intent.enabled``，缺省视为 True。"""
    if not isinstance(permission_config, dict):
        return True
    cfg = permission_config.get("command_intent")
    if not isinstance(cfg, dict):
        return True
    val = cfg.get("enabled", True)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off")
    return True


def is_l3_cmd_enabled(permission_config: dict[str, Any] | None) -> bool:
    """L3-Cmd 总开关，等价 ``command_intent.enabled``（阶段二会再加 ``script_scan_enabled``）。"""
    return is_command_intent_enabled(permission_config)


def resolve_l3_cmd_timeout(permission_config: dict[str, Any] | None) -> float:
    """读取 ``permissions.command_intent.timeout_seconds``，非法/缺省退回到 ``_DEFAULT_LLM_TIMEOUT_SECONDS``。

    允许负数会被强制为默认值；过大的值（>120s）也会被截断到 120s，避免某次抖动把整个
    审批管线拖死。
    """
    fallback = _DEFAULT_LLM_TIMEOUT_SECONDS
    if not isinstance(permission_config, dict):
        return fallback
    cfg = permission_config.get("command_intent")
    if not isinstance(cfg, dict):
        return fallback
    raw = cfg.get("timeout_seconds")
    if raw is None:
        return fallback
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[command_intent] invalid command_intent.timeout_seconds=%r; falling back to %.1fs",
            raw, fallback,
        )
        return fallback
    if val <= 0:
        return fallback
    if val > 120.0:
        logger.warning(
            "[command_intent] command_intent.timeout_seconds=%.1fs too large; clamped to 120s",
            val,
        )
        return 120.0
    return val


def resolve_l3_cmd_extra_body(
        permission_config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """读取 ``permissions.command_intent.extra_body``，透传给 ``llm.stream/invoke``。

    生产观测：豆包 / 火山方舟 thinking 端点在 stream 模式下会推大量
    ``reasoning_content`` chunk，``content`` 长时间为空（典型 reasoning_chars=2000+
    / content_chars=0 / 15s 仍未出 answer）。L3-Cmd 在审批主链路上阻塞用户，
    思考链对静态路径抽取没有价值——但用户已明确反馈不希望换专用小模型，
    所以提供这个**参数级**逃生通道：同 model / 同 endpoint，仅通过 ``extra_body``
    把厂商专用参数（豆包 ``thinking``、Qwen ``enable_thinking``、OpenAI
    ``reasoning_effort`` 等）透传到 OpenAI 兼容请求体。

    示例（在 ``config.yaml`` 里）::

        permissions:
          command_intent:
            extra_body:
              thinking: {type: disabled}   # 豆包深度思考端点
              # enable_thinking: false      # Qwen3 思考开关
              # reasoning_effort: "low"     # OpenAI o-series

    透传链路：``Model.stream(**kwargs)`` → ``OpenAIModelClient.stream(**kwargs)``
    → ``_build_request_params`` 末尾 ``params.update(filtered_kwargs)``
    → ``async_client.chat.completions.create(extra_body=..., **params)``
    （OpenAI Python SDK 会自动把 ``extra_body`` merge 到最终 HTTP body）。

    返回 ``None`` 表示不注入；非 dict / 空 dict 一律视为未配，避免在底层 client
    上产生空字段。
    """
    if not isinstance(permission_config, dict):
        return None
    cfg = permission_config.get("command_intent")
    if not isinstance(cfg, dict):
        return None
    raw = cfg.get("extra_body")
    if not isinstance(raw, dict) or not raw:
        return None
    return dict(raw)


def resolve_l3_cmd_model_name(
        permission_config: dict[str, Any] | None,
        fallback_model_name: str | None,
) -> str | None:
    """读取 ``permissions.command_intent.model_name``；为空 / 未配则回退到主 agent 模型。

    这是为 reasoning / thinking 类主模型（如 doubao 深度思考端点）准备的逃生通道：
    那类端点即便 ``is_stream=true`` 也会在思考阶段 buffer 全部 token 不下发，TTFT
    经常 >15s，对 command_intent 这种"小输入小输出 + 主链路阻塞用户"的场景致命。
    生产建议**显式**配一个 doubao-lite / 1.5-pro-32k 之类不带深度思考的小端点专门
    给 command_intent 用，主聊天端点保持原样。

    返回的模型名会作为 ``llm.stream(model=...)`` / ``llm.invoke(model=...)`` 的
    ``model`` 参数传给底层 OpenAI 兼容 client；前提是同一 LLM client 账户能路由到
    多个 endpoint id（豆包 / OpenAI 通常是这样）。如果你的部署里不同模型必须用不
    同 client，再迭代到「专用 LLM 实例」的方向。
    """
    if not isinstance(permission_config, dict):
        return fallback_model_name
    cfg = permission_config.get("command_intent")
    if not isinstance(cfg, dict):
        return fallback_model_name
    raw = cfg.get("model_name")
    if not isinstance(raw, str):
        return fallback_model_name
    name = raw.strip()
    if not name:
        return fallback_model_name
    return name


# ---------- L1: shlex 抽取 ----------


def _summary_for_action(action: str, path: str, executable: str | None) -> str:
    verb = {"read": "读取", "write": "写入", "exec": "执行"}.get(action, action)
    if executable and action == "exec":
        return f"通过 {executable} 执行 {path}"
    return f"{verb} {path}"


def _command_text_from_args(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name in _CODE_TOOLS:
        for key in ("code", "script", "source"):
            v = tool_args.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return ""
    for key in ("command", "cmd"):
        v = tool_args.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _resolve_workdir(tool_args: dict[str, Any], workspace: Path) -> Path:
    workdir = tool_args.get("workdir") or ""
    if not isinstance(workdir, str) or not workdir.strip():
        return workspace
    try:
        wd = Path(workdir.strip())
        if not wd.is_absolute():
            wd = (workspace / wd)
        return wd.resolve()
    except (OSError, RuntimeError):
        return workspace


def _executable_basename(command_text: str) -> str | None:
    if not command_text:
        return None
    head = command_text.strip().split()[0] if command_text.strip() else ""
    if not head:
        return None
    base = Path(head.replace("\\", "/")).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base or None


def extract_l1_intents(
        tool_name: str,
        tool_args: dict[str, Any],
        workspace: Path,
) -> list[CommandIntent]:
    """L1：基于 shlex 与启发式拆解命令串，输出原子 ``CommandIntent`` 列表。

    仅适用于 shell 类与代码类工具；其他工具（``read_file`` 等）由 ``file_guard``
    走"工具参数 → 注册表"路径，本函数不重复抽取。
    """
    if tool_name not in _SHELL_TOOLS:
        # 代码类工具的参数不是 shell 命令，L1 不可能解析；交给 L3-Cmd
        return []

    command = _command_text_from_args(tool_name, tool_args)
    if not command:
        return []
    workdir = _resolve_workdir(tool_args, workspace)
    executable = _executable_basename(command)

    intents: list[CommandIntent] = []
    seen: set[tuple[str, str]] = set()
    for path, action in extract_shell_path_accesses(command, workdir):
        try:
            ps = path.resolve().as_posix()
        except (OSError, RuntimeError):
            ps = str(path).replace("\\", "/")
        key = (ps, action)
        if key in seen:
            continue
        seen.add(key)
        intents.append(CommandIntent(
            summary=_summary_for_action(action, ps, executable if action == "exec" else None),
            action=action,  # type: ignore[arg-type]
            paths=(ps,),
            executable=executable,
            source="shlex",
        ))
    return intents


# ---------- L3-Cmd: LLM 解析复杂命令串 ----------


_LLM_INTENT_PROMPT = (
    "你是一个安全工具调用解析器。下面给出**单条命令字符串**，请只用静态分析提取它会"
    "对哪些**文件/目录**做哪些**原子动作**。\n\n"
    "工具名: {tool_name}\n"
    "命令字符串:\n```\n{command}\n```\n\n"
    "**输出格式硬约束（违反将导致解析失败）：**\n"
    "- 直接输出 JSON 对象，**不要**加 ``\u0060\u0060\u0060json`` 围栏，**不要**任何前后说明 / 思考过程。\n"
    "- 第一个字符必须是 ``{{``，最后一个字符必须是 ``}}``。\n\n"
    "JSON schema：\n"
    "{{\n"
    '  "intents": [\n'
    "    {{\n"
    '      "summary": "中文一句话描述此原子动作（例：读取 /etc/passwd）",\n'
    '      "action":  "read|write|exec",   // 严格单值，只能是这三个之一；复合命令请拆成多条\n'
    '      "paths":   ["..."],              // 规范化路径；可多条但都属于此 action\n'
    '      "executable": "..."              // 触发该动作的可执行文件名（如 python / bash / find / cp）\n'
    "    }}\n"
    "  ]\n"
    "}}\n\n"
    "语义约束：\n"
    "1. **action 只能是 ``read`` / ``write`` / ``exec`` 三个字符串之一**；不要用 ``delete`` / "
    "   ``remove`` / ``copy`` / ``move`` 这种动词，统一映射：\n"
    "   - ``rm`` / ``del`` / ``unlink`` / ``mv`` 目标 / ``cp`` 目标 / ``>`` / ``>>`` 重定向 → ``write``\n"
    "   - ``cat`` / ``ls`` / ``type`` / ``mv`` 源 / ``cp`` 源 / ``<`` 重定向 → ``read``\n"
    "   - ``python x.py`` / ``bash x.sh`` 中的脚本 → ``exec``；``cp / mv / rm / find -exec`` "
    "     等系统命令本身**不要**标 ``exec``。\n"
    "2. 不要猜测：若无法静态确定路径，省略该条目，不要编造。\n"
    "3. 路径若为相对路径，按命令的 cwd 已展开后给出。\n"
    "4. 不要输出对 stdin/stdout/管道虚拟流的访问。\n"
    "5. 控制流（``if exist`` / ``for ... in ...`` / ``test -f``）中**被测试的路径**也算 ``read``，"
    "   分支体里的 ``del`` / ``rm`` / ``cp`` 仍按上面的规则各自产出 ``read`` / ``write`` 条目。\n"
)


def _build_l3_prompt(tool_name: str, command: str) -> str:
    truncated = command[:_MAX_LLM_INPUT_CHARS]
    return _LLM_INTENT_PROMPT.format(tool_name=tool_name, command=truncated)


def _coerce_str_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        out: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    return []


# 模型经常无视 prompt，输出动词同义词；这里做一次本地兜底映射，避免整条 intent 被丢弃。
# 仅覆盖**含义明确**的同义词；含义模糊的（``copy`` / ``move``：源是 read、目标是 write）
# 不在这里展开 —— prompt 已要求模型自己拆，模糊词维持丢弃以防误判。
_ACTION_SYNONYMS: dict[str, CommandIntentAction] = {
    "read": "read", "view": "read", "show": "read", "list": "read",
    "cat": "read", "head": "read", "tail": "read", "type": "read",
    "stat": "read", "test": "read", "exists": "read", "exist": "read",
    "write": "write", "create": "write", "make": "write", "mkdir": "write",
    "modify": "write", "edit": "write", "update": "write", "append": "write",
    "delete": "write", "remove": "write", "unlink": "write", "rm": "write",
    "del": "write", "rmdir": "write", "truncate": "write",
    "exec": "exec", "execute": "exec", "run": "exec", "invoke": "exec",
}


def _coerce_action(raw: Any) -> CommandIntentAction | None:
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v in ("read", "write", "exec"):
        return v  # type: ignore[return-value]
    return _ACTION_SYNONYMS.get(v)


def _expand_path_for_llm(raw: str, workdir: Path) -> str | None:
    s = raw.strip().strip('"').strip("'")
    if not s:
        return None
    try:
        expanded = os.path.expandvars(os.path.expanduser(s))
        p = Path(expanded)
        if not p.is_absolute():
            p = (workdir / p)
        return p.resolve().as_posix()
    except (OSError, RuntimeError):
        return None


def _strip_markdown_fence(text: str) -> str:
    """剥掉 markdown 代码围栏。

    覆盖 ``\u0060\u0060\u0060json ... \u0060\u0060\u0060`` / ``\u0060\u0060\u0060 ... \u0060\u0060\u0060``
    这两类常见形态，LLM 在被要求"只输出 JSON"时仍很爱用它们包裹结果。
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    # ```json\n{...}\n``` 或 ```\n{...}\n```
    s = s[3:]
    if s.lower().startswith("json"):
        s = s[4:]
    s = s.lstrip("\r\n")
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def _candidate_json_objects(raw_text: str) -> list[str]:
    """从模型输出里挑出所有可能的顶层 JSON 对象片段。

    模型常见三种污染：
    1. ``\u0060\u0060\u0060json\n{...}\n\u0060\u0060\u0060``：剥围栏；
    2. ``推理过程... {真正的 JSON} 收尾`` ：用括号深度扫描提取最外层平衡 ``{...}``；
    3. 干净 ``{...}`` 输出：原样返回。

    返回多个候选，按可能性排序，调用方依次尝试 ``json.loads``。
    """
    if not raw_text:
        return []
    candidates: list[str] = []
    cleaned = _strip_markdown_fence(raw_text)
    if cleaned and cleaned[:1] == "{":
        candidates.append(cleaned)
    # 括号深度扫描：找出所有平衡的 ``{...}`` 子串（按起点位置）
    text = cleaned or raw_text
    depth = 0
    start_idx = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    snippet = text[start_idx:i + 1]
                    if snippet not in candidates:
                        candidates.append(snippet)
                    start_idx = -1
    return candidates


def _parse_llm_json(raw_text: str) -> tuple[list[dict[str, Any]], str]:
    """返回 ``(intents_list, fail_reason)``；空 list 时 ``fail_reason`` 说明原因。"""
    if not raw_text:
        return [], "empty_response"
    candidates = _candidate_json_objects(raw_text)
    if not candidates:
        return [], "no_json_object_found"
    last_err: str = ""
    for snippet in candidates:
        try:
            obj = json.loads(snippet)
        except (json.JSONDecodeError, TypeError) as exc:
            last_err = f"json_decode_error:{exc.__class__.__name__}"
            continue
        if not isinstance(obj, dict):
            last_err = "top_level_not_object"
            continue
        intents = obj.get("intents")
        if not isinstance(intents, list):
            last_err = "missing_intents_field"
            continue
        items = [x for x in intents if isinstance(x, dict)]
        if not items:
            return [], "intents_empty_or_non_dict"
        return items, ""
    return [], last_err or "all_candidates_failed"


def _normalize_llm_intents(
        raw_items: list[dict[str, Any]],
        workdir: Path,
        executable_hint: str | None,
) -> tuple[list[CommandIntent], dict[str, int]]:
    """归一化 LLM 输出。返回 ``(intents, drop_stats)``，``drop_stats`` 用于诊断日志。"""
    out: list[CommandIntent] = []
    seen: set[tuple[str, str]] = set()
    drop = {
        "action_invalid": 0,
        "no_paths": 0,
        "all_paths_unresolvable": 0,
    }
    for item in raw_items:
        action = _coerce_action(item.get("action"))
        if action is None:
            drop["action_invalid"] += 1
            continue
        raw_paths = _coerce_str_list(item.get("paths"))
        if not raw_paths:
            drop["no_paths"] += 1
            continue
        normalized: list[str] = []
        for rp in raw_paths:
            p = _expand_path_for_llm(rp, workdir)
            if not p:
                continue
            key = (p, action)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(p)
        if not normalized:
            drop["all_paths_unresolvable"] += 1
            continue
        executable = item.get("executable")
        if not isinstance(executable, str) or not executable.strip():
            executable = executable_hint
        else:
            executable = executable.strip()
        summary = item.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = _summary_for_action(
                action,
                normalized[0],
                executable if action == "exec" else None,
            )
        out.append(CommandIntent(
            summary=summary.strip(),
            action=action,
            paths=tuple(normalized),
            executable=executable,
            source="llm",
        ))
    return out, drop


# 已知"代码运行器"白名单：第一个 token 命中且后续 args 带对应 inline-code flag 时，
# 我们认定参数字符串里嵌套了另一个 shell / 解释器的完整代码，必须放行到 L3-Cmd。
#
# 由生产事故驱动：``powershell -Command "if (Test-Path ...) { Remove-Item ... }"``
# 在 bash AST 视角下就是个简单单 head 命令——没管道、没 ``&&``、没 cmd-if/else，
# ``parse.kind="simple"`` + ``flags.has_risky_structure()=False``，闸门会把它判成
# "无害命令"直接 skip。但参数里实际是 PowerShell 一行流，文件操作（Remove-Item）
# 完全藏在引号里。这类「外层 shell 单 head、内层嵌套另一种 shell 代码」模式在
# Windows / 跨平台 agent 输出里非常常见，闸门必须能识别。
#
# key = 解释器可执行名（小写、剥掉路径和 .exe），value = 触发"参数是代码"的 flag 集合。
_INLINE_CODE_INTERPRETERS: dict[str, set[str]] = {
    # PowerShell（-Command / -EncodedCommand / 缩写 -C / -Enc）
    "powershell": {"-command", "-encodedcommand", "-c", "-enc"},
    "powershell.exe": {"-command", "-encodedcommand", "-c", "-enc"},
    "pwsh": {"-command", "-encodedcommand", "-c", "-enc"},
    "pwsh.exe": {"-command", "-encodedcommand", "-c", "-enc"},
    # Windows cmd
    "cmd": {"/c", "/k"},
    "cmd.exe": {"/c", "/k"},
    # POSIX shells
    "bash": {"-c"},
    "sh": {"-c"},
    "zsh": {"-c"},
    "fish": {"-c"},
    "dash": {"-c"},
    "ksh": {"-c"},
    # 通用解释器：参数是 inline 代码串
    "python": {"-c"},
    "python3": {"-c"},
    "python2": {"-c"},
    "python.exe": {"-c"},
    "python3.exe": {"-c"},
    "py": {"-c"},
    "py.exe": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"},
    "node.exe": {"-e", "--eval", "-p", "--print"},
    "ruby": {"-e"},
    "perl": {"-e"},
    "osascript": {"-e"},
    # WSL：相当于嵌套 linux 命令
    "wsl": {"-e", "--exec"},
    "wsl.exe": {"-e", "--exec"},
}


def _has_inline_interpreter_code(command: str) -> bool:
    """识别"外层 shell 单 head 包了内层解释器 inline 代码字符串"的模式。

    形态例：``powershell -Command "..."`` / ``python -c "..."`` / ``bash -c "..."``
    / ``cmd /c "if ... else ..."``。这类命令的文件操作藏在引号里，bash AST 看不见。

    判定规则（保守偏向 fail-open，因为闸门"打开"只是多调一次 LLM 拿 file_ops，
    不会改最终 ASK/ALLOW 决策）：

    1. 用 ``shlex(posix=False)`` 拆 token——非 posix 模式不拆 Windows 反斜杠路径，
       且能正确把 ``"if (Test-Path ...) { ... }"`` 当一整段保留。
    2. head 剥掉目录前缀和 ``.exe`` 后小写匹配 ``_INLINE_CODE_INTERPRETERS``。
    3. 后续任一 token（lower 化）命中对应 inline-code flag → 命中。

    注意：``python script.py``、``pwsh -File foo.ps1`` 这种**脚本路径**模式
    **不**会触发，因为没有 inline-code flag——L1 已经能把它抽成 ``exec`` 意图，
    没必要再给 LLM 看一遍。
    """
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    head = tokens[0].strip().strip('"').strip("'").lower()
    # 路径前缀剥离：``C:\Windows\System32\powershell.exe`` → ``powershell.exe``
    head = head.replace("\\", "/").rsplit("/", 1)[-1]
    flags = _INLINE_CODE_INTERPRETERS.get(head)
    if not flags:
        return False
    for tok in tokens[1:]:
        if tok.lower() in flags:
            return True
    return False


def _should_invoke_l3_cmd(tool_name: str, command: str) -> bool:
    """L3-Cmd 闸门。命中任一条件即调 LLM 拆原子动作，否则交给 L1 / 直接走基线。

    对 ``_SHELL_TOOLS`` 的判定层级：

    - ``_has_inline_interpreter_code``：``powershell -Command / python -c / bash -c``
      这类**嵌套解释器**模式 —— bash AST 看到的是简单单 head，参数引号里却藏着另
      一种 shell 的完整代码（含 ``Remove-Item`` / ``rm`` / ``Test-Path`` 等文件操
      作）。这是从生产日志里 fail-open 抓到的边界，必须先于 ``parse_shell_for_permission``
      短路放行。
    - ``too_complex``：tree-sitter 解析成功但识别到不可信结构（subshell / 命令组 /
      命令替换 / 参数展开 / heredoc 等）→ L3。
    - ``parse_unavailable``：解析器**自报无法安全分析**——典型场景是 Windows cmd
      ``if/else``、``for``，或 fallback scanner 判定有风险结构。``parse_unavailable``
      的语义即"下游必须 fail-closed"，而 L3 正是这条 fail-closed 路径上唯一能把
      命令拆成原子 file_ops 的兜底，因此**必须**调 → L3。
    - 其余：即便 ``kind="simple"``，只要 ``flags.has_risky_structure()`` 命中
      （管道、复合操作符、I/O 重定向…）也走 L3，避免长 pipeline 漏掉文件操作。
    """
    if not command.strip():
        return False
    if tool_name in _CODE_TOOLS:
        return True
    if tool_name in _SHELL_TOOLS:
        if _has_inline_interpreter_code(command):
            return True
        try:
            parse = parse_shell_for_permission(command)
        except Exception:  # noqa: BLE001 — defensive
            return True
        if parse.kind in ("too_complex", "parse_unavailable"):
            return True
        if parse.flags.has_risky_structure():
            return True
    return False


async def invoke_llm(
        llm: Any,
        model_name: str | None,
        prompt: str,
        timeout: float,
        *,
        tool_name: str = "",
        command_preview: str = "",
        extra_body: dict[str, Any] | None = None,
) -> str | None:
    """优先用 ``llm.stream`` 累积 chunk；不可用时退回 ``llm.invoke``。

    设计动机（生产数据驱动）：在豆包/火山引擎 OpenAI 兼容 endpoint 上观测到——
    同一 model 的 ``invoke``（``is_stream=False``）调用整 ``timeout=15s`` 一字未回，
    而同期主聊天 ``stream`` 模式 5s 内已收完整。非流式模式服务端会等所有 token
    生成完才一次性返回，加上 reasoning 累计 RT 经常 >15s；且部分中间链路在长
    pending 期间会主动切连接，导致超时被放大。

    L3-Cmd 在审批主链路上阻塞用户，**任何长尾**都不可接受。改用 stream：
    - first-token 通常 1~3s，命中超时窗口的概率显著上升；
    - 拿到 first-token 后即开始累积，超时窗口结束时即便没收完也能落到 ``parse``
      这一步，让破损 JSON 的 ``parse.fail`` 比"一字未回"更易诊断；
    - 老模型 / mock 不支持 stream 时退回到 ``invoke``，行为与历史一致。

    ``timeout`` 是**端到端**预算（含连接 + 排队 + first-token + 累积 token）。
    """
    if llm is None:
        return None
    try:
        from openjiuwen.core.foundation.llm import UserMessage  # type: ignore
    except ImportError:
        logger.debug("[command_intent] L3-Cmd skipped: openjiuwen UserMessage unavailable")
        return None

    common_kwargs: dict[str, Any] = {"messages": [UserMessage(content=prompt)]}
    if model_name:
        common_kwargs["model"] = model_name
    if extra_body:
        # 透传到 OpenAI 兼容 client：openai-python 的 ``chat.completions.create``
        # 把 ``extra_body`` 自动 merge 到最终 HTTP body，厂商专用参数（豆包
        # ``thinking``、Qwen ``enable_thinking`` 等）就是这样落到模型侧的。
        common_kwargs["extra_body"] = extra_body

    if hasattr(llm, "stream"):
        return await _invoke_llm_streaming(
            llm, common_kwargs, timeout,
            tool_name=tool_name, command_preview=command_preview,
        )
    return await _invoke_llm_blocking(
        llm, common_kwargs, timeout,
        tool_name=tool_name, command_preview=command_preview,
    )


async def _invoke_llm_streaming(
        llm: Any,
        invoke_kwargs: dict[str, Any],
        timeout: float,
        *,
        tool_name: str,
        command_preview: str,
) -> str | None:
    """流式累积 chunk。整个 stream 周期共享一个端到端超时预算。

    诊断观测点（由 reasoning 端点踩过的坑提炼出来的）：

    - **chunks_total**：``async for`` 迭代次数。0 → 真的一字未回（连接 / 排队 /
      服务端没开始流式响应）。
    - **content_chars**：累计 ``chunk.content`` 字符数，喂给 JSON parser 的素材。
    - **reasoning_chars**：累计 ``chunk.reasoning_content`` 字符数。**专门为 doubao
      / qwen 等深度思考端点准备**：这类模型在思考阶段持续推 chunk 但每条 ``content``
      都是空串，``reasoning_content`` 才有内容。如果只看 content 就会误判成"流死了"。

    超时日志根据这三个数会给出**精准归因**：

    - chunks_total=0 → ``no chunks at all``（连接或服务端排队问题）
    - chunks_total>0 且 content_chars=0 且 reasoning_chars>0 → ``model still thinking``
      （reasoning 模型，思考阶段没结束就被超时切断；推荐通过专用小模型或关闭 thinking）
    - chunks_total>0 且 content_chars>0 但内容残缺 → ``partial content``（边界裁切，
      调大 timeout 可能解决）
    """

    # ⚠️ 关键：stats 必须放在 `_consume` 之外的闭包作用域里。
    # 因为 `asyncio.wait_for(coro, timeout=...)` 在超时时会 cancel `_consume`，
    # 协程栈被销毁、协程内的所有局部变量都没了。如果 chunks_total / reasoning_chars
    # 写在 `_consume` 内部 local，超时分支只能拿到一个 `elapsed`，根本没法回答
    # 「到底是 0 chunk 还是只在 thinking」——这正是上一次日志里看到的现象。
    # 把它做成 dict 让 `_consume` 边收边写，超时分支照样能读到当前累计值。
    content_pieces: list[str] = []
    stats: dict[str, Any] = {
        "chunks_total": 0,
        "first_chunk_at": None,
        "first_content_at": None,
        "reasoning_chars": 0,
        "content_chars": 0,
        "elapsed": 0.0,
    }

    async def _consume() -> str:
        loop = asyncio.get_event_loop()
        started = loop.time()
        async for chunk in llm.stream(**invoke_kwargs):
            stats["chunks_total"] += 1
            if stats["first_chunk_at"] is None:
                stats["first_chunk_at"] = loop.time() - started
            piece = getattr(chunk, "content", None)
            if piece is None:
                piece = getattr(chunk, "delta", None) or getattr(chunk, "text", None)
            if piece:
                if stats["first_content_at"] is None:
                    stats["first_content_at"] = loop.time() - started
                content_pieces.append(str(piece))
                stats["content_chars"] += len(str(piece))
            reasoning = getattr(chunk, "reasoning_content", None)
            if isinstance(reasoning, str) and reasoning:
                stats["reasoning_chars"] += len(reasoning)
            # 注意：在每个 chunk 末尾刷新 elapsed，让超时分支拿到的是「最后一次收到
            # chunk 的时刻」而不是 0。这对诊断很关键——比如 stream 中途卡住 10s 没新
            # chunk 也能看出来。
            stats["elapsed"] = loop.time() - started
        stats["elapsed"] = loop.time() - started
        return "".join(content_pieces)

    started_outer = asyncio.get_event_loop().time()
    timed_out = False
    content: str = ""
    try:
        content = await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        stats["elapsed"] = asyncio.get_event_loop().time() - started_outer
        # 超时时 content_pieces 已经被 _consume 累积了一部分（如果有 content chunk 的话），
        # 我们仍然把这部分拼起来——下游 parser 拿一个残缺 JSON 也比 None 更易诊断
        # （会落到 parse.fail，能看到 raw_preview，方便对照 prompt 改）。
        content = "".join(content_pieces)
    except Exception:  # noqa: BLE001 — never let LLM error escape into permission path
        logger.warning(
            "[command_intent] L3-Cmd llm.stream failed tool=%s preview=%r",
            tool_name or "?", command_preview[:80], exc_info=True,
        )
        return None

    # 不管超时与否，stream done / partial 一律输出统一格式的 INFO，含全部诊断字段
    logger.info(
        "[command_intent] L3-Cmd stream %s tool=%s chunks=%d first_chunk=%s "
        "first_content=%s content_chars=%d reasoning_chars=%d total=%.2fs",
        "timeout" if timed_out else "done",
        tool_name or "?",
        stats["chunks_total"],
        f"{stats['first_chunk_at']:.2f}s" if stats["first_chunk_at"] is not None else "n/a",
        f"{stats['first_content_at']:.2f}s" if stats["first_content_at"] is not None else "n/a",
        stats["content_chars"],
        stats["reasoning_chars"],
        stats["elapsed"],
    )

    # content 不为空：拿到了真正的答复内容，无论是否超时都让 parser 试一下
    if content and content.strip():
        if timed_out:
            logger.warning(
                "[command_intent] L3-Cmd llm.stream partial_content tool=%s chunks=%d "
                "content_chars=%d reasoning_chars=%d elapsed=%.2fs preview=%r "
                "(超时但已收到部分 content，下游 parser 会尝试解析；如频繁触发，调大 timeout_seconds)",
                tool_name or "?", stats["chunks_total"], stats["content_chars"],
                stats["reasoning_chars"], stats["elapsed"], command_preview[:80],
            )
        return content

    # content 为空：精准三档分流
    if stats["chunks_total"] == 0:
        logger.warning(
            "[command_intent] L3-Cmd llm.stream no_chunks tool=%s timed_out=%s elapsed=%.2fs preview=%r "
            "(整个 stream 周期未收到任何 chunk；连接 / 排队 / 服务端响应未开始)",
            tool_name or "?", timed_out, stats["elapsed"], command_preview[:80],
        )
    elif stats["reasoning_chars"] > 0:
        logger.warning(
            "[command_intent] L3-Cmd llm.stream thinking_only tool=%s timed_out=%s chunks=%d "
            "reasoning_chars=%d content_chars=0 elapsed=%.2fs preview=%r "
            "(reasoning 模型只产出了 thinking，最终答复未开始；典型表现：思考链很长，"
            "answer 还没生成；建议：启用 permissions.command_intent.model_name 走非 thinking 端点，"
            "或调大 timeout_seconds)",
            tool_name or "?", timed_out, stats["chunks_total"], stats["reasoning_chars"],
            stats["elapsed"], command_preview[:80],
        )
    else:
        logger.warning(
            "[command_intent] L3-Cmd llm.stream empty_content tool=%s timed_out=%s chunks=%d preview=%r",
            tool_name or "?", timed_out, stats["chunks_total"], command_preview[:80],
        )
    return None


async def _invoke_llm_blocking(
        llm: Any,
        invoke_kwargs: dict[str, Any],
        timeout: float,
        *,
        tool_name: str,
        command_preview: str,
) -> str | None:
    """退化路径：``llm.invoke`` 阻塞调用。仅在 client 不支持 ``stream`` 时使用。"""
    try:
        ai_msg = await asyncio.wait_for(llm.invoke(**invoke_kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "[command_intent] L3-Cmd llm.invoke timeout=%.1fs tool=%s preview=%r "
            "(可在 permissions.command_intent.timeout_seconds 调大)",
            timeout, tool_name or "?", command_preview[:80],
        )
        return None
    except Exception:  # noqa: BLE001 — never let LLM error escape into permission path
        logger.warning(
            "[command_intent] L3-Cmd llm.invoke failed tool=%s preview=%r",
            tool_name or "?", command_preview[:80], exc_info=True,
        )
        return None

    if ai_msg is None:
        logger.warning(
            "[command_intent] L3-Cmd llm.invoke returned None tool=%s preview=%r",
            tool_name or "?", command_preview[:80],
        )
        return None
    content = ai_msg.content if hasattr(ai_msg, "content") else str(ai_msg)
    if not content or not content.strip():
        logger.warning(
            "[command_intent] L3-Cmd llm.invoke empty content tool=%s ai_msg_type=%s preview=%r",
            tool_name or "?", type(ai_msg).__name__, command_preview[:80],
        )
        return None
    return content


async def run_l3_cmd_intents(
        tool_name: str,
        tool_args: dict[str, Any],
        workspace: Path,
        *,
        llm: Any = None,
        model_name: str | None = None,
        enabled: bool = True,
        timeout: float = _DEFAULT_LLM_TIMEOUT_SECONDS,
        extra_body: dict[str, Any] | None = None,
) -> list[CommandIntent]:
    """L3-Cmd：调用 LLM 解析复杂命令串。失败/超时/未注入 LLM 一律返回 ``[]``，**永不**抛出。

    全链路诊断日志（INFO 级），每一阶段都会落一条形如
    ``[command_intent] L3-Cmd <stage> tool=... ...`` 的事件，方便定位空 file_ops 是哪一步丢的：
      - ``skip``：未启用 / 无 LLM / 命令为空 / 闸门未开
      - ``invoke.start`` / ``invoke.done``
      - ``parse.fail`` 带 ``reason``（``no_json_object_found`` / ``json_decode_error`` …）
      - ``normalize.empty`` 带 ``drop_stats``（``action_invalid`` / ``no_paths`` /
        ``all_paths_unresolvable``）
      - ``intents.ok`` 带 ``count`` / ``actions``
    """
    if not enabled or llm is None:
        logger.info(
            "[command_intent] L3-Cmd skip tool=%s reason=%s",
            tool_name,
            "disabled" if not enabled else "no_llm_handle",
        )
        return []
    command = _command_text_from_args(tool_name, tool_args)
    if not command:
        logger.info("[command_intent] L3-Cmd skip tool=%s reason=empty_command", tool_name)
        return []
    if not _should_invoke_l3_cmd(tool_name, command):
        logger.info(
            "[command_intent] L3-Cmd skip tool=%s reason=gate_closed preview=%r",
            tool_name, command[:120],
        )
        return []

    workdir = _resolve_workdir(tool_args, workspace)
    executable = _executable_basename(command)
    prompt = _build_l3_prompt(tool_name, command)

    logger.info(
        "[command_intent] L3-Cmd invoke.start tool=%s model=%s timeout=%.1fs prompt_len=%d preview=%r",
        tool_name, model_name or "?", timeout, len(prompt), command[:120],
    )

    raw_text = await invoke_llm(
        llm, model_name, prompt, timeout,
        tool_name=tool_name, command_preview=command,
        extra_body=extra_body,
    )
    if not raw_text:
        # invoke_llm 内部已根据具体原因（timeout / exception / no content）打了 warning，
        # 这里再补一条 INFO 让"L3-Cmd 没拿到任何东西"这个事实在主链路上可见。
        logger.info(
            "[command_intent] L3-Cmd invoke.done tool=%s raw_len=0 -> empty_intents",
            tool_name,
        )
        return []

    logger.info(
        "[command_intent] L3-Cmd invoke.done tool=%s raw_len=%d preview=%r",
        tool_name, len(raw_text), raw_text[:200],
    )

    raw_items, fail_reason = _parse_llm_json(raw_text)
    if not raw_items:
        logger.warning(
            "[command_intent] L3-Cmd parse.fail tool=%s reason=%s raw_preview=%r",
            tool_name, fail_reason, raw_text[:200],
        )
        return []

    intents, drop_stats = _normalize_llm_intents(raw_items, workdir, executable)
    if not intents:
        logger.warning(
            "[command_intent] L3-Cmd normalize.empty tool=%s raw_items=%d drop_stats=%s "
            "raw_preview=%r",
            tool_name, len(raw_items), drop_stats, raw_text[:300],
        )
        return []

    logger.info(
        "[command_intent] L3-Cmd intents.ok tool=%s count=%d actions=%s drop_stats=%s",
        tool_name,
        len(intents),
        [it.action for it in intents],
        drop_stats,
    )
    return intents


# ---------- 合并去重 ----------


def merge_intents(*lists: Iterable[CommandIntent]) -> list[CommandIntent]:
    """按 (path, action) 去重，``shlex`` 优先于 ``llm``（L1 比 LLM 更可信）。"""
    by_key: dict[tuple[str, str], CommandIntent] = {}
    source_priority = {"shlex": 0, "llm": 1, "script_scan": 2}
    for batch in lists:
        for intent in batch:
            for path in intent.paths:
                key = (path, intent.action)
                cur = by_key.get(key)
                if cur is None:
                    by_key[key] = CommandIntent(
                        summary=intent.summary,
                        action=intent.action,
                        paths=(path,),
                        executable=intent.executable,
                        source=intent.source,
                    )
                    continue
                if source_priority.get(intent.source, 9) < source_priority.get(cur.source, 9):
                    by_key[key] = CommandIntent(
                        summary=intent.summary,
                        action=intent.action,
                        paths=(path,),
                        executable=intent.executable,
                        source=intent.source,
                    )
    return list(by_key.values())


async def collect_command_intents(
        tool_name: str,
        tool_args: dict[str, Any],
        workspace: Path,
        permission_config: dict[str, Any] | None,
        *,
        llm: Any = None,
        model_name: str | None = None,
) -> list[CommandIntent]:
    """L1 + L3-Cmd 一站式入口；不会抛出异常，无 LLM 时退化为纯 L1。

    ``permissions.command_intent.model_name`` 若配置了独立模型，这里会用它替换
    传入的 ``model_name``——这是给「主模型是 reasoning / thinking 端点、TTFT 高」
    的部署留的可选逃生通道。**不强制**：默认该字段为空，复用主模型，行为与历史
    一致。
    """
    l1 = extract_l1_intents(tool_name, tool_args, workspace)
    if not is_l3_cmd_enabled(permission_config):
        return l1
    effective_model = resolve_l3_cmd_model_name(permission_config, model_name)
    if effective_model != model_name:
        logger.info(
            "[command_intent] L3-Cmd model.override tool=%s configured=%s fallback_main=%s",
            tool_name, effective_model, model_name,
        )
    extra_body = resolve_l3_cmd_extra_body(permission_config)
    if extra_body:
        # 关键事件：用户配了厂商专用参数（豆包 thinking、Qwen enable_thinking 等），
        # 落 INFO 让人能在日志里直接确认"参数确实注进去了"——
        # 上一个版本就是因为没这条日志，关 thinking 这种事完全黑盒。
        logger.info(
            "[command_intent] L3-Cmd extra_body.injected tool=%s keys=%s",
            tool_name, sorted(extra_body.keys()),
        )
    l3 = await run_l3_cmd_intents(
        tool_name,
        tool_args,
        workspace,
        llm=llm,
        model_name=effective_model,
        enabled=True,
        timeout=resolve_l3_cmd_timeout(permission_config),
        extra_body=extra_body,
    )
    return merge_intents(l1, l3)
