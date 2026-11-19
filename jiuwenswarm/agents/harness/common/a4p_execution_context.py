# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility shim — prefer ``a4p.execution_context``.

Keeps a single ``AUTHORIZATION_EXECUTION_CONTEXTS`` store so adapter activate
and tool/RPC lookups cannot diverge across flat vs package imports.
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.a4p.execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    A4PAuthorizationAttempt,
    AuthorizationExecutionContext,
    AuthorizationExecutionContextStore,
    AuthorizerRoute,
    authorizer_route_from_request,
    build_authorization_execution_context,
)

__all__ = [
    "A4PAuthorizationAttempt",
    "AUTHORIZATION_EXECUTION_CONTEXTS",
    "AuthorizationExecutionContext",
    "AuthorizationExecutionContextStore",
    "AuthorizerRoute",
    "authorizer_route_from_request",
    "build_authorization_execution_context",
]
