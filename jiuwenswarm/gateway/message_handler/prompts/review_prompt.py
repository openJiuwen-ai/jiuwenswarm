# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PR 审查 prompt（单一数据源）."""

from __future__ import annotations

from jiuwenswarm.gateway.message_handler.prompts import response_language_line


def build_review_prompt(pr_arg: str) -> str:
    """构建 /review 的 prompt，指示 Agent 使用 gh CLI 审查 PR。

    gh-only、args 原样透传、始终包含 ``PR number:`` 行；无 git/gh 预检。
    输出语言来自 config ``preferred_language``。
    """
    args = pr_arg.strip() if pr_arg else ""
    lang_line = response_language_line()
    return f"""You are an expert code reviewer. Follow these steps:

1. If no PR number is provided in the args, run `gh pr list` to show open PRs
2. If a PR number is provided, run `gh pr view <number>` to get PR details
3. Run `gh pr diff <number>` to get the diff
4. Analyze the changes and provide a thorough code review that includes:
   - Overview of what the PR does
   - Analysis of code quality and style
   - Specific suggestions for improvements
   - Any potential issues or risks

Keep your review concise but thorough. Focus on:
- Code correctness
- Following project conventions
- Performance implications
- Test coverage
- Security considerations

Format your review with clear sections and bullet points.

{lang_line}
PR number: {args}"""
