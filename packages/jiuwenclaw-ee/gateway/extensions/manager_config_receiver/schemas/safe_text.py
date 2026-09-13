"""模板等自由文本字段的统一安全校验（拦截 XSS / SQL 注入恶意片段）。"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel, model_validator

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_SCRIPT_URI_RE = re.compile(r"(?i)\b(javascript|vbscript|data\s*:\s*text\s*/\s*html)\s*:")
_EVENT_HANDLER_RE = re.compile(r"(?i)\bon[a-z]+\s*=")
_SQLI_RE = re.compile(
    r"""(?ix)
    (
      '\s*(or|and)\s+('?\d+'?\s*=\s*'?\d+'?|true|false)
      |;\s*(drop|delete|update|insert|alter|truncate|exec|execute|create)\b
      |\bunion\b\s+(all\s+)?\bselect\b
      |/\*
      |\*/
      |('|;)(\s)*--
      |\bxp_cmdshell\b
      |\binformation_schema\b
    )
    """
)

SAFE_TEXT_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "template_name",
        "policy_name",
        "description",
        "namespace",
        "pod_name",
        "container_name",
        "port_name",
        "skill_id",
        "model_id",
        "model_provider",
        "agent_runtime",
        "agent_image",
    }
)
SAFE_TEXT_LIST_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "model_tags",
        "embed_tags",
        "a2a_tags",
    }
)


def is_safe_text(value: str) -> bool:
    try:
        assert_safe_text(value)
        return True
    except ValueError:
        return False


def assert_safe_text(value: str) -> str:
    if _CONTROL_RE.search(value):
        raise ValueError("contains illegal control characters")
    if "<" in value or ">" in value or _HTML_TAG_RE.search(value):
        raise ValueError("HTML / script content is not allowed")
    if _SCRIPT_URI_RE.search(value) or _EVENT_HANDLER_RE.search(value):
        raise ValueError("unsafe script URI or event handler is not allowed")
    if _SQLI_RE.search(value):
        raise ValueError("potentially malicious SQL fragment is not allowed")
    return value


def _check_value(value: Any) -> None:
    if isinstance(value, str):
        assert_safe_text(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                assert_safe_text(item)


def enforce_safe_text_on_model(model: BaseModel) -> None:
    for name in SAFE_TEXT_FIELD_NAMES | SAFE_TEXT_LIST_FIELD_NAMES:
        if name not in model.model_fields_set:
            continue
        _check_value(getattr(model, name, None))
    if "hook_config" in model.model_fields_set:
        hook = getattr(model, "hook_config", None)
        handler = getattr(hook, "handler", None) if hook is not None else None
        if isinstance(handler, str):
            assert_safe_text(handler)


class SafeTextMixin(BaseModel):
    _skip_safe_text: ClassVar[bool] = False

    @model_validator(mode="after")
    def _enforce_safe_text(self) -> SafeTextMixin:
        if not self._skip_safe_text:
            enforce_safe_text_on_model(self)
        return self
