# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""UTF-8 BOM compatibility without changing dotenv override semantics."""

import os

import pytest

from jiuwenswarm.dotenv_early import load_dotenv_runtime
from jiuwenswarm.common.media_capability_config import migrate_media_capability_switches


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize("override", [False, True])
def test_runtime_reads_first_key_and_preserves_override(tmp_path, monkeypatch, encoding, override):
    path = tmp_path / ".env"
    path.write_text("BOM_TEST_VALUE=file\n", encoding=encoding)
    monkeypatch.setenv("BOM_TEST_VALUE", "shell")
    monkeypatch.delenv("\ufeffBOM_TEST_VALUE", raising=False)
    assert load_dotenv_runtime(path, override=override)
    assert os.environ["BOM_TEST_VALUE"] == ("file" if override else "shell")
    assert "\ufeffBOM_TEST_VALUE" not in os.environ


def test_bom_does_not_bypass_explicit_disabled_media_switch(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "VISION_ENABLED=false\nVISION_API_BASE=https://example.com/v1\n"
        "VISION_API_KEY=dummy\nVISION_MODEL_NAME=test\nVISION_PROVIDER=OpenAI\n",
        encoding="utf-8-sig",
    )
    runtime = {}
    updates = migrate_media_capability_switches(path, environ=runtime)
    assert runtime["VISION_ENABLED"] == "false"
    assert "VISION_ENABLED" not in updates
    assert path.read_text(encoding="utf-8-sig").count("VISION_ENABLED=") == 1
