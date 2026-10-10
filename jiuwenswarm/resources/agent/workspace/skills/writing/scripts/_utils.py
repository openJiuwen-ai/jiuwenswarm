# -*- coding: utf-8 -*-
"""
_utils.py — 跨 stage 共享的 helper（从旧 scripts/main.py 抽出）

* ``_safe_get``: 嵌套 dict 安全取值（缺则返回 default）
* ``_strip_code_fence``: 剥 LLM 偶尔返的 ```xxx\n...\n``` 围栏
* ``_escape_latex``: 最小化 LaTeX 字符转义（防止 key_papers 中含 & % $ # _ { } ~ ^）

设计：纯函数无副作用，stage 脚本可放心 import。
"""
from __future__ import annotations

import re
from typing import Any


def _safe_get(d: dict, *keys: str, default: Any = "") -> Any:
    """嵌套安全取值；缺则返回 default。"""
    cur: Any = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _strip_code_fence(s: str) -> str:
    """剥掉 LLM 偶尔返回的 ```latex ... ``` 围栏。"""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s, count=1)
        s = re.sub(r"\n```\s*$", "", s, count=1)
    return s.strip()


def _escape_latex(s: str) -> str:
    """最小化 LaTeX 转义，防止 key_papers 中含 & % $ # _ { } 等。"""
    if not isinstance(s, str):
        s = str(s)
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    pattern = re.compile("|".join(re.escape(k) for k in repl.keys()))
    return pattern.sub(lambda m: repl[m.group(0)], s)
