from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ..core.enterprise_config.expressions import validate_match_expr


def _validate_match_expr(value: Any) -> Any:
    validate_match_expr(value)
    return value


MatchExprField = Annotated[Any, BeforeValidator(_validate_match_expr)]


class InstanceAgentResourceUpsertRequest(BaseModel):
    """对齐 instance_agent_resource：NOT NULL 列必填/非 null；可空列允许 null。

    ``created_at`` / ``updated_at`` 由服务层写入，不在请求体中。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    resource_id: str = Field(..., min_length=1, max_length=100)
    ref_template_id: str = Field(..., min_length=1, max_length=100)
    resource_name: str = Field(..., min_length=1, max_length=128)
    resource_desc: str | None = Field(default=None, max_length=512)
    match_expr: MatchExprField | None = None
    granted_by: str | None = Field(default=None, max_length=64)
    enabled: bool = Field(default=True)
    expires_at: datetime | None = None
    data: dict[str, Any] | None = None
