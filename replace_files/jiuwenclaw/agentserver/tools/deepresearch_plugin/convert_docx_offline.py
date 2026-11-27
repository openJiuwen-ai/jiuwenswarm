# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import logging
from pathlib import Path

from jiuwenclaw.agentserver.tools.deepresearch_plugin.docx_conversion_core import (
    convert_md_to_docx as convert_md_to_docx_core,
)

logger = logging.getLogger(__name__)


def convert_md_to_docx(md_path: str | Path, docx_path: str | Path) -> None:
    """Convert a Markdown file to a DOCX file."""
    convert_md_to_docx_core(
        md_path,
        docx_path,
        logger=logger,
    )


if __name__ == "__main__":
    try:
        convert_md_to_docx("input.md", "output.docx")
    except Exception:
        logger.exception("DOCX conversion failed")
        raise
