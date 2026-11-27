# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""以 detached 方式启动 delayed_restart_app，供 bash 通过 skill 调用。

本脚本会立即 spawn 子进程并退出，子进程与当前进程树脱离，因此当 app 被终止时
子进程不会随之结束，可以正常完成延迟重启。

用法:
    python launch_delayed_restart.py --pid <PID> [--delay 5]
    （需在技能目录或指定脚本路径下执行）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="以 detached 方式启动延迟重启")
    parser.add_argument("--pid", type=int, required=True, help="要终止的 app 进程 PID")
    parser.add_argument("--delay", type=float, default=5, help="延迟秒数（默认 5）")
    args = parser.parse_args()

    try:
        from jiuwenclaw.paths import get_root_dir
        root = get_root_dir()
    except Exception:
        root = Path.cwd()

    cmd = [
        sys.executable,
        "-m",
        "jiuwenclaw.scripts.delayed_restart_app",
        "--pid", str(args.pid),
        "--delay", str(max(1, min(args.delay, 300))),
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    subprocess.Popen(
        cmd,
        cwd=str(root),
        creationflags=creationflags if sys.platform == "win32" else 0,
        start_new_session=(sys.platform != "win32"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
