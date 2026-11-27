# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pypandoc
from jiuwenclaw.agentserver.tools.deepresearch_plugin.conversion_utils import (
    ensure_pandoc,
    make_safe_filename_component,
    normalize_docx_fonts,
    normalize_headings,
    preprocess_markdown_text_for_docx,
    read_text_with_fallback,
)


def convert_md_to_docx(
    md_path: str | Path,
    docx_path: str | Path,
    *,
    logger: logging.Logger,
) -> None:
    """Convert a Markdown file to a DOCX document."""
    ensure_pandoc()

    md_file = Path(md_path).resolve()
    docx_file = Path(docx_path).resolve()
    if not md_file.exists():
        raise FileNotFoundError(f"Markdown file does not exist: {md_file}")

    docx_file.parent.mkdir(parents=True, exist_ok=True)

    safe_stem = make_safe_filename_component(docx_file.stem)
    temp_prefix = f".tmp_{safe_stem}_{uuid.uuid4().hex}"
    temp_md = docx_file.parent / f"{temp_prefix}.md"

    try:
        content = read_text_with_fallback(md_file)
        content = preprocess_markdown_text_for_docx(content)
        content = normalize_headings(content)
        temp_md.write_text(content, encoding="utf-8")

        pypandoc.convert_file(
            str(temp_md),
            "docx",
            outputfile=str(docx_file),
            extra_args=[
                "--from=gfm",
                "--resource-path",
                str(docx_file.parent),
            ],
        )
        normalize_docx_fonts(docx_file)
        logger.info("DOCX generated successfully: %s", docx_file)
    finally:
        temp_md.unlink(missing_ok=True)
