from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ResponseModel(BaseModel, Generic[T]):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    data: T | None = None
    code: int
    message: str
