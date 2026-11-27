from __future__ import annotations

from agent_gateway.auth.credential_access import ModelCredential
from agent_gateway.auth.session_login_bindings import SessionLoginBinding


class NullSessionLoginBindings:
    """No-op session login binding for tests outside the auth contract."""

    async def remember(self, session_id: str, credential_ref: str, model_name: str) -> None:
        return None

    async def lookup(self, session_id: str) -> SessionLoginBinding | None:
        return None

    async def model_auth_for_binding(self, binding: SessionLoginBinding) -> ModelCredential | None:
        return None
