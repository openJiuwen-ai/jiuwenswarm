# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DB 行 → 普通 dict。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        out = dict(row)
    else:
        model_dump = getattr(row, "model_dump", None)
        to_dict = getattr(row, "to_dict", None)
        if callable(model_dump):
            dumped = model_dump(mode="json")
            out = dumped if isinstance(dumped, dict) else {}
        elif callable(to_dict):
            dumped = to_dict()
            out = dumped if isinstance(dumped, dict) else {}
        elif hasattr(row, "keys"):
            out = {k: row[k] for k in row.keys()}
        else:
            field_names = getattr(row, "__dataclass_fields__", None) or getattr(
                row, "__annotations__", None
            )
            if not field_names:
                field_names = vars(row)
            out = {k: getattr(row, k) for k in field_names if not k.startswith("_sa_")}

    cleaned = {k: v for k, v in out.items() if not k.startswith("_sa_")}
    for key, value in list(cleaned.items()):
        if isinstance(value, (datetime, date)):
            cleaned[key] = value.isoformat()
    return cleaned


__all__ = ["row_to_dict"]
