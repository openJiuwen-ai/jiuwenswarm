# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""分层守护测试：防止引入新的跨层依赖（gateway 解耦工程）。

以 UT 形式调用 ``scripts/check_layering.py``（规则与燃尽白名单见其头部
常量），断言无白名单外的新增违规：

  R1 common 禁止 import server/agents/channels/gateway/extensions/symphony
  R2 server  禁止 import gateway
  R3 gateway 禁止 import server/agents/channels

上游同步、本仓提交引入新的跨层依赖时本测试失败；白名单内的存量
（燃尽清单）随解耦 PR 逐步缩减——存量消除后白名单未同步缩减同样失败
（脚本输出会标注 ``[已消除]``）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_layering.py"


def test_no_new_layering_violations() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=300,
    )
    assert result.returncode == 0, (
        "分层检查未通过：存在白名单外的新增跨层依赖，"
        "或存量已消除但白名单未缩减。明细：\n" + result.stdout
    )
