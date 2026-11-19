#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""自动开通华为云 MaaS 预置服务（文本生成）模型。

前置条件：
- 浏览器已启动并通过 CDP 暴露
- 用户已登录华为云
- API Key 已创建（由 ``auto_create_apikey.py`` 处理）

本脚本职责：
- 导航到预置服务页
- 搜索第一个目标模型并点击其"开通服务"按钮，打开批量订阅弹窗
- 在弹窗中一次性勾选所有目标模型，勾选同意声明并点击"一键开通"
- 已开通的模型记入 ``already_opened``，不重复点击
- 返回批量开通结果

**关键约束**：
- **不抛异常给上层**：失败时返回 ``{ok:false, stage:..., error:...}``
- 仅开通类型为【文本生成】的模型（可通过 --type-filter 调整）
- 整个流程 ≤ 90s

用法::

    python auto_open_model.py --cdp-url http://127.0.0.1:9333 --json \\
        --model "GLM-5.2" \\
        --model "DeepSeek-V4-Flash"
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
    emit,
    emit_progress,
    output_json,
    resolve_cdp_url,
)
from lib.flow_state import make_failure, make_success  # noqa: E402
from lib.huawei_selectors import (  # noqa: E402
    MAAS_DEPLOYMENT_ROW_NAME,
    MAAS_DEPLOYMENT_ROW_OPEN_BTN,
    MAAS_DEPLOYMENT_ROW_STATUS,
    MAAS_DEPLOYMENT_ROW_TYPE,
    MAAS_DEPLOYMENT_ROWS,
    MAAS_DEPLOYMENT_SEARCH_BOX,
    MAAS_DEPLOYMENT_URL,
)

_DEFAULT_MODELS_FILE = str(Path(__file__).resolve().parent.parent / "models.json")

# 弹窗容器候选：优先精确匹配 Ti3 开通弹窗（subscribe-model-modal / #subScribe-model），
# .ti3-modal 作为兜底（仍需内容信号校验，避免满意度评价等浮层被误判为开通弹窗）。
_DIALOG_CSS = (
    "subscribe-model-modal, #subScribe-model",
    ".ti3-modal",
)
# 滚动定位时使用的容器选择器
_DIALOG_SELECTOR_JS = "subscribe-model-modal, #subScribe-model, .ti3-modal"
# 弹窗内容信号：检测到这些文本即视为弹窗已渲染（容器类名兜底）
_DIALOG_SIGNAL_TEXTS = ("一键开通", "我已阅读并同意", "开通预置模型服务")
# 干扰浮层关键词：命中即尝试自动关闭（华为云"满意度评价"等弹框），
# 避免其遮挡"开通服务"按钮或被 _wait_for_dialog 误判为目标弹窗。
_DISTRACTOR_KEYWORDS = ("满意度评价", "诚邀您", "满意度调查")
# 订阅弹窗关闭按钮：精确匹配 id + Ti3 通用 close 类
_CLOSE_BTN_CSS = "#subScribe-model_close, .ti3-modal-close, .ti3-icon-close"


def _dismiss_satisfaction_popup(page, timeout_s: float = 3.0) -> bool:
    """自动关闭华为云"满意度评价"等干扰浮层，返回是否执行过关闭动作。

    华为云控制台会不定时弹出满意度评价浮框（文案如"诚邀您对 MaaS 模型即服务
    进行满意度评价"），它不属于开通弹窗，却会：
      - 遮挡页面右侧"开通服务"操作列（fixed-right），导致点击超时；
      - 被 _wait_for_dialog 的 .ti3-modal 选择器误命中，判定"弹窗已出现"，
        随后脚本在错误的浮层里找不到模型/同意勾选/订阅按钮，白白空转。

    做法：
      1. 先按关键词检测干扰浮层（避免误伤真正的开通弹窗）；
      2. 在可见的 .ti3-modal 中定位命中关键词的浮层；
      3. 点击其关闭按钮（.ti3-modal-close / #subScribe-model_close）；
      4. 兜底按 Escape 关闭。
    """
    try:
        body_text = page.locator("body").inner_text(timeout=800) or ""
    except Exception:
        body_text = ""
    if not any(k in body_text for k in _DISTRACTOR_KEYWORDS):
        return False

    # 在可见的 Ti3 modal 中查找命中关键词的干扰浮层
    try:
        modals = page.locator(".ti3-modal")
        count = min(modals.count(), 5)
        for i in range(count):
            modal = modals.nth(i)
            try:
                if not modal.is_visible(timeout=500):
                    continue
                text = modal.inner_text(timeout=800) or ""
            except Exception as e:
                logging.debug("读取 modal 文本失败: %s", e)
                continue
            if not any(k in text for k in _DISTRACTOR_KEYWORDS):
                continue
            # 找到干扰浮层，优先点其关闭按钮
            closed = False
            for close_sel in (
                ".ti3-modal-close",
                "#subScribe-model_close",
                ".ti3-icon-close",
            ):
                try:
                    btn = modal.locator(close_sel).first
                    if btn.is_visible(timeout=500):
                        btn.click(timeout=1_500)
                        closed = True
                        break
                except Exception as e:
                    logging.debug("点击关闭按钮失败: %s", e)
                    continue
            if not closed:
                try:
                    modal.press("Escape")
                    closed = True
                except Exception as e:
                    logging.debug("按 Escape 关闭干扰浮层失败: %s", e)
            time.sleep(0.5)
            emit("model", "已自动关闭干扰浮层（满意度评价等）")
            return closed
    except Exception as e:
        logging.debug("关闭干扰浮层异常: %s", e)
    # 兜底：直接按 Escape
    try:
        page.keyboard.press("Escape")
        time.sleep(0.4)
        return True
    except Exception:
        return False


def _is_distractor_dialog(cand_text: str) -> bool:
    """判断弹窗候选文本是否命中干扰关键词（即并非开通弹窗）。"""
    return any(k in cand_text for k in _DISTRACTOR_KEYWORDS)


def _wait_for_dialog(page, timeout_s: float = 15.0, poll_s: float = 0.8):
    """等待开通弹窗出现。

    1. 优先按精确容器选择器（subscribe-model-modal / #subScribe-model）匹配，
       .ti3-modal 作为兜底；命中后还需校验内容不包含干扰关键词；
    2. 若容器均未命中，用内容信号（"一键开通"/"我已阅读并同意" 文本）兜底，
       返回首个可见的 .ti3-modal 作为操作范围；
    3. 遇到干扰浮层（满意度评价等）时尝试关闭一次，然后继续等待目标弹窗；
    4. 均未命中时返回 None。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for css in _DIALOG_CSS:
            try:
                loc = page.locator(css).first
                if loc.is_visible(timeout=int(poll_s * 1000)):
                    try:
                        cand_text = loc.inner_text(timeout=800) or ""
                    except Exception:
                        cand_text = ""
                    if not _is_distractor_dialog(cand_text):
                        return loc
                    # 命中的是干扰浮层 -> 关闭后继续等待
                    _close_dialog(page, loc)
                    continue
            except Exception as e:
                logging.debug("等待弹窗选择器失败: %s", e)
                continue
        # 内容信号兜底（容器类名未渲染但内容已出现）
        try:
            signal = page.locator(
                ", ".join(f"text={t}" for t in _DIALOG_SIGNAL_TEXTS)
            ).first
            if signal.is_visible(timeout=int(poll_s * 1000)):
                emit("model", "检测到弹窗内容信号（已渲染弹窗内容）")
                # 返回首个可见的 Ti3 modal 容器，而非整页 body
                modal = page.locator(".ti3-modal:visible").first
                if modal.count() > 0:
                    return modal
                return page.locator("body").first
        except Exception as e:
            logging.debug("内容信号兜底检测失败: %s", e)
        time.sleep(poll_s)
    return None


def _close_dialog(page, dialog) -> bool:
    """尽力关闭当前弹窗（点击关闭按钮，失败按 Escape），返回是否已关闭。"""
    if dialog is not None:
        try:
            btn = dialog.locator(_CLOSE_BTN_CSS).first
            if btn.is_visible(timeout=1_000):
                btn.click(timeout=2_000)
                time.sleep(0.5)
                return True
        except Exception as e:
            logging.debug("点击弹窗关闭按钮失败: %s", e)
    try:
        page.keyboard.press("Escape")
        time.sleep(0.5)
        return True
    except Exception:
        return False


def _load_display_names(models_file: str) -> list[str]:
    """从 models.json 读取模型 display_name 列表。"""
    p = Path(models_file)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [
        m["display_name"]
        for m in models
        if isinstance(m, dict) and m.get("display_name")
    ]


def _is_checked(page, input_selector: str) -> bool:
    """查询原生 checkbox 是否勾选。"""
    try:
        return bool(page.locator(input_selector).first.is_checked())
    except Exception:
        return False


def _ensure_agreement_checked(page) -> bool:
    """勾选'我已阅读并同意…《MaaS 模型即服务声明》'（input#agreement）。

    华为云 Ti3 结构：``input#agreement`` + ``label#agreement_checkbox``（for=agreement），
    真实文案在 ``label.agreement-label`` 中。优先点 label，失败再点 input。
    返回最终是否勾选。
    """
    try:
        if _is_checked(page, "#agreement"):
            return True
        label = page.locator("#agreement_checkbox").first
        if label.count() > 0 and label.is_visible():
            label.click(timeout=3_000)
        else:
            page.locator("#agreement").first.click(timeout=3_000)
        time.sleep(0.3)
        return _is_checked(page, "#agreement")
    except Exception as exc:
        emit("model", f"勾选同意声明失败: {exc}")
        return _is_checked(page, "#agreement")


def _page_text_preview(page, max_chars: int = 1500) -> str:
    """截取当前页面可读文本，用于失败时返回 DOM 线索以便定位。"""
    try:
        text = page.locator("body").inner_text(timeout=2_000) or ""
    except Exception:
        return ""
    return " ".join(text.split())[:max_chars]


def _scroll_dialog_down(page) -> None:
    """向下滚动弹窗内容区，用于查找列表深处的勾选项。"""
    try:
        page.evaluate(
            """(dialogSelector) => {
                const dialog = document.querySelector(dialogSelector);
                if (!dialog) return false;
                const scrollable = dialog.querySelector(
                    '.ti3-modal-body, ti-modal-body, .ti3-scrollbar-container, '
                    + '[class*="scroll"], [class*="content"], [class*="body"]'
                ) || dialog;
                scrollable.scrollTop += 300;
                return true;
            }""",
            _DIALOG_SELECTOR_JS,
        )
        time.sleep(0.4)
    except Exception as e:
        logging.debug("滚动弹窗失败: %s", e)


def _is_single_model_dialog(fragment: str, model_name: str) -> bool:
    """判断弹窗是否为单模型弹窗（含一键开通与模型名，且无批量勾选特征）。"""
    if "一键开通" not in fragment:
        return False
    if model_name not in fragment:
        return False
    if "全选模型服务" in fragment:
        return False
    if "全选" in fragment:
        return False
    if "commercialServiceList" in fragment:
        return False
    return True


def _ensure_model_checked(page, dialog, model_name: str,
                          max_scrolls: int = 12) -> bool:
    """确保开通弹窗中目标模型处于勾选状态。

    弹窗可能是批量勾选列表（含多模型 checkbox），也可能是单模型弹窗。
    - 批量列表：滚动查找并勾选该模型；
    - 单模型弹窗：弹窗正文包含模型名且存在"一键开通"即视为无需勾选；
    返回 True 表示已就绪，False 表示无法确认。

    **短路策略（真实 DOM 2025.08 结构）**：Ti3 开通弹窗内 checkbox 为
    ``span.ti3-checkbox-group-item > input + label.ti3-checkbox``（二者平级，
    服务名在 ``label.service-list`` 内）。若在弹窗中找不到命名的 checkbox，
    或 checkbox 为 disabled 态，立即返回 False，不空转滚动。
    """
    # 短路 1：弹窗正文缺少"开通预置模型服务"/"一键开通"信号 -> 非开通弹窗
    try:
        dialog_fragment = dialog.inner_text(timeout=1_000) or ""
    except Exception:
        dialog_fragment = ""
    _has_open_signal = False
    for _signal_text in ("开通预置模型服务", "一键开通", "我已阅读并同意"):
        if _signal_text in dialog_fragment:
            _has_open_signal = True
            break
    if not _has_open_signal:
        emit("model", f"{model_name} 弹窗缺少开通信号，视为非开通弹窗")
        return False
    # 短路 2：单模型弹窗（弹窗正文含模型名 + "一键开通"按钮，且无 checkbox 勾选列表）
    # 注意：批量列表弹窗正文同样含"一键开通"和模型名，必须再排除"全选模型服务"
    # 等 checkbox 组特征，避免把需逐个勾选的批量弹窗误判为单模型弹窗而跳过勾选。
    if _is_single_model_dialog(dialog_fragment, model_name):
        emit("model", f"{model_name} 弹窗正文含模型名与一键开通，视为单模型弹窗")
        return True

    for _ in range(max_scrolls):
        try:
            # Ti3 真实结构: span.ti3-checkbox-group-item > input + label.ti3-checkbox
            #   label.ti3-checkbox > ... > label.service-list(模型名)
            group = dialog.locator(
                f"span.ti3-checkbox-group-item:has-text('{model_name}')"
            ).first
            if group.is_visible(timeout=1_200):
                # 读取 checkbox 状态（input 与 label 平级）
                inp = group.locator("input[type='checkbox']").first
                try:
                    if inp.count() > 0:
                        if inp.is_checked():
                            emit("model", f"{model_name} 已勾选，跳过")
                            return True
                        if inp.is_disabled():
                            emit("model", f"{model_name} 的 checkbox 为禁用态，无法勾选")
                            return False
                except Exception as e:
                    logging.debug("读取 checkbox 状态失败: %s", e)
                # 点击 checkbox 的行元素（label.ti3-checkbox），避免点 input 抖动
                row_label = group.locator("label.ti3-checkbox").first
                try:
                    if row_label.count() > 0 and row_label.is_visible(timeout=800):
                        row_label.click(timeout=1_500)
                    else:
                        group.click(timeout=1_500)
                except Exception:
                    group.click(timeout=1_500)
                time.sleep(0.3)
                return True
        except Exception as e:
            logging.debug("定位模型勾选项失败: %s", e)
        _scroll_dialog_down(page)

    return False


def _wait_table_ready(page, timeout_s: float = 15.0) -> bool:
    """等待预置服务表格出现数据行。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            row = page.locator(MAAS_DEPLOYMENT_ROWS).first
            if row.is_visible(timeout=1_500):
                return True
        except Exception as e:
            logging.debug("等待表格数据行失败: %s", e)
        time.sleep(0.8)
    return False


def _search_model(page, model_name: str) -> None:
    """通过顶部搜索框按名称筛选预置服务列表。"""
    try:
        box = page.locator(MAAS_DEPLOYMENT_SEARCH_BOX).first
        box.fill(model_name)
        box.press("Enter")
        time.sleep(1.2)
    except Exception as exc:
        emit("model", f"搜索框操作失败（改为直接在列表中查找）: {exc}")


def _row_name_text(row) -> str:
    """读取行内服务名称。"""
    try:
        el = row.locator(MAAS_DEPLOYMENT_ROW_NAME).first
        return (el.inner_text(timeout=1_500) or "").strip()
    except Exception:
        return ""


def _row_status_text(row) -> str:
    """读取行内状态（未开通 / 已开通 / 开通中）。"""
    try:
        el = row.locator(MAAS_DEPLOYMENT_ROW_STATUS).first
        return (el.inner_text(timeout=1_500) or "").strip()
    except Exception:
        return ""


def _row_type_text(row) -> str:
    """读取行内"类型"列文本（如 文本生成 / 视频生成）。

    类型列是第 4 个 td（按表头顺序：展开图标/服务名称/状态/类型/...），
    内部使用 grid-tip-text-render 组件渲染。
    """
    try:
        cell = row.locator("td:nth-child(4)").first
        # 优先读取 grid-tip-text-render 内的文本，兜底读取整个单元格
        el = cell.locator(MAAS_DEPLOYMENT_ROW_TYPE).first
        if el.count() > 0:
            return " ".join((el.inner_text(timeout=1_500) or "").split())
        return " ".join((cell.inner_text(timeout=1_500) or "").split())
    except Exception:
        return ""


def _row_open_button(row):
    """返回行内右侧"开通服务"按钮 Locator，不存在则返回 None。

    Ti3 操作列结构：``ti-actionmenu#ActiveServiceMenu >
    span.ti3-action-menu-item[aria-label='开通服务'] > ti-link > section(开通服务)``。
    已开通行此处显示"关闭服务"（id=closeServiceMenu），无"开通服务"按钮。
    """
    try:
        op_cell = row.locator("td.ti3-table-column-fixed-right").first
        if op_cell.count() == 0:
            return None
        btn = op_cell.locator(
            f"{MAAS_DEPLOYMENT_ROW_OPEN_BTN}"
        ).first
        if btn.is_visible(timeout=1_500):
            return btn
    except Exception as e:
        logging.debug("查找开通服务按钮失败: %s", e)
    return None


def _scan_rows_for_name(page, model_name: str):
    """在当前渲染行中查找名称精确匹配的行，返回 Locator 或 None。"""
    try:
        rows = page.locator(MAAS_DEPLOYMENT_ROWS)
        count = rows.count()
        for i in range(count):
            row = rows.nth(i)
            if _row_name_text(row) == model_name:
                return row
    except Exception as exc:
        emit("model", f"扫描行失败: {exc}")
    return None


def _find_model_row(page, model_name: str):
    """按名称定位目标模型行。

    1. 先通过搜索框按名称筛选；
    2. 未命中时清空搜索并扫描当前页；
    3. 均未命中返回 None。
    """
    _search_model(page, model_name)
    row = _scan_rows_for_name(page, model_name)
    if row is not None:
        return row
    # 兜底：刷新页面恢复完整列表，扫描当前第一页
    try:
        page.reload(wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        logging.debug("刷新页面失败: %s", e)
    if not _wait_table_ready(page):
        return None
    return _scan_rows_for_name(page, model_name)


def _row_status(row) -> tuple[str, str]:
    """返回 (类型文本, 状态文本) 供校验使用。"""
    return _row_type_text(row), _row_status_text(row)


def auto_open_models(
    cdp_url: str,
    models: list[str],
    type_filter: str = "文本生成",
    timeout_s: int = 90,
) -> dict:
    """批量开通"文本生成"类型的预置服务模型。

    优化策略：只需搜索第一个目标模型并点击其"开通服务"按钮，
    在弹出的批量订阅弹窗中一次性勾选所有目标模型，然后点击一次"一键开通"。
    避免逐模型搜索+开通的冗余操作。
    """
    total = len(models)
    steps = total + 5
    emit_progress(0, steps, "连接浏览器...")
    try:
        pw, browser, page = connect_page(cdp_url, timeout_ms=15_000)
    except Exception as exc:
        return make_failure("connect_failed", f"无法连接浏览器: {exc}",
                            cdp_url=cdp_url)

    opened: list[str] = []
    already_opened: list[str] = []
    failed: list[dict] = []
    try:
        # 1. 导航到预置服务页
        emit_progress(1, steps, f"导航到 {MAAS_DEPLOYMENT_URL}")
        try:
            page.goto(MAAS_DEPLOYMENT_URL, wait_until="domcontentloaded",
                      timeout=30_000)
            time.sleep(3.0)  # 等待 SPA 异步渲染完成
        except Exception as exc:
            return make_failure("navigate_failed", f"导航失败: {exc}",
                                cdp_url=cdp_url)
        if not _wait_table_ready(page):
            return make_failure(
                "table_timeout",
                "等待预置服务列表加载超时：未出现数据行（可能未登录或页面异常）",
                cdp_url=cdp_url,
                page_text_preview=_page_text_preview(page),
            )
        # 清理满意度评价等干扰浮层，避免遮挡"开通服务"按钮
        _dismiss_satisfaction_popup(page)

        # 2. 搜索第一个目标模型，点击其"开通服务"按钮打开批量订阅弹窗。
        #    若第一个模型已开通（无"开通服务"按钮），则依次尝试下一个，
        #    直到找到可打开的入口或全部已开通/失败。
        dialog = None
        for model_name in models:
            emit("model", f"搜索模型: {model_name}")
            row = _find_model_row(page, model_name)
            if row is None:
                failed.append({
                    "model": model_name,
                    "error": "在预置服务列表中未找到该模型（可能不支持开通或名称不同）",
                })
                continue

            type_text, status_text = _row_status(row)
            if type_text != type_filter:
                failed.append({
                    "model": model_name,
                    "error": f"该服务类型为 {type_text or '未知'}，"
                             f"仅开通{type_filter}，跳过",
                })
                emit("model", f"{model_name} 类型为 {type_text}，跳过")
                continue

            open_btn = _row_open_button(row)
            if open_btn is None:
                already_opened.append(model_name)
                emit("model",
                     f"{model_name} 无'开通服务'按钮"
                     f"（状态: {status_text or '未知'}），视为已开通")
                continue

            # 找到可开通的模型，点击"开通服务"打开批量订阅弹窗
            _dismiss_satisfaction_popup(page)
            emit("model", f"点击 {model_name} 的'开通服务'")
            try:
                open_btn.click(timeout=8_000)
                time.sleep(1.5)  # 等待弹窗出现
            except Exception as exc:
                failed.append({
                    "model": model_name,
                    "error": f"点击'开通服务'失败: {exc}",
                })
                continue

            dialog = _wait_for_dialog(page)
            if dialog is None:
                failed.append({
                    "model": model_name,
                    "error": "等待开通弹窗超时",
                })
                continue

            emit("model", f"已通过 {model_name} 打开批量订阅弹窗")
            break

        # 3. 在批量弹窗中勾选所有待开通模型，然后一次性点击"一键开通"
        if dialog is not None:
            emit_progress(2, steps, "在弹窗中批量勾选目标模型…")
            time.sleep(0.8)

            # 待开通模型 = 全部模型中尚未标记为已开通/失败的
            failed_names = {f["model"] for f in failed}
            pending = [
                m for m in models
                if m not in already_opened and m not in failed_names
            ]

            # 在弹窗中确保每个待开通模型已勾选
            checked_models: list[str] = []
            for model_name in pending:
                if _ensure_model_checked(page, dialog, model_name):
                    checked_models.append(model_name)
                else:
                    emit("model", f"警告: 未能在弹窗中勾选 {model_name}")

            # 4. 勾选同意声明并点击"一键开通"
            emit_progress(3, steps, "勾选同意声明并开通…")
            emit("model", "勾选同意声明...")
            if not _ensure_agreement_checked(page):
                emit("model", "警告: 未能勾选同意声明，'一键开通'可能保持禁用")
            try:
                confirm_btn = page.locator(
                    "#subscribe-button, [data-qa-id='subscribe-button']"
                ).first
                confirm_btn.wait_for(state="visible", timeout=8_000)
                if confirm_btn.is_disabled():
                    emit("model", "'一键开通'仍为禁用态，等待启用...")
                    deadline_wait = time.time() + 8
                    while time.time() < deadline_wait and confirm_btn.is_disabled():
                        time.sleep(0.5)
                if confirm_btn.is_disabled():
                    for model_name in pending:
                        failed.append({
                            "model": model_name,
                            "error": "'一键开通'按钮禁用："
                                     "模型未勾选或同意声明未勾选",
                        })
                    _close_dialog(page, dialog)
                else:
                    confirm_btn.click()
                    emit("model", "已点击'一键开通'")

                    # 5. 等待开通完成（弹窗关闭 / 成功提示）
                    emit("model", "等待开通完成...")
                    emit_progress(4, steps, "等待开通完成…")
                    deadline = time.time() + min(30, timeout_s)
                    success = False
                    while time.time() < deadline:
                        time.sleep(1.5)
                        try:
                            if page.locator("#subscribe-button").count() == 0:
                                success = True
                                break
                        except Exception as e:
                            logging.debug("检测订阅按钮消失失败: %s", e)
                        try:
                            if not dialog.is_visible(timeout=1_000):
                                success = True
                                break
                        except Exception:
                            success = True
                            break
                        try:
                            success_msg = page.locator(
                                ".ti3-message-success, .ti3-toast-success, "
                                "text=开通成功, text=开通完成"
                            ).first
                            if success_msg.is_visible(timeout=800):
                                success = True
                                break
                        except Exception as e:
                            logging.debug("检测开通成功提示失败: %s", e)

                    if success:
                        # 弹窗关闭 ≠ 开通生效，回表格逐个验证状态
                        _close_dialog(page, dialog)
                        emit("model", "弹窗已关闭，回表格验证开通状态…")
                        time.sleep(3.0)
                        try:
                            page.reload(wait_until="domcontentloaded",
                                        timeout=20_000)
                            _wait_table_ready(page)
                            _dismiss_satisfaction_popup(page)
                        except Exception as e:
                            logging.debug("开通后刷新页面失败: %s", e)
                        for model_name in checked_models:
                            row = _find_model_row(page, model_name)
                            if row is not None:
                                status = _row_status_text(row)
                                if "已开通" in status:
                                    opened.append(model_name)
                                    emit("model",
                                         f"{model_name} 验证通过: {status}")
                                else:
                                    failed.append({
                                        "model": model_name,
                                        "error": f"开通后状态为"
                                                 f"'{status or '未知'}'，"
                                                 f"可能未真正生效",
                                    })
                                    emit("model",
                                         f"{model_name} 状态异常: "
                                         f"{status or '未知'}")
                            else:
                                failed.append({
                                    "model": model_name,
                                    "error": "开通后未能在列表中找到该模型",
                                })
                        # 未能在弹窗中勾选的模型标记为失败
                        for model_name in pending:
                            if model_name not in checked_models:
                                failed.append({
                                    "model": model_name,
                                    "error": "未能在弹窗中找到并勾选该模型",
                                })
                        emit("model",
                             f"开通验证完成: 已开通 "
                             f"{', '.join(opened) if opened else '无'}，"
                             f"失败 {len(failed)} 个")
                    else:
                        for model_name in checked_models:
                            failed.append({
                                "model": model_name,
                                "error": "等待开通完成超时"
                                         "（弹窗未关闭或未出现成功提示）",
                            })
                    _close_dialog(page, dialog)
                    time.sleep(0.5)
            except Exception as exc:
                for model_name in pending:
                    if model_name not in {f["model"] for f in failed}:
                        failed.append({
                            "model": model_name,
                            "error": f"点击'一键开通'失败: {exc}",
                        })
                _close_dialog(page, dialog)

        # 6. 汇总
        emit_progress(steps - 1, steps, "收集结果...")
        return make_success(
            "open_models",
            cdp_url=cdp_url,
            models=models,
            opened=opened,
            already_opened=already_opened,
            failed=failed,
            all_done=len(failed) == 0,
        )
    except Exception as exc:
        return make_failure("exception", f"未预期错误: {exc}", cdp_url=cdp_url)
    finally:
        try:
            pw.stop()
        except Exception as e:
            logging.debug("停止 playwright 失败: %s", e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自动开通预置模型（文本生成）")
    parser.add_argument("--cdp-url", default="", help="CDP endpoint")
    parser.add_argument(
        "--model",
        action="append",
        help="模型名称，可多次指定（优先于 --models-file）",
    )
    parser.add_argument(
        "--models-file",
        default=_DEFAULT_MODELS_FILE,
        help="模型列表 JSON 文件路径（当未指定 --model 时使用）",
    )
    parser.add_argument(
        "--type-filter",
        default="文本生成",
        help="仅开通该类型的服务（读取预置服务列表'类型'列）",
    )
    parser.add_argument("--timeout", type=int, default=90,
                        help="整体超时秒数")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    cdp_url = (args.cdp_url or "").strip() or resolve_cdp_url()
    if not cdp_url:
        result = make_failure("init", "未找到 CDP URL")
        output_json(result)
        return 1

    if args.model:
        models = [s.strip() for s in args.model]
    else:
        models = _load_display_names(args.models_file)
        if not models:
            result = make_failure("init", f"未从 {args.models_file} 加载到模型")
            output_json(result)
            return 1

    result = auto_open_models(cdp_url=cdp_url, models=models,
                              type_filter=args.type_filter,
                              timeout_s=args.timeout)
    output_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())