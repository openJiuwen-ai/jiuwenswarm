#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""通用 URL 跳转脚本（V2 折中方案核心脚本）。

只做一件事：通过 CDP 连接已有浏览器，跳转到指定 URL，立即返回。
不做任何 DOM 操作、不做登录检测、不做任何自动化。

由 Skill 编排层在每个关键步骤调用，跳转后立即用 ``ask_user``
提醒用户操作。

用法::

    python navigate.py --cdp-url http://127.0.0.1:9333 --json \\
        --url "https://console.huaweicloud.com/modelarts/..."
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.append(str(_SCRIPT_DIR))

from lib.cdp_client import (  # noqa: E402
    connect_page,
    emit_progress,
    output_json,
    resolve_cdp_url,
)
from lib.flow_state import make_failure, make_success  # noqa: E402


def navigate(url: str, cdp_url: str, wait_until: str = "domcontentloaded",
             timeout_ms: int = 30_000) -> dict:
    """连接到已有浏览器，跳转到 URL，立即返回。

    - **不关闭浏览器**：保留窗口供后续脚本使用
    - **不等待用户**：DOM ready 即可返回
    - **不检测登录态**：由 Skill 编排层通过 ask_user 让用户自行确认
    """
    emit_progress(0, 3, "正在连接浏览器...")
    try:
        pw, browser, page = connect_page(cdp_url, timeout_ms=timeout_ms)
    except Exception as exc:
        return make_failure(
            "connect_failed",
            f"无法连接浏览器: {exc}",
            cdp_url=cdp_url,
        )

    try:
        emit_progress(1, 3, f"导航到 {url}")
        page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        # 给前端路由一点时间重定向（避免 race）
        time.sleep(0.5)
        current_url = page.url or ""
        title = ""
        try:
            title = page.title()
        except Exception as e:
            logging.warning("page title fetch failed: %s", e)
        emit_progress(2, 3, f"已打开: {current_url}")
        emit_progress(3, 3, "导航完成")
        return make_success(
            "navigated",
            cdp_url=cdp_url,
            requested_url=url,
            current_url=current_url,
            title=title,
        )
    except Exception as exc:
        return make_failure(
            "navigate_failed",
            f"导航失败: {exc}",
            cdp_url=cdp_url,
        )
    finally:
        # 不关闭浏览器！只关 Playwright 进程
        try:
            pw.stop()
        except Exception as e:
            logging.warning("pw.stop failed: %s", e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通用浏览器导航")
    parser.add_argument("--url", required=True, help="目标 URL")
    parser.add_argument("--cdp-url", default="", help="CDP endpoint，留空自动解析")
    parser.add_argument(
        "--wait-until",
        default="domcontentloaded",
        choices=["load", "domcontentloaded", "networkidle"],
        help="page.goto 的 wait_until 参数",
    )
    parser.add_argument("--timeout-ms", type=int, default=30_000, help="导航超时毫秒")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args(argv)

    cdp_url = (args.cdp_url or "").strip() or resolve_cdp_url()
    if not cdp_url:
        result = make_failure(
            "init",
            "未找到 CDP URL，请先启动浏览器或在参数中传入 --cdp-url",
        )
        output_json(result)
        return 1

    result = navigate(
        url=args.url,
        cdp_url=cdp_url,
        wait_until=args.wait_until,
        timeout_ms=args.timeout_ms,
    )
    output_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
