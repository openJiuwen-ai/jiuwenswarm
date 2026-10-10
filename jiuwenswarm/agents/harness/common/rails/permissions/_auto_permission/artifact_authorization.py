"""Exact file-delivery authorization for Smart Approval decisions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    SendFileExecutionGrant,
    create_send_file_execution_grant,
    current_send_file_execution_grant,
    publish_send_file_execution_grant,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.url_safety import (
    inspect_network_scope,
)

_FILE_DELIVERY_PROHIBITION_PATTERNS = (
    re.compile(
        r"\b(?:do\s+not|don't|dont|never|no)[\s-]+"
        r"(?:send|attach(?:ment)?s?|deliver(?:y)?|share|download(?:s| link)?|"
        r"upload(?:s)?|file[\s-]+delivery)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:不要|别|无需|不需要)(?:发送|发给我|上传|传文件|附上|附件|交付|分享|下载|给下载链接)"
    ),
)
_FILE_DELIVERY_TOOLS = frozenset({"send_file_to_user", "upload_file", "upload_photo"})
_FILE_DELIVERY_ARG_KEYS = frozenset(
    "attachment attachments file files file_path file_paths abs_file_path "
    "abs_file_paths download_link download_links download_url download_urls".split()
)


def has_user_file_delivery_prohibition(text: str) -> bool:
    """Recognize an explicit English or Chinese file-delivery prohibition."""
    normalized = str(text or "").strip()
    return bool(
        normalized
        and any(
            pattern.search(normalized)
            for pattern in _FILE_DELIVERY_PROHIBITION_PATTERNS
        )
    )


def is_file_delivery_action(facts: ToolDecisionFacts) -> bool:
    """Return whether this action structurally transfers a local file."""
    if facts.tool_name in _FILE_DELIVERY_TOOLS:
        return True
    if facts.tool_name == "send_message":
        return bool(facts.read_paths or _has_file_delivery_field(facts.untrusted_args))
    return "external_send" in facts.capability.static_side_effects and bool(
        facts.read_paths or inspect_network_scope(facts).upload_like
    )


def _has_file_delivery_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (str(key) in _FILE_DELIVERY_ARG_KEYS and _has_delivery_value(nested))
            or _has_file_delivery_field(nested)
            for key, nested in value.items()
        )
    if isinstance(value, tuple | list):
        return any(_has_file_delivery_field(item) for item in value)
    return False


def _has_delivery_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | tuple | list):
        return bool(value)
    return bool(value)


def _requested_file_paths(facts: ToolDecisionFacts) -> tuple[Path, ...]:
    return tuple(
        Path(path).expanduser().resolve(strict=False) for path in facts.read_paths
    )


def _send_file_execution_grant_for_allow(
    facts: ToolDecisionFacts,
) -> tuple[SendFileExecutionGrant | None, str]:
    """Resolve the exact current-task grant for an already-allowed call."""
    if facts.tool_name != "send_file_to_user":
        return None, ""
    requested_paths = _requested_file_paths(facts)
    if not requested_paths:
        return None, "send_file_authorization_missing_path"
    expected = create_send_file_execution_grant(
        requested_paths=requested_paths,
        target_channels=facts.untrusted_args.get("target_channels"),
    )
    grant = current_send_file_execution_grant()
    if grant is None:
        return None, "send_file_authorization_missing"
    if grant != expected:
        return None, "send_file_authorization_mismatch"
    return grant, ""


class AutoPermissionArtifactAuthorizationMixin:
    """Publish exact file-delivery grants for approved tool calls."""

    @staticmethod
    def _issue_send_file_authorization(facts: ToolDecisionFacts) -> str:
        if facts.tool_name != "send_file_to_user":
            return ""
        requested_paths = _requested_file_paths(facts)
        if not requested_paths:
            return "send_file_authorization_missing_path"
        try:
            publish_send_file_execution_grant(
                create_send_file_execution_grant(
                    requested_paths,
                    target_channels=facts.untrusted_args.get("target_channels"),
                )
            )
        except ValueError:
            return "send_file_authorization_not_file"
        return ""
