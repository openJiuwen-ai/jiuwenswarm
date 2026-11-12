#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""自动完成华为云 MaaS 委托授权（V2 折中方案）。

前置条件：
- 浏览器已由 ``ensure_browser.py`` 启动并通过 CDP 暴露
- 用户已在浏览器中登录华为云（由 ``navigate.py`` + ``ask_user`` 处理）

本脚本职责：
- 导航到 MaaS 首页（如果尚未到达）
- 检测委托授权警告条
- 如果有警告：自动点击"此处"→ 确认对话框 → 点击"确定"
- 等待"权限更新成功"提示

**关键约束**：
- **不抛异常给上层**：失败时返回 ``{ok:false, stage:"authorize", error:...}``，
  由 Skill 决定是否降级为手动模式
- **失败不中断后续步骤**：用户可能已授权，只是检测逻辑没识别到
- 整个流程 ≤ 30s

用法::

    python auto_authorize.py --cdp-url http://127.0.0.1:9333 --json
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
    MAAS_HOMEPAGE_URL,
    SELECTOR_AUTH_CONFIRM,
    SELECTOR_AUTH_DIALOG,
    SELECTOR_AUTH_HERE_LINK,
    SELECTOR_AUTH_SUCCESS,
    SELECTOR_AUTH_WARNING,
    click_first_visible,
    click_wait_first,
    handle_disclaimer,
)

# 授权警告关键词（命中任一即认为有未完成授权）
_AUTH_WARNING_KEYWORDS = (
    "已有委托",
    "权限不足",
    "尚未授权",
    "访问授权",
    "委托授权",
    "模型使用权限",
)


def _detect_auth_warning(page) -> bool:
    """快速判定页面是否存在授权警告（< 2s）。

    华为云 MaaS 首页的授权提示固定在 ``#authGlobalMessage``（maui-add-auth）：
    - 已授权：该容器存在但内容为空；
    - 未授权：容器内有警告文案。
    容器缺失时退化为整页文本扫描。
    """
    try:
        container = page.locator("#authGlobalMessage").first
        if container.count() > 0 and container.is_visible():
            text = container.inner_text(timeout=1_000) or ""
            return any(k in text for k in _AUTH_WARNING_KEYWORDS) and len(text.strip()) > 0
    except Exception as e:
        logging.debug("检测授权警告容器失败: %s", e)
    # 容器缺失/不可见：按警告选择器做一次即时快照
    try:
        warning = SELECTOR_AUTH_WARNING.first_visible(page, timeout_ms=1_000)
        if warning is not None:
            try:
                text = warning.inner_text(timeout=1_000) or ""
                if any(k in text for k in _AUTH_WARNING_KEYWORDS):
                    return True
            except Exception:
                return True
    except Exception as e:
        logging.debug("检测授权警告选择器失败: %s", e)
    return False


def auto_authorize(cdp_url: str, timeout_s: int = 45) -> dict:
    """自动检测并完成 MaaS 委托授权。返回结果 dict。"""
    emit_progress(0, 5, "连接浏览器...")
    try:
        pw, browser, page = connect_page(cdp_url, timeout_ms=15_000)
    except Exception as exc:
        return make_failure("connect_failed", f"无法连接浏览器: {exc}", cdp_url=cdp_url)

    try:
        # 1. 导航到 MaaS 首页
        emit_progress(1, 5, f"导航到 {MAAS_HOMEPAGE_URL}")
        try:
            page.goto(MAAS_HOMEPAGE_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            return make_failure("navigate_failed", f"导航失败: {exc}", cdp_url=cdp_url)

        # 2. 等待 SPA 渲染并检测授权警告（快速判定）
        emit_progress(2, 5, "检测授权警告...")
        time.sleep(2.5)  # 等 SPA 子视图渲染完成再判定，避免空容器误判

        # 2.5 处理 MaaS 服务声明弹窗（首次访问时出现，会遮挡页面）
        disclaimer_handled = handle_disclaimer(page)
        if disclaimer_handled:
            # 弹窗关闭后等待页面重新渲染
            time.sleep(1.5)

        if not _detect_auth_warning(page):
            emit("auth", "未发现授权警告，视为已授权")
            emit_progress(5, 5, "已授权")
            return make_success(
                "authorize",
                cdp_url=cdp_url,
                auth_done=False,
                skipped_reason="no_warning",
                disclaimer_handled=disclaimer_handled,
            )

        # 3. 点击"此处"链接
        emit("auth", "检测到授权提示，尝试自动更新")
        emit_progress(3, 5, "点击'此处'链接...")
        here_link = SELECTOR_AUTH_HERE_LINK.wait_first(page, timeout_ms=3_000)
        if here_link is not None:
            try:
                here_link.click()
            except Exception as exc:
                emit("auth", f"点击'此处'失败: {exc}")
                # 兜底：直接点警告元素本身
                try:
                    warning = SELECTOR_AUTH_WARNING.first_visible(page, timeout_ms=1_000)
                    if warning is not None:
                        warning.click()
                except Exception as exc2:
                    return make_failure(
                        "click_failed",
                        f"无法点击授权提示: {exc2}",
                        cdp_url=cdp_url,
                    )
        else:
            # 兜底：直接点警告元素本身
            try:
                warning = SELECTOR_AUTH_WARNING.first_visible(page, timeout_ms=1_000)
                if warning is not None:
                    warning.click()
            except Exception as exc:
                return make_failure(
                    "click_failed",
                    f"无法点击授权提示: {exc}",
                    cdp_url=cdp_url,
                )

        # 4. 等待授权对话框
        emit_progress(4, 5, "等待授权对话框...")
        dialog = SELECTOR_AUTH_DIALOG.wait_first(page, timeout_ms=10_000)
        if dialog is None:
            return make_failure(
                "dialog_timeout",
                "等待授权对话框超时（追加至已有委托）",
                cdp_url=cdp_url,
            )
        time.sleep(1.0)  # 给对话框动画一点时间

        # 5. 点击"确定"（追加至已有委托默认已选中）
        if not click_wait_first(page, SELECTOR_AUTH_CONFIRM, timeout_ms=5_000):
            return make_failure(
                "confirm_not_found",
                "未找到'确定'按钮",
                cdp_url=cdp_url,
            )

        # 6. 等待成功提示
        emit_progress(5, 5, "等待授权成功提示...")
        success = SELECTOR_AUTH_SUCCESS.wait_first(page, timeout_ms=15_000)
        if success is None:
            return make_failure(
                "success_timeout",
                "等待'权限更新成功'提示超时",
                cdp_url=cdp_url,
            )

        emit("auth", "授权更新成功")
        time.sleep(1.0)
        return make_success(
            "authorize",
            cdp_url=cdp_url,
            auth_done=True,
            skipped_reason=None,
            disclaimer_handled=disclaimer_handled,
        )
    except Exception as exc:
        return make_failure("exception", f"未预期错误: {exc}", cdp_url=cdp_url)
    finally:
        try:
            pw.stop()
        except Exception as e:
            logging.debug("停止 playwright 失败: %s", e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自动完成 MaaS 委托授权")
    parser.add_argument("--cdp-url", default="", help="CDP endpoint")
    parser.add_argument("--timeout", type=int, default=45, help="单步超时秒数")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    cdp_url = (args.cdp_url or "").strip() or resolve_cdp_url()
    if not cdp_url:
        result = make_failure("init", "未找到 CDP URL")
        output_json(result)
        return 1

    result = auto_authorize(cdp_url, timeout_s=args.timeout)
    output_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
