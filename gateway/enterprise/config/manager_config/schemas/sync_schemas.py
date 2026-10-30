# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Manager → Gateway 同步请求 Body 组合（无信封字段）。"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T", bound=BaseModel)


class EmptySyncBody(BaseModel):
    """DELETE 等无业务字段的写接口 Body（允许空对象；忽略历史信封字段）。"""

    model_config = ConfigDict(extra="ignore")


def make_sync_body(name: str, business: type[BaseModel] | None = None) -> type[BaseModel]:
    """返回写接口 request body 模型；无业务字段时用空模型。

    ``name`` 保留以兼容旧调用方（OpenAPI 名由业务模型自身决定）。
    """
    _ = name
    if business is None:
        return EmptySyncBody
    return business


# DELETE / 无业务字段写接口常用（历史名保留）
SyncEnvelopeOnlyBody = EmptySyncBody
