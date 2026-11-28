# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Machine checks for the documented memory-plugin contract (docs contract section)."""

from pathlib import Path

DOCS = Path(__file__).resolve().parents[4] / "docs"
REQUIRED = ("plugins/memory", "plugin.py", "build_provider(config)")


def test_doc_plugin_contract_terms():
    for doc in (DOCS / "zh" / "记忆.md", DOCS / "en" / "Memory.md"):
        text = doc.read_text(encoding="utf-8")
        for term in REQUIRED:
            assert term in text, f"{doc} missing contract term: {term}"
        assert text.count("build_provider(config)") >= 1
