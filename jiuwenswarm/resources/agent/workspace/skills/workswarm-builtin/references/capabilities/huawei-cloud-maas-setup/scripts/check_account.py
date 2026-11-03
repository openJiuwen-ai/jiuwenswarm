#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""检测华为云账号状态（实名认证 + 引导弹窗关闭）。

前置条件：
- 浏览器已由 ``ensure_browser.py`` 启动并通过 CDP 暴露
- 用户已在 ``navigate.py`` 跳转到费用中心首页并完成登录

本脚本职责：
- 自动关闭费用中心引导弹窗（ti-guide-modal，"费用总览全面升级"等）
- 检测账号是否已完成实名认证（通过 ``window.myRoleTags`` JS 变量）
- 返回检测结果，供 Skill 编排层决定 ask_user 文案

**关键约束**：
- **不导航**：用户已在步骤 1 打开费用中心页面，此步骤只做检测和关弹窗
- **不抛异常给上层**：失败时返回 ``{ok:false, stage:"check_account", error:...}``
- 整个流程 ≤ 15s

用法::

    python check_account.py --cdp-url http://127.0.0.1:9333 --json
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.append(str(_SCRIPT_DIR))

from lib.cdp_client import (  # noqa: E402
    connect_page,
    emit,
    emit_progress,
    output_json,
    resolve_cdp_url,
)
from lib.flow_state import make_failure, make_success  # noqa: E402
from lib.huawei_selectors import (  # noqa: E402
    HUAWEI_REALNAME_AUTH_URL,
    detect_realname_status,
    dismiss_popups,
)


def check_account(cdp_url: str, timeout_s: int = 15) -> dict:
    """关闭引导弹窗并检测实名认证状态。返回结果 dict。

    - 复用已有 page（不新建标签页，不导航）
    - 先关弹窗再做检测（弹窗可能遮挡页面元素，影响 JS 变量读取）
    """
    emit_progress(0, 4, "连接浏览器...")
    try:
        pw, browser, page = connect_page(cdp_url, timeout_ms=15_000)
    except Exception as exc:
        return make_failure(
            "connect_failed",
            f"无法连接浏览器: {exc}",
            cdp_url=cdp_url,
        )

    try:
        # 1. 等待 SPA 渲染（用户刚确认登录，页面可能仍在加载）
        emit_progress(1, 4, "等待页面渲染...")
        time.sleep(2.0)

        # 2. 关闭引导弹窗（费用中心 ti-guide-modal）
        emit_progress(2, 4, "关闭引导弹窗...")
        popups_closed = dismiss_popups(page, max_rounds=3)
        if popups_closed > 0:
            emit("popup", f"已关闭 {popups_closed} 个引导弹窗")
            time.sleep(0.5)
        else:
            emit("popup", "未发现引导弹窗")

        # 3. 检测实名认证状态
        emit_progress(3, 4, "检测实名认证状态...")
        realname_authenticated = detect_realname_status(page, timeout_ms=8_000)

        emit_progress(4, 4, "检测完成")
        if realname_authenticated:
            emit("realname", "账号已完成实名认证")
        else:
            emit("realname", "账号尚未完成实名认证")

        return make_success(
            "check_account",
            cdp_url=cdp_url,
            realname_authenticated=realname_authenticated,
            popups_closed=popups_closed,
            realname_auth_url=HUAWEI_REALNAME_AUTH_URL,
        )
    except Exception as exc:
        return make_failure(
            "exception",
            f"检测账号状态时发生未预期错误: {exc}",
            cdp_url=cdp_url,
        )
    finally:
        try:
            pw.stop()
        except Exception as e:
            logging.debug("停止 playwright 失败: %s", e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检测华为云账号状态（实名认证 + 关闭引导弹窗）")
    parser.add_argument("--cdp-url", default="", help="CDP endpoint，留空自动解析")
    parser.add_argument("--timeout", type=int, default=15, help="整体超时秒数")
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

    result = check_account(cdp_url, timeout_s=args.timeout)
    output_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
