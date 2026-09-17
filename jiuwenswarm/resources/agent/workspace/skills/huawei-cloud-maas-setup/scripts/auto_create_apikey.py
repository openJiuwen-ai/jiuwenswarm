#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""自动创建华为云 MaaS API Key 并捕获完整 Key（V2 折中方案）。

前置条件：
- 浏览器已启动并通过 CDP 暴露
- 用户已在浏览器中登录华为云
- MaaS 委托授权已完成（由 ``auto_authorize.py`` 处理）

本脚本职责：
- 导航到 API Key 管理页
- 点击"创建 API Key"按钮
- 正确填写表单：标签 + 描述 + 权限设置（选择"全部"）
- 提交后检测是否欠费，如欠费返回 ``stage=insufficient_balance``
- 从结果弹窗提取完整 Key（仅显示一次）
- 关闭弹窗

**关键约束**：
- **不抛异常给上层**：失败时返回 ``{ok:false, stage:..., error:...}``
- **三级 Key 提取策略**：input[readonly] -> code/pre 元素 -> 正则匹配弹窗文本
- **欠费检测**：提交后如果出现"欠费"/"余额不足"提示，返回特定 stage
- 整个流程 ≤ 30s

用法::

    python auto_create_apikey.py --cdp-url http://127.0.0.1:9333 --json \\
        --tag "jiuwenswarm" \\
        --description "jiuwenswarm-config"
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone
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
    MAAS_APIKEY_URL,
    SELECTOR_APIKEY_CONFIRM,
    SELECTOR_APIKEY_DESC_INPUT,
    SELECTOR_APIKEY_DIALOG,
    SELECTOR_APIKEY_PERMISSION,
    SELECTOR_APIKEY_SAVED_BTN,
    SELECTOR_APIKEY_TAG_INPUT,
    SELECTOR_CREATE_APIKEY_BTN,
    SELECTOR_INSUFFICIENT_BALANCE,
    SELECTOR_REALNAME_REQUIRED,
    click_copy_key_button,
    click_wait_first,
    extract_api_key_from_dialog,
)


_API_KEY_RE = re.compile(r"\b[A-Za-z0-9_\-]{30,}\b")


def _unique_suffix() -> str:
    """生成基于当前日期时间的唯一后缀（``YYYYMMDD_HHMMSS``）。

    追加到标签和描述末尾，避免标签重复导致华为云拒绝创建 API Key。
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")


def _is_plausible_key(text: str) -> bool:
    """判定文本是否像 API Key。"""
    return bool(text) and len(text) >= 30 and not text.isdigit()


def _detect_insufficient_balance(page, timeout_ms: int = 3_000,
                                 include_body: bool = True) -> bool:
    """检测页面是否出现欠费/余额不足的错误提示。

    在点击"确定"创建 Key 后调用，如果出现错误提示且含欠费关键词，
    返回 True 表示用户需要先充值。
    """
    # 先看页面是否有错误提示元素（轮询场景下不额外 sleep，交给调用方节奏）
    error_el = SELECTOR_INSUFFICIENT_BALANCE.first_visible(page, timeout_ms=timeout_ms)
    if error_el is not None:
        try:
            error_text = error_el.inner_text(timeout=1_000) or ""
            for keyword in ("欠费", "余额不足", "请先充值", "insufficient", "冻结", "资源不足"):
                if keyword in error_text:
                    emit("apikey", f"检测到欠费提示: {error_text[:100]}")
                    return True
        except Exception as e:
            logging.warning(f"读取欠费提示元素文本失败: {e}")
    # 兜底：检查整页文本（开销较大，轮询时可关闭）
    if include_body:
        try:
            body_text = page.locator("body").inner_text(timeout=2_000) or ""
            for keyword in ("欠费", "余额不足", "请先充值"):
                if keyword in body_text:
                    emit("apikey", f"页面文本中检测到欠费关键词: {keyword}")
                    return True
        except Exception as e:
            logging.warning(f"读取页面文本检测欠费失败: {e}")
    return False


def _detect_realname_required(page, timeout_ms: int = 3_000,
                              include_body: bool = True) -> bool:
    """检测页面是否出现未实名认证的错误提示。

    在点击"确定"创建 Key 后调用，如果出现错误提示且含实名认证关键词，
    返回 True 表示用户需要先完成实名认证。
    """
    error_el = SELECTOR_REALNAME_REQUIRED.first_visible(page, timeout_ms=timeout_ms)
    if error_el is not None:
        try:
            error_text = error_el.inner_text(timeout=1_000) or ""
            for keyword in ("real-name authentication", "Cloud services require",
                            "实名认证"):
                if keyword in error_text:
                    emit("apikey", f"检测到未实名认证提示: {error_text[:100]}")
                    return True
        except Exception as e:
            logging.warning(f"读取未实名认证提示元素文本失败: {e}")
    if include_body:
        try:
            body_text = page.locator("body").inner_text(timeout=2_000) or ""
            for keyword in ("real-name authentication",
                            "Cloud services require real-name", "实名认证"):
                if keyword in body_text:
                    emit("apikey", f"页面文本中检测到未实名认证关键词: {keyword}")
                    return True
        except Exception as e:
            logging.warning(f"读取页面文本检测未实名认证失败: {e}")
    return False


def _select_permission_all(page) -> bool:
    """选择 API Key 权限范围为"全部"。

    华为云客服建议：权限范围选择"全部"便于访问全部模型。
    权限字段可能是下拉选择（Ti3 select）或单选按钮组，尝试多种方式选择"全部"。
    失败时返回 False，由调用方决定是否降级。
    """
    # 策略1：弹窗内直接查找"全部"可点击元素（单选按钮或已展开的选项）
    for sel in (
        ".ti3-modal [class*='radio']:has-text('全部')",
        ".ti3-modal label:has-text('全部')",
        ".ti3-modal [class*='option']:has-text('全部')",
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                loc.click(timeout=2_000)
                emit("apikey", "已选择权限范围: 全部")
                return True
        except Exception as e:
            logging.warning(f"选择权限范围失败: {e}")
            continue

    # 策略2：点击权限下拉框触发器，打开选项面板后再选"全部"
    permission = SELECTOR_APIKEY_PERMISSION.first_visible(page, timeout_ms=1_000)
    if permission is not None:
        try:
            permission.click(timeout=2_000)
            time.sleep(0.5)
            # Ti3 select 下拉面板可能挂载在 body 下（不在 .ti3-modal 内）
            for sel in (
                "[class*='select-option']:has-text('全部')",
                "[class*='select-panel'] li:has-text('全部')",
                "ti3-select-option:has-text('全部')",
            ):
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible():
                        loc.click(timeout=2_000)
                        emit("apikey", "已通过下拉选择权限范围: 全部")
                        return True
                except Exception as e:
                    logging.warning(f"下拉选择权限范围失败: {e}")
                    continue
        except Exception as exc:
            emit("apikey", f"点击权限下拉框失败: {exc}")

    return False


def _page_text_preview(page, max_chars: int = 1500) -> str:
    """截取当前页面可读文本，用于失败时返回 DOM 线索以便定位。"""
    try:
        text = page.locator("body").inner_text(timeout=2_000) or ""
    except Exception:
        return ""
    return " ".join(text.split())[:max_chars]


def auto_create_apikey(
    cdp_url: str,
    tag: str = "jiuwenswarm",
    description: str = "jiuwenswarm-config",
    timeout_s: int = 30,
) -> dict:
    """在 API Key 管理页创建新 Key 并捕获完整 Key。

    表单字段：
    - 标签（tag）：1-100 字符，支持大小写英文字母、数字、下划线、中划线
    - 描述（description）：自由文本
    - 权限设置（permission）：选择"全部"权限范围，便于访问全部模型

    为避免标签重复导致创建失败，会自动在 ``tag`` 和 ``description`` 末尾
    追加日期时间后缀（如 ``-20250816_153045``）。

    返回值：
    - 成功：``{ok: true, api_key: "...", ...}``
    - 欠费：``{ok: false, stage: "insufficient_balance", error: "..."}`
    - 其他失败：``{ok: false, stage: "...", error: "..."}``
    """
    # 追加日期时间后缀，避免标签/描述重复导致创建 Key 失败
    suffix = _unique_suffix()
    tag = f"{tag}-{suffix}"
    description = f"{description}-{suffix}"
    emit("apikey", f"本次创建标签: {tag}，描述: {description}")

    emit_progress(0, 7, "连接浏览器...")
    try:
        pw, browser, page = connect_page(cdp_url, timeout_ms=15_000)
    except Exception as exc:
        return make_failure("connect_failed", f"无法连接浏览器: {exc}", cdp_url=cdp_url)

    try:
        # 1. 导航到 API Key 管理页
        emit_progress(1, 7, f"导航到 {MAAS_APIKEY_URL}")
        try:
            page.goto(MAAS_APIKEY_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            return make_failure("navigate_failed", f"导航失败: {exc}", cdp_url=cdp_url)

        # 2. 点击"创建 API Key"按钮
        # 按钮为 Angular SPA 异步渲染，等待 15s（组合等待：存在且可见）
        emit_progress(2, 7, "点击'创建 API Key'...")
        if not click_wait_first(page, SELECTOR_CREATE_APIKEY_BTN, timeout_ms=15_000):
            preview = _page_text_preview(page)
            emit("apikey", f"未找到创建按钮，当前页面: {page.url} | {preview}")
            return make_failure(
                "create_btn_not_found",
                "未找到'创建 API Key'按钮",
                cdp_url=cdp_url,
                page_url=page.url,
                page_text_preview=preview,
            )
        time.sleep(1.0)  # 等待对话框动画

        # 3. 填写标签（第一个字段，必填）
        emit_progress(3, 7, f"填写标签: {tag}")
        tag_input = SELECTOR_APIKEY_TAG_INPUT.wait_first(page, timeout_ms=4_000)
        if tag_input is not None:
            try:
                tag_input.fill(tag)
            except Exception as exc:
                emit("apikey", f"填写标签失败: {exc}")
                # 标签填写失败不中断，尝试用 type
                try:
                    tag_input.click()
                    page.keyboard.type(tag)
                except Exception as e:
                    logging.warning(f"键盘输入标签失败: {e}")
        else:
            emit("apikey", "未找到标签输入框，跳过")

        # 4. 填写描述（第二个字段）
        emit_progress(4, 7, f"填写描述: {description}")
        desc_input = SELECTOR_APIKEY_DESC_INPUT.wait_first(page, timeout_ms=4_000)
        if desc_input is not None:
            try:
                desc_input.fill(description)
            except Exception as exc:
                emit("apikey", f"填写描述失败（可忽略）: {exc}")

        # 5. 权限设置：选择"全部"权限范围
        # 华为云客服建议：权限范围选择"全部"便于访问全部模型
        if not _select_permission_all(page):
            emit("apikey", "未能选择'全部'权限范围，保持默认值（如默认为全部则无影响）")

        # 6. 点击"确定"提交
        emit_progress(5, 7, "提交创建...")
        if not click_wait_first(page, SELECTOR_APIKEY_CONFIRM, timeout_ms=5_000):
            return make_failure(
                "confirm_not_found",
                "未找到 API Key 创建'确定'按钮",
                cdp_url=cdp_url,
            )

        # 7. 欠费检测 + Key 提取
        # 提交后改为短轮询：一旦出现 Key 展示弹窗（copy-key-modal）即返回，
        # 不必固定 sleep(2s) 再等 12s，避免"Key 已创建但脚本仍空等"的卡顿。
        emit_progress(6, 7, "检测创建结果...")
        poll_deadline = time.time() + 14.0
        dialog_ok = False
        while time.time() < poll_deadline:
            # 7a. 优先精确检测 copy-key-modal（Key 已创建成功）
            if SELECTOR_APIKEY_DIALOG.first_visible(page, timeout_ms=500) is not None:
                dialog_ok = True
                break
            # 7b. 每次轮询顺带检测欠费提示和未实名认证提示（跳过整页文本扫描）
            if _detect_insufficient_balance(page, timeout_ms=500,
                                             include_body=False):
                return make_failure(
                    "insufficient_balance",
                    "账户余额不足，请先充值后再创建 API Key",
                    cdp_url=cdp_url,
                    recharge_url="https://account.huaweicloud.com/usercenter/#/accountindex/balance",
                )
            if _detect_realname_required(page, timeout_ms=500,
                                          include_body=False):
                return make_failure(
                    "realname_required",
                    "账号未完成实名认证，请先完成实名认证后再创建 API Key",
                    cdp_url=cdp_url,
                    realname_url=HUAWEI_REALNAME_AUTH_URL,
                )
            time.sleep(0.5)

        if not dialog_ok:
            return make_failure(
                "dialog_timeout",
                "等待 API Key 弹窗超时（可能创建失败）",
                cdp_url=cdp_url,
            )
        time.sleep(0.6)

        # 7c. 提取 Key（三级策略）
        api_key = extract_api_key_from_dialog(page)
        if not api_key or not _is_plausible_key(api_key):
            return make_failure(
                "extract_failed",
                "无法从页面提取 API Key。请手动复制 Key 后通过 ask_user 填入。",
                cdp_url=cdp_url,
            )

        emit("apikey", f"捕获 API Key 成功（长度 {len(api_key)}）")
        emit_progress(7, 7, "Key 捕获成功")

        # 8. best-effort 复制 Key，再点击"我已保存，确认关闭"关闭弹窗
        if click_copy_key_button(page):
            emit("apikey", "已点击复制按钮")
        dialog_closed = click_wait_first(page, SELECTOR_APIKEY_SAVED_BTN, timeout_ms=5_000)
        if dialog_closed:
            emit("apikey", "已点击'我已保存，确认关闭'")
        else:
            emit("apikey", "警告: 未找到'我已保存，确认关闭'按钮，弹窗可能未关闭")
        time.sleep(0.5)

        return make_success(
            "apikey",
            cdp_url=cdp_url,
            api_key=api_key,
            tag=tag,
            description=description,
            dialog_closed=dialog_closed,
        )
    except Exception as exc:
        return make_failure("exception", f"未预期错误: {exc}", cdp_url=cdp_url)
    finally:
        try:
            pw.stop()
        except Exception as e:
            logging.warning(f"停止 playwright 失败: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自动创建并捕获 API Key")
    parser.add_argument("--cdp-url", default="", help="CDP endpoint")
    parser.add_argument("--tag", default="jiuwenswarm", help="API Key 标签（1-100 字符）")
    parser.add_argument(
        "--description",
        default="jiuwenswarm-config",
        help="API Key 描述",
    )
    parser.add_argument("--timeout", type=int, default=30, help="单步超时秒数")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    cdp_url = (args.cdp_url or "").strip() or resolve_cdp_url()
    if not cdp_url:
        result = make_failure("init", "未找到 CDP URL")
        output_json(result)
        return 1

    result = auto_create_apikey(
        cdp_url=cdp_url,
        tag=args.tag,
        description=args.description,
        timeout_s=args.timeout,
    )
    output_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
