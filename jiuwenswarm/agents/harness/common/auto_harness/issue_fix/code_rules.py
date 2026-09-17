# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code rule loading for GitCode issue-fix tasks."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_CODE_RULE_FILE = "code_rule.txt"


@lru_cache(maxsize=1)
def load_code_rules() -> str:
    """Load repository code rules for issue-fix prompts."""
    rule_path = Path(__file__).resolve().parent / _CODE_RULE_FILE
    if rule_path.is_file():
        return rule_path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    return ""


def format_code_rules_prompt() -> str:
    """Build the prompt section that constrains generated code style."""
    rules = load_code_rules()
    if not rules:
        return ""
    return (
        "编程规范约束（必须遵守仓库 code_rule.txt）:\n"
        "以下规范适用于本次 issue 修复产生的所有产品代码和测试代码；"
        "若 issue 修复方案与规范冲突，必须优先满足规范并在日志中说明取舍。\n"
        f"{rules}\n"
    )
