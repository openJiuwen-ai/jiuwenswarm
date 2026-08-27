#!/usr/bin/env python3
"""preCompact: remind user/agent to flush docs/ai before context loss."""

from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    pct = data.get("context_usage_percent", "?")
    msg = (
        f"[知识库] 上下文即将压缩（约 {pct}%）。"
        "若本会话有重要决策 / QA / 进化原因，请让 Agent 先写入 docs/ai/ 当前阶段目录，"
        "并更新 HANDOFF.md。"
    )
    print(json.dumps({"user_message": msg}, ensure_ascii=False))


if __name__ == "__main__":
    main()

