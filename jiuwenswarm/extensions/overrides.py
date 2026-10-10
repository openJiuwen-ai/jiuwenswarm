# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Require package declaration and operator consent for built-in replacements."""

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import logger

CONFIG_KEY = "extensions.allow_overrides"

# A package may not replace the rails that enforce permission and approval.
PROTECTED: dict[str, str] = {
    "rails.swarm.permission_interrupt": "it checks every tool call against the permission policy",
    "rails.core.confirm_interrupt": "it is the gate that asks a person to approve a tool call",
}


def override_key(kind: str, element_id: str) -> str:
    """The ``allow_overrides`` entry that permits replacing one element."""
    return f"{kind}.{element_id}"


def allowed_overrides() -> frozenset[str]:
    """Return configured entries, logging and removing protected entries."""
    section = get_config().get("extensions")
    raw = section.get("allow_overrides") if isinstance(section, dict) else None
    if not isinstance(raw, list):
        return frozenset()
    entries = {entry for entry in (str(item).strip() for item in raw) if entry}
    for entry in sorted(entries & set(PROTECTED)):
        logger.error(
            "[extensions] %s lists %s, which can never be replaced because %s. "
            "The entry grants nothing. Remove it.",
            CONFIG_KEY,
            entry,
            PROTECTED[entry],
        )
    return frozenset(entries - set(PROTECTED))


def override_permitted(kind: str, element_id: str) -> bool:
    """Check whether the operator permits this unprotected replacement."""
    key = override_key(kind, element_id)
    return key not in PROTECTED and key in allowed_overrides()


def require_override_permitted(kind: str, element_id: str, *, source: str = "") -> None:
    """Raise unless this element may be replaced and the operator permits it."""
    key = override_key(kind, element_id)
    who = source or "an unnamed package"
    reason = PROTECTED.get(key)
    if reason is not None:
        raise ValueError(
            f"{who} declares that it replaces {key}, which can never be replaced "
            f"because {reason}. No configuration permits this."
        )
    if key not in allowed_overrides():
        raise ValueError(
            f"{who} declares that it replaces the built-in {key}, but {CONFIG_KEY} does "
            f"not list it. Add {key!r} to {CONFIG_KEY} to permit the replacement, or "
            f"drop the declaration from the package."
        )
