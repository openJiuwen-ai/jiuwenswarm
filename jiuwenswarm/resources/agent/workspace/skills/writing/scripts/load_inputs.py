# -*- coding: utf-8 -*-
"""
load_inputs.py — 顶层入口（re-export from stage1_load_inputs）

兼容 writing_flow.py 中的 `from scripts.load_inputs import load_inputs` 调用。
实际实现全部在 scripts/stage1_load_inputs.py。
"""
from __future__ import annotations

from scripts.stage1_load_inputs import load_inputs  # noqa: F401

__all__ = ["load_inputs"]
