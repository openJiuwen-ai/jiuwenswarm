# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JSON提取工具 -- 从LLM返回值中健壮地提取JSON。"""

from __future__ import annotations

import json
import re
from typing import Any, Union


def extract_llm_json(
    raw: Union[str, dict, list],
    expected_type: type = dict,
) -> Any:
    """
    从LLM返回值中健壮地提取JSON。
    
    兼容四种返回形态：
      1. 已经是 dict/list（Agent 直接返回结构化对象）
      2. 纯 JSON 字符串
      3. ```json ... ``` 包裹的字符串
      4. 夹杂文本的响应（用括号计数法提取第一个完整 JSON 结构）
    
    Args:
        raw: LLM返回的原始数据
        expected_type: 期望的JSON类型（dict或list）
    
    Returns:
        解析后的JSON对象
    
    Raises:
        ValueError: 无法解析JSON时抛出
    
    Example:
        >>> response = await self.call_llm("生成JSON")
        >>> data = extract_llm_json(response, expected_type=dict)
        >>> items = extract_llm_json(response, expected_type=list)
    """
    # 如果已经是目标类型，直接返回
    if isinstance(raw, expected_type):
        return raw
    
    # 如果是其他结构化类型，也接受
    if isinstance(raw, (dict, list)):
        return raw
    
    if not isinstance(raw, str):
        raise ValueError(f"LLM返回了未预期的类型: {type(raw)}")
    
    # 尝试直接解析
    first_error: json.JSONDecodeError | None = None
    try:
        result = json.loads(raw)
        if isinstance(result, expected_type):
            return result
        # 解析成功但类型不匹配
        first_error = None
    except json.JSONDecodeError as e:
        first_error = e
    
    # 提取 ```json ... ``` / ``` ... ``` 代码块
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if code_block:
        try:
            result = json.loads(code_block.group(1).strip())
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass
    
    # 用括号计数法提取第一个完整 JSON 结构
    open_char = "[" if expected_type == list else "{"
    close_char = "]" if expected_type == list else "}"
    candidate = _extract_outermost_json(raw, open_char, close_char)
    if candidate is not None:
        try:
            result = json.loads(candidate)
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass
    
    # 构建包含具体错误原因的报错信息
    if first_error is not None:
        context_start = max(0, first_error.pos - 80)
        context_end = min(len(raw), first_error.pos + 80)
        error_context = raw[context_start:context_end].replace("\n", "\\n")
        raise ValueError(
            f"无法从LLM输出中解析JSON（期望{expected_type.__name__}）："
            f"{first_error.msg}（第{first_error.lineno}行第{first_error.colno}列）。"
            f"出错位置附近：...{error_context}..."
        )
    raise ValueError(
        f"无法从LLM输出中解析JSON（期望{expected_type.__name__}）：{raw[:300]}"
    )


def _extract_outermost_json(
    text: str,
    open_char: str,
    close_char: str,
) -> str | None:
    """
    使用括号计数法提取最外层完整的JSON结构。
    
    Args:
        text: 原始文本
        open_char: 开括号字符（{ 或 [）
        close_char: 闭括号字符（} 或 ]）
    
    Returns:
        提取的JSON字符串，如果未找到则返回None
    """
    depth = 0
    start_idx = -1
    in_string = False
    escape = False
    
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == close_char:
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    return text[start_idx:i + 1]
    
    return None
