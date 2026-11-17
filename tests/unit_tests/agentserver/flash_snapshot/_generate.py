#!/usr/bin/env python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regenerate golden flash prompt snapshots（本地开发工具，CI 不需要）。

CI 里 ``agent_shield`` 已安装，``offline.assemble()`` 直接可 import；本地缺
``agent_shield``，flash rail 的 ``DeepAgentRail`` / ``AgentCallbackContext`` import 会失败。
本脚本用两个叶模块桩绕过这两处 import（它们仅作基类/类型提示，不影响离线组装结果），
再导入 ``offline`` + ``snapshot`` 生成 golden JSON / text。

用法（在仓库根目录）：
    python tests/unit_tests/agentserver/flash_snapshot/_generate.py [输出目录]

依赖：``agent-core``（dev-stable openjiuwen）需与本仓同级；可用 ``AGENT_CORE`` 环境变量覆盖。
"""

from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path

# Windows 控制台 GBK 下避免 Unicode 输出报错。
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]  # tests/unit_tests/agentserver/flash_snapshot -> 仓库根
AGENT_CORE = Path(os.environ.get("AGENT_CORE", REPO.parent / "agent-core"))

sys.path.insert(0, str(AGENT_CORE))
sys.path.insert(0, str(REPO))


def _stub_leaf(name: str, attrs: dict) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def _make_pkg(name: str, path: list[str]) -> None:
    module = types.ModuleType(name)
    module.__path__ = path
    sys.modules[name] = module


def _install_stubs() -> None:
    # 仅桩 ``openjiuwen.harness.rails.base.DeepAgentRail``：flash rail 的安全基类，其真实
    # 父包 ``openjiuwen.harness.rails`` 会触发 agent_shield（本地缺失）。``AgentCallbackContext``
    # 所在的 ``openjiuwen.core.single_agent.rail.base`` 是 core 链、可正常 import，不桩。
    _stub_leaf(
        "openjiuwen.harness.rails.base",
        {"DeepAgentRail": type("DeepAgentRail", (), {})},
    )
    # 让 jiuwenswarm.agents.harness.flash 包名解析到真实目录，但不触发其 __init__（重依赖）。
    flash_dir = str(REPO / "jiuwenswarm" / "agents" / "harness" / "flash")
    _make_pkg("jiuwenswarm.agents.harness.flash", [flash_dir])
    _make_pkg("jiuwenswarm.agents.harness.flash.rails", [os.path.join(flash_dir, "rails")])


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    _install_stubs()

    from jiuwenswarm.agents.harness.flash.prompt.offline import (
        LANGUAGES,
        SNAPSHOT_DOMAINS,
        assemble,
    )
    from jiuwenswarm.agents.harness.flash.prompt.snapshot import (
        build_prompt_snapshot,
        write_prompt_snapshot,
    )

    print(f"repo      = {REPO}")
    print(f"agent-core= {AGENT_CORE} (exists={AGENT_CORE.is_dir()})")
    print(f"domains   = {list(SNAPSHOT_DOMAINS)}")
    print(f"output    = {output_dir}")

    for language in LANGUAGES:
        sections = assemble(language)
        snapshot = build_prompt_snapshot(sections, language=language)
        json_path, text_path = write_prompt_snapshot(
            snapshot, output_dir, stem=f"flash_prompt.{language}"
        )
        names = [f"{s.priority}:{s.name}" for s in snapshot.sections]
        print(
            f"[{language}] sections={len(snapshot.sections)} "
            f"full_hash={snapshot.full_hash} static_hash={snapshot.static_hash}"
        )
        print(f"[{language}]   {names}")
        print(f"[{language}]   -> {json_path.name}, {text_path.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
