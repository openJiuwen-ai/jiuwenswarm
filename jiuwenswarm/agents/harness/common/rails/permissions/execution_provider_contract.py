# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Closed execution-provider contracts consumed by Auto Permission."""

from __future__ import annotations

JIUWENBOX_SANDBOX_EXECUTION_TOOLS = frozenset({"bash", "mcp_exec_command", "code"})
ACP_IDE_EXECUTION_TOOLS = frozenset({"create_terminal"})
# run_tests starts the project's test runner directly on the host (not through
# JiuwenBox). It is listed here so the provider partition stays closed and
# exhaustive; the manual review in automatic permission comes from its "code"
# category (tool_capabilities._CODE_TOOLS) and its absence from
# JIUWENBOX_SANDBOX_EXECUTION_TOOLS, checked by
# requires_manual_execution_provider_review below.
UNKNOWN_PROVIDER_EXECUTION_TOOLS = frozenset({"cmd", "powershell", "run_tests"})

EXECUTION_PROVIDER_CONTRACT_UNVERIFIED = "execution_provider_contract_unverified"


def requires_no_host_fallback(
    tool_name: str,
    *,
    tool_category: str,
) -> bool:
    """Return whether automatic execution must remain in JiuwenBox."""

    return (
        tool_category in {"shell", "code"}
        and tool_name in JIUWENBOX_SANDBOX_EXECUTION_TOOLS
    )


def requires_manual_execution_provider_review(
    tool_name: str,
    *,
    tool_category: str,
) -> bool:
    """Return whether the Host cannot verify an Auto execution contract."""

    return (
        tool_category in {"shell", "code"}
        and tool_name not in JIUWENBOX_SANDBOX_EXECUTION_TOOLS
    )
