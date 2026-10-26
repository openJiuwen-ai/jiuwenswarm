# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FastAPI 依赖：同步请求 Body 解析（无验签/解密）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

from fastapi import Depends, Request
from pydantic import BaseModel

from ..http.sync_security import split_business
from ..schemas.sync_schemas import SyncEnvelopeOnlyBody

TBody = TypeVar("TBody", bound=BaseModel)


@dataclass(frozen=True)
class SyncContext:
    """同步上下文（不含业务分发）。"""

    business: dict[str, Any]
    method: str


async def build_sync_context(body: BaseModel, method: str) -> SyncContext:
    # PATCH/PUT 只保留客户端显式传入的字段；exclude_unset=False 会把未传可选字段
    # 填成 None，再经 model_validate 变成「已设置」，最终把 enabled 等 NOT NULL 列写成 null。
    raw = body.model_dump(mode="python", exclude_unset=True)
    business = split_business(raw)
    business.pop("jiuwenclaw_id", None)  # 历史字段，忽略
    return SyncContext(
        business=business,
        method=method.upper(),
    )


def verify_sync(body_cls: type[TBody]) -> Callable[..., Any]:
    """生成带明确 request body 模型的依赖（历史名保留，已无验签）。"""

    async def _dep(
        request: Request,
        body: body_cls,  # type: ignore[valid-type]
    ) -> SyncContext:
        return await build_sync_context(body, request.method)

    _dep.__name__ = f"verify_sync_{getattr(body_cls, '__name__', 'body')}"
    _dep.__annotations__ = {
        "request": Request,
        "body": body_cls,
        "return": SyncContext,
    }
    return _dep


# DELETE / 无业务字段写接口常用
VerifySyncEnvelopeOnly = Annotated[
    SyncContext, Depends(verify_sync(SyncEnvelopeOnlyBody))
]


def sync_write_data(_sync: SyncContext, result: Any) -> Any:
    """写接口统一 data：业务结果（可为 null）。"""
    return result
