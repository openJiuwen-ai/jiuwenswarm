# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Datatypes produced by URL ingest (``FetchedPage``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FetchedPage:
    """One fetched and parsed document from ``url_ingest``."""

    text: str
    source_url: str
    title: str = ""
    source_type: str = ""
    id_: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
