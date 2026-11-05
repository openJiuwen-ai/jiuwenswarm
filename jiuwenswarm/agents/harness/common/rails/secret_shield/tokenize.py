# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Token format, masking and token substitution for the model-visible view."""

from __future__ import annotations

import re

from jiuwenswarm.agents.harness.common.rails.secret_shield.vault import SecretVault

TOKEN_PREFIX = "JIUWEN_SECRET::"
TOKEN_PATTERN = re.compile(r"JIUWEN_SECRET::([0-9a-f]{16})")
MASK_TEMPLATE = "[REDACTED:{fp}]"

TOKEN_HINT_TEMPLATE = (
    "\n\n[JIUWEN_SECRET::{fp}] A sensitive value was withheld for safety. "
    "Quote this token verbatim in tool arguments when you need to use it."
)
MAX_HINTS_PER_RESULT = 5

# Failure notices are injected as model-visible text so a failed protection
# becomes an explicit instruction instead of a silent error.
PROTECTION_FAILED_NOTICE = (
    "[SECRET_SHIELD=FAILED] Sensitive-content protection failed and the "
    "result was withheld. Tell the user protection failed; do not retry, "
    "infer, reconstruct, or bypass the withheld result during this request."
)
APPROVAL_TIMEOUT_NOTICE = (
    "[SECRET_SHIELD=APPROVAL_TIMEOUT] Sensitive content was withheld because "
    "approval timed out. Do not retry, infer, reconstruct, or bypass this "
    "decision during the current request."
)
USER_REJECTED_NOTICE = (
    "[SECRET_SHIELD=REJECTED] The user explicitly refused to share the "
    "detected sensitive value. This is a user decision, not a system error."
)
TOKEN_UNRESOLVED_NOTICE = (
    "[SECRET_SHIELD=TOKEN_UNRESOLVED] The tool call referenced a sensitive-"
    "value token that can no longer be resolved (it may have expired). The "
    "call was not executed. Do not retry, infer, reconstruct, or bypass this "
    "value during the current request."
)


def token_for(fp: str) -> str:
    return f"{TOKEN_PREFIX}{fp}"


def mask_for(fp: str) -> str:
    return MASK_TEMPLATE.format(fp=fp)


def tokenize_text(text: str, vault: SecretVault) -> tuple[str, list[str]]:
    """Replace vault plaintexts and their encodings with reference tokens.

    Returns the rewritten text and the fingerprints that matched, in order.
    """
    if not text:
        return text, []
    out = text
    hits: list[str] = []
    entries = sorted(
        vault.entries(),
        key=lambda entry: max(
            [len(entry.plaintext), *(len(variant) for variant in entry.variants)]
        ),
        reverse=True,
    )
    for entry in entries:
        matched = False
        for value in (entry.plaintext, *entry.variants):
            if value and value in out:
                out = out.replace(value, token_for(entry.fp))
                matched = True
        if matched:
            hits.append(entry.fp)
    return out, hits


def mask_text(text: str, vault: SecretVault, *, mask_tokens: bool = True) -> tuple[str, list[str]]:
    """Replace vault plaintexts, their encodings and tokens with stable masks.

    Returns the rewritten text and the fingerprints that matched, in order.
    Longer values are replaced first so a value cannot be masked partly by a
    shorter sibling.
    """
    if not text:
        return text, []
    out = text
    hits: list[str] = []
    entries = sorted(
        vault.entries(),
        key=lambda entry: max(
            [len(entry.plaintext), *(len(variant) for variant in entry.variants)]
        ),
        reverse=True,
    )
    for entry in entries:
        matched = False
        for value in (entry.plaintext, *entry.variants):
            if value and value in out:
                out = out.replace(value, mask_for(entry.fp))
                matched = True
        if matched:
            hits.append(entry.fp)
    if mask_tokens:
        for match in sorted(set(TOKEN_PATTERN.findall(out))):
            out = out.replace(token_for(match), mask_for(match))
            if match not in hits:
                hits.append(match)
    return out, hits


def substitute_tokens(text: str, vault: SecretVault) -> tuple[str, list[str]]:
    """Replace resolvable tokens with plaintext for one execution.

    Unknown tokens stay untouched so a fabricated token degrades to literal
    text instead of being silently dropped.
    """
    if not text:
        return text, []
    resolved: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        entry = vault.entry(match.group(1))
        if entry is None:
            return match.group(0)
        resolved.append(entry.fp)
        return entry.plaintext

    return TOKEN_PATTERN.sub(_replace, text), resolved


def token_hints(fps: list[str]) -> str:
    """Build model-visible reference hints for newly tokenized values."""
    unique = list(dict.fromkeys(fps))[:MAX_HINTS_PER_RESULT]
    return "".join(TOKEN_HINT_TEMPLATE.format(fp=fp) for fp in unique)


__all__ = [
    "APPROVAL_TIMEOUT_NOTICE",
    "MASK_TEMPLATE",
    "MAX_HINTS_PER_RESULT",
    "PROTECTION_FAILED_NOTICE",
    "TOKEN_HINT_TEMPLATE",
    "TOKEN_PATTERN",
    "TOKEN_PREFIX",
    "TOKEN_UNRESOLVED_NOTICE",
    "USER_REJECTED_NOTICE",
    "mask_for",
    "mask_text",
    "substitute_tokens",
    "token_for",
    "token_hints",
    "tokenize_text",
]
