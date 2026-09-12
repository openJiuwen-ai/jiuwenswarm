# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Download Code Graph grammars during init/start, never during a query.

``uv pip install tree-sitter-language-pack`` only installs the loader wheel.
The grammars come from GitHub on first ``get_parser()``. Other projects do
that on first parse; we do it in ``jiuwenswarm-init`` / ``jiuwenswarm-start``
so the coding agent is not the one that waits.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_PRELOAD_TIMEOUT_SECONDS = 90
_PRELOAD_SNIPPET = (
    "from openjiuwen.core.retrieval.code_graph.indexing.parser "
    "import preload_language_pack; "
    "raise SystemExit(0 if preload_language_pack() else 1)"
)


def preload_code_graph_grammars() -> bool:
    """If the pack is installed, download grammars when the cache is empty.

    No-op when the pack is missing or the cache is already warm. Never raises.
    """
    if not _language_pack_importable():
        return False
    if _parser_already_ready():
        return True
    logger.info("Downloading Code Graph grammars (setup, not the coding agent).")
    env = os.environ.copy()
    env["OPENJIUWEN_CODE_GRAPH_ALLOW_PARSER_DOWNLOAD"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PRELOAD_SNIPPET],
            check=False,
            timeout=_PRELOAD_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "Download tree-sitter-language-pack to enable Code Graph. Falling back to grep. (%s)",
            exc,
        )
        return False
    if completed.returncode != 0:
        logger.warning(
            "Download tree-sitter-language-pack to enable Code Graph. Falling back to grep."
        )
        return False
    return True


def _language_pack_importable() -> bool:
    try:
        import tree_sitter_language_pack  # noqa: F401
    except ImportError:
        return False
    return True


def _parser_already_ready() -> bool:
    try:
        from openjiuwen.core.retrieval.code_graph.indexing.parser import parser_available
    except ImportError:
        return False
    return parser_available()
