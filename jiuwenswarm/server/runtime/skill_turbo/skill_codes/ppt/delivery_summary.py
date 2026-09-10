from __future__ import annotations

import re
from typing import Any

DELIVERY_SUMMARY_START = "以下是完成情况概要："

_PAGE_HEADING_RE = re.compile(r"^### P(\d+):\s*(.*)$", re.MULTILINE)
_FIELD_RE = re.compile(r"\*\*(标题|内容概要)\*\*[：:]\s*(.+)")
_STYLE_LABELS = {
    "business-classic": "商务经典",
    "tech-minimal": "科技极简",
    "elegant-narrative": "优雅叙事",
    "industrial-tech": "工业科技",
    "custom": "自定义风格",
}


def is_backup_listing_path(path: str) -> bool:
    parts = str(path or "").replace("\\", "/").strip("/").split("/")
    return "_backup" in parts


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _missing(value: Any, fallback: str = "未提供") -> str:
    text = _one_line(str(value)) if value is not None else ""
    return text if text else fallback


def parse_outline_pages(outline_text: str) -> list[tuple[int, str, str]]:
    text = str(outline_text or "")
    matches = list(_PAGE_HEADING_RE.finditer(text))
    pages: list[tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end]
        fields = {key: _one_line(val) for key, val in _FIELD_RE.findall(block)}
        title = fields.get("标题") or _one_line(match.group(2)) or "未提供"
        core = fields.get("内容概要") or "未提供"
        pages.append((int(match.group(1)), title, core))
    pages.sort(key=lambda item: item[0])
    return pages


def _narrative_from_outline(outline_text: str) -> str:
    match = re.search(r"\*\*叙事主线\*\*[：:]\s*(.+)", str(outline_text or ""))
    return _one_line(match.group(1)) if match else ""


def _pick_core_pages(
    pages: list[tuple[int, str, str]],
    total_pages: int,
) -> list[tuple[int, str, str]]:
    if pages:
        if len(pages) <= 3:
            return pages
        middle = pages[len(pages) // 2]
        picked = [pages[0], middle, pages[-1]]
        unique: list[tuple[int, str, str]] = []
        seen: set[int] = set()
        for item in picked:
            if item[0] in seen:
                continue
            seen.add(item[0])
            unique.append(item)
        return unique
    count = total_pages if total_pages > 0 else 1
    return [(index, "未提供", "未提供") for index in range(1, min(count, 3) + 1)]


def _speaker_notes_line(need_speaker_notes: bool, speaker_notes_status: str) -> str:
    status = _one_line(speaker_notes_status).lower()
    if not need_speaker_notes or status in ("", "skipped"):
        return "未请求演讲备注"
    if status == "ok":
        return "已注入演讲备注"
    if status == "partial":
        return "部分页演讲备注注入失败"
    if status in ("failed", "error"):
        return "演讲备注注入失败，PPTX 已交付"
    return _missing(speaker_notes_status)


def _gate_line(delivery_status: str, export_status: str, pages_ok: bool) -> str:
    export = _one_line(export_status) or "未提供"
    pages = "HTML 页数与大纲一致" if pages_ok else "HTML 页数校验未完全对齐"
    delivery = _one_line(delivery_status) or "未提供"
    return f"导出 {export}；{pages}；交付 {delivery}"


def build_delivery_summary_skeleton(
    *,
    pptx_filename: str,
    total_pages: int,
    delivery_status: str,
    send_file_status: str,
    pages_ok: bool,
    outline_text: str = "",
    topic: str = "",
    style_id: str = "",
    speaker_notes_status: str = "",
    need_speaker_notes: bool = False,
    has_documents: bool = False,
    image_map_path: str = "",
    export_status: str = "",
) -> str:
    filename = _missing(pptx_filename)
    page_count = total_pages if total_pages > 0 else 1
    pages = parse_outline_pages(outline_text)
    core_pages = _pick_core_pages(pages, page_count)
    narrative = _narrative_from_outline(outline_text)
    style_label = _STYLE_LABELS.get(_one_line(style_id), _missing(style_id))
    image_strategy = "已按素材配图" if _one_line(image_map_path) else "按无图布局生成"
    input_source = "用户主题与上传文档" if has_documents else "用户主题与对话要求"
    if topic:
        input_source = f"{input_source}（{_missing(topic)}）"
    notes_line = _speaker_notes_line(need_speaker_notes, speaker_notes_status)
    # Titles only: page 内容概要 is already streamed as Stage 6 process text.
    # Pasting it here made the user-facing delivery summary look duplicated.
    core_lines = "\n".join(f"- P{num}：{_missing(title)}" for num, title, _core in core_pages)
    added = filename if send_file_status == "sent" else f"{filename}（发送状态：{_missing(send_file_status)}）"
    return (
        f"{DELIVERY_SUMMARY_START}\n"
        "\n"
        "✅ 已完成：PPT 生成\n"
        "\n"
        f"已处理：生成演示文稿《{filename}》，共 {page_count} 页。\n"
        f"产物概括：{_missing(narrative, '未提供')}\n"
        f"门禁校验：{_gate_line(delivery_status, export_status, pages_ok)}\n"
        "\n"
        "**📌 本次执行口径**\n"
        f"📁 输入来源：{input_source}\n"
        f"📊 页面范围：P1-P{page_count}\n"
        f"🎨 设计口径：{style_label}\n"
        f"🖼️ 图片策略：{image_strategy}\n"
        "⏭️ 未处理内容：无\n"
        "\n"
        "**🎯 核心内容**\n"
        f"{core_lines}\n"
        "\n"
        "**⚠️ 重点提醒**\n"
        "- 版式：加速通道已完成页面生成与导出\n"
        "- 字体：未提供\n"
        f"- 备注：{notes_line}\n"
        "\n"
        "**📁 文件变更**\n"
        "\n"
        "| 类型 | 内容 |\n"
        "|---|---|\n"
        f"| 新增 | {added} |\n"
        "| 更新 | 无 |\n"
        "| 保留 | 无 |\n"
        "| 未自动处理 | 无 |\n"
        "\n"
        "**✅ 建议校对**\n"
        "1. 核对封面标题与主题是否一致\n"
        "2. 核对内页专有名词与关键数据\n"
        "3. 过一遍页序、备注与图片裁切\n"
        "\n"
        "**🔄 你可以继续让我**\n"
        "- 调整风格或配色\n"
        "- 压缩或扩写某页内容\n"
        "- 补充或重写演讲备注\n"
    )
