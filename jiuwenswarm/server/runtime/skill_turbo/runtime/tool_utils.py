"""ToolOutput 兼容层：统一处理 call_tool 返回值的属性/字典访问。

含 bash 工具返回值解析机器（BashResult / run_bash / parse_bash_payload 等，
自 ppt 范例 ``skill_codes/ppt/utils/bash_utils.py`` 的已验证逻辑上收）。

背景：dev-stable 实际运行时 bash 工具只回 ``data={"content": ...}``，
不带 exit_code/stdout/stderr 字段；退出码须经 ``_coerce_exit_code`` 由
失败信号（success=False / error / [ERROR] 标记 / "Exit code N" 文本）
反推，stdout 须经 content/output/result 回退链提取。
"""
from __future__ import annotations

import gc
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode

logger = logging.getLogger(__name__)

# cat -n 行号前缀：行首空格+数字+tab（如 "     8\t### P1"）
_LINE_NUMBER_PREFIX = re.compile(r"^ *\d+\t", re.MULTILINE)

# 瞬态错误模式：底层 per-file SQLite 读写锁存在 use-after-close 竞态
# （"44.PPT任务未完成"案例），read_file/write_file 可能瞬时报
# "Cannot operate on a closed database"；失败调用的清理路径会驱逐陈旧锁
# 实例，紧随其后的重试走全新锁实例即可成功。
_TRANSIENT_ERROR_PATTERNS = ("Cannot operate on a closed database",)

# markdown code fence 正则：匹配 ```lang\n...\n``` 或 ```\n...\n```
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)

# fallback：LLM 在 code fence 前输出思考文本时（如 "Looking at...\n```html\n..."），
# 用 search 提取首个 code fence 内容
_CODE_FENCE_FALLBACK_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)\n```", re.DOTALL)

# HTML 兜底：LLM 输出思考文本 + 裸 HTML 文档（无 code fence）时，
# 提取 <!DOCTYPE html> 到 </html> 的完整文档
_HTML_DOC_RE = re.compile(r"(<!DOCTYPE html\b.*?</html>)", re.DOTALL | re.IGNORECASE)


def strip_line_numbers(content: str) -> str:
    """去除 read_file 返回内容中的 cat -n 行号前缀。

    read_file 工具返回的 content 为 ``cat -n`` 格式，每行前有
    行号+tab 前缀（如 ``     8\\t### P1``）。本函数去除该前缀，
    返回干净内容。对不含行号前缀的内容无影响。
    """
    if not content:
        return content
    return _LINE_NUMBER_PREFIX.sub("", content)


def strip_code_fence(raw: str) -> str:
    """剥离 LLM 输出中的 markdown code fence（```json/```markdown/```html 等）。

    LLM（尤其 glm-5.2）常将 JSON/HTML/Markdown 内容包裹在 code fence 中，
    如 ``\\`\\`\\`json\\n{...}\\n\\`\\`\\` ``。本函数剥离 fence，返回纯内容。
    对不含 fence 的内容无影响。

    当 LLM 在 code fence 前输出思考文本时（如 "Looking at...\\n```html..."），
    通过 fallback 正则提取首个 code fence 内容；若无 fence 但含完整 HTML 文档，
    则提取 ``<!DOCTYPE html>`` 到 ``</html>`` 的内容。
    """
    if not raw:
        return raw
    text = raw.strip()
    match = _CODE_FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    # fallback 1：思考文本 + code fence
    match = _CODE_FENCE_FALLBACK_RE.search(text)
    if match:
        return match.group(1).strip()
    # fallback 2：思考文本 + 裸 HTML 文档（无 code fence）
    match = _HTML_DOC_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


# 内嵌引号修复用结构字符：开引号的前一个非空白字符 / 闭引号的后一个非空白字符
# 必须是 JSON 结构字符（{ [ , : / , } ] :），否则视为 LLM 未转义的内嵌引号。
_STRUCT_BEFORE = frozenset("{[,:")
_STRUCT_AFTER = frozenset(",}]:")


def _prev_is_structural(text: str, i: int) -> bool:
    """判断位置 i 前侧是否为 JSON 结构位置（前一个非空白字符是 { [ , : 或文本起点）。"""
    j = i - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    return j < 0 or text[j] in _STRUCT_BEFORE


def _next_is_structural(text: str, i: int) -> bool:
    """判断位置 i 后侧是否为 JSON 结构位置（后一个非空白字符是 , } ] : 或文本终点）。"""
    j = i + 1
    n = len(text)
    while j < n and text[j] in " \t\r\n":
        j += 1
    return j >= n or text[j] in _STRUCT_AFTER


def _repair_inner_quotes(text: str) -> str:
    """将 JSON 字符串值内部未转义的 ASCII 双引号替换为中文引号。

    根因（2026-09-16 PPT block_002 案例）：glm-5.2 在中文段落里用
    ASCII 双引号强调术语（如 ``从2024年的"稳健"到2025年的"适度宽松"``），
    且反馈重试后引号不减反增，导致 ``json.loads`` 稳定失败。

    判定规则：一个 ``"`` 是结构性引号当且仅当
    - 开引号：前一个非空白字符是 ``{ [ , :``（或文本起点）；
    - 闭引号：后一个非空白字符是 ``, } ] :``（或文本终点）。
    其余 ``"`` 视为字符串内嵌引号，成对交替替换为 ``“``/``”``。

    已知局限（可接受）：内嵌引号紧跟 ASCII ``,`` 且后接空格+引号的
    罕见场景会被误判为结构闭引号，修复后仍解析失败则抛出，
    与未修复前行为一致（调用方走既有重试/失败路径）。
    """
    out: list[str] = []
    in_str = False
    open_pending = True  # 内嵌引号成对交替：“ 开、” 关
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            if not in_str:
                if _prev_is_structural(text, i):
                    in_str = True
                    open_pending = True
                    out.append('"')
                else:
                    # 字符串外的孤立引号：保守替换，不引入结构变化
                    out.append("”")
            elif _next_is_structural(text, i):
                in_str = False
                out.append('"')
            else:
                out.append("“" if open_pending else "”")
                open_pending = not open_pending
            i += 1
            continue
        if in_str and ch == "\\":
            # 转义序列（如 \"）原样保留两个字符
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_json_tolerant(text: str) -> Any:
    """json.loads + 内嵌引号修复兜底，供所有 LLM JSON 解析点复用。

    先按标准 ``json.loads`` 解析（绝大多数输出不受影响）；
    失败时用 :func:`_repair_inner_quotes` 修复未转义内嵌引号后重试。
    修复后仍失败则抛出 ``json.JSONDecodeError``，与原始行为一致。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_repair_inner_quotes(text))


def parse_llm_json(raw: str) -> dict:
    """从 LLM 输出中提取并解析 JSON，自动剥离 markdown code fence。

    典型用法：``parsed = parse_llm_json(await self.stream_llm_collect(...))``
    避免每次手写 fence 剥离 + json.loads 逻辑。
    解析失败时自动尝试内嵌引号修复（见 :func:`parse_json_tolerant`）。
    """
    text = strip_code_fence(raw)
    return parse_json_tolerant(text)


def _extract_data(result: Any) -> dict[str, Any] | None:
    """从 ToolOutput 对象或 dict 中提取 data dict。"""
    if result is None:
        return None
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if isinstance(result, dict):
        return result
    return None


def get_tool_data(result: Any) -> dict[str, Any]:
    """从工具返回值中提取 data dict（公开入口，兼容 ToolOutput/dict）。

    需要访问特定 data 字段（如 grep 的 mode、edit_file 的 replacements）时
    使用；文本/文件列表提取优先用 get_tool_content / get_tool_stdout /
    get_tool_file_list 等语义化 getter。
    """
    return _extract_data(result) or {}


def get_tool_success(result: Any) -> bool:
    """从 call_tool 返回值中提取 success 状态。

    兼容 ToolOutput 对象（有 .success 属性）和纯 dict（有 success 键）。
    """
    if result is None:
        return False
    if hasattr(result, "success"):
        return bool(result.success)
    data = _extract_data(result)
    if data is not None:
        return bool(data.get("success", False))
    return False


def get_tool_error(result: Any) -> str:
    """从 call_tool 返回值中提取 error 信息。

    兼容 ToolOutput 对象（有 .error 属性）和纯 dict（有 error 键）。
    """
    if result is None:
        return ""
    if hasattr(result, "error"):
        return str(result.error or "")
    data = _extract_data(result)
    if data is not None:
        return str(data.get("error", ""))
    return ""


def get_tool_content(result: Any) -> str:
    """从 read_file 返回值中提取文本内容（自动去除 cat -n 行号前缀）。

    read_file 返回的 content 是 ``cat -n`` 格式（每行前有行号+tab），
    本函数自动去除行号前缀，返回干净内容。调用方无需在正则中容忍行号。
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return strip_line_numbers(result)
    data = _extract_data(result)
    if data is not None:
        content = data.get("content", "")
        if isinstance(content, str):
            return strip_line_numbers(content)
        return str(content)
    return str(result)


def get_tool_stdout(result: Any) -> str:
    """从 bash/code 工具返回值中提取 stdout。

    bash 工具实际只回 ``data={"content": ...}``，故 stdout 缺失时回退
    ``content/output/result`` 键链（见 ``_extract_bash_result``）；
    非 ToolOutput/dict 形态回退 ``normalize_tool_text`` 的文本归一化。
    """
    if result is None:
        return ""
    parsed = _extract_bash_result(result)
    if parsed is not None:
        return parsed.stdout
    return normalize_tool_text(result)


def get_tool_stderr(result: Any) -> str:
    """从 bash/code 工具返回值中提取 stderr。

    data dict 含 ``stderr`` 字段（bash 部分形态 / code/grep 工具）时直接取；
    显式失败但 data 缺失时，``normalize_tool_text`` 会把 error 属性归一为
    ``[ERROR]: ...`` 文本，此处将其作为 stderr 返回供错误报告使用。
    """
    if result is None:
        return ""
    parsed = _extract_bash_result(result)
    if parsed is not None:
        return parsed.stderr
    text = normalize_tool_text(result)
    if text.startswith("[ERROR]"):
        return text
    return ""


def get_tool_exit_code(result: Any, default: int = 1) -> int:
    """从 bash/code 工具返回值中提取 exit_code（带失败信号反推）。

    语义（与 ppt 范例 ``_extract_bash_result`` 一致）：
    - data 内显式 ``exit_code`` → 直接采用（再经 ``_coerce_exit_code`` 修正）；
    - 无显式 exit_code 时默认 0，但出现失败信号（``success is False`` /
      ``error`` 非空 / 输出含 ``[ERROR]`` / 文本含 ``Exit code N``）则升为 1；
    - ``success=True`` 的成功调用即使 data 缺 exit_code 也返回 0
      （覆盖调用方传入的 ``default=1``，修复"成功的 bash 被误判失败"）；
    - 仅当 ``result is None`` 或完全无信息时才回落 ``default``。
    """
    if result is None:
        return default
    parsed = _extract_bash_result(result)
    if parsed is not None:
        return parsed.exit_code
    text = normalize_tool_text(result)
    if not text.strip():
        return default
    return parse_bash_payload(text).exit_code


def get_tool_file_list(result: Any) -> list[str]:
    """从 list_files 工具返回值中提取文件名列表。

    list_files 返回 data={'files': ['a.txt', 'b.py'], 'dirs': []}，
    每个元素是文件名字符串（不是 dict）。
    本函数兼容多种可能的返回格式。
    """
    if result is None:
        return []
    data = _extract_data(result)
    if data is not None:
        for key in ("files", "entries", "filenames", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [_basename(item) for item in v]
    return []


def is_transient_tool_error(error: str) -> bool:
    """判断工具返回的 error 是否为已知瞬态错误（重试一次即可恢复）。"""
    return any(pattern in (error or "") for pattern in _TRANSIENT_ERROR_PATTERNS)


def _is_explicit_failure(result: Any) -> bool:
    """判断 call_tool 返回值是否显式失败（``success is False``）。

    兼容 ToolOutput 对象（.success 属性）与 dict（success 键）。
    """
    if hasattr(result, "success"):
        return result.success is False
    data = _extract_data(result)
    if isinstance(data, dict):
        return data.get("success", True) is False
    return False


def _collect_gc_before_retry() -> None:
    """重试前主动 GC，回收陈旧关闭锁实例。

    根因（2026-09-15 PPT 案例）：文件锁单例注册表是 WeakValueDictionary，
    openjiuwen 的 evict_singleton 因注册表错配是 no-op，锁关闭后仅当最后
    强引用被 GC 回收时，弱注册表条目才消失、下次构造才能拿到全新锁实例。
    瞬态失败后立即重试若钉住者尚未被回收仍会命中陈旧关闭实例，因此
    重试前主动触发一次 gc.collect()（毫秒级，仅在瞬态错误路径触发）。
    """
    gc.collect()


async def call_tool_with_retry(
    node: Any,
    tool_name: str,
    *,
    log_prefix: str = "[skill]",
    **kwargs: Any,
) -> Any:
    """调用 ``node.call_tool``；已知瞬态失败自动重试 1 次，返回最终结果。

    重试范围（与范例 ``PptCommon.read_file_with_retry`` 语义一致）：
    - ``call_tool`` 抛出非 AbortError 异常（AbortError 透传不重试；
      CancelledError 为 BaseException，不被 ``except Exception`` 捕获，
      自然穿透）；
    - 返回值显式 ``success is False`` 且 error 匹配已知瞬态模式
      （如 SQLite use-after-close 竞态）。

    非瞬态业务失败（如文件不存在）不重试，原样返回，由调用方按既有
    逻辑处理。重试后仍失败的返回最终结果（可能 success=False）或抛出
    最终异常。目的是让瞬态错误在节点内自愈，不升级为节点失败、不消耗
    executor 的 plan 级 fallback 预算。
    """
    for attempt in (1, 2):
        try:
            result = await node.call_tool(tool_name, **kwargs)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            if attempt < 2:
                logger.warning(
                    "%s %s 调用异常(第%d次)，重试: %s",
                    log_prefix,
                    tool_name,
                    attempt,
                    e,
                )
                _collect_gc_before_retry()
                continue
            raise
        if attempt < 2 and _is_explicit_failure(result):
            error = get_tool_error(result)
            if is_transient_tool_error(error):
                logger.warning(
                    "%s %s 瞬态失败(第%d次)，重试: %s",
                    log_prefix,
                    tool_name,
                    attempt,
                    error,
                )
                _collect_gc_before_retry()
                continue
        return result
    return None  # 不可达：循环体内必 return 或 raise


def _basename(item: Any) -> str:
    """从文件名项中提取纯文件名（兼容 str 和 dict 格式）。"""
    if isinstance(item, str):
        return item.replace("\\", "/").rstrip("/").split("/")[-1]
    if isinstance(item, dict):
        return str(item.get("name", item.get("path", "")))
    return str(item)


# ─────────────────── bash 工具返回值解析（自 ppt 范例上收） ───────────────────

# bash 工具偶发只回 content="Exit code 1\n..."，不带 exit_code 字段；
# 若不识别会把失败当成功（P9 convert 假完成）。
_EXIT_CODE_RE = re.compile(r"(?im)^\s*Exit code\s+(\d+)\s*$")
_EXIT_CODE_PREFIX_RE = re.compile(r"^Exit code (\d+)\n?", re.IGNORECASE)


class BashExecError(RuntimeError):
    """bash 命令执行失败（required=True 时抛出）。"""


@dataclass(frozen=True)
class BashResult:
    """bash 命令执行结果（含经失败信号反推修正后的 exit_code）。"""

    exit_code: int
    stdout: str
    stderr: str
    raw: str


def quote_path(path: str) -> str:
    """将路径转义为双引号包裹的 shell 参数。"""
    normalized = path.replace('"', '\\"')
    return f'"{normalized}"'


def _parse_exit_code_from_text(text: str) -> int | None:
    """从 ``Exit code N`` 文本前缀解析退出码。"""
    m = _EXIT_CODE_PREFIX_RE.match(text.strip())
    return int(m.group(1)) if m else None


def _exit_code_from_text(*parts: str) -> int | None:
    """从输出文本中逐行匹配 ``Exit code N`` 行。"""
    for part in parts:
        if not part:
            continue
        for line in str(part).splitlines():
            matched = _EXIT_CODE_RE.match(line.strip())
            if matched:
                return int(matched.group(1))
    return None


def _coerce_exit_code(
    exit_code: int,
    *,
    stdout: str = "",
    stderr: str = "",
    success: bool | None = None,
    error: str = "",
) -> int:
    """exit_code=0 时的反向修正：出现失败信号则升为 1。

    失败信号：``success is False`` / error 非空 / 输出含 ``[ERROR]`` /
    文本中含 ``Exit code N``（N != 0）。
    """
    if exit_code != 0:
        return exit_code
    if success is False:
        return 1
    if error.strip():
        return 1
    if "[ERROR]" in stdout or "[ERROR]" in stderr:
        return 1
    inferred = _exit_code_from_text(stdout, stderr)
    if inferred is not None and inferred != 0:
        return inferred
    return exit_code


def normalize_tool_text(result: Any) -> str:
    """把任意形态的工具返回值归一为纯文本。

    str → 本身；``.data`` dict → 依次取 stdout/output/result/content；
    dict → 同键链 + 嵌套 data；error 属性 → ``[ERROR]: {error}``。
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result

    data = result.data if hasattr(result, "data") else None
    if isinstance(data, dict):
        for key in ("stdout", "output", "result", "content"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(data, ensure_ascii=False)

    if isinstance(result, dict):
        for key in ("stdout", "output", "result", "content"):
            value = result.get(key)
            if isinstance(value, str):
                return value
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("stdout", "output", "result", "content"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
        return json.dumps(result, ensure_ascii=False)

    error = result.error if hasattr(result, "error") else None
    if isinstance(error, str) and error.strip():
        return f"[ERROR]: {error}"
    return str(result)


def parse_bash_payload(text: str) -> BashResult:
    """从 bash 工具的文本 payload 解析出 BashResult。

    兼容三种文本形态：
    - ``[ERROR]: ...`` 前缀 → exit 1；
    - ``Exit code N\\n...`` 前缀 → 解析 N 并剥掉前缀行；
    - JSON payload（含 exit_code/stdout/stderr 字段）→ 字段提取 + coerce；
    - 普通文本 → 按无失败信号处理（exit 0，再经 coerce 修正）。
    """
    stripped = text.strip()
    if stripped.startswith("[ERROR]"):
        return BashResult(exit_code=1, stdout="", stderr=stripped, raw=text)

    parsed_exit = _parse_exit_code_from_text(stripped)
    if parsed_exit is not None:
        stdout = _EXIT_CODE_PREFIX_RE.sub("", stripped, count=1)
        return BashResult(exit_code=parsed_exit, stdout=stdout, stderr="", raw=text)

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        exit_code = _coerce_exit_code(0, stdout=stripped)
        return BashResult(exit_code=exit_code, stdout=stripped, stderr="", raw=text)

    if not isinstance(payload, dict):
        exit_code = _coerce_exit_code(0, stdout=stripped)
        return BashResult(exit_code=exit_code, stdout=stripped, stderr="", raw=text)

    exit_code = int(payload.get("exit_code", 0) or 0)
    stdout = str(payload.get("stdout") or payload.get("content") or "")
    stderr = str(payload.get("stderr") or "")
    success = payload.get("success")
    error = str(payload.get("error") or "")
    exit_code = _coerce_exit_code(
        exit_code,
        stdout=stdout,
        stderr=stderr,
        success=success if isinstance(success, bool) else None,
        error=error,
    )
    return BashResult(exit_code=exit_code, stdout=stdout, stderr=stderr, raw=text)


def _extract_bash_result(raw: Any) -> BashResult | None:
    """从 raw tool 返回值直接提取 exit_code/stdout/stderr。

    已是 ``BashResult``（如 ``run_bash`` 的返回值）时直接返回，保证
    ``get_tool_exit_code`` 等 getter 对其同样正确（否则文本兜底会恒返 0）。
    兼容嵌套结构：``raw.data``（ToolOutput 属性）/ ``raw["data"]`` /
    ``raw["result"].data`` / ``raw["result"]["data"]``。提取失败返回
    None，由调用方 fallback 到 ``parse_bash_payload(normalize_tool_text(...))``。
    """
    if isinstance(raw, BashResult):
        return raw
    data: dict[str, Any] | None = None
    success: bool | None = None

    if hasattr(raw, "data") and isinstance(raw.data, dict):
        data = raw.data
        if hasattr(raw, "success"):
            success = raw.success
    elif isinstance(raw, dict):
        if "data" in raw and isinstance(raw["data"], dict):
            data = raw["data"]
            success = raw.get("success")
        elif "result" in raw:
            inner = raw["result"]
            if hasattr(inner, "data") and isinstance(inner.data, dict):
                data = inner.data
                if hasattr(inner, "success"):
                    success = inner.success
            elif isinstance(inner, dict) and "data" in inner and isinstance(inner["data"], dict):
                data = inner["data"]
                success = inner.get("success")

    if data is None:
        return None

    exit_code = int(data.get("exit_code", 0) or 0)
    # stdout 缺失时依次回退到 content/output/result（bash 工具常只回 content）
    stdout = ""
    for key in ("stdout", "content", "output", "result"):
        value = data.get(key)
        if isinstance(value, str) and value:
            stdout = value
            break
    stderr = str(data.get("stderr") or "")
    success = data.get("success")
    if not isinstance(success, bool) and hasattr(raw, "success"):
        # 直接属性访问；skill_code 侧禁止 getattr（builtin AST 校验）
        raw_success = raw.success
        success = raw_success if isinstance(raw_success, bool) else None
    error = str(data.get("error") or "")
    if not error and hasattr(raw, "error"):
        raw_error = raw.error
        if isinstance(raw_error, str):
            error = raw_error
    exit_code = _coerce_exit_code(
        exit_code,
        stdout=stdout,
        stderr=stderr,
        success=success if isinstance(success, bool) else None,
        error=error,
    )
    return BashResult(exit_code=exit_code, stdout=stdout, stderr=stderr, raw=str(raw))


def parse_bash_result(result: Any) -> BashResult:
    """把 bash/code 工具返回值统一解析为 BashResult。

    优先结构化提取（``_extract_bash_result``），失败时回退文本解析
    （``parse_bash_payload(normalize_tool_text(...))``）。
    """
    parsed = _extract_bash_result(result)
    if parsed is not None:
        return parsed
    return parse_bash_payload(normalize_tool_text(result))


async def run_bash(
    node: PlanNode,
    command: str,
    *,
    timeout_seconds: int = 300,
    required: bool = True,
    workdir: str | None = None,
) -> BashResult:
    """通过节点执行 bash 命令并解析结果（自 ppt 范例上收的公共入口）。

    两次 tool_attempts（带 timeout 与不带）：部分 bash 工具注册形态不支持
    timeout 参数，第一次 ValueError 后降级重试。``required=True`` 时非零
    退出码抛 ``BashExecError``；AbortError（HITL 中断）一律透传。
    """
    last_error: Exception | None = None

    tool_attempts: list[tuple[str, dict[str, Any]]] = [
        (
            "bash",
            _build_bash_kwargs(command, timeout_seconds, workdir, with_timeout=True),
        ),
        (
            "bash",
            _build_bash_kwargs(command, timeout_seconds, workdir, with_timeout=False),
        ),
    ]

    for tool_name, kwargs in tool_attempts:
        try:
            raw = await node.call_tool(tool_name, **kwargs)
            parsed = _extract_bash_result(raw) or parse_bash_payload(
                normalize_tool_text(raw)
            )
            if parsed.exit_code != 0 and required:
                detail = parsed.stderr or parsed.stdout or parsed.raw
                raise BashExecError(
                    f"命令执行失败 (exit={parsed.exit_code}): {command}\n{detail}"
                )
            return parsed
        except BashExecError:
            raise
        except ValueError:
            continue
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            last_error = exc
            continue

    if last_error is not None:
        raise BashExecError(f"无法执行 bash 命令: {command}") from last_error
    raise BashExecError(f"未注册 bash 工具，无法执行: {command}")


def _build_bash_kwargs(
    command: str,
    timeout_seconds: int,
    workdir: str | None,
    *,
    with_timeout: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"command": command}
    if with_timeout:
        kwargs["timeout"] = timeout_seconds
    if workdir:
        kwargs["workdir"] = workdir
    return kwargs


def combined_output(result: BashResult) -> str:
    """合并 stdout/stderr 作为命令输出详情。"""
    return f"{result.stdout}\n{result.stderr}".strip()


# 路径 token 终止字符：空白、成对/不成对引号、各类闭合括号与中西文标点。
# 路径段本身可含中文、数字、下划线、点号等（如 D:\基线\20页_case_01\a.pptx）。
_PATH_STOP_CHARS = frozenset(' \t\r\n"\'“”‘’()（）[]【】{}《》<>,，;；?？!！。')

# Unix 绝对路径起始 '/' 的前置排除字符：字母数字（相对路径/变量）、
# '/'（URL 的 '//'）、'.'（相对路径 '../'）、':'/'?'（URL 协议 'https://'）
_PATH_PRECEDING_SKIP = frozenset('/.:?')


def extract_path_candidates(
    text: Any, *, suffixes: tuple[str, ...] = ()
) -> list[str]:
    """从自由文本（如用户 query）提取显式本地路径候选（Windows/Unix 绝对路径）。

    场景：用户 query 常明文携带文件路径（如「总结一下这个 pptx
    （D:\\dir\\报告.pptx）的内容」），但框架只把 ``query`` 整段透传给节点。
    入口节点应先调用本函数从 query 提取路径候选，命中即免 glob/ask_user。

    识别规则：
    - Windows：盘符 + ``\\`` 或 ``/`` 开头（``D:\\dir\\file.pptx``）
    - Unix：``/`` 绝对路径（前置字符为字母数字、``/ . : ?`` 时跳过，
      以排除 URL 协议 ``https://`` 与相对路径）
    - token 于空白/引号/闭合括号/中西文标点处终止，兼容全角括号包裹
    - ``suffixes`` 非空时仅保留以其结尾的路径（大小写不敏感），
      如 ``suffixes=(".pptx",)``；为空则返回全部路径候选
    - 目录路径保留尾部 ``\\``/``/``；按出现顺序去重
    """
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        start = -1
        if (
            ch.isalpha()
            # 盘符前不得是字母数字（排除 URL 协议尾字母，如 https 的 s）
            and (i == 0 or not text[i - 1].isalnum())
            and i + 2 < n
            and text[i + 1] == ":"
            and text[i + 2] in "\\/"
        ):
            # Windows 盘符路径：D:\ 或 D:/
            start = i
            i += 3
        elif (
            ch == "/"
            and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in _PATH_PRECEDING_SKIP))
        ):
            # Unix 绝对路径：/ 开头，排除 URL 协议与相对路径
            start = i
            i += 1
        else:
            i += 1
            continue
        j = i
        while j < n and text[j] not in _PATH_STOP_CHARS:
            j += 1
        candidate = text[start:j]
        i = j
        if len(candidate) <= 3:  # 仅 "D:\" 或 "/" 骨架，无实际路径段
            continue
        if suffixes and not candidate.lower().endswith(tuple(s.lower() for s in suffixes)):
            continue
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found
