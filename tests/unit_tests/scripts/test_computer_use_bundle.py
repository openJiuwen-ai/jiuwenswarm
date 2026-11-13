# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Frozen Computer Use 验包探针必须在引擎的 exec 编译口径下通过。

打包流水线的第 4 步才在冻结 exe 内执行 verify_computer_use_bundle.py；
该脚本交给 REPL 的 PROBE 由 computer_use_mcp 以
compile(code + "\\n", "computer-use-agent.py", "exec", dont_inherit=True) 编译。
探针一旦出现顶层 await 等 exec 不支持的语法，只有完整构建结束后才会失败，
所以这里用同样的编译口径静态锁定该契约。

PROBE 直接从源码取字面量而不导入模块：验包脚本会拉起整个 Computer Use /
agent-core 依赖栈，在默认开启覆盖率的单测里代价过高。
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
VERIFIER = ROOT / "scripts/verify_computer_use_bundle.py"
COMPILE_FILENAME = "computer-use-agent.py"


def _probe() -> str:
    module = ast.parse(VERIFIER.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PROBE" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("verify_computer_use_bundle.py no longer defines a literal PROBE")


def test_probe_compiles_under_the_engine_exec_mode():
    # 不做 try/except：SyntaxError 即测试失败，与打包期暴露的缺陷一致。
    compile(_probe() + "\n", COMPILE_FILENAME, "exec", dont_inherit=True)
