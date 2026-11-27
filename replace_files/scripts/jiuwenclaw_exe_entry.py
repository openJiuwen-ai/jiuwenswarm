# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""PyInstaller 打包入口：根据参数分发到主应用或子命令。"""

from __future__ import annotations

import sys


def _pop_flag(flag: str) -> bool:
    if flag not in sys.argv:
        return False
    sys.argv.remove(flag)
    return True


def main() -> None:
    # 子命令：初始化工作区（首次使用需运行 jiuwenclaw.exe init）
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "init":
        sys.argv.pop(1)
        from jiuwenclaw.init_workspace import main as init_main
        init_main()
        return
    # 子命令：CLI 命令分发
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "acp":
        from jiuwenclaw.app_cli import main as cli_main
        cli_main()
        return
    if _pop_flag("--desktop-run-app"):
        from jiuwenclaw.app import main as app_main
        app_main()
        return
    if _pop_flag("--desktop-run-web"):
        from jiuwenclaw.app_web import main as web_main
        web_main()
        return
    # 子命令：浏览器启动（供主进程 subprocess 调用）
    if "--browser-start-client" in sys.argv:
        idx = sys.argv.index("--browser-start-client")
        sys.argv.pop(idx)
        from jiuwenclaw.agentserver.tools.browser_start_client import main as browser_main
        raise SystemExit(browser_main())
    # 默认运行桌面应用。
    from jiuwenclaw.desktop_app import main as desktop_main
    desktop_main()


if __name__ == "__main__":
    main()
