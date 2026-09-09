import json
import re
from pathlib import Path

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

CJK_PDF_TO_WORD_NOTE = (
    "文档含中文，当前环境不支持生成中文 PDF，已自动改用 Word 格式输出。"
)
CJK_PDF_BLOCKED_MESSAGE = (
    "文档含中文，当前环境不支持生成中文 PDF，请保留 Word 格式或"
    "使用 document_generator 直接生成 Word。"
)
CJK_WATERMARK_BLOCKED_MESSAGE = "水印文本含中文，当前环境不支持在 PDF 中渲染中文水印，请使用纯英文水印文本。"


def contains_cjk(text: str) -> bool:
    return bool(text and _CJK_RE.search(text))


def _item_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("text", "") or "")
    return str(item) if item is not None else ""


def _join_text_items(value) -> str:
    items = value if isinstance(value, list) else [value]
    return "\n".join(text for text in (_item_text(item) for item in items) if text)


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return _join_text_items(value)
    return str(value).strip()


def _normalize_table_row(row) -> list[str]:
    if isinstance(row, list):
        return ["" if cell is None else str(cell) for cell in row]
    if row is None:
        return []
    return [str(row)]


def _coerce_table(table) -> list:
    raw_rows: list = []
    if isinstance(table, list):
        raw_rows = table
    elif isinstance(table, dict):
        data = table.get("data")
        if isinstance(data, list) and data:
            raw_rows = data
        else:
            headers = table.get("headers") or table.get("columns") or []
            rows = table.get("rows") or []
            if headers:
                raw_rows.append(headers)
            if isinstance(rows, list):
                raw_rows.extend(rows)
    else:
        return []

    grid = [_normalize_table_row(row) for row in raw_rows]
    grid = [row for row in grid if row]
    if not grid or max(len(row) for row in grid) == 0:
        return []
    return grid


def _table_column_count(data: list) -> int:
    return max((len(row) for row in data), default=0)


_FORMAT_ALIASES = {
    "pdf": "pdf",
    "word": "word",
    "doc": "word",
    "docx": "word",
    "excel": "excel",
    "xls": "excel",
    "xlsx": "excel",
    "ppt": "ppt",
    "pptx": "ppt",
}
_PAYLOAD_KEYS = ("format", "filename", "output_dir")
_CONTENT_BODY_KEYS = (
    "slides",
    "paragraphs",
    "tables",
    "sheets",
    "rows",
    "title",
    "bullets",
    "table",
)
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FILENAME_EXTENSIONS = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt")


def _try_parse_json(value):
    current = value
    for _ in range(2):
        if not isinstance(current, str):
            return current
        text = current.strip()
        if not text or text[0] not in "{[":
            return current
        try:
            current = json.loads(text)
        except json.JSONDecodeError:
            return current
    return current


def _sanitize_filename(name: str) -> str:
    sanitized = Path(str(name)).name
    lowered = sanitized.lower()
    for ext in _FILENAME_EXTENSIONS:
        if lowered.endswith(ext):
            sanitized = sanitized[: -len(ext)]
            break
    sanitized = _INVALID_FILENAME_CHARS.sub("_", sanitized).strip(" .")
    return sanitized or "document"


def _normalize_format(fmt) -> str:
    key = str(fmt or "").strip().lower().lstrip(".")
    return _FORMAT_ALIASES.get(key, key)


def coerce_generator_inputs(inputs: dict) -> dict:
    parsed = dict(inputs or {})
    content = _try_parse_json(parsed.get("content", {}))
    if isinstance(content, dict) and any(key in content for key in _PAYLOAD_KEYS):
        if "content" in content or any(key in content for key in _CONTENT_BODY_KEYS):
            for key in _PAYLOAD_KEYS:
                if not parsed.get(key) and content.get(key):
                    parsed[key] = content[key]
            inner = content.get("content")
            if inner is not None:
                content = _try_parse_json(inner)
            else:
                content = {
                    key: value
                    for key, value in content.items()
                    if key not in _PAYLOAD_KEYS
                }
    if isinstance(content, str) and content.strip():
        content = {"paragraphs": [content.strip()]}
    elif isinstance(content, list):
        slide_like = content and all(
            isinstance(item, dict)
            and any(key in item for key in ("title", "body", "bullets", "tables"))
            for item in content
        )
        content = {"slides": content} if slide_like else {"paragraphs": content}
    parsed["content"] = content
    parsed["format"] = _normalize_format(parsed.get("format", ""))
    filename = parsed.get("filename", "")
    if filename:
        parsed["filename"] = _sanitize_filename(str(filename))
    return parsed


def _coerce_tables(tables: list) -> list:
    return [coerced for coerced in (_coerce_table(table) for table in tables) if coerced]


def _merge_slide_body(slide: dict) -> str:
    parts: list[str] = []
    for key in ("body", "subtitle", "bullets", "paragraphs"):
        text = _as_text(slide.get(key))
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def normalize_generator_content(content: dict) -> dict:
    normalized = dict(content)
    bullets = normalized.get("bullets")
    if bullets:
        paragraphs = list(normalized.get("paragraphs") or [])
        paragraphs.extend(bullets if isinstance(bullets, list) else [bullets])
        normalized["paragraphs"] = paragraphs
    tables = list(normalized.get("tables") or [])
    table = normalized.get("table")
    if table is not None:
        tables.append(table)
    if tables:
        normalized["tables"] = _coerce_tables(tables)
    slides = normalized.get("slides")
    if not isinstance(slides, list):
        return normalized
    normalized_slides = []
    for slide in slides:
        if not isinstance(slide, dict):
            normalized_slides.append(slide)
            continue
        slide = dict(slide)
        merged_body = _merge_slide_body(slide)
        if merged_body:
            slide["body"] = merged_body
        slide_tables = list(slide.get("tables") or [])
        slide_table = slide.get("table")
        if slide_table is not None:
            slide_tables.append(slide_table)
        if slide_tables:
            slide["tables"] = _coerce_tables(slide_tables)
        normalized_slides.append(slide)
    normalized["slides"] = normalized_slides
    return normalized


def validate_generator_content(content: dict, fmt: str) -> str | None:
    if fmt != "ppt":
        return None
    slides = content.get("slides")
    if not isinstance(slides, list):
        return None
    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            continue
        title = _as_text(slide.get("title"))
        body = _as_text(slide.get("body"))
        tables = slide.get("tables") or []
        if title and not body and not tables:
            return (
                f"第 {index} 页「{title}」缺少正文，请提供 body、bullets、"
                "paragraphs、subtitle 或 tables"
            )
    return None


def _collect_table_cells(table) -> list[str]:
    parts: list[str] = []
    for row in _coerce_table(table):
        for cell in row:
            if cell:
                parts.append(cell)
    return parts


def collect_structured_content_text(content: dict) -> str:
    parts: list[str] = []
    for key in ("title", "subtitle"):
        text = _as_text(content.get(key))
        if text:
            parts.append(text)
    for para in content.get("paragraphs", []):
        text = _item_text(para)
        if text:
            parts.append(text)
    content_tables = list(content.get("tables") or [])
    if not content_tables and content.get("table") is not None:
        content_tables = [content.get("table")]
    for table in content_tables:
        parts.extend(_collect_table_cells(table))
    for slide in content.get("slides", []):
        if not isinstance(slide, dict):
            continue
        for key in ("title", "body", "subtitle", "bullets", "paragraphs"):
            text = _as_text(slide.get(key))
            if text:
                parts.append(text)
        slide_tables = list(slide.get("tables") or [])
        if not slide_tables and slide.get("table") is not None:
            slide_tables = [slide.get("table")]
        for table in slide_tables:
            parts.extend(_collect_table_cells(table))
    for sheet in content.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        sheet_name = _as_text(sheet.get("sheet_name"))
        if sheet_name:
            parts.append(sheet_name)
        parts.extend(_collect_table_cells(sheet))
    parts.extend(_collect_table_cells(content.get("rows") or []))
    return "\n".join(parts)


def collect_docx_text(doc) -> str:
    parts = [para.text for para in doc.paragraphs if para.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text)
    return "\n".join(parts)


def collect_presentation_text(prs) -> str:
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
    return "\n".join(parts)
