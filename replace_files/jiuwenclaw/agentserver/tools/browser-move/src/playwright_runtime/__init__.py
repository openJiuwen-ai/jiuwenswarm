#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Playwright runtime package bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

# Make repo-local modules importable when running from repo root or src/.
_HERE = Path(__file__).resolve().parent
SRC_ROOT = _HERE.parent
REPO_ROOT = SRC_ROOT.parent
PROJECT_ROOT = REPO_ROOT.parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from .openjiuwen_monkeypatch import apply_openjiuwen_monkeypatch

apply_openjiuwen_monkeypatch()

__all__ = ["PROJECT_ROOT", "REPO_ROOT", "SRC_ROOT", "apply_openjiuwen_monkeypatch"]
